"""Генерация PDF из HTML с двумя движками.

Приоритет — WeasyPrint (лучшее качество, используется на боевом Linux-сервере,
ставится в Docker вместе с системными шрифтами). Если WeasyPrint недоступен
(типично для локальной Windows без GTK) — откат на xhtml2pdf (чистый Python).

Оба движка используют одно семейство шрифтов 'DejaVu' в CSS:
на Linux его находит WeasyPrint (пакет fonts-dejavu), на Windows под этим именем
регистрируется системный Arial для xhtml2pdf — так один CSS работает везде.
"""
import io
import logging
import os

logger = logging.getLogger(__name__)

_WEASYPRINT_HTML = None  # None = ещё не проверяли; False = недоступен; иначе класс HTML
_FONTS_REGISTERED = False

# Кандидаты Cyrillic-шрифтов (regular, bold) для встраивания в xhtml2pdf
_FONT_CANDIDATES = [
    (r"C:/Windows/Fonts/arial.ttf", r"C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
]


def _weasyprint_html_class():
    """Ленивая проверка WeasyPrint (один раз за процесс, чтобы не шуметь предупреждениями)."""
    global _WEASYPRINT_HTML
    if _WEASYPRINT_HTML is None:
        try:
            from weasyprint import HTML

            _WEASYPRINT_HTML = HTML
        except Exception:  # noqa: BLE001 — нет GTK/Pango (типично для Windows)
            _WEASYPRINT_HTML = False
            logger.info("WeasyPrint недоступен, PDF будет генерироваться через xhtml2pdf")
    return _WEASYPRINT_HTML


def render_pdf(html_string, base_url=None):
    """HTML-строка → bytes PDF. Возвращает (pdf_bytes, engine_name)."""
    html_class = _weasyprint_html_class()
    if html_class:
        return html_class(string=html_string, base_url=base_url).write_pdf(), "weasyprint"
    return _render_with_xhtml2pdf(html_string), "xhtml2pdf"


def _find_font_pair():
    for regular, bold in _FONT_CANDIDATES:
        if os.path.exists(regular):
            return regular, bold if os.path.exists(bold) else regular
    return None, None


def _register_fonts_for_xhtml2pdf():
    """Регистрирует Cyrillic-TTF под именем 'DejaVu' в reportlab и таблице xhtml2pdf.

    Так CSS `font-family: 'DejaVu'` встраивает TTF (кириллица рисуется и извлекается),
    минуя проблемный на Windows @font-face с временными файлами.
    """
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    _FONTS_REGISTERED = True

    regular, bold = _find_font_pair()
    if not regular:
        logger.warning("Не найден кириллический TTF для xhtml2pdf — кириллица может не отобразиться")
        return

    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from xhtml2pdf.default import DEFAULT_FONT

    pdfmetrics.registerFont(TTFont("DejaVu", regular))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", bold))
    addMapping("DejaVu", 0, 0, "DejaVu")
    addMapping("DejaVu", 1, 0, "DejaVu-Bold")
    addMapping("DejaVu", 0, 1, "DejaVu")
    addMapping("DejaVu", 1, 1, "DejaVu-Bold")
    DEFAULT_FONT["dejavu"] = "DejaVu"


def _render_with_xhtml2pdf(html_string):
    from xhtml2pdf import pisa

    _register_fonts_for_xhtml2pdf()
    out = io.BytesIO()
    pisa.CreatePDF(src=html_string, dest=out, encoding="utf-8")
    return out.getvalue()
