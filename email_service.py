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


def smtp_status() -> str:
    """Resumo de quais variáveis SMTP estão presentes (sem expor valores)."""
    return (
        f"SMTP_HOST={'OK' if SMTP_HOST else 'FALTANDO'} | "
        f"SMTP_PORT={SMTP_PORT} | "
        f"SMTP_USER={'OK' if SMTP_USER else 'FALTANDO'} | "
        f"SMTP_PASSWORD={'OK' if SMTP_PASSWORD else 'FALTANDO'} | "
        f"ADMIN_EMAIL={'OK' if ADMIN_EMAIL else 'FALTANDO'}"
    )


def _mask_cpf(cpf: str) -> str:
    digits = cpf.replace(".", "").replace("-", "")
    if len(digits) == 11:
        return f"***.{digits[3:6]}.{digits[6:9]}-**"
    return "***.***.***-**"


async def _send(to: str, subject: str, html_body: str, text_body: str = "") -> None:
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, to]):
        raise RuntimeError(
            "SMTP não configurado (verifique SMTP_HOST, SMTP_USER e SMTP_PASSWORD nas variáveis)"
        )

    message = MIMEMultipart("alternative")
    message["From"] = f"SinSaúdeSP <{SMTP_USER}>"
    message["To"] = to
    message["Subject"] = subject
    message["Reply-To"] = SMTP_USER
    # Versão texto puro primeiro (reduz pontuação de spam), HTML por último (preferida)
    if text_body:
        message.attach(MIMEText(text_body, "plain", "utf-8"))
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


BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")


async def send_confirmation_email(
    to_email: str, nome: str, protocolo: int,
    modalidade: str = "", agendamento: str = "",
) -> None:
    subject = "Cadastro recebido e atendimento agendado — SinSaúdeSP"
    modalidade_str = "Online (via Microsoft Teams)" if modalidade == "online" else "Presencial"
    extra_online = (
        "<p>Você receberá o link da reunião do Teams no seu e-mail ou WhatsApp antes do horário marcado.</p>"
        if modalidade == "online"
        else "<p>Nossa equipe confirmará o endereço do atendimento pelo WhatsApp ou e-mail cadastrado.</p>"
    )
    body = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#2E7D32;">Cadastro recebido com sucesso!</h2>
      <p>Olá, <strong>{nome}</strong>.</p>
      <p>Recebemos seu cadastro para análise de <strong>Aposentadoria Especial por Perigo Biológico</strong>.</p>
      <p><strong>Protocolo:</strong> #{protocolo:06d}</p>
      <p><strong>Atendimento agendado:</strong> {agendamento}<br>
      <strong>Modalidade:</strong> {modalidade_str}</p>
      {extra_online}
      <p>Se precisar remarcar, basta responder este e-mail ou falar com o sindicato.</p>
      <hr style="border:1px solid #eee;margin:20px 0;">
      <p style="font-size:12px;color:#666;">
        SinSaúdeSP — Sindicato dos Trabalhadores da Saúde de São Paulo
      </p>
    </div>
    </body></html>
    """
    text = (
        f"Olá, {nome}.\n\n"
        f"Recebemos seu cadastro para análise de Aposentadoria Especial por Perigo Biológico.\n\n"
        f"Protocolo: #{protocolo:06d}\n"
        f"Atendimento agendado: {agendamento}\n"
        f"Modalidade: {modalidade_str}\n\n"
        f"Se precisar remarcar, basta responder este e-mail ou falar com o sindicato.\n\n"
        f"SinSaúdeSP — Sindicato dos Trabalhadores da Saúde de São Paulo"
    )
    try:
        await _send(to_email, subject, body, text)
    except Exception as e:
        logger.error("Failed to send confirmation email to %s: %s", to_email, e)


async def send_admin_notification(
    nome: str, cpf: str, hospital: str, cargo: str, filiado: bool, cadastro_id: int,
    modalidade: str = "", agendamento: str = "",
) -> None:
    if not ADMIN_EMAIL:
        return
    cpf_masked = _mask_cpf(cpf)
    filiado_str = "Sim" if filiado else "Não"
    modalidade_str = "Online (Teams)" if modalidade == "online" else "Presencial"
    subject = f"Novo cadastro + agendamento — Aposentadoria Especial: {nome}"
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
        <tr><td style="padding:8px;border-bottom:1px solid #eee;"><strong>Atendimento</strong></td><td style="padding:8px;border-bottom:1px solid #eee;">{agendamento} — {modalidade_str}</td></tr>
        <tr><td style="padding:8px;"><strong>Protocolo</strong></td><td style="padding:8px;">#{cadastro_id:06d}</td></tr>
      </table>
      <p><a href="{BASE_URL}/admin/cadastro/{cadastro_id}" style="background:#2E7D32;color:white;padding:10px 20px;text-decoration:none;border-radius:5px;">Ver no painel</a></p>
    </div>
    </body></html>
    """
    text = (
        f"Novo cadastro recebido\n\n"
        f"Nome: {nome}\n"
        f"CPF: {cpf_masked}\n"
        f"Hospital/Empresa: {hospital}\n"
        f"Cargo: {cargo}\n"
        f"Filiado: {filiado_str}\n"
        f"Atendimento: {agendamento} — {modalidade_str}\n"
        f"Protocolo: #{cadastro_id:06d}\n\n"
        f"Ver no painel: {BASE_URL}/admin/cadastro/{cadastro_id}"
    )
    try:
        await _send(ADMIN_EMAIL, subject, body, text)
    except Exception as e:
        logger.error("Failed to send admin notification for cadastro %s: %s", cadastro_id, e)


async def send_lembrete_email(to_email: str, lead_id_publico: str, numero: int) -> bool:
    descadastro_link = f"{BASE_URL}/descadastro/{lead_id_publico}"
    cadastro_link = f"{BASE_URL}/cadastro"

    if numero == 1:
        subject = "Você esqueceu — análise gratuita de aposentadoria especial"
        corpo_principal = (
            "<p>Você começou a solicitar sua análise gratuita de <strong>Aposentadoria Especial por "
            "Perigo Biológico</strong> mas não concluiu o cadastro.</p>"
            "<p>Trabalhadores da saúde têm direito a se aposentar com apenas 25 anos de contribuição. "
            "Nossa equipe pode verificar o seu caso gratuitamente — sem compromisso.</p>"
        )
        texto_principal = (
            "Você começou a solicitar sua análise gratuita de Aposentadoria Especial por "
            "Perigo Biológico mas não concluiu o cadastro.\n\n"
            "Trabalhadores da saúde têm direito a se aposentar com apenas 25 anos de contribuição. "
            "Nossa equipe pode verificar o seu caso gratuitamente, sem compromisso."
        )
    else:
        subject = "Última mensagem — análise gratuita ainda disponível"
        corpo_principal = (
            "<p>Esta é nossa última mensagem sobre sua análise gratuita de "
            "<strong>Aposentadoria Especial por Perigo Biológico</strong>.</p>"
            "<p>Se tiver interesse, ainda é possível agendar seu atendimento. "
            "Após isso, não enviaremos mais lembretes.</p>"
        )
        texto_principal = (
            "Esta é nossa última mensagem sobre sua análise gratuita de Aposentadoria Especial por "
            "Perigo Biológico.\n\n"
            "Se tiver interesse, ainda é possível agendar seu atendimento. "
            "Após isso, não enviaremos mais lembretes."
        )

    body = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#2E7D32;">SinSaúdeSP — Aposentadoria Especial</h2>
      {corpo_principal}
      <p style="margin:28px 0;">
        <a href="{cadastro_link}" style="background:#2E7D32;color:white;padding:14px 28px;text-decoration:none;border-radius:8px;font-weight:bold;">
          Concluir meu cadastro
        </a>
      </p>
      <hr style="border:1px solid #eee;margin:24px 0;">
      <p style="font-size:11px;color:#999;">
        SinSaúdeSP — Sindicato dos Trabalhadores da Saúde de São Paulo<br>
        <a href="{descadastro_link}" style="color:#999;">Não quero mais receber lembretes</a>
      </p>
    </div>
    </body></html>
    """
    text = (
        f"{texto_principal}\n\n"
        f"Concluir meu cadastro: {cadastro_link}\n\n"
        f"SinSaúdeSP — Sindicato dos Trabalhadores da Saúde de São Paulo\n"
        f"Não quero mais receber lembretes: {descadastro_link}"
    )
    try:
        await _send(to_email, subject, body, text)
        return True
    except Exception as e:
        logger.error("Failed to send lembrete %d to %s: %s", numero, to_email, e)
        return False


async def send_etapa2_email(to_email: str, nome: str, link: str) -> bool:
    subject = "Próximo passo: envie seus documentos e assine a procuração — SinSaúdeSP"
    body = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#2E7D32;">Vamos dar andamento ao seu processo!</h2>
      <p>Olá, <strong>{nome}</strong>.</p>
      <p>Conforme conversado no seu atendimento, chegou a hora de enviar a documentação completa
      e assinar a procuração digital para darmos andamento ao seu processo de
      <strong>Aposentadoria Especial</strong>.</p>
      <p style="margin:24px 0;">
        <a href="{link}" style="background:#2E7D32;color:white;padding:14px 28px;text-decoration:none;border-radius:8px;font-weight:bold;">
          Enviar documentos e assinar
        </a>
      </p>
      <p style="font-size:13px;color:#666;">Este link é pessoal e exclusivo seu. Não compartilhe com outras pessoas.</p>
      <hr style="border:1px solid #eee;margin:20px 0;">
      <p style="font-size:12px;color:#666;">
        SinSaúdeSP — Sindicato dos Trabalhadores da Saúde de São Paulo
      </p>
    </div>
    </body></html>
    """
    text = (
        f"Olá, {nome}.\n\n"
        f"Conforme conversado no seu atendimento, chegou a hora de enviar a documentação completa "
        f"e assinar a procuração digital para darmos andamento ao seu processo de Aposentadoria Especial.\n\n"
        f"Acesse o link abaixo para enviar os documentos e assinar:\n{link}\n\n"
        f"Este link é pessoal e exclusivo seu. Não compartilhe com outras pessoas.\n\n"
        f"SinSaúdeSP — Sindicato dos Trabalhadores da Saúde de São Paulo"
    )
    try:
        await _send(to_email, subject, body, text)
        return True
    except Exception as e:
        logger.error("Failed to send etapa2 email to %s: %s", to_email, e)
        return False
