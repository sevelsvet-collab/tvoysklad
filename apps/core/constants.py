from decimal import Decimal

VAT_20 = "20"
VAT_10 = "10"
VAT_0 = "0"
VAT_NONE = "none"

VAT_CHOICES = [
    (VAT_20, "20%"),
    (VAT_10, "10%"),
    (VAT_0, "0%"),
    (VAT_NONE, "Без НДС"),
]

VAT_RATES = {
    VAT_20: Decimal("0.20"),
    VAT_10: Decimal("0.10"),
    VAT_0: Decimal("0"),
    VAT_NONE: Decimal("0"),
}

# Статусы проведения документов
DOC_DRAFT = "draft"
DOC_POSTED = "posted"
DOC_STATUS_CHOICES = [
    (DOC_DRAFT, "Черновик"),
    (DOC_POSTED, "Проведён"),
]


def line_total(quantity, price, discount_percent=0):
    """Сумма строки с учётом скидки (скидка хранится в процентах)."""
    gross = Decimal(quantity) * Decimal(price)
    if discount_percent:
        gross = gross * (Decimal(1) - Decimal(discount_percent) / Decimal(100))
    return gross.quantize(Decimal("0.01"))


def vat_amount(total, vat_rate):
    """НДС «в том числе» из суммы с НДС."""
    rate = VAT_RATES.get(vat_rate, Decimal("0"))
    if not rate:
        return Decimal("0")
    return (total * rate / (1 + rate)).quantize(Decimal("0.01"))
