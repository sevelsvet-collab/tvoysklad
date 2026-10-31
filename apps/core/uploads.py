"""Загруженные файлы: что принимается как скан и как файлы отдаются браузеру.

Файлы живут на том же адресе, что и программа, поэтому показывать их в браузере
можно только в типах без исполняемого содержимого. Остальное — на скачивание,
в том числе файлы, загруженные до появления проверки.

Сканы договоров получают те же роли, которым открыты страницы договоров.
"""
import posixpath
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.utils._os import safe_join
from django.utils.http import content_disposition_header
from django.views.decorators.cache import cache_control
from django.views.static import serve
from PIL import Image

from apps.core import roles

SCAN_FOLDER = "contracts"
SCAN_ROLES = (roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_ACCOUNTANT)

SHOWN_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

SCAN_IMAGE_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".tif": "TIFF", ".tiff": "TIFF"}
SCAN_EXTENSIONS = (".pdf", *SCAN_IMAGE_FORMATS)
SCAN_TYPES_TEXT = "PDF, JPG, PNG или TIFF"


def _extension(name):
    return posixpath.splitext(name)[1].lower()


def scan_problem(name, stream):
    """Почему файл не годится как скан; None — годится. Поток остаётся в начале."""
    extension = _extension(name)
    if extension not in SCAN_EXTENSIONS:
        return f"можно загрузить только {SCAN_TYPES_TEXT}"
    stream.seek(0)
    try:
        if extension == ".pdf":
            matches = b"%PDF-" in stream.read(1024)
        else:
            Image.open(stream, formats=[SCAN_IMAGE_FORMATS[extension]]).verify()
            matches = True
    except Exception:  # Pillow сообщает о негодном файле исключениями разных типов
        matches = False
    finally:
        stream.seek(0)
    return None if matches else f"содержимое файла не соответствует расширению {extension}"


def validate_scan(upload):
    problem = scan_problem(upload.name, upload)
    if problem:
        raise ValidationError(problem, code="invalid_scan")


def _in_scan_folder(name):
    """Сравниваем настоящие пути: именно такой файл откроет serve, как ни запиши адрес."""
    folder = Path(settings.MEDIA_ROOT, SCAN_FOLDER).resolve()
    return Path(safe_join(settings.MEDIA_ROOT, name)).resolve().is_relative_to(folder)


@login_required
@cache_control(private=True, no_cache=True)
def media_file(request, path):
    """Файлы только для вошедших: страницы без входа и печатные формы их не используют.

    Браузер обязан спрашивать сервер перед показом копии: за тем же компьютером
    следующий пользователь может не иметь прав на этот файл.
    """
    if "\x00" in path:  # такого файла быть не может, а проверка пути на Linux на нём падает
        raise Http404
    name = posixpath.normpath(path).lstrip("/")
    if _in_scan_folder(name) and not request.user.has_role(*SCAN_ROLES):
        raise PermissionDenied("Недостаточно прав")
    content_type = SHOWN_TYPES.get(_extension(name))
    if content_type is None:
        # Ответ «не изменился» вернул бы браузеру прежнюю копию с прежним типом
        request.META.pop("HTTP_IF_MODIFIED_SINCE", None)
    response = serve(request, path, document_root=settings.MEDIA_ROOT)
    if response.status_code != 200:
        return response
    if content_type:
        response["Content-Type"] = content_type
    else:
        response["Content-Type"] = "application/octet-stream"
        response["Content-Disposition"] = content_disposition_header(True, posixpath.basename(name))
        del response["Content-Encoding"]
    return response
