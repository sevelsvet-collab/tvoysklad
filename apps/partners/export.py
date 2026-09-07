"""Выгрузка договора в PDF и Word (.docx)."""
import io

from django.template.loader import render_to_string

from apps.documents.pdf import render_pdf

from .contracts import build_contract_text


def contract_filename(contract, ext):
    number = (contract.number or "б-н").replace("/", "-")
    return f"Договор_{number}.{ext}"


def contract_pdf(contract, request=None):
    """PDF договора (тем же движком, что счета и накладные)."""
    html = render_to_string(
        "partners/print/contract.html",
        {"contract": contract, "body": build_contract_text(contract)},
        request=request,
    )
    base_url = request.build_absolute_uri("/") if request else None
    pdf, _engine = render_pdf(html, base_url=base_url)
    return pdf


def contract_docx(contract):
    """Word-версия договора — текст абзацами, моноширинные блоки реквизитов."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    body = build_contract_text(contract)
    document = Document()

    style = document.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(11)

    for raw_line in body.split("\n"):
        line = raw_line.rstrip()
        paragraph = document.add_paragraph()
        run = paragraph.add_run(line)
        stripped = line.strip()
        # Заголовок договора и названия разделов — жирным
        is_heading = stripped.startswith("ДОГОВОР") or (
            stripped[:2].rstrip(".").isdigit() and stripped.isupper()
        )
        if is_heading:
            run.bold = True
        if stripped.startswith("ДОГОВОР"):
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_after = Pt(2)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
