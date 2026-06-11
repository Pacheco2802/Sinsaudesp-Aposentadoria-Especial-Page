import html
from datetime import datetime
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

VERDE = colors.HexColor("#2E7D32")
AMARELO = colors.HexColor("#FFC107")

PROCURACAO_TEXTO = """
PROCURAÇÃO AD JUDICIA ET EXTRA

Pelo presente instrumento particular de procuração, eu, abaixo identificado(a) e qualificado(a), \
nomeio e constituo como meu(minha) procurador(a) os advogados do Departamento Jurídico do SinSaúdeSP \
– Sindicato dos Trabalhadores da Saúde de São Paulo, com poderes para o foro em geral, conforme \
artigo 105 do Código de Processo Civil, podendo propor ações, contestar, recorrer, transigir, \
desistir, dar e receber quitação, substabelecer com ou sem reservas, e praticar todos os atos \
necessários ao bom e fiel cumprimento deste mandato, especificamente para:

1. Requerer, instruir e acompanhar o processo de concessão de Aposentadoria Especial por exposição \
habitual e permanente a agentes biológicos nocivos à saúde, nos termos do art. 57 da Lei nº 8.213/1991;

2. Apresentar documentos, requerimentos e recursos administrativos perante o INSS;

3. Representar o outorgante em processos judiciais perante a Justiça Federal e Turmas Recursais;

4. Praticar todos os demais atos necessários ao fiel cumprimento deste mandato.

5. Representar o outorgante em audiências, perícias e demais atos processuais.

Esta procuração é válida até a conclusão definitiva do processo administrativo ou judicial referente \
à aposentadoria especial, podendo ser revogada a qualquer tempo mediante comunicação por escrito.

O presente mandato é outorgado de forma gratuita, sem qualquer ônus para o outorgante, sendo o \
serviço prestado pelo sindicato como benefício aos seus filiados e representados.
"""


def generate_procuration_pdf(nome: str, cpf: str, ip: str = "") -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=2.5 * cm,
        leftMargin=2.5 * cm,
        topMargin=2.5 * cm,
        bottomMargin=2.5 * cm,
    )

    styles = getSampleStyleSheet()

    style_title = ParagraphStyle(
        "Title",
        parent=styles["Normal"],
        fontSize=16,
        fontName="Helvetica-Bold",
        textColor=VERDE,
        alignment=TA_CENTER,
        spaceAfter=4,
    )
    style_subtitle = ParagraphStyle(
        "Subtitle",
        parent=styles["Normal"],
        fontSize=11,
        fontName="Helvetica",
        textColor=colors.grey,
        alignment=TA_CENTER,
        spaceAfter=20,
    )
    style_body = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontSize=10,
        fontName="Helvetica",
        leading=14,
        alignment=TA_JUSTIFY,
        spaceAfter=10,
    )
    style_label = ParagraphStyle(
        "Label",
        parent=styles["Normal"],
        fontSize=10,
        fontName="Helvetica-Bold",
        alignment=TA_LEFT,
        spaceAfter=4,
    )
    style_footer = ParagraphStyle(
        "Footer",
        parent=styles["Normal"],
        fontSize=8,
        fontName="Helvetica",
        textColor=colors.grey,
        alignment=TA_CENTER,
    )

    agora = datetime.now()
    data_str = agora.strftime("%d/%m/%Y às %H:%M")

    story = []

    story.append(Paragraph("SinSaúdeSP", style_title))
    story.append(Paragraph("Sindicato dos Trabalhadores da Saúde de São Paulo", style_subtitle))
    story.append(Spacer(1, 0.3 * cm))

    for linha in PROCURACAO_TEXTO.strip().split("\n"):
        linha = linha.strip()
        if not linha:
            story.append(Spacer(1, 0.2 * cm))
            continue
        story.append(Paragraph(html.escape(linha), style_body))

    story.append(Spacer(1, 0.8 * cm))
    story.append(Paragraph("IDENTIFICAÇÃO DO OUTORGANTE", style_label))
    story.append(Paragraph(f"<b>Nome completo:</b> {html.escape(nome)}", style_body))
    story.append(Paragraph(f"<b>CPF:</b> {html.escape(cpf)}", style_body))
    story.append(Paragraph(f"<b>Data da outorga:</b> {data_str}", style_body))

    story.append(Spacer(1, 1.5 * cm))
    story.append(Paragraph("_" * 60, style_body))
    story.append(Paragraph(html.escape(nome), style_body))
    story.append(Paragraph(f"CPF: {html.escape(cpf)}", style_body))

    story.append(Spacer(1, 1 * cm))
    footer_parts = [f"Documento gerado em {data_str}"]
    if ip:
        footer_parts.append(f"IP: {html.escape(ip)}")
    story.append(Paragraph(" | ".join(footer_parts), style_footer))

    doc.build(story)
    return buffer.getvalue()
