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
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from auth import (
    create_access_token,
    get_current_admin,
    hash_password,
    verify_password,
)
from database import AsyncSessionLocal, Base, engine, get_db
from email_service import (
    send_admin_notification,
    send_confirmation_email,
    send_etapa2_email,
    smtp_status,
)
from models import AdminUsuario, Cadastro, Documento
from pdf_generator import generate_procuration_pdf
from schemas import (
    CadastroCreate,
    NotaUpdateIn,
    StatusUpdateIn,
)
from zapsign import consultar_documento as zapsign_consultar
from zapsign import criar_documento as zapsign_criar

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STORAGE_ROOT = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "./storage"))
TEMP_DIR = STORAGE_ROOT / "temp"
DOCS_DIR = STORAGE_ROOT / "documentos"

S3_BUCKET = os.environ.get("AWS_S3_BUCKET_NAME", "")
USE_S3 = bool(S3_BUCKET)
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
DOCS_OBRIGATORIOS_ETAPA2 = {"RG", "CTPS", "Holerite"}


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
TIPOS_VALIDOS = {"RG", "CPF", "CTPS", "Holerite", "PPP", "CNIS", "Outro"}


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
                papel="admin",
            )
            db.add(admin)
            await db.commit()
            logger.info("Admin user seeded: %s", ADMIN_INITIAL_EMAIL)
        elif existing.papel != "admin":
            existing.papel = "admin"
            await db.commit()
            logger.info("Admin user promoted to admin: %s", ADMIN_INITIAL_EMAIL)


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
        # Migração: garante a coluna 'papel' em bancos criados antes desse campo existir
        await conn.execute(text(
            "ALTER TABLE admin_usuarios ADD COLUMN IF NOT EXISTS papel "
            "VARCHAR(20) NOT NULL DEFAULT 'juridico'"
        ))
        # Migração: campos de endereço, agendamento e etapa 2
        for ddl in (
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS cep VARCHAR(9)",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS logradouro VARCHAR(255)",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS numero VARCHAR(20)",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS complemento VARCHAR(100)",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS bairro VARCHAR(100)",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS cidade VARCHAR(100)",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS uf VARCHAR(2)",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS analise_estabilidade BOOLEAN NOT NULL DEFAULT false",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS modalidade_atendimento VARCHAR(20)",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS agendamento TIMESTAMP",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS etapa2_token VARCHAR(64)",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS etapa2_liberada_em TIMESTAMP",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS etapa2_concluida_em TIMESTAMP",
        ):
            await conn.execute(text(ddl))
    logger.info("Database tables created/verified")

    logger.info("Diagnóstico e-mail: %s", smtp_status())

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


async def get_current_admin_obj(request: Request, db=Depends(get_db)) -> AdminUsuario:
    """Carrega o usuário logado do banco (com papel)."""
    email = await get_current_admin(request)
    user = await db.scalar(select(AdminUsuario).where(AdminUsuario.email == email))
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/admin/login"})
    return user


async def require_admin_role(user: AdminUsuario = Depends(get_current_admin_obj)) -> AdminUsuario:
    """Permite acesso apenas a usuários com papel 'admin'."""
    if user.papel != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")
    return user


# ─── Public pages ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html")


@app.get("/inicio")
async def inicio_redirect():
    return RedirectResponse(url="/", status_code=301)


@app.get("/cadastro", response_class=HTMLResponse)
async def cadastro_page(request: Request):
    csrf_token = secrets.token_hex(32)
    response = templates.TemplateResponse(request, "cadastro.html", {
        "csrf_token": csrf_token,
    })
    response.set_cookie("csrf_token", csrf_token, samesite="lax", httponly=False, max_age=3600)
    return response


@app.get("/obrigado", response_class=HTMLResponse)
async def obrigado(
    request: Request,
    protocolo: Optional[str] = None,
    data: Optional[str] = None,
    hora: Optional[str] = None,
    modalidade: Optional[str] = None,
    etapa: Optional[str] = None,
):
    return templates.TemplateResponse(request, "obrigado.html", {
        "protocolo": protocolo or "000000",
        "data": data,
        "hora": hora,
        "modalidade": modalidade if modalidade in ("online", "presencial") else None,
        "etapa2": etapa == "2",
    })


# ─── ZapSign ──────────────────────────────────────────────────────────────────

@app.post("/api/etapa2/{token}/zapsign")
@limiter.limit("30/hour")
async def api_etapa2_zapsign(request: Request, token: str, db=Depends(get_db)):
    cadastro = await db.scalar(select(Cadastro).where(Cadastro.etapa2_token == token))
    if not cadastro or cadastro.etapa2_concluida_em:
        raise HTTPException(status_code=404, detail="Link inválido ou já utilizado")
    try:
        pdf_bytes = generate_procuration_pdf(cadastro.nome_completo, cadastro.cpf)
        result = await zapsign_criar(cadastro.nome_completo, cadastro.cpf, pdf_bytes)
        return result
    except Exception as e:
        logger.error("ZapSign create error: %s", e)
        raise HTTPException(status_code=502, detail="Erro ao criar documento na ZapSign. Tente novamente.")


@app.get("/api/zapsign/status")
@limiter.limit("1200/hour")
async def api_zapsign_status(request: Request, doc_token: str):
    try:
        info = await zapsign_consultar(doc_token)
    except Exception as e:
        logger.error("ZapSign status error: %s", e)
        raise HTTPException(status_code=502, detail="Erro ao consultar status na ZapSign")

    doc_status = (info.get("status") or "").lower()
    signers = info.get("signers", [])
    todos_assinaram = bool(signers) and all(
        (s.get("status") or "").lower() == "signed" for s in signers
    )
    assinado = doc_status == "signed" or todos_assinaram
    return {"signed": assinado}


# ─── File upload ──────────────────────────────────────────────────────────────

@app.post("/api/upload")
@limiter.limit("300/hour")
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


# ─── Documentos: helpers de sessão ────────────────────────────────────────────

async def _listar_tipos_sessao(session_id: str) -> list[str]:
    """Lista os tipos de documento já enviados na sessão temporária (sem mover nada)."""
    tipos: list[str] = []
    if USE_S3:
        s3 = _s3()
        resp = await asyncio.to_thread(
            s3.list_objects_v2, Bucket=S3_BUCKET, Prefix=f"temp/{session_id}/"
        )
        for obj in resp.get("Contents", []):
            if obj["Key"].endswith(".meta.json"):
                try:
                    meta_obj = await asyncio.to_thread(
                        s3.get_object, Bucket=S3_BUCKET, Key=obj["Key"]
                    )
                    meta_data = json.loads(meta_obj["Body"].read())
                    tipos.append(meta_data.get("tipo", "Outro"))
                except ClientError:
                    pass
    else:
        session_dir = TEMP_DIR / session_id
        if session_dir.exists():
            for meta_path in session_dir.glob("*.meta.json"):
                try:
                    meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
                    tipos.append(meta_data.get("tipo", "Outro"))
                except Exception:
                    pass
    return tipos


async def _mover_documentos_sessao(db, session_id: str, cadastro_id: int) -> int:
    """Move os arquivos temporários da sessão para o cadastro. Retorna quantos moveu."""
    movidos = 0
    if USE_S3:
        s3 = _s3()
        resp = await asyncio.to_thread(
            s3.list_objects_v2, Bucket=S3_BUCKET, Prefix=f"temp/{session_id}/"
        )
        all_objects = resp.get("Contents", [])
        for obj in [o for o in all_objects if not o["Key"].endswith(".meta.json")]:
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
            db.add(Documento(
                cadastro_id=cadastro_id,
                tipo=tipo_doc,
                nome_arquivo=nome_original,
                caminho_arquivo=filename,
                tamanho_bytes=head["ContentLength"],
            ))
            movidos += 1

        for obj in all_objects:
            if obj["Key"].endswith(".meta.json"):
                await asyncio.to_thread(s3.delete_object, Bucket=S3_BUCKET, Key=obj["Key"])
    else:
        session_dir = TEMP_DIR / session_id
        if not session_dir.exists():
            return 0
        dest_dir = DOCS_DIR / str(cadastro_id)
        dest_dir.mkdir(parents=True, exist_ok=True)

        for temp_file in [f for f in session_dir.iterdir() if f.is_file() and f.suffix != ".json"]:
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
            db.add(Documento(
                cadastro_id=cadastro_id,
                tipo=tipo_doc,
                nome_arquivo=nome_original,
                caminho_arquivo=temp_file.name,
                tamanho_bytes=dest_path.stat().st_size,
            ))
            movidos += 1

        for meta_path in session_dir.glob("*.meta.json"):
            meta_path.unlink(missing_ok=True)
        try:
            session_dir.rmdir()
        except OSError:
            pass
    return movidos


# ─── Agenda de atendimentos ───────────────────────────────────────────────────

AGENDA_HORARIOS = ["09:00", "10:00", "11:00", "13:00", "14:00", "15:00", "16:00"]
AGENDA_DIAS_JANELA = 60  # agendamento permitido até N dias à frente


def _validar_data_agenda(data_str: str) -> datetime:
    try:
        dia = datetime.strptime(data_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida")
    hoje = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if dia <= hoje:
        raise HTTPException(status_code=400, detail="Escolha uma data a partir de amanhã")
    if dia > hoje + timedelta(days=AGENDA_DIAS_JANELA):
        raise HTTPException(status_code=400, detail=f"Agendamento disponível até {AGENDA_DIAS_JANELA} dias à frente")
    if dia.weekday() >= 5:
        raise HTTPException(status_code=400, detail="Atendimentos apenas em dias úteis")
    return dia


@app.get("/api/agenda/horarios")
@limiter.limit("300/hour")
async def api_agenda_horarios(request: Request, data: str, db=Depends(get_db)):
    dia = _validar_data_agenda(data)

    inicio = dia
    fim = dia + timedelta(days=1)
    result = await db.execute(
        select(Cadastro.agendamento).where(
            and_(Cadastro.agendamento >= inicio, Cadastro.agendamento < fim)
        )
    )
    ocupados = {dt.strftime("%H:%M") for (dt,) in result.all() if dt}

    return {
        "data": data,
        "horarios": [
            {"hora": h, "disponivel": h not in ocupados} for h in AGENDA_HORARIOS
        ],
    }


# ─── Registration (Etapa 1) ───────────────────────────────────────────────────

@app.post("/api/cadastro")
@limiter.limit("50/hour")
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
    cep: str = Form(...),
    logradouro: str = Form(...),
    numero: str = Form(...),
    complemento: str = Form(""),
    bairro: str = Form(...),
    cidade: str = Form(...),
    uf: str = Form(...),
    analise_estabilidade: str = Form("false"),
    modalidade_atendimento: str = Form(...),
    agendamento_data: str = Form(...),
    agendamento_hora: str = Form(...),
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
    estabilidade_bool = analise_estabilidade.lower() in ("true", "1", "sim", "yes", "on")

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
            analise_estabilidade=estabilidade_bool,
            cep=cep,
            logradouro=logradouro,
            numero=numero,
            complemento=complemento,
            bairro=bairro,
            cidade=cidade,
            uf=uf,
            modalidade_atendimento=modalidade_atendimento,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Valida o horário de agendamento
    dia = _validar_data_agenda(agendamento_data)
    if agendamento_hora not in AGENDA_HORARIOS:
        raise HTTPException(status_code=400, detail="Horário inválido")
    hora_h, hora_m = map(int, agendamento_hora.split(":"))
    slot = dia.replace(hour=hora_h, minute=hora_m)

    ocupado = await db.scalar(
        select(func.count(Cadastro.id)).where(Cadastro.agendamento == slot)
    )
    if ocupado:
        raise HTTPException(
            status_code=409,
            detail="Este horário acabou de ser reservado. Escolha outro horário.",
        )

    ip = get_client_ip(request)
    user_agent = request.headers.get("User-Agent", "")

    cadastro = Cadastro(
        nome_completo=data.nome_completo,
        cpf=data.cpf,
        telefone=data.telefone,
        email=str(data.email),
        hospital=data.hospital,
        cargo=data.cargo,
        tempo_servico=data.tempo_servico,
        filiado=data.filiado,
        cep=data.cep,
        logradouro=data.logradouro,
        numero=data.numero,
        complemento=data.complemento or None,
        bairro=data.bairro,
        cidade=data.cidade,
        uf=data.uf,
        analise_estabilidade=data.analise_estabilidade,
        modalidade_atendimento=data.modalidade_atendimento,
        agendamento=slot,
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

    # CNIS é opcional: move se houver upload na sessão
    await _mover_documentos_sessao(db, session_id, cadastro_id)

    await db.commit()

    agendamento_str = slot.strftime("%d/%m/%Y às %H:%M")
    asyncio.create_task(
        send_confirmation_email(
            str(data.email), data.nome_completo, cadastro_id,
            modalidade=data.modalidade_atendimento, agendamento=agendamento_str,
        )
    )
    asyncio.create_task(
        send_admin_notification(
            data.nome_completo, data.cpf, data.hospital, data.cargo, data.filiado, cadastro_id,
            modalidade=data.modalidade_atendimento, agendamento=agendamento_str,
        )
    )

    return JSONResponse({
        "redirect": (
            f"/obrigado?protocolo={cadastro_id:06d}"
            f"&data={slot.strftime('%d/%m/%Y')}&hora={agendamento_hora}"
            f"&modalidade={data.modalidade_atendimento}"
        )
    })


# ─── Etapa 2: documentação completa + procuração ─────────────────────────────

@app.get("/etapa2/{token}", response_class=HTMLResponse)
async def etapa2_page(request: Request, token: str, db=Depends(get_db)):
    cadastro = await db.scalar(select(Cadastro).where(Cadastro.etapa2_token == token))
    if not cadastro:
        raise HTTPException(status_code=404, detail="Link inválido ou expirado")

    csrf_token = secrets.token_hex(32)
    response = templates.TemplateResponse(request, "etapa2.html", {
        "cadastro": cadastro,
        "token": token,
        "concluido": cadastro.etapa2_concluida_em is not None,
        "csrf_token": csrf_token,
    })
    response.set_cookie("csrf_token", csrf_token, samesite="lax", httponly=False, max_age=3600)
    return response


@app.post("/api/etapa2/{token}")
@limiter.limit("30/hour")
async def api_etapa2_submit(
    request: Request,
    token: str,
    zapsign_doc_token: str = Form(...),
    session_id: str = Form(...),
    csrf_token: str = Form(...),
    db=Depends(get_db),
):
    cookie_csrf = request.cookies.get("csrf_token", "")
    if not cookie_csrf or not secrets.compare_digest(cookie_csrf, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido")

    try:
        uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="session_id inválido")

    cadastro = await db.scalar(select(Cadastro).where(Cadastro.etapa2_token == token))
    if not cadastro:
        raise HTTPException(status_code=404, detail="Link inválido ou expirado")
    if cadastro.etapa2_concluida_em:
        raise HTTPException(status_code=409, detail="A documentação deste cadastro já foi enviada.")

    if not zapsign_doc_token.strip():
        raise HTTPException(status_code=400, detail="Assine a procuração antes de enviar.")

    # Valida documentos obrigatórios antes de mover qualquer arquivo
    tipos_enviados = set(await _listar_tipos_sessao(session_id))
    faltando = DOCS_OBRIGATORIOS_ETAPA2 - tipos_enviados
    if faltando:
        raise HTTPException(
            status_code=400,
            detail=f"Faltam documentos obrigatórios: {', '.join(sorted(faltando))}",
        )

    await _mover_documentos_sessao(db, session_id, cadastro.id)

    cadastro.zapsign_doc_token = zapsign_doc_token.strip()
    cadastro.etapa2_concluida_em = datetime.now()
    cadastro.status = "em_andamento"
    cadastro.updated_at = datetime.now()
    await db.commit()

    return JSONResponse({"redirect": f"/obrigado?protocolo={cadastro.id:06d}&etapa=2"})


@app.post("/admin/cadastro/{cadastro_id}/liberar-etapa2")
async def admin_liberar_etapa2(
    request: Request,
    cadastro_id: int,
    admin_email: str = Depends(get_current_admin),
    db=Depends(get_db),
):
    cadastro = await db.get(Cadastro, cadastro_id)
    if not cadastro:
        raise HTTPException(status_code=404, detail="Cadastro não encontrado")
    if cadastro.etapa2_concluida_em:
        raise HTTPException(status_code=409, detail="Etapa 2 já concluída para este cadastro")

    if not cadastro.etapa2_token:
        cadastro.etapa2_token = secrets.token_urlsafe(32)
    cadastro.etapa2_liberada_em = datetime.now()
    cadastro.updated_at = datetime.now()
    await db.commit()

    base = BASE_URL or str(request.base_url).rstrip("/")
    link = f"{base}/etapa2/{cadastro.etapa2_token}"
    email_enviado = await send_etapa2_email(cadastro.email, cadastro.nome_completo, link)
    return {"ok": True, "email_enviado": email_enviado, "link": link}


@app.post("/admin/cadastro/{cadastro_id}/excluir")
async def admin_excluir_cadastro(
    cadastro_id: int,
    current=Depends(require_admin_role),
    db=Depends(get_db),
):
    cadastro = await db.get(Cadastro, cadastro_id)
    if not cadastro:
        raise HTTPException(status_code=404, detail="Cadastro não encontrado")

    # Remove os documentos do armazenamento
    if USE_S3:
        s3 = _s3()
        resp = await asyncio.to_thread(
            s3.list_objects_v2, Bucket=S3_BUCKET, Prefix=f"documentos/{cadastro_id}/"
        )
        for obj in resp.get("Contents", []):
            await asyncio.to_thread(s3.delete_object, Bucket=S3_BUCKET, Key=obj["Key"])
    else:
        doc_dir = DOCS_DIR / str(cadastro_id)
        if doc_dir.exists():
            shutil.rmtree(doc_dir, ignore_errors=True)

    # Remove o cadastro (documentos no banco caem por cascade)
    await db.delete(cadastro)
    await db.commit()
    return {"ok": True}


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
    current=Depends(require_admin_role),
    db=Depends(get_db),
):
    result = await db.execute(select(AdminUsuario).order_by(AdminUsuario.created_at.asc()))
    usuarios = result.scalars().all()
    return templates.TemplateResponse(request, "admin/usuarios.html", {
        "usuarios": usuarios,
        "admin_email": current.email,
        "is_admin": True,
        "msg": msg,
        "erro": erro,
    })


@app.post("/admin/usuarios")
async def admin_criar_usuario(
    request: Request,
    nome: str = Form(...),
    email: str = Form(...),
    senha: str = Form(...),
    papel: str = Form("juridico"),
    current=Depends(require_admin_role),
    db=Depends(get_db),
):
    nome = nome.strip()
    email = email.strip().lower()
    papel = papel if papel in ("admin", "juridico") else "juridico"

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

    novo = AdminUsuario(nome=nome, email=email, senha_hash=hash_password(senha), papel=papel)
    db.add(novo)
    await db.commit()
    return RedirectResponse(url="/admin/usuarios?msg=Usu%C3%A1rio+criado+com+sucesso", status_code=302)


@app.post("/admin/usuarios/{usuario_id}/excluir")
async def admin_excluir_usuario(
    usuario_id: int,
    current=Depends(require_admin_role),
    db=Depends(get_db),
):
    alvo = await db.get(AdminUsuario, usuario_id)
    if not alvo:
        return RedirectResponse(url="/admin/usuarios?erro=Usu%C3%A1rio+n%C3%A3o+encontrado", status_code=302)

    if alvo.email == current.email:
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
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    admin_email = current.email
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
        "is_admin": current.papel == "admin",
    })


@app.get("/admin/cadastro/{cadastro_id}", response_class=HTMLResponse)
async def admin_detalhe(
    request: Request,
    cadastro_id: int,
    current=Depends(get_current_admin_obj),
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
        "admin_email": current.email,
        "is_admin": current.papel == "admin",
        "status_opcoes": ["novo", "em_andamento", "concluido"],
    })


@app.get("/admin/cadastro/{cadastro_id}/procuracao")
async def admin_baixar_procuracao(
    cadastro_id: int,
    admin_email: str = Depends(get_current_admin),
    db=Depends(get_db),
):
    cadastro = await db.get(Cadastro, cadastro_id)
    if not cadastro or not cadastro.zapsign_doc_token:
        raise HTTPException(status_code=404, detail="Procuração não encontrada para este cadastro")

    try:
        info = await zapsign_consultar(cadastro.zapsign_doc_token)
    except Exception as e:
        logger.error("ZapSign consulta error: %s", e)
        raise HTTPException(status_code=502, detail="Erro ao consultar a procuração na ZapSign")

    pdf_url = info.get("signed_file") or info.get("original_file")
    if not pdf_url:
        raise HTTPException(status_code=404, detail="PDF da procuração ainda não disponível")

    return RedirectResponse(url=pdf_url, status_code=302)


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
