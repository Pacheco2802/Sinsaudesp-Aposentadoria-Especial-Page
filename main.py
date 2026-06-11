import asyncio
import json
import logging
import os
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from auth import (
    create_access_token,
    get_current_admin,
    hash_password,
    verify_password,
)
from database import AsyncSessionLocal, Base, engine, get_db
from email_service import send_admin_notification, send_confirmation_email
from models import AdminUsuario, Cadastro, Documento
from pdf_generator import generate_procuration_pdf
from schemas import (
    CadastroCreate,
    NotaUpdateIn,
    StatusUpdateIn,
    ZapSignCreateIn,
)
from zapsign import criar_documento as zapsign_criar

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STORAGE_ROOT = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "./storage"))
TEMP_DIR = STORAGE_ROOT / "temp"
DOCS_DIR = STORAGE_ROOT / "documentos"

S3_BUCKET = os.environ.get("AWS_S3_BUCKET_NAME", "")
USE_S3 = bool(S3_BUCKET)


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )

JWT_SECRET = os.environ.get("JWT_SECRET", "")
ADMIN_INITIAL_EMAIL = os.environ.get("ADMIN_INITIAL_EMAIL", "admin@sinsaudesp.org.br")
ADMIN_INITIAL_PASSWORD = os.environ.get("ADMIN_INITIAL_PASSWORD", "")

ALLOWED_MIME_MAGIC = {
    b"%PDF": "application/pdf",
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG": "image/png",
}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
TIPOS_VALIDOS = {"RG", "CPF", "CTPS", "Holerite", "PPP"}


async def seed_admin() -> None:
    if not ADMIN_INITIAL_PASSWORD:
        logger.warning("ADMIN_INITIAL_PASSWORD not set, skipping admin seed")
        return
    async with AsyncSessionLocal() as db:
        existing = await db.scalar(
            select(AdminUsuario).where(AdminUsuario.email == ADMIN_INITIAL_EMAIL)
        )
        if not existing:
            admin = AdminUsuario(
                nome="Administrador",
                email=ADMIN_INITIAL_EMAIL,
                senha_hash=hash_password(ADMIN_INITIAL_PASSWORD),
            )
            db.add(admin)
            await db.commit()
            logger.info("Admin user seeded: %s", ADMIN_INITIAL_EMAIL)


async def cleanup_temp_files() -> None:
    while True:
        await asyncio.sleep(3600)
        cutoff = datetime.now() - timedelta(hours=24)
        try:
            for session_dir in TEMP_DIR.iterdir():
                if session_dir.is_dir():
                    mtime = datetime.fromtimestamp(session_dir.stat().st_mtime)
                    if mtime < cutoff:
                        shutil.rmtree(session_dir, ignore_errors=True)
                        logger.info("Cleaned up temp dir: %s", session_dir)
        except Exception as e:
            logger.error("Temp cleanup error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if len(JWT_SECRET) < 32:
        raise SystemExit("JWT_SECRET must be at least 32 characters")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created/verified")

    await seed_admin()

    asyncio.create_task(cleanup_temp_files())
    logger.info("Application startup complete")
    yield
    await engine.dispose()


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def safe_file_path(base_dir: Path, *parts: str) -> Path:
    path = base_dir.joinpath(*parts).resolve()
    if not str(path).startswith(str(base_dir.resolve())):
        raise HTTPException(status_code=403, detail="Acesso negado")
    return path


def detect_magic(header: bytes) -> bool:
    for magic in ALLOWED_MIME_MAGIC:
        if header.startswith(magic):
            return True
    return False


# ─── Public pages ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html")


@app.get("/cadastro", response_class=HTMLResponse)
async def cadastro_page(request: Request):
    csrf_token = secrets.token_hex(32)
    response = templates.TemplateResponse(request, "cadastro.html", {
        "csrf_token": csrf_token,
    })
    response.set_cookie("csrf_token", csrf_token, samesite="lax", httponly=False, max_age=3600)
    return response


@app.get("/obrigado", response_class=HTMLResponse)
async def obrigado(request: Request, protocolo: Optional[str] = None):
    return templates.TemplateResponse(request, "obrigado.html", {
        "protocolo": protocolo or "000000",
    })


# ─── ZapSign ──────────────────────────────────────────────────────────────────

@app.post("/api/zapsign/criar-documento")
async def api_criar_documento(payload: ZapSignCreateIn):
    try:
        pdf_bytes = generate_procuration_pdf(payload.nome, payload.cpf)
        result = await zapsign_criar(payload.nome, payload.cpf, pdf_bytes)
        return result
    except Exception as e:
        logger.error("ZapSign create error: %s", e)
        raise HTTPException(status_code=502, detail="Erro ao criar documento na ZapSign. Tente novamente.")


# ─── File upload ──────────────────────────────────────────────────────────────

@app.post("/api/upload")
@limiter.limit("30/hour")
async def api_upload(
    request: Request,
    file: UploadFile = File(...),
    tipo: str = Form(...),
    session_id: str = Form(...),
):
    if tipo not in TIPOS_VALIDOS:
        raise HTTPException(status_code=400, detail=f"Tipo inválido. Permitidos: {TIPOS_VALIDOS}")

    try:
        uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="session_id inválido")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="Arquivo muito grande (máx 10MB)")

    if not detect_magic(content[:8]):
        raise HTTPException(status_code=400, detail="Formato não permitido. Use PDF, JPG ou PNG.")

    ext = Path(file.filename or "file").suffix.lower()
    if ext not in (".pdf", ".jpg", ".jpeg", ".png"):
        ext = ".bin"

    file_uuid = str(uuid.uuid4())
    filename = f"{file_uuid}{ext}"
    meta = json.dumps({"tipo": tipo, "nome_original": file.filename or filename})

    if USE_S3:
        s3 = _s3()
        await asyncio.to_thread(
            s3.put_object,
            Bucket=S3_BUCKET,
            Key=f"temp/{session_id}/{filename}",
            Body=content,
        )
        await asyncio.to_thread(
            s3.put_object,
            Bucket=S3_BUCKET,
            Key=f"temp/{session_id}/{file_uuid}.meta.json",
            Body=meta.encode(),
        )
    else:
        session_dir = safe_file_path(TEMP_DIR, session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / filename).write_bytes(content)
        (session_dir / f"{file_uuid}.meta.json").write_text(meta, encoding="utf-8")

    return {
        "file_id": file_uuid,
        "filename": filename,
        "tipo": tipo,
        "nome_original": file.filename,
        "tamanho_bytes": len(content),
    }


# ─── Registration ─────────────────────────────────────────────────────────────

@app.post("/api/cadastro")
@limiter.limit("5/hour")
async def api_cadastro(
    request: Request,
    nome_completo: str = Form(...),
    cpf: str = Form(...),
    telefone: str = Form(...),
    email: str = Form(...),
    hospital: str = Form(...),
    cargo: str = Form(...),
    tempo_servico: str = Form(...),
    filiado: str = Form(...),
    zapsign_doc_token: str = Form(...),
    session_id: str = Form(...),
    csrf_token: str = Form(...),
    db=Depends(get_db),
):
    # CSRF check
    cookie_csrf = request.cookies.get("csrf_token", "")
    if not cookie_csrf or not secrets.compare_digest(cookie_csrf, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido")

    # Validate session_id
    try:
        uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="session_id inválido")

    filiado_bool = filiado.lower() in ("true", "1", "sim", "yes", "on")

    # Pydantic validation
    try:
        data = CadastroCreate(
            nome_completo=nome_completo,
            cpf=cpf,
            telefone=telefone,
            email=email,
            hospital=hospital,
            cargo=cargo,
            tempo_servico=tempo_servico,
            filiado=filiado_bool,
            zapsign_doc_token=zapsign_doc_token,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    # List uploaded temp files
    if USE_S3:
        s3 = _s3()
        resp = await asyncio.to_thread(
            s3.list_objects_v2,
            Bucket=S3_BUCKET,
            Prefix=f"temp/{session_id}/",
        )
        all_objects = resp.get("Contents", [])
        temp_files_s3 = [o for o in all_objects if not o["Key"].endswith(".meta.json")]
        if not all_objects:
            raise HTTPException(status_code=400, detail="Nenhum documento enviado. Faça o upload dos documentos antes de enviar.")
        if not temp_files_s3:
            raise HTTPException(status_code=400, detail="Nenhum documento encontrado. Envie ao menos um documento.")
    else:
        session_dir = TEMP_DIR / session_id
        if not session_dir.exists():
            raise HTTPException(status_code=400, detail="Nenhum documento enviado. Faça o upload dos documentos antes de enviar.")
        temp_files = [f for f in session_dir.iterdir() if f.is_file() and f.suffix != ".json"]
        if not temp_files:
            raise HTTPException(status_code=400, detail="Nenhum documento encontrado. Envie ao menos um documento.")

    ip = get_client_ip(request)
    user_agent = request.headers.get("User-Agent", "")

    # Save cadastro
    cadastro = Cadastro(
        nome_completo=data.nome_completo,
        cpf=data.cpf,
        telefone=data.telefone,
        email=str(data.email),
        hospital=data.hospital,
        cargo=data.cargo,
        tempo_servico=data.tempo_servico,
        filiado=data.filiado,
        zapsign_doc_token=data.zapsign_doc_token,
        ip_cadastro=ip,
        user_agent=user_agent[:500] if user_agent else None,
    )
    db.add(cadastro)

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="CPF já cadastrado no sistema.")

    cadastro_id = cadastro.id

    if USE_S3:
        for obj in temp_files_s3:
            key = obj["Key"]
            filename = Path(key).name
            file_uuid_str = Path(key).stem
            tipo_doc = "Outro"
            nome_original = filename
            try:
                meta_obj = await asyncio.to_thread(
                    s3.get_object,
                    Bucket=S3_BUCKET,
                    Key=f"temp/{session_id}/{file_uuid_str}.meta.json",
                )
                meta_data = json.loads(meta_obj["Body"].read())
                tipo_doc = meta_data.get("tipo", "Outro")
                nome_original = meta_data.get("nome_original", filename)
            except ClientError:
                pass

            new_key = f"documentos/{cadastro_id}/{filename}"
            await asyncio.to_thread(
                s3.copy_object,
                Bucket=S3_BUCKET,
                CopySource={"Bucket": S3_BUCKET, "Key": key},
                Key=new_key,
            )
            await asyncio.to_thread(s3.delete_object, Bucket=S3_BUCKET, Key=key)

            head = await asyncio.to_thread(s3.head_object, Bucket=S3_BUCKET, Key=new_key)
            doc = Documento(
                cadastro_id=cadastro_id,
                tipo=tipo_doc,
                nome_arquivo=nome_original,
                caminho_arquivo=filename,
                tamanho_bytes=head["ContentLength"],
            )
            db.add(doc)

        for obj in all_objects:
            if obj["Key"].endswith(".meta.json"):
                await asyncio.to_thread(s3.delete_object, Bucket=S3_BUCKET, Key=obj["Key"])
    else:
        dest_dir = DOCS_DIR / str(cadastro_id)
        dest_dir.mkdir(parents=True, exist_ok=True)

        for temp_file in temp_files:
            file_uuid_str = temp_file.stem
            meta_path = session_dir / f"{file_uuid_str}.meta.json"
            tipo_doc = "Outro"
            nome_original = temp_file.name
            if meta_path.exists():
                try:
                    meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
                    tipo_doc = meta_data.get("tipo", "Outro")
                    nome_original = meta_data.get("nome_original", temp_file.name)
                except Exception:
                    pass

            dest_path = dest_dir / temp_file.name
            shutil.move(str(temp_file), str(dest_path))
            doc = Documento(
                cadastro_id=cadastro_id,
                tipo=tipo_doc,
                nome_arquivo=nome_original,
                caminho_arquivo=temp_file.name,
                tamanho_bytes=dest_path.stat().st_size,
            )
            db.add(doc)

        try:
            session_dir.rmdir()
        except OSError:
            pass

    await db.commit()

    asyncio.create_task(
        send_confirmation_email(str(data.email), data.nome_completo, cadastro_id)
    )
    asyncio.create_task(
        send_admin_notification(
            data.nome_completo, data.cpf, data.hospital, data.cargo, data.filiado, cadastro_id
        )
    )

    return JSONResponse({"redirect": f"/obrigado?protocolo={cadastro_id:06d}"})


# ─── Admin auth ───────────────────────────────────────────────────────────────

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse(request, "admin/login.html", {"erro": None})


@app.post("/admin/login")
async def admin_login(
    request: Request,
    email: str = Form(...),
    senha: str = Form(...),
    db=Depends(get_db),
):
    admin = await db.scalar(select(AdminUsuario).where(AdminUsuario.email == email))
    if not admin or not verify_password(senha, admin.senha_hash):
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"erro": "Email ou senha inválidos"},
            status_code=401,
        )
    token = create_access_token({"sub": admin.email})
    response = RedirectResponse(url="/admin", status_code=302)
    response.set_cookie(
        "access_token",
        token,
        httponly=True,
        samesite="strict",
        secure=False,  # Set to True in production (Railway has HTTPS)
        max_age=8 * 3600,
    )
    return response


@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie("access_token")
    return response


# ─── Admin: gerenciamento de usuários ─────────────────────────────────────────

@app.get("/admin/usuarios", response_class=HTMLResponse)
async def admin_usuarios(
    request: Request,
    msg: Optional[str] = None,
    erro: Optional[str] = None,
    admin_email: str = Depends(get_current_admin),
    db=Depends(get_db),
):
    result = await db.execute(select(AdminUsuario).order_by(AdminUsuario.created_at.asc()))
    usuarios = result.scalars().all()
    return templates.TemplateResponse(request, "admin/usuarios.html", {
        "usuarios": usuarios,
        "admin_email": admin_email,
        "msg": msg,
        "erro": erro,
    })


@app.post("/admin/usuarios")
async def admin_criar_usuario(
    request: Request,
    nome: str = Form(...),
    email: str = Form(...),
    senha: str = Form(...),
    admin_email: str = Depends(get_current_admin),
    db=Depends(get_db),
):
    nome = nome.strip()
    email = email.strip().lower()

    if not nome or not email or len(senha) < 8:
        return RedirectResponse(
            url="/admin/usuarios?erro=Preencha+todos+os+campos+(senha+m%C3%ADnimo+8+caracteres)",
            status_code=302,
        )

    existing = await db.scalar(select(AdminUsuario).where(AdminUsuario.email == email))
    if existing:
        return RedirectResponse(
            url="/admin/usuarios?erro=J%C3%A1+existe+um+usu%C3%A1rio+com+esse+e-mail",
            status_code=302,
        )

    novo = AdminUsuario(nome=nome, email=email, senha_hash=hash_password(senha))
    db.add(novo)
    await db.commit()
    return RedirectResponse(url="/admin/usuarios?msg=Usu%C3%A1rio+criado+com+sucesso", status_code=302)


@app.post("/admin/usuarios/{usuario_id}/excluir")
async def admin_excluir_usuario(
    usuario_id: int,
    admin_email: str = Depends(get_current_admin),
    db=Depends(get_db),
):
    alvo = await db.get(AdminUsuario, usuario_id)
    if not alvo:
        return RedirectResponse(url="/admin/usuarios?erro=Usu%C3%A1rio+n%C3%A3o+encontrado", status_code=302)

    if alvo.email == admin_email:
        return RedirectResponse(
            url="/admin/usuarios?erro=Voc%C3%AA+n%C3%A3o+pode+excluir+o+pr%C3%B3prio+usu%C3%A1rio",
            status_code=302,
        )

    total = await db.scalar(select(func.count(AdminUsuario.id)))
    if total <= 1:
        return RedirectResponse(
            url="/admin/usuarios?erro=N%C3%A3o+%C3%A9+poss%C3%ADvel+excluir+o+%C3%BAnico+usu%C3%A1rio",
            status_code=302,
        )

    await db.delete(alvo)
    await db.commit()
    return RedirectResponse(url="/admin/usuarios?msg=Usu%C3%A1rio+exclu%C3%ADdo", status_code=302)


# ─── Admin dashboard ──────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    status: Optional[str] = None,
    filiado: Optional[str] = None,
    q: Optional[str] = None,
    page: int = 1,
    admin_email: str = Depends(get_current_admin),
    db=Depends(get_db),
):
    page = max(1, page)
    per_page = 20

    conditions = []
    if status:
        conditions.append(Cadastro.status == status)
    if filiado == "sim":
        conditions.append(Cadastro.filiado == True)
    elif filiado == "nao":
        conditions.append(Cadastro.filiado == False)
    if q:
        conditions.append(
            or_(
                Cadastro.nome_completo.ilike(f"%{q}%"),
                Cadastro.cpf.contains(q),
            )
        )

    where_clause = and_(*conditions) if conditions else True

    total = await db.scalar(select(func.count(Cadastro.id)).where(where_clause))
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)

    result = await db.execute(
        select(Cadastro)
        .where(where_clause)
        .order_by(Cadastro.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    cadastros = result.scalars().all()

    stats_result = await db.execute(
        select(Cadastro.status, func.count(Cadastro.id)).group_by(Cadastro.status)
    )
    stats_raw = dict(stats_result.all())
    stats = {
        "total": total or 0,
        "novo": stats_raw.get("novo", 0),
        "em_andamento": stats_raw.get("em_andamento", 0),
        "concluido": stats_raw.get("concluido", 0),
    }

    return templates.TemplateResponse(request, "admin/dashboard.html", {
        "cadastros": cadastros,
        "stats": stats,
        "page": page,
        "pages": pages,
        "filters": {"status": status or "", "filiado": filiado or "", "q": q or ""},
        "admin_email": admin_email,
    })


@app.get("/admin/cadastro/{cadastro_id}", response_class=HTMLResponse)
async def admin_detalhe(
    request: Request,
    cadastro_id: int,
    admin_email: str = Depends(get_current_admin),
    db=Depends(get_db),
):
    result = await db.execute(
        select(Cadastro)
        .options(selectinload(Cadastro.documentos))
        .where(Cadastro.id == cadastro_id)
    )
    cadastro = result.scalar_one_or_none()
    if not cadastro:
        raise HTTPException(status_code=404, detail="Cadastro não encontrado")

    return templates.TemplateResponse(request, "admin/detalhe.html", {
        "cadastro": cadastro,
        "admin_email": admin_email,
        "status_opcoes": ["novo", "em_andamento", "concluido"],
    })


@app.post("/admin/cadastro/{cadastro_id}/status")
async def admin_update_status(
    cadastro_id: int,
    payload: StatusUpdateIn,
    admin_email: str = Depends(get_current_admin),
    db=Depends(get_db),
):
    cadastro = await db.get(Cadastro, cadastro_id)
    if not cadastro:
        raise HTTPException(status_code=404)
    cadastro.status = payload.status
    cadastro.updated_at = datetime.now()
    await db.commit()
    return {"ok": True, "status": payload.status}


@app.post("/admin/cadastro/{cadastro_id}/nota")
async def admin_update_nota(
    cadastro_id: int,
    payload: NotaUpdateIn,
    admin_email: str = Depends(get_current_admin),
    db=Depends(get_db),
):
    cadastro = await db.get(Cadastro, cadastro_id)
    if not cadastro:
        raise HTTPException(status_code=404)
    cadastro.nota_interna = payload.nota
    cadastro.updated_at = datetime.now()
    await db.commit()
    return {"ok": True}


@app.post("/admin/cadastro/{cadastro_id}/tipo-documento/{doc_id}")
async def admin_update_doc_tipo(
    cadastro_id: int,
    doc_id: int,
    tipo: str = Form(...),
    admin_email: str = Depends(get_current_admin),
    db=Depends(get_db),
):
    if tipo not in TIPOS_VALIDOS:
        raise HTTPException(status_code=400)
    doc = await db.get(Documento, doc_id)
    if not doc or doc.cadastro_id != cadastro_id:
        raise HTTPException(status_code=404)
    doc.tipo = tipo
    await db.commit()
    return {"ok": True}


@app.get("/admin/documentos/{documento_id}")
async def admin_download_documento(
    documento_id: int,
    admin_email: str = Depends(get_current_admin),
    db=Depends(get_db),
):
    doc = await db.get(Documento, documento_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado")

    if USE_S3:
        s3 = _s3()
        s3_key = f"documentos/{doc.cadastro_id}/{doc.caminho_arquivo}"
        try:
            obj = await asyncio.to_thread(s3.get_object, Bucket=S3_BUCKET, Key=s3_key)
            content = await asyncio.to_thread(obj["Body"].read)
        except ClientError:
            raise HTTPException(status_code=404, detail="Arquivo não encontrado no storage")
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{doc.nome_arquivo}"'},
        )

    file_path = safe_file_path(DOCS_DIR, str(doc.cadastro_id), doc.caminho_arquivo)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado no storage")
    return FileResponse(
        path=str(file_path),
        filename=doc.nome_arquivo,
        media_type="application/octet-stream",
    )
