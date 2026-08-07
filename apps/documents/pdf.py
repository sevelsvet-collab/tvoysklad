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
    """HTML-строка → bytes PDF. Возвращает (pdf_bytes, engine_name).

    Приоритет движков:
    1. WeasyPrint — боевой Linux (Docker), идеальный CSS-рендер таблиц.
    2. Playwright (headless Chromium) — для Windows-разработки, где WeasyPrint
       без GTK недоступен. Рендерит как настоящий браузер: сложные таблицы
       ТОРГ-12/УПД выходят ровно.
    3. xhtml2pdf — последний откат (чистый Python), но плохо считает широкие
       таблицы с большим числом узких колонок.
    """
    html_class = _weasyprint_html_class()
    if html_class:
        return html_class(string=html_string, base_url=base_url).write_pdf(), "weasyprint"
    pdf = _render_with_playwright(html_string)
    if pdf is not None:
        return pdf, "playwright"
    return _render_with_xhtml2pdf(html_string), "xhtml2pdf"


_PLAYWRIGHT_OK = None  # None = не проверяли; False = недоступен


def _render_with_playwright(html_string):
    """HTML → PDF через headless Chromium. None, если Playwright не установлен/не запустился.

    Картинки подписи/печати передаются как data: URI, поэтому base_url не нужен.
    @page-правила (A4 landscape и т.п.) применяются через prefer_css_page_size.
    """
    global _PLAYWRIGHT_OK
    if _PLAYWRIGHT_OK is False:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _PLAYWRIGHT_OK = False
        logger.info("Playwright не установлен — PDF будет через xhtml2pdf")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(html_string, wait_until="load")
                pdf = page.pdf(prefer_css_page_size=True, print_background=True)
            finally:
                browser.close()
        _PLAYWRIGHT_OK = True
        return pdf
    except Exception:  # noqa: BLE001 — не удалось запустить браузер
        logger.exception("Playwright не смог отрендерить PDF")
        _PLAYWRIGHT_OK = False
        return None


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
