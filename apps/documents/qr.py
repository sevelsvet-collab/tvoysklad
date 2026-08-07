"""Платёжный QR-код по ГОСТ Р 56042-2014 (формат ST00012).

Такой QR сканируют мобильные приложения российских банков и автоматически
подставляют реквизиты получателя, сумму и назначение платежа.

Строка вида:
    ST00012|Name=...|PersonalAcc=...|BankName=...|BIC=...|CorrespAcc=...|Sum=...|Purpose=...
Первые 8 символов — идентификатор формата: ST0001 + код кодировки
(1=Win-1251, 2=UTF-8, 3=KOI8-R). Мы используем UTF-8 → префикс ST00012.
"""
import base64
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO

SEPARATOR = "|"

# Обязательные реквизиты по ГОСТ (без них банк не примет QR).
REQUIRED_KEYS = ("Name", "PersonalAcc", "BankName", "BIC", "CorrespAcc")


def _clean(value):
    """Значение поля → строка без разделителя и переводов строк."""
    if value is None:
        return ""
    text = str(value).strip()
    # Разделитель и управляющие символы недопустимы внутри значения.
    return text.replace(SEPARATOR, " ").replace("\r", " ").replace("\n", " ")


def _digits(value):
    """Оставляет только цифры (для счетов и БИК — там не должно быть пробелов)."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def build_payment_payload(org, doc, total):
    """Собирает строку ST00012 из реквизитов организации и счёта.

    Возвращает None, если нет обязательных банковских реквизитов —
    тогда QR показывать нельзя (банк его не распознает).
    """
    fields = [
        ("Name", _clean(org.full_name or org.name)),
        ("PersonalAcc", _digits(org.bank_account)),
        ("BankName", _clean(org.bank_name)),
        ("BIC", _digits(org.bik)),
        ("CorrespAcc", _digits(org.corr_account)),
    ]
    values = dict(fields)
    if not all(values.get(key) for key in REQUIRED_KEYS):
        return None

    # Необязательные, но полезные поля.
    if getattr(org, "inn", ""):
        fields.append(("PayeeINN", _digits(org.inn)))
    if getattr(org, "kpp", ""):
        fields.append(("KPP", _digits(org.kpp)))
    if total is not None:
        kopecks = (Decimal(total) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        fields.append(("Sum", str(int(kopecks))))
    purpose = f"Оплата по счёту № {doc.number} от {doc.date:%d.%m.%Y}"
    fields.append(("Purpose", _clean(purpose)))

    body = SEPARATOR.join(f"{key}={value}" for key, value in fields if value)
    return f"ST00012{SEPARATOR}{body}"


def qr_data_uri(payload, box_size=4, border=2):
    """Строка ST00012 → data:image/png;base64 с QR-кодом (для вставки в HTML)."""
    import qrcode

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    # UTF-8 явно: иначе qrcode пробует latin-1 и падает на кириллице.
    qr.add_data(payload.encode("utf-8"))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def invoice_qr_data_uri(org, doc, total):
    """Готовый data-URI QR для счёта или None, если реквизитов недостаточно."""
    payload = build_payment_payload(org, doc, total)
    if not payload:
        return None
    return qr_data_uri(payload)
