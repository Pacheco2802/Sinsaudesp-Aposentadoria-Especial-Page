import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

logger = logging.getLogger(__name__)

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")


def _mask_cpf(cpf: str) -> str:
    digits = cpf.replace(".", "").replace("-", "")
    if len(digits) == 11:
        return f"***.{digits[3:6]}.{digits[6:9]}-**"
    return "***.***.***-**"


async def _send(to: str, subject: str, html_body: str) -> None:
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, to]):
        logger.warning("Email not configured or missing recipient, skipping send")
        return

    message = MIMEMultipart("alternative")
    message["From"] = f"SinSaúdeSP <{SMTP_USER}>"
    message["To"] = to
    message["Subject"] = subject
    message.attach(MIMEText(html_body, "html", "utf-8"))

    kwargs: dict = {
        "hostname": SMTP_HOST,
        "port": SMTP_PORT,
        "username": SMTP_USER,
        "password": SMTP_PASSWORD,
    }
    if SMTP_PORT == 465:
        kwargs["use_tls"] = True
    else:
        kwargs["start_tls"] = True

    await aiosmtplib.send(message, **kwargs)


async def send_confirmation_email(to_email: str, nome: str, protocolo: int) -> None:
    subject = "Seu cadastro foi recebido — SinSaúdeSP"
    body = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#2E7D32;">Cadastro recebido com sucesso!</h2>
      <p>Olá, <strong>{nome}</strong>.</p>
      <p>Recebemos seu cadastro para análise de <strong>Aposentadoria Especial por Perigo Biológico</strong>.</p>
      <p><strong>Protocolo:</strong> #{protocolo:06d}</p>
      <p>Um advogado especializado do SinSaúdeSP entrará em contato em até 48 horas pelo WhatsApp ou e-mail cadastrado.</p>
      <hr style="border:1px solid #eee;margin:20px 0;">
      <p style="font-size:12px;color:#666;">
        SinSaúdeSP — Sindicato dos Trabalhadores da Saúde de São Paulo<br>
        Serviço gratuito para filiados.
      </p>
    </div>
    </body></html>
    """
    try:
        await _send(to_email, subject, body)
    except Exception as e:
        logger.error("Failed to send confirmation email to %s: %s", to_email, e)


async def send_admin_notification(
    nome: str, cpf: str, hospital: str, cargo: str, filiado: bool, cadastro_id: int
) -> None:
    if not ADMIN_EMAIL:
        return
    cpf_masked = _mask_cpf(cpf)
    filiado_str = "Sim" if filiado else "Não"
    subject = f"Novo cadastro — Aposentadoria Especial: {nome}"
    body = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#2E7D32;">Novo cadastro recebido</h2>
      <table style="width:100%;border-collapse:collapse;">
        <tr><td style="padding:8px;border-bottom:1px solid #eee;"><strong>Nome</strong></td><td style="padding:8px;border-bottom:1px solid #eee;">{nome}</td></tr>
        <tr><td style="padding:8px;border-bottom:1px solid #eee;"><strong>CPF</strong></td><td style="padding:8px;border-bottom:1px solid #eee;">{cpf_masked}</td></tr>
        <tr><td style="padding:8px;border-bottom:1px solid #eee;"><strong>Hospital/Empresa</strong></td><td style="padding:8px;border-bottom:1px solid #eee;">{hospital}</td></tr>
        <tr><td style="padding:8px;border-bottom:1px solid #eee;"><strong>Cargo</strong></td><td style="padding:8px;border-bottom:1px solid #eee;">{cargo}</td></tr>
        <tr><td style="padding:8px;border-bottom:1px solid #eee;"><strong>Filiado</strong></td><td style="padding:8px;border-bottom:1px solid #eee;">{filiado_str}</td></tr>
        <tr><td style="padding:8px;"><strong>Protocolo</strong></td><td style="padding:8px;">#{cadastro_id:06d}</td></tr>
      </table>
      <p><a href="/admin/cadastro/{cadastro_id}" style="background:#2E7D32;color:white;padding:10px 20px;text-decoration:none;border-radius:5px;">Ver no painel</a></p>
    </div>
    </body></html>
    """
    try:
        await _send(ADMIN_EMAIL, subject, body)
    except Exception as e:
        logger.error("Failed to send admin notification for cadastro %s: %s", cadastro_id, e)
