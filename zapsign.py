import asyncio
import base64
import logging
import os
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

ZAPSIGN_API_TOKEN = os.environ.get("ZAPSIGN_API_TOKEN", "")
ZAPSIGN_SANDBOX = os.environ.get("ZAPSIGN_SANDBOX", "true").lower() == "true"
ZAPSIGN_FOLDER_PATH = os.environ.get(
    "ZAPSIGN_FOLDER_PATH", "/Procurações aposentadoria especial pelo site"
)
# ID do modelo DOCX criado na plataforma da ZapSign (kit completo). Se vazio,
# usa a procuração simples gerada em código (fallback).
ZAPSIGN_TEMPLATE_ID = os.environ.get("ZAPSIGN_TEMPLATE_ID", "")
BASE_URL = "https://api.zapsign.com.br/api/v1"


def usar_modelo() -> bool:
    return bool(ZAPSIGN_TEMPLATE_ID)


async def _post_with_retry(url: str, payload: dict) -> dict:
    headers = {"Authorization": f"Bearer {ZAPSIGN_API_TOKEN}", "Content-Type": "application/json"}
    last_error: Exception | None = None

    for attempt, delay in enumerate([0, 1, 2]):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
            last_error = e
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                raise
            logger.warning("ZapSign attempt %d failed: %s", attempt + 1, e)

    raise RuntimeError(f"ZapSign request failed after retries: {last_error}")


async def criar_documento(nome: str, cpf: str, pdf_bytes: bytes) -> dict:
    if not ZAPSIGN_API_TOKEN:
        raise RuntimeError("ZAPSIGN_API_TOKEN not configured")

    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    data_hoje = datetime.now().strftime("%Y%m%d")
    cpf_digits = cpf.replace(".", "").replace("-", "")

    payload = {
        "sandbox": ZAPSIGN_SANDBOX,
        "name": f"Procuracao_{cpf_digits}_{data_hoje}",
        "base64_pdf": b64,
        "lang": "pt-br",
        "folder_path": ZAPSIGN_FOLDER_PATH,
        "signers": [
            {
                "name": nome,
                "send_automatic_email": False,
            }
        ],
    }

    data = await _post_with_retry(f"{BASE_URL}/docs/", payload)

    doc_token = data.get("token", "")
    signers = data.get("signers", [])
    if not signers:
        raise RuntimeError("ZapSign returned no signers in response")

    signer_token = signers[0].get("token", "")
    return {"doc_token": doc_token, "signer_token": signer_token}


async def criar_via_modelo(signer_name: str, signer_email: str, campos: dict) -> dict:
    """Cria um documento a partir do modelo DOCX (kit) cadastrado na ZapSign.

    `campos` é um dict {nome_da_variavel: valor}. As chaves devem corresponder
    exatamente às variáveis definidas no modelo da ZapSign (sem as chaves {{ }}).
    """
    if not ZAPSIGN_API_TOKEN:
        raise RuntimeError("ZAPSIGN_API_TOKEN not configured")
    if not ZAPSIGN_TEMPLATE_ID:
        raise RuntimeError("ZAPSIGN_TEMPLATE_ID not configured")

    payload = {
        "sandbox": ZAPSIGN_SANDBOX,
        "template_id": ZAPSIGN_TEMPLATE_ID,
        "signer_name": signer_name,
        "folder_path": ZAPSIGN_FOLDER_PATH,
        "data": [{"de": f"{{{{{k}}}}}", "para": ("" if v is None else str(v))} for k, v in campos.items()],
    }
    if signer_email:
        payload["signer_email"] = signer_email

    data = await _post_with_retry(f"{BASE_URL}/models/create-doc/", payload)

    doc_token = data.get("token", "")
    signers = data.get("signers", [])
    if not signers:
        raise RuntimeError("ZapSign returned no signers in response")
    signer_token = signers[0].get("token", "")
    return {"doc_token": doc_token, "signer_token": signer_token}


async def consultar_documento(doc_token: str) -> dict:
    if not ZAPSIGN_API_TOKEN:
        raise RuntimeError("ZAPSIGN_API_TOKEN not configured")

    headers = {"Authorization": f"Bearer {ZAPSIGN_API_TOKEN}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{BASE_URL}/docs/{doc_token}/", headers=headers)
        resp.raise_for_status()
        return resp.json()


async def listar_modelos() -> list[dict]:
    """Retorna todos os modelos (templates DOCX) da conta ZapSign."""
    if not ZAPSIGN_API_TOKEN:
        raise RuntimeError("ZAPSIGN_API_TOKEN not configured")

    headers = {"Authorization": f"Bearer {ZAPSIGN_API_TOKEN}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{BASE_URL}/models/", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        # A API retorna {"results": [...]} ou diretamente uma lista
        if isinstance(data, dict):
            return data.get("results", [])
        return data
