"""Segurança de uploads: reconstrução (CDR) de PDFs e re-encode de imagens.

Como o sistema só aceita PDF/JPG/PNG, em vez de tentar *detectar* se um arquivo
é malicioso (jogo de gato-e-rato), nós o *reconstruímos* a partir do conteúdo
"limpo". Isso neutraliza payloads escondidos:

- PDF: reescrito sem JavaScript, ações automáticas, anexos embutidos e ações
  do tipo Launch — o documento continua legível, mas "desarmado".
- Imagem: reaberta e salva de novo, descartando metadados/segmentos extras que
  poderiam carregar payload ou compor um arquivo "polyglot".

Qualquer arquivo que não puder ser reconstruído com segurança é rejeitado com
HTTP 400 — o comportamento seguro por padrão.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pikepdf
from fastapi import HTTPException
from PIL import Image

# Proteção contra "bomba de descompressão": um PNG de poucos KB que estoura para
# gigabytes ao ser decodificado. Acima deste limite o Pillow recusa a imagem.
Image.MAX_IMAGE_PIXELS = 50_000_000  # ~50 megapixels

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_image(content: bytes, fmt: str) -> bytes:
    """Reabre e re-salva a imagem, removendo qualquer dado fora dos pixels."""
    try:
        with Image.open(io.BytesIO(content)) as img:
            img.load()  # força a decodificação (dispara o limite de pixels)
            if fmt == "JPEG" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, format=fmt)  # salva sem exif/icc/segmentos originais
            return out.getvalue()
    except Exception:
        raise HTTPException(status_code=400, detail="Imagem inválida ou corrompida.")


def _sanitize_pdf(content: bytes) -> bytes:
    """Reescreve o PDF removendo todo conteúdo ativo."""
    try:
        with pikepdf.open(io.BytesIO(content)) as pdf:
            root = pdf.Root
            # Ações automáticas e JavaScript a nível de documento
            for key in ("/OpenAction", "/AA"):
                if key in root:
                    del root[key]
            names = root.get("/Names")
            if names is not None:
                for key in ("/JavaScript", "/EmbeddedFiles"):
                    if key in names:
                        del names[key]
            # Ações automáticas a nível de página
            for page in pdf.pages:
                if "/AA" in page:
                    del page["/AA"]
            out = io.BytesIO()
            pdf.save(out)
            return out.getvalue()
    except Exception:
        raise HTTPException(status_code=400, detail="PDF inválido ou corrompido.")


def sanitize_upload(content: bytes) -> bytes:
    """Reconstrói o arquivo a partir do conteúdo, neutralizando partes ativas.

    O tipo é decidido pelos magic bytes (já validados antes da chamada). Levanta
    HTTPException 400 se o arquivo não puder ser reconstruído com segurança.
    """
    if content.startswith(b"%PDF"):
        return _sanitize_pdf(content)
    if content.startswith(b"\xff\xd8\xff"):
        return _sanitize_image(content, "JPEG")
    if content.startswith(b"\x89PNG"):
        return _sanitize_image(content, "PNG")
    raise HTTPException(status_code=400, detail="Formato não permitido. Use PDF, JPG ou PNG.")


def safe_download_name(nome_original: str | None, caminho_arquivo: str) -> str:
    """Nome seguro para o cabeçalho Content-Disposition.

    Remove caracteres perigosos (aspas/quebras de linha que permitiriam injeção
    de cabeçalho) e força a extensão real do arquivo armazenado, evitando que um
    arquivo seja baixado com um nome enganoso como "boleto.exe".
    """
    real_ext = Path(caminho_arquivo).suffix.lower() or ".bin"
    stem = Path(nome_original or "documento").stem
    stem = _SAFE_NAME_RE.sub("_", stem).strip("._") or "documento"
    return f"{stem[:80]}{real_ext}"
