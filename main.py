import asyncio
import json
import logging
import os
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
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
    send_lembrete_email,
    smtp_status,
)
from file_security import safe_download_name, sanitize_upload
from models import (
    AdminUsuario,
    BloqueioAgenda,
    Cadastro,
    ConfigAgenda,
    Documento,
    EventoSessao,
    HistoricoCadastro,
    Lead,
    NotaCadastro,
)
from pdf_generator import generate_procuration_pdf
from schemas import (
    AtendenteUpdateIn,
    CadastroCreate,
    CadastroUpdateIn,
    EventoSessaoCreate,
    LeadCreate,
    NotaUpdateIn,
    StatusUpdateIn,
)
from zapsign import consultar_documento as zapsign_consultar
from zapsign import criar_documento as zapsign_criar
from zapsign import criar_via_modelo as zapsign_criar_modelo
from zapsign import usar_modelo as zapsign_usar_modelo

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


async def processar_lembretes() -> None:
    while True:
        await asyncio.sleep(1800)  # a cada 30 min
        agora = datetime.now()
        try:
            async with AsyncSessionLocal() as db:
                # Lembrete 1: +24h sem conversão, termos aceitos
                resultado_1 = await db.execute(
                    select(Lead).where(
                        Lead.convertido_em.is_(None),
                        Lead.descadastrado == False,
                        Lead.consentimento_termos == True,
                        Lead.lembrete_1_enviado_em.is_(None),
                        Lead.criado_em <= agora - timedelta(hours=24),
                    ).limit(50)
                )
                for lead in resultado_1.scalars().all():
                    ok = await send_lembrete_email(lead.email, lead.id_publico, 1)
                    if ok:
                        lead.lembrete_1_enviado_em = agora
                await db.commit()

                # Lembrete 2: +72h, lembrete 1 já enviado
                resultado_2 = await db.execute(
                    select(Lead).where(
                        Lead.convertido_em.is_(None),
                        Lead.descadastrado == False,
                        Lead.lembrete_1_enviado_em.is_not(None),
                        Lead.lembrete_2_enviado_em.is_(None),
                        Lead.criado_em <= agora - timedelta(hours=72),
                    ).limit(50)
                )
                for lead in resultado_2.scalars().all():
                    ok = await send_lembrete_email(lead.email, lead.id_publico, 2)
                    if ok:
                        lead.lembrete_2_enviado_em = agora
                await db.commit()
        except Exception as e:
            logger.error("Erro ao processar lembretes: %s", e)


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
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS rg VARCHAR(20)",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS data_nascimento DATE",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS estado_civil VARCHAR(30)",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS nacionalidade VARCHAR(40)",
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS recebe_outro_beneficio BOOLEAN NOT NULL DEFAULT false",
            # Novas tabelas: leads e eventos_sessao
            """CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                id_publico VARCHAR(36) NOT NULL,
                email VARCHAR(255) NOT NULL,
                consentimento_termos BOOLEAN NOT NULL DEFAULT false,
                consentimento_marketing BOOLEAN NOT NULL DEFAULT false,
                cadastro_id INTEGER REFERENCES cadastros(id) ON DELETE SET NULL,
                convertido_em TIMESTAMP,
                lembrete_1_enviado_em TIMESTAMP,
                lembrete_2_enviado_em TIMESTAMP,
                descadastrado BOOLEAN NOT NULL DEFAULT false,
                ip VARCHAR(45),
                user_agent TEXT,
                criado_em TIMESTAMP NOT NULL DEFAULT now()
            )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_id_publico ON leads(id_publico)",
            "CREATE INDEX IF NOT EXISTS ix_leads_email ON leads(email)",
            "CREATE INDEX IF NOT EXISTS ix_leads_cadastro_id ON leads(cadastro_id)",
            """CREATE TABLE IF NOT EXISTS eventos_sessao (
                id SERIAL PRIMARY KEY,
                lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
                session_id VARCHAR(36) NOT NULL,
                tipo VARCHAR(30) NOT NULL,
                payload TEXT,
                criado_em TIMESTAMP NOT NULL DEFAULT now()
            )""",
            "CREATE INDEX IF NOT EXISTS ix_eventos_session_id ON eventos_sessao(session_id)",
            "CREATE INDEX IF NOT EXISTS ix_eventos_tipo ON eventos_sessao(tipo)",
            # Tabelas de configuração da agenda
            """CREATE TABLE IF NOT EXISTS config_agenda (
                id INTEGER PRIMARY KEY DEFAULT 1,
                hora_inicio VARCHAR(5) NOT NULL DEFAULT '09:00',
                hora_fim VARCHAR(5) NOT NULL DEFAULT '16:00',
                intervalo_minutos INTEGER NOT NULL DEFAULT 60,
                atualizado_em TIMESTAMP NOT NULL DEFAULT now(),
                atualizado_por VARCHAR(255) NOT NULL DEFAULT 'sistema'
            )""",
            "INSERT INTO config_agenda (id) VALUES (1) ON CONFLICT DO NOTHING",
            """CREATE TABLE IF NOT EXISTS bloqueios_agenda (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL UNIQUE,
                motivo TEXT,
                criado_em TIMESTAMP NOT NULL DEFAULT now(),
                criado_por VARCHAR(255) NOT NULL DEFAULT 'sistema'
            )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_bloqueios_data ON bloqueios_agenda(data)",
            # Atribuição de atendente jurídico aos cadastros
            "ALTER TABLE cadastros ADD COLUMN IF NOT EXISTS atendente_id INTEGER REFERENCES admin_usuarios(id) ON DELETE SET NULL",
            "CREATE INDEX IF NOT EXISTS ix_cadastros_atendente_id ON cadastros(atendente_id)",
            # Histórico de ações por cadastro
            """CREATE TABLE IF NOT EXISTS historico_cadastro (
                id SERIAL PRIMARY KEY,
                cadastro_id INTEGER NOT NULL REFERENCES cadastros(id) ON DELETE CASCADE,
                tipo VARCHAR(40) NOT NULL,
                ator_email VARCHAR(255),
                ator_nome VARCHAR(255),
                descricao TEXT NOT NULL,
                valor_anterior VARCHAR(255),
                valor_novo VARCHAR(255),
                criado_em TIMESTAMP NOT NULL DEFAULT now()
            )""",
            "CREATE INDEX IF NOT EXISTS ix_historico_cadastro_id ON historico_cadastro(cadastro_id)",
            "CREATE INDEX IF NOT EXISTS ix_historico_tipo ON historico_cadastro(tipo)",
            # Notas internas em feed (várias por cadastro)
            """CREATE TABLE IF NOT EXISTS notas_cadastro (
                id SERIAL PRIMARY KEY,
                cadastro_id INTEGER NOT NULL REFERENCES cadastros(id) ON DELETE CASCADE,
                autor_id INTEGER REFERENCES admin_usuarios(id) ON DELETE SET NULL,
                autor_email VARCHAR(255),
                autor_nome VARCHAR(255),
                texto TEXT NOT NULL,
                criado_em TIMESTAMP NOT NULL DEFAULT now()
            )""",
            "CREATE INDEX IF NOT EXISTS ix_notas_cadastro_id ON notas_cadastro(cadastro_id)",
        ):
            await conn.execute(text(ddl))
    logger.info("Database tables created/verified")

    logger.info("Diagnóstico e-mail: %s", smtp_status())
    logger.info(
        "Diagnóstico ZapSign: TEMPLATE_ID=%s | SANDBOX=%s | TOKEN=%s",
        os.environ.get("ZAPSIGN_TEMPLATE_ID", "(vazio)"),
        os.environ.get("ZAPSIGN_SANDBOX", "true"),
        "OK" if os.environ.get("ZAPSIGN_API_TOKEN") else "FALTANDO",
    )

    await seed_admin()

    asyncio.create_task(cleanup_temp_files())
    asyncio.create_task(processar_lembretes())
    logger.info("Application startup complete")
    yield
    await engine.dispose()


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def _detectar_dispositivo(user_agent: str | None) -> str:
    if not user_agent:
        return "desconhecido"
    ua = user_agent.lower()
    if any(k in ua for k in ("mobile", "android", "iphone", "ipad", "ipod", "tablet", "phone")):
        return "celular"
    return "pc"


templates.env.globals["dispositivo"] = _detectar_dispositivo


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def registrar_historico(
    db,
    cadastro_id: int,
    tipo: str,
    descricao: str,
    ator: Optional[AdminUsuario] = None,
    valor_anterior: Optional[str] = None,
    valor_novo: Optional[str] = None,
) -> None:
    """Adiciona um evento ao histórico do cadastro (não commita — chame db.commit() depois)."""
    db.add(HistoricoCadastro(
        cadastro_id=cadastro_id,
        tipo=tipo,
        ator_email=ator.email if ator else None,
        ator_nome=ator.nome if ator else None,
        descricao=descricao,
        valor_anterior=(valor_anterior[:255] if valor_anterior else None),
        valor_novo=(valor_novo[:255] if valor_novo else None),
    ))


STATUS_LABELS = {
    "novo": "Novo",
    "em_andamento": "Em andamento",
    "concluido": "Concluído",
}


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


@app.get("/politica-privacidade", response_class=HTMLResponse)
async def politica_privacidade(request: Request):
    return templates.TemplateResponse(request, "politica-privacidade.html")


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


# ─── Lead capture & analytics ────────────────────────────────────────────────

@app.post("/api/lead")
@limiter.limit("10/hour")
async def api_lead(request: Request, body: LeadCreate, db=Depends(get_db)):
    email = str(body.email).lower().strip()
    ip = get_client_ip(request)
    user_agent = request.headers.get("User-Agent", "")[:500]

    # Upsert: se já existe lead não convertido com esse email, atualiza consentimento
    existente = await db.scalar(
        select(Lead).where(Lead.email == email, Lead.convertido_em.is_(None))
    )
    if existente:
        existente.consentimento_termos = body.consentimento_termos
        existente.consentimento_marketing = body.consentimento_marketing
        existente.ip = ip
        await db.commit()
        return {"lead_id": existente.id_publico}

    lead = Lead(
        email=email,
        consentimento_termos=body.consentimento_termos,
        consentimento_marketing=body.consentimento_marketing,
        ip=ip,
        user_agent=user_agent,
    )
    db.add(lead)
    await db.commit()
    return {"lead_id": lead.id_publico}


@app.post("/api/analytics/evento", status_code=204)
@limiter.limit("600/hour")
async def api_analytics_evento(request: Request, body: EventoSessaoCreate, db=Depends(get_db)):
    lead_pk: Optional[int] = None
    if body.lead_id:
        lead_pk = await db.scalar(
            select(Lead.id).where(Lead.id_publico == body.lead_id)
        )

    payload_str = json.dumps(body.payload, ensure_ascii=False) if body.payload else None
    db.add(EventoSessao(
        lead_id=lead_pk,
        session_id=body.session_id,
        tipo=body.tipo,
        payload=payload_str,
    ))
    await db.commit()


@app.get("/descadastro/{lead_id_publico}", response_class=HTMLResponse)
async def descadastro(request: Request, lead_id_publico: str, db=Depends(get_db)):
    lead = await db.scalar(select(Lead).where(Lead.id_publico == lead_id_publico))
    if lead and not lead.descadastrado:
        lead.descadastrado = True
        await db.commit()
    return templates.TemplateResponse(request, "descadastro.html")


# ─── ZapSign ──────────────────────────────────────────────────────────────────

def _kit_campos(c: Cadastro) -> dict:
    """Monta os valores que substituem as variáveis do modelo (kit) na ZapSign.

    As chaves precisam ser idênticas às variáveis definidas no modelo da ZapSign.
    "X" marca caixas de seleção; "" deixa em branco.
    """
    endereco = c.logradouro or ""
    return {
        "NOME COMPLETO": c.nome_completo,
        "CPF": c.cpf,
        "RG": c.rg or "",
        "DATA NASCIMENTO": c.data_nascimento.strftime("%d/%m/%Y") if c.data_nascimento else "",
        "ESTADO CIVIL": c.estado_civil or "",
        "NACIONALIDADE": c.nacionalidade or "",
        "PROFISSAO": c.cargo or "",
        "TELEFONE": c.telefone or "",
        "EMAIL": c.email or "",
        "LOGRADOURO": endereco,
        "NUMERO": c.numero or "",
        "COMPLEMENTO": c.complemento or "",
        "BAIRRO": c.bairro or "",
        "CIDADE": c.cidade or "",
        "ESTADO": c.uf or "",
        "CEP": c.cep or "",
        "DATA": datetime.now().strftime("%d/%m/%Y"),
        # Fins específicos: sempre "Requerer benefícios, revisão e interpor recursos"
        "FIM REQUERER BENEFICIOS": "X",
        # Declaração de recebimento de benefício de outro regime
        "NAO RECEBE OUTRO REGIME": "" if c.recebe_outro_beneficio else "X",
        "RECEBE OUTRO REGIME": "X" if c.recebe_outro_beneficio else "",
    }


@app.post("/api/etapa2/{token}/zapsign")
@limiter.limit("30/hour")
async def api_etapa2_zapsign(request: Request, token: str, db=Depends(get_db)):
    cadastro = await db.scalar(select(Cadastro).where(Cadastro.etapa2_token == token))
    if not cadastro or cadastro.etapa2_concluida_em:
        raise HTTPException(status_code=404, detail="Link inválido ou já utilizado")
    try:
        if zapsign_usar_modelo():
            result = await zapsign_criar_modelo(
                cadastro.nome_completo, cadastro.email, _kit_campos(cadastro)
            )
        else:
            pdf_bytes = generate_procuration_pdf(cadastro.nome_completo, cadastro.cpf)
            result = await zapsign_criar(cadastro.nome_completo, cadastro.cpf, pdf_bytes)
        return result
    except Exception as e:
        import httpx as _httpx
        zap_detail = str(e)
        if isinstance(e, _httpx.HTTPStatusError):
            try:
                body = e.response.json()
                zap_detail = body.get("detail") or body.get("message") or e.response.text[:300]
            except Exception:
                zap_detail = e.response.text[:300]
            logger.error(
                "ZapSign HTTP %s — sandbox=%s template_id=%s — body: %s",
                e.response.status_code,
                os.environ.get("ZAPSIGN_SANDBOX", "true"),
                os.environ.get("ZAPSIGN_TEMPLATE_ID", "(vazio)"),
                zap_detail,
            )
        else:
            logger.error("ZapSign create error [%s]: %s", type(e).__name__, e)
        raise HTTPException(
            status_code=502,
            detail=f"Erro ao criar documento na ZapSign: {zap_detail}",
        )


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

    # Reconstrói o arquivo (CDR): remove conteúdo ativo de PDFs e re-encoda
    # imagens. O que for armazenado é o conteúdo "limpo", não o original.
    content = sanitize_upload(content)

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

AGENDA_DIAS_JANELA = 60  # agendamento permitido até N dias à frente
_AGENDA_CONFIG_DEFAULT = {"hora_inicio": "09:00", "hora_fim": "16:00", "intervalo_minutos": 60}


def _gerar_horarios(hora_inicio: str, hora_fim: str, intervalo_min: int) -> list[str]:
    inicio = datetime.strptime(hora_inicio, "%H:%M")
    fim = datetime.strptime(hora_fim, "%H:%M")
    slots, atual = [], inicio
    while atual <= fim:
        slots.append(atual.strftime("%H:%M"))
        atual += timedelta(minutes=intervalo_min)
    return slots


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

    # Verifica se o dia está bloqueado
    bloqueio = await db.scalar(
        select(BloqueioAgenda).where(BloqueioAgenda.data == dia.date())
    )
    if bloqueio:
        return {
            "data": data,
            "bloqueado": True,
            "motivo": bloqueio.motivo,
            "horarios": [],
        }

    # Carrega configuração (usa padrão se ainda não configurado)
    config = await db.scalar(select(ConfigAgenda).where(ConfigAgenda.id == 1))
    hora_inicio = config.hora_inicio if config else _AGENDA_CONFIG_DEFAULT["hora_inicio"]
    hora_fim = config.hora_fim if config else _AGENDA_CONFIG_DEFAULT["hora_fim"]
    intervalo = config.intervalo_minutos if config else _AGENDA_CONFIG_DEFAULT["intervalo_minutos"]

    horarios_slot = _gerar_horarios(hora_inicio, hora_fim, intervalo)

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
        "bloqueado": False,
        "horarios": [{"hora": h, "disponivel": h not in ocupados} for h in horarios_slot],
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
    rg: str = Form(...),
    data_nascimento: str = Form(...),
    estado_civil: str = Form(...),
    nacionalidade: str = Form(...),
    recebe_outro_beneficio: str = Form("false"),
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
    lead_id: Optional[str] = Form(None),
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
    outro_beneficio_bool = recebe_outro_beneficio.lower() in ("true", "1", "sim", "yes", "on")

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
            rg=rg,
            data_nascimento=data_nascimento,
            estado_civil=estado_civil,
            nacionalidade=nacionalidade,
            recebe_outro_beneficio=outro_beneficio_bool,
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

    bloqueio_dia = await db.scalar(
        select(BloqueioAgenda).where(BloqueioAgenda.data == dia.date())
    )
    if bloqueio_dia:
        raise HTTPException(status_code=400, detail="Esta data não tem atendimento disponível")

    cfg = await db.scalar(select(ConfigAgenda).where(ConfigAgenda.id == 1))
    slots_validos = _gerar_horarios(
        cfg.hora_inicio if cfg else _AGENDA_CONFIG_DEFAULT["hora_inicio"],
        cfg.hora_fim if cfg else _AGENDA_CONFIG_DEFAULT["hora_fim"],
        cfg.intervalo_minutos if cfg else _AGENDA_CONFIG_DEFAULT["intervalo_minutos"],
    )
    if agendamento_hora not in slots_validos:
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
        rg=data.rg,
        data_nascimento=datetime.strptime(data.data_nascimento, "%Y-%m-%d").date(),
        estado_civil=data.estado_civil,
        nacionalidade=data.nacionalidade,
        recebe_outro_beneficio=data.recebe_outro_beneficio,
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

    registrar_historico(
        db,
        cadastro_id=cadastro_id,
        tipo="cadastro_criado",
        descricao=(
            f"Cadastro criado pelo cliente — agendamento em "
            f"{slot.strftime('%d/%m/%Y às %H:%M')} ({data.modalidade_atendimento})"
        ),
    )

    await db.commit()

    # Vincula o lead ao cadastro se fornecido
    if lead_id:
        lead = await db.scalar(select(Lead).where(Lead.id_publico == lead_id))
        if lead and not lead.convertido_em:
            lead.cadastro_id = cadastro_id
            lead.convertido_em = datetime.now()
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

    status_anterior = cadastro.status
    cadastro.zapsign_doc_token = zapsign_doc_token.strip()
    cadastro.etapa2_concluida_em = datetime.now()
    cadastro.status = "em_andamento"
    cadastro.updated_at = datetime.now()

    registrar_historico(
        db,
        cadastro_id=cadastro.id,
        tipo="etapa2_concluida",
        descricao="Cliente enviou a documentação e assinou a procuração",
    )
    if status_anterior != "em_andamento":
        registrar_historico(
            db,
            cadastro_id=cadastro.id,
            tipo="status_alterado",
            descricao=(
                f"Status alterado automaticamente para \"Em andamento\""
                f" (após conclusão da Etapa 2)"
            ),
            valor_anterior=status_anterior,
            valor_novo="em_andamento",
        )
    await db.commit()

    return JSONResponse({"redirect": f"/obrigado?protocolo={cadastro.id:06d}&etapa=2"})


@app.post("/admin/cadastro/{cadastro_id}/liberar-etapa2")
async def admin_liberar_etapa2(
    request: Request,
    cadastro_id: int,
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    cadastro = await db.get(Cadastro, cadastro_id)
    if not cadastro:
        raise HTTPException(status_code=404, detail="Cadastro não encontrado")
    if cadastro.etapa2_concluida_em:
        raise HTTPException(status_code=409, detail="Etapa 2 já concluída para este cadastro")

    primeira_liberacao = cadastro.etapa2_liberada_em is None
    if not cadastro.etapa2_token:
        cadastro.etapa2_token = secrets.token_urlsafe(32)
    cadastro.etapa2_liberada_em = datetime.now()
    cadastro.updated_at = datetime.now()

    registrar_historico(
        db,
        cadastro_id=cadastro.id,
        tipo="etapa2_liberada",
        descricao=("Etapa 2 liberada e e-mail enviado ao cliente"
                   if primeira_liberacao
                   else "Etapa 2 reenviada (e-mail reenviado ao cliente)"),
        ator=current,
    )
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


# ─── Admin: configuração da agenda ────────────────────────────────────────────

@app.get("/admin/agenda", response_class=HTMLResponse)
async def admin_agenda_page(
    request: Request,
    msg: Optional[str] = None,
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    config = await db.scalar(select(ConfigAgenda).where(ConfigAgenda.id == 1))
    result = await db.execute(
        select(BloqueioAgenda).order_by(BloqueioAgenda.data.asc())
    )
    bloqueios = result.scalars().all()
    cfg_hi = config.hora_inicio if config else _AGENDA_CONFIG_DEFAULT["hora_inicio"]
    cfg_hf = config.hora_fim if config else _AGENDA_CONFIG_DEFAULT["hora_fim"]
    cfg_iv = config.intervalo_minutos if config else _AGENDA_CONFIG_DEFAULT["intervalo_minutos"]
    hoje = date.today()
    return templates.TemplateResponse(request, "admin/agenda.html", {
        "config": config,
        "bloqueios": bloqueios,
        "horarios_preview": _gerar_horarios(cfg_hi, cfg_hf, cfg_iv),
        "hoje": hoje.isoformat(),
        "hoje_date": hoje,
        "admin_email": current.email,
        "is_admin": current.papel == "admin",
        "current_user_id": current.id,
        "msg": msg,
    })


@app.post("/admin/api/agenda/config")
async def admin_update_agenda_config(
    request: Request,
    hora_inicio: str = Form(...),
    hora_fim: str = Form(...),
    intervalo_minutos: int = Form(...),
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    import re
    if not re.match(r"^\d{2}:\d{2}$", hora_inicio) or not re.match(r"^\d{2}:\d{2}$", hora_fim):
        return RedirectResponse("/admin/agenda?msg=Formato+de+hora+inv%C3%A1lido", status_code=303)
    if not (15 <= intervalo_minutos <= 480):
        return RedirectResponse("/admin/agenda?msg=Intervalo+deve+ser+entre+15+e+480+minutos", status_code=303)
    if datetime.strptime(hora_inicio, "%H:%M") >= datetime.strptime(hora_fim, "%H:%M"):
        return RedirectResponse("/admin/agenda?msg=Hora+in%C3%ADcio+deve+ser+anterior+%C3%A0+hora+fim", status_code=303)

    config = await db.scalar(select(ConfigAgenda).where(ConfigAgenda.id == 1))
    if not config:
        config = ConfigAgenda(id=1)
        db.add(config)
    config.hora_inicio = hora_inicio
    config.hora_fim = hora_fim
    config.intervalo_minutos = intervalo_minutos
    config.atualizado_em = datetime.now()
    config.atualizado_por = current.email
    await db.commit()
    return RedirectResponse("/admin/agenda?msg=Configura%C3%A7%C3%A3o+salva+com+sucesso", status_code=303)


@app.post("/admin/api/agenda/bloqueio")
async def admin_add_bloqueio(
    request: Request,
    data_bloqueio: str = Form(...),
    motivo: str = Form(""),
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    try:
        dia = date.fromisoformat(data_bloqueio)
    except ValueError:
        return RedirectResponse("/admin/agenda?msg=Data+inv%C3%A1lida", status_code=303)

    existing = await db.scalar(select(BloqueioAgenda).where(BloqueioAgenda.data == dia))
    if not existing:
        bloqueio = BloqueioAgenda(
            data=dia,
            motivo=motivo.strip() or None,
            criado_por=current.email,
        )
        db.add(bloqueio)
        await db.commit()
    return RedirectResponse("/admin/agenda?msg=Dia+bloqueado+com+sucesso", status_code=303)


@app.post("/admin/api/agenda/bloqueio/{data_str}/excluir")
async def admin_remove_bloqueio(
    data_str: str,
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    try:
        dia = date.fromisoformat(data_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida")

    bloqueio = await db.scalar(select(BloqueioAgenda).where(BloqueioAgenda.data == dia))
    if bloqueio:
        await db.delete(bloqueio)
        await db.commit()
    return RedirectResponse("/admin/agenda?msg=Bloqueio+removido", status_code=303)


@app.get("/admin/api/agenda/dia")
async def admin_agenda_dia(
    data: str,
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    try:
        dia = date.fromisoformat(data)
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida")

    bloqueio = await db.scalar(select(BloqueioAgenda).where(BloqueioAgenda.data == dia))
    config = await db.scalar(select(ConfigAgenda).where(ConfigAgenda.id == 1))
    cfg_hi = config.hora_inicio if config else _AGENDA_CONFIG_DEFAULT["hora_inicio"]
    cfg_hf = config.hora_fim if config else _AGENDA_CONFIG_DEFAULT["hora_fim"]
    cfg_iv = config.intervalo_minutos if config else _AGENDA_CONFIG_DEFAULT["intervalo_minutos"]

    dia_inicio = datetime(dia.year, dia.month, dia.day, 0, 0, 0)
    dia_fim = datetime(dia.year, dia.month, dia.day, 23, 59, 59)
    result = await db.execute(
        select(Cadastro)
        .where(Cadastro.agendamento >= dia_inicio, Cadastro.agendamento <= dia_fim)
        .order_by(Cadastro.agendamento.asc())
    )
    cadastros = result.scalars().all()

    slots = _gerar_horarios(cfg_hi, cfg_hf, cfg_iv)
    agendados = {c.agendamento.strftime("%H:%M"): c for c in cadastros if c.agendamento}

    horarios = []
    for slot in slots:
        c = agendados.get(slot)
        horarios.append({
            "horario": slot,
            "livre": c is None,
            "cadastro": {
                "id": c.id,
                "nome": c.nome_completo,
                "cpf": c.cpf,
                "telefone": c.telefone,
                "modalidade": c.modalidade_atendimento or "—",
                "status": c.status,
                "etapa2_concluida": c.etapa2_concluida_em is not None,
                "atendente_id": c.atendente_id,
                "atendente_nome": c.atendente.nome if c.atendente else None,
            } if c else None,
        })

    # Agendamentos fora dos slots padrão (ex: config mudou depois)
    fora_do_slot = [
        {
            "horario": c.agendamento.strftime("%H:%M"),
            "livre": False,
            "cadastro": {
                "id": c.id,
                "nome": c.nome_completo,
                "cpf": c.cpf,
                "telefone": c.telefone,
                "modalidade": c.modalidade_atendimento or "—",
                "status": c.status,
                "etapa2_concluida": c.etapa2_concluida_em is not None,
                "atendente_id": c.atendente_id,
                "atendente_nome": c.atendente.nome if c.atendente else None,
            },
        }
        for c in cadastros
        if c.agendamento and c.agendamento.strftime("%H:%M") not in slots
    ]

    res_j = await db.execute(
        select(AdminUsuario)
        .where(AdminUsuario.papel.in_(("admin", "juridico")))
        .order_by(AdminUsuario.nome)
    )
    juridicos = [
        {"id": j.id, "nome": j.nome, "papel": j.papel} for j in res_j.scalars().all()
    ]

    dias_semana = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    return {
        "data": data,
        "dia_semana": dias_semana[dia.weekday()],
        "fim_de_semana": dia.weekday() >= 5,
        "bloqueado": bloqueio is not None,
        "motivo_bloqueio": bloqueio.motivo if bloqueio else None,
        "total_slots": len(slots),
        "total_agendados": len(cadastros),
        "current_user_id": current.id,
        "current_papel": current.papel,
        "juridicos": juridicos,
        "horarios": horarios,
        "fora_do_slot": fora_do_slot,
    }


# ─── Admin: diagnóstico ZapSign ───────────────────────────────────────────────

@app.get("/admin/zapsign/modelos")
async def admin_zapsign_modelos(current=Depends(require_admin_role)):
    """Diagnóstico da configuração ZapSign (a API da ZapSign não expõe endpoint de listagem de modelos)."""
    template_id = os.environ.get("ZAPSIGN_TEMPLATE_ID", "")
    sandbox = os.environ.get("ZAPSIGN_SANDBOX", "true")
    token_configurado = bool(os.environ.get("ZAPSIGN_API_TOKEN", ""))

    return {
        "ZAPSIGN_API_TOKEN_configurado": token_configurado,
        "ZAPSIGN_TEMPLATE_ID": template_id or "(não definido)",
        "ZAPSIGN_SANDBOX": sandbox,
        "modo_ativo": "modelo DOCX via ZapSign" if template_id else "PDF gerado em código (fallback)",
        "observacao": (
            "A API da ZapSign não oferece endpoint para listar ou consultar modelos. "
            "Para encontrar o token do modelo, acesse app.zapsign.com.br → Modelos "
            "e copie o UUID da URL da página do modelo."
        ),
    }


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
    atendente: Optional[str] = None,
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
    if atendente == "meu":
        conditions.append(Cadastro.atendente_id == current.id)
    elif atendente and atendente.isdigit():
        conditions.append(Cadastro.atendente_id == int(atendente))

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

    juridicos = []
    if current.papel == "admin":
        res_j = await db.execute(
            select(AdminUsuario)
            .where(AdminUsuario.papel.in_(("admin", "juridico")))
            .order_by(AdminUsuario.nome)
        )
        juridicos = res_j.scalars().all()

    return templates.TemplateResponse(request, "admin/dashboard.html", {
        "cadastros": cadastros,
        "stats": stats,
        "page": page,
        "pages": pages,
        "filters": {"status": status or "", "filiado": filiado or "", "q": q or "", "atendente": atendente or ""},
        "admin_email": admin_email,
        "is_admin": current.papel == "admin",
        "juridicos": juridicos,
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

    juridicos = []
    if current.papel == "admin":
        res_j = await db.execute(
            select(AdminUsuario)
            .where(AdminUsuario.papel.in_(("admin", "juridico")))
            .order_by(AdminUsuario.nome)
        )
        juridicos = res_j.scalars().all()

    return templates.TemplateResponse(request, "admin/detalhe.html", {
        "cadastro": cadastro,
        "admin_email": current.email,
        "is_admin": current.papel == "admin",
        "status_opcoes": ["novo", "em_andamento", "concluido"],
        "juridicos": juridicos,
        "current_user_id": current.id,
        "current_papel": current.papel,
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
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    cadastro = await db.get(Cadastro, cadastro_id)
    if not cadastro:
        raise HTTPException(status_code=404)
    status_anterior = cadastro.status
    if status_anterior != payload.status:
        cadastro.status = payload.status
        cadastro.updated_at = datetime.now()
        registrar_historico(
            db,
            cadastro_id=cadastro.id,
            tipo="status_alterado",
            descricao=(
                f"Status alterado de \"{STATUS_LABELS.get(status_anterior, status_anterior)}\""
                f" para \"{STATUS_LABELS.get(payload.status, payload.status)}\""
            ),
            ator=current,
            valor_anterior=status_anterior,
            valor_novo=payload.status,
        )
    await db.commit()
    return {"ok": True, "status": payload.status}


@app.get("/admin/cadastro/{cadastro_id}/historico")
async def admin_get_historico(
    cadastro_id: int,
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    cadastro = await db.get(Cadastro, cadastro_id)
    if not cadastro:
        raise HTTPException(status_code=404, detail="Cadastro não encontrado")

    result = await db.execute(
        select(HistoricoCadastro)
        .where(HistoricoCadastro.cadastro_id == cadastro_id)
        .order_by(HistoricoCadastro.criado_em.desc())
    )
    eventos = result.scalars().all()
    return {
        "eventos": [
            {
                "id": e.id,
                "tipo": e.tipo,
                "descricao": e.descricao,
                "ator_nome": e.ator_nome,
                "ator_email": e.ator_email,
                "valor_anterior": e.valor_anterior,
                "valor_novo": e.valor_novo,
                "criado_em": e.criado_em.isoformat(),
                "criado_em_fmt": e.criado_em.strftime("%d/%m/%Y às %H:%M"),
            }
            for e in eventos
        ]
    }


@app.get("/admin/cadastro/{cadastro_id}/notas")
async def admin_list_notas(
    cadastro_id: int,
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    cadastro = await db.get(Cadastro, cadastro_id)
    if not cadastro:
        raise HTTPException(status_code=404, detail="Cadastro não encontrado")
    result = await db.execute(
        select(NotaCadastro)
        .where(NotaCadastro.cadastro_id == cadastro_id)
        .order_by(NotaCadastro.criado_em.desc())
    )
    notas = result.scalars().all()
    return {
        "notas": [
            {
                "id": n.id,
                "texto": n.texto,
                "autor_id": n.autor_id,
                "autor_nome": n.autor_nome,
                "autor_email": n.autor_email,
                "criado_em": n.criado_em.isoformat(),
                "criado_em_fmt": n.criado_em.strftime("%d/%m/%Y às %H:%M"),
                "pode_excluir": current.papel == "admin" or n.autor_id == current.id,
            }
            for n in notas
        ]
    }


@app.post("/admin/cadastro/{cadastro_id}/notas")
async def admin_add_nota(
    cadastro_id: int,
    payload: NotaUpdateIn,
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    cadastro = await db.get(Cadastro, cadastro_id)
    if not cadastro:
        raise HTTPException(status_code=404, detail="Cadastro não encontrado")
    texto = payload.nota.strip()
    if not texto:
        raise HTTPException(status_code=400, detail="A nota não pode ficar em branco")

    nota = NotaCadastro(
        cadastro_id=cadastro_id,
        autor_id=current.id,
        autor_email=current.email,
        autor_nome=current.nome,
        texto=texto,
    )
    db.add(nota)
    cadastro.updated_at = datetime.now()
    await db.commit()
    await db.refresh(nota)
    return {
        "ok": True,
        "nota": {
            "id": nota.id,
            "texto": nota.texto,
            "autor_id": nota.autor_id,
            "autor_nome": nota.autor_nome,
            "autor_email": nota.autor_email,
            "criado_em": nota.criado_em.isoformat(),
            "criado_em_fmt": nota.criado_em.strftime("%d/%m/%Y às %H:%M"),
            "pode_excluir": True,
        },
    }


@app.delete("/admin/cadastro/{cadastro_id}/notas/{nota_id}")
async def admin_delete_nota(
    cadastro_id: int,
    nota_id: int,
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    nota = await db.get(NotaCadastro, nota_id)
    if not nota or nota.cadastro_id != cadastro_id:
        raise HTTPException(status_code=404, detail="Nota não encontrada")
    if current.papel != "admin" and nota.autor_id != current.id:
        raise HTTPException(status_code=403, detail="Você só pode excluir suas próprias notas")
    await db.delete(nota)
    await db.commit()
    return {"ok": True}


# Endpoint legado mantido para compatibilidade — agora cria uma nota nova em vez de sobrescrever.
@app.post("/admin/cadastro/{cadastro_id}/nota")
async def admin_update_nota_legacy(
    cadastro_id: int,
    payload: NotaUpdateIn,
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    return await admin_add_nota(cadastro_id, payload, current, db)


@app.put("/admin/cadastro/{cadastro_id}/atendente")
async def admin_update_atendente(
    cadastro_id: int,
    payload: AtendenteUpdateIn,
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    cadastro = await db.get(Cadastro, cadastro_id)
    if not cadastro:
        raise HTTPException(status_code=404, detail="Cadastro não encontrado")

    novo_id = payload.atendente_id
    atendente_anterior_id = cadastro.atendente_id
    atendente_anterior_nome = cadastro.atendente.nome if cadastro.atendente else None

    if current.papel == "admin":
        if novo_id is not None:
            alvo = await db.get(AdminUsuario, novo_id)
            if not alvo or alvo.papel not in ("admin", "juridico"):
                raise HTTPException(status_code=400, detail="Atendente inválido")
        cadastro.atendente_id = novo_id
    elif current.papel == "juridico":
        if novo_id is None:
            if cadastro.atendente_id != current.id:
                raise HTTPException(status_code=403, detail="Você não pode liberar um caso que não é seu")
            cadastro.atendente_id = None
        elif novo_id == current.id:
            if cadastro.atendente_id is not None and cadastro.atendente_id != current.id:
                raise HTTPException(status_code=403, detail="Este caso já está atribuído a outro atendente")
            cadastro.atendente_id = current.id
        else:
            raise HTTPException(status_code=403, detail="Jurídico não pode redistribuir para outro atendente")
    else:
        raise HTTPException(status_code=403, detail="Acesso negado")

    cadastro.updated_at = datetime.now()

    if atendente_anterior_id != cadastro.atendente_id:
        if cadastro.atendente_id is None:
            registrar_historico(
                db,
                cadastro_id=cadastro.id,
                tipo="atendente_removido",
                descricao=f"Atendente removido (antes: {atendente_anterior_nome or '—'})",
                ator=current,
                valor_anterior=atendente_anterior_nome,
                valor_novo=None,
            )
        else:
            novo_nome = None
            if cadastro.atendente_id == current.id:
                novo_nome = current.nome
            else:
                alvo = await db.get(AdminUsuario, cadastro.atendente_id)
                novo_nome = alvo.nome if alvo else None
            registrar_historico(
                db,
                cadastro_id=cadastro.id,
                tipo="atendente_atribuido",
                descricao=(
                    f"Atendente atribuído: {novo_nome or '—'}"
                    + (f" (antes: {atendente_anterior_nome})" if atendente_anterior_nome else "")
                ),
                ator=current,
                valor_anterior=atendente_anterior_nome,
                valor_novo=novo_nome,
            )

    await db.commit()
    await db.refresh(cadastro)
    nome = cadastro.atendente.nome if cadastro.atendente else None
    return {"ok": True, "atendente_id": cadastro.atendente_id, "atendente_nome": nome}


@app.put("/admin/cadastro/{cadastro_id}/dados")
async def admin_update_dados(
    cadastro_id: int,
    payload: CadastroUpdateIn,
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    from datetime import date as _date
    cadastro = await db.get(Cadastro, cadastro_id)
    if not cadastro:
        raise HTTPException(status_code=404, detail="Cadastro não encontrado")

    # Verificar CPF duplicado se mudou
    if payload.cpf != cadastro.cpf:
        existing = await db.scalar(select(Cadastro).where(Cadastro.cpf == payload.cpf))
        if existing:
            raise HTTPException(status_code=400, detail="CPF já cadastrado em outro registro")

    cadastro.nome_completo = payload.nome_completo
    cadastro.cpf = payload.cpf
    cadastro.rg = payload.rg
    cadastro.data_nascimento = _date.fromisoformat(payload.data_nascimento)
    cadastro.estado_civil = payload.estado_civil
    cadastro.nacionalidade = payload.nacionalidade
    cadastro.telefone = payload.telefone
    cadastro.email = payload.email
    cadastro.hospital = payload.hospital
    cadastro.cargo = payload.cargo
    cadastro.tempo_servico = payload.tempo_servico
    cadastro.filiado = payload.filiado
    cadastro.recebe_outro_beneficio = payload.recebe_outro_beneficio
    cadastro.cep = payload.cep
    cadastro.logradouro = payload.logradouro
    cadastro.numero = payload.numero
    cadastro.complemento = payload.complemento
    cadastro.bairro = payload.bairro
    cadastro.cidade = payload.cidade
    cadastro.uf = payload.uf
    cadastro.updated_at = datetime.now()
    registrar_historico(
        db,
        cadastro_id=cadastro.id,
        tipo="dados_editados",
        descricao="Dados do cadastro editados",
        ator=current,
    )
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


@app.get("/admin/analytics", response_class=HTMLResponse)
async def admin_analytics(
    request: Request,
    dias: int = 30,
    current=Depends(get_current_admin_obj),
    db=Depends(get_db),
):
    dias = max(1, min(dias, 365))
    desde = datetime.now() - timedelta(days=dias)

    total_leads = await db.scalar(
        select(func.count(Lead.id)).where(Lead.criado_em >= desde)
    ) or 0
    leads_convertidos = await db.scalar(
        select(func.count(Lead.id)).where(
            Lead.criado_em >= desde, Lead.convertido_em.is_not(None)
        )
    ) or 0
    leads_abandonados = total_leads - leads_convertidos
    taxa_conversao = round((leads_convertidos / total_leads * 100) if total_leads else 0, 1)

    # Drop-off por seção (PostgreSQL JSON)
    secoes_result = await db.execute(
        text("""
            SELECT payload::json->>'secao' AS secao, COUNT(DISTINCT session_id) AS sessoes
            FROM eventos_sessao
            WHERE tipo = 'secao_vista' AND criado_em >= :desde AND payload IS NOT NULL
            GROUP BY 1 ORDER BY sessoes DESC
        """),
        {"desde": desde},
    )
    secoes_raw = secoes_result.all()
    max_sessoes = max((r.sessoes for r in secoes_raw), default=1)
    secoes_funil = [(r.secao, r.sessoes, int(r.sessoes / max_sessoes * 100)) for r in secoes_raw]

    # Lembretes
    lembretes_1 = await db.scalar(select(func.count(Lead.id)).where(Lead.lembrete_1_enviado_em.is_not(None))) or 0
    lembretes_1_conv = await db.scalar(
        select(func.count(Lead.id)).where(
            Lead.lembrete_1_enviado_em.is_not(None),
            Lead.convertido_em.is_not(None),
            Lead.convertido_em > Lead.lembrete_1_enviado_em,
        )
    ) or 0
    lembretes_2 = await db.scalar(select(func.count(Lead.id)).where(Lead.lembrete_2_enviado_em.is_not(None))) or 0
    lembretes_2_conv = await db.scalar(
        select(func.count(Lead.id)).where(
            Lead.lembrete_2_enviado_em.is_not(None),
            Lead.convertido_em.is_not(None),
            Lead.convertido_em > Lead.lembrete_2_enviado_em,
        )
    ) or 0
    descadastros = await db.scalar(select(func.count(Lead.id)).where(Lead.descadastrado == True)) or 0

    # Leads recentes não convertidos
    leads_recentes_result = await db.execute(
        select(Lead)
        .where(Lead.convertido_em.is_(None), Lead.descadastrado == False)
        .order_by(Lead.criado_em.desc())
        .limit(20)
    )
    leads_recentes = leads_recentes_result.scalars().all()

    # Dispositivos — leads capturados no período
    uas_leads = await db.execute(select(Lead.user_agent).where(Lead.criado_em >= desde))
    leads_disp = {"pc": 0, "celular": 0}
    for (ua,) in uas_leads.all():
        d = _detectar_dispositivo(ua)
        leads_disp["celular" if d == "celular" else "pc"] += 1

    # Dispositivos — cadastros concluídos no período
    uas_cad = await db.execute(select(Cadastro.user_agent).where(Cadastro.created_at >= desde))
    cad_disp = {"pc": 0, "celular": 0}
    for (ua,) in uas_cad.all():
        d = _detectar_dispositivo(ua)
        cad_disp["celular" if d == "celular" else "pc"] += 1

    return templates.TemplateResponse(request, "admin/analytics.html", {
        "total_leads": total_leads,
        "leads_convertidos": leads_convertidos,
        "leads_abandonados": leads_abandonados,
        "taxa_conversao": taxa_conversao,
        "secoes_funil": secoes_funil,
        "lembretes_1": lembretes_1,
        "lembretes_1_conv": lembretes_1_conv,
        "lembretes_2": lembretes_2,
        "lembretes_2_conv": lembretes_2_conv,
        "descadastros": descadastros,
        "leads_recentes": leads_recentes,
        "leads_disp": leads_disp,
        "cad_disp": cad_disp,
        "dias": dias,
        "admin_email": current.email,
        "is_admin": current.papel == "admin",
    })


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
        download_name = safe_download_name(doc.nome_arquivo, doc.caminho_arquivo)
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{download_name}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    file_path = safe_file_path(DOCS_DIR, str(doc.cadastro_id), doc.caminho_arquivo)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado no storage")
    return FileResponse(
        path=str(file_path),
        filename=safe_download_name(doc.nome_arquivo, doc.caminho_arquivo),
        media_type="application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
    )
