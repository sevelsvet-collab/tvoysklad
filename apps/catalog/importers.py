"""Импорт товаров из Excel (.xlsx).

Поддерживает два формата:
1. Экспорт товаров из «МойСклад» (колонки «Цена: Цена продажи», «Единица
   измерения», «Штрихкод EAN13», «НДС», «Неснижаемый остаток», «Архивный»…).
2. Простой шаблон: Наименование* | Тип | Группа | Артикул | Код | Штрихкод |
   Ед. изм. | Ставка НДС | Закупочная цена | Цена продажи | Мин. остаток

Строка заголовков ищется в первых 10 строках каждого листа, регистр не важен.
Существующий товар находится по артикулу, затем по коду, и только если нет
ни того ни другого — по наименованию (в МойСклад бывают одноимённые товары
с разными кодами).
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from openpyxl import load_workbook

from apps.core.constants import VAT_0, VAT_10, VAT_20, VAT_NONE

from .models import Product, ProductGroup, Unit

# Возможные названия колонок: простой шаблон и экспорт МойСклад
COLUMNS = {
    "name": ["наименование"],
    "type": ["тип"],
    "group": ["группа", "группы"],
    "article": ["артикул"],
    "code": ["код"],
    "barcode": ["штрихкод", "штрихкод ean13", "штрихкод ean8", "штрихкод code128",
                "штрихкод upc", "штрихкод gtin"],
    "unit": ["ед. изм.", "ед.изм.", "единица", "единица измерения"],
    "vat": ["ставка ндс", "ндс"],
    "purchase_price": ["закупочная цена"],
    "sale_price": ["цена продажи", "цена: цена продажи"],
    "min_stock": ["мин. остаток", "неснижаемый остаток"],
    "description": ["описание"],
    "archived": ["архивный"],
}

VAT_MAP = {"20": VAT_20, "10": VAT_10, "0": VAT_0}


def _clean(value):
    return "" if value is None else str(value).strip()


def _dec(value, default=Decimal("0")):
    """«2 500,00» → 2500.00; пусто или мусор → default."""
    raw = _clean(value).replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not raw:
        return default
    try:
        return Decimal(raw)
    except InvalidOperation:
        return default


def _vat(value):
    """«20», «20%», «20.0», «без НДС», пусто → код ставки. Пусто — 20%."""
    raw = _clean(value).lower().replace("%", "").replace(",", ".").strip()
    if not raw:
        return VAT_20
    if "без" in raw or raw in ("нет", "-"):
        return VAT_NONE
    try:
        return VAT_MAP.get(str(int(Decimal(raw))), VAT_20)
    except InvalidOperation:
        return VAT_20


def _first_code(value):
    """В ячейке штрихкода МойСклад бывает несколько кодов через запятую — берём первый."""
    for part in _clean(value).replace(";", ",").split(","):
        if part.strip():
            return part.strip()
    return ""


def _truncate_fields(obj):
    """Обрезает строковые поля до max_length модели (PostgreSQL иначе падает)."""
    for f in obj._meta.concrete_fields:
        max_length = getattr(f, "max_length", None)
        value = getattr(obj, f.attname, None)
        if max_length and isinstance(value, str) and len(value) > max_length:
            setattr(obj, f.attname, value[:max_length])


def _find_header(wb, max_rows=10):
    """Лист и строка с заголовками (там есть «Наименование»)."""
    for ws in wb.worksheets:
        for row_idx, row in enumerate(ws.iter_rows(max_row=max_rows, values_only=True), start=1):
            cols = {_clean(h).lower(): i for i, h in enumerate(row) if _clean(h)}
            if "наименование" in cols:
                return ws, row_idx, cols
    return None, None, None


class _Row:
    """Доступ к ячейкам строки по смысловому имени колонки."""

    def __init__(self, row, cols):
        self.row, self.cols = row, cols

    def get(self, key):
        """Первое непустое значение среди колонок-синонимов."""
        for name in COLUMNS[key]:
            idx = self.cols.get(name)
            if idx is not None and idx < len(self.row) and _clean(self.row[idx]):
                return self.row[idx]
        return None

    def has(self, key):
        return any(name in self.cols for name in COLUMNS[key])


class _Lookups:
    """Кэш единиц и групп — чтобы на тысячу строк не делать тысячу запросов."""

    def __init__(self):
        self.units = {}
        self.groups = {}
        self.default_unit, _ = Unit.objects.get_or_create(name="шт", defaults={"okei_code": "796"})

    def unit(self, name):
        if not name:
            return self.default_unit
        if name not in self.units:
            self.units[name] = Unit.objects.get_or_create(name=name[:32])[0]
        return self.units[name]

    def group(self, path):
        """«Электроника/Кабели» → вложенные группы."""
        parent = None
        for part in [p.strip() for p in path.split("/") if p.strip()]:
            key = (parent.pk if parent else None, part)
            if key not in self.groups:
                self.groups[key] = ProductGroup.objects.get_or_create(name=part[:255], parent=parent)[0]
            parent = self.groups[key]
        return parent


def _find_existing(article, code, name):
    if article:
        return Product.objects.filter(article=article).first()
    if code:
        return Product.objects.filter(code=code).first()
    return Product.objects.filter(name=name, article="", code="").first()


def import_products(file):
    # НЕ read_only: экспорт МойСклада объявляет неверную размерность листа
    # («A1»), и в экономном режиме openpyxl видит одну ячейку вместо всех.
    wb = load_workbook(file, data_only=True)
    ws, header_row, cols = _find_header(wb)
    if ws is None:
        return 0, 0, ["Не найдена колонка «Наименование» — проверьте, что в файле есть строка заголовков"]

    lookups = _Lookups()
    created = updated = 0
    errors = []

    for n, raw in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
        row = _Row(raw, cols)
        name = _clean(row.get("name"))
        if not name:
            continue
        try:
            with transaction.atomic():  # одна плохая строка не ломает весь импорт
                article = _clean(row.get("article"))
                code = _clean(row.get("code"))
                product = _find_existing(article, code, name)
                was_created = product is None
                if was_created:
                    product = Product(unit=lookups.default_unit)

                product.name = name
                product.article = article or product.article
                product.code = code or product.code
                if row.has("type"):
                    is_service = _clean(row.get("type")).lower() == "услуга"
                    product.item_type = Product.TYPE_SERVICE if is_service else Product.TYPE_PRODUCT
                if row.has("group"):
                    group_path = _clean(row.get("group"))
                    product.group = lookups.group(group_path) if group_path else None
                barcode = _first_code(row.get("barcode"))
                if barcode:
                    product.barcode = barcode
                if row.has("unit") or was_created:
                    product.unit = lookups.unit(_clean(row.get("unit")))
                if row.has("vat"):
                    product.vat_rate = _vat(row.get("vat"))
                if row.has("purchase_price"):
                    product.purchase_price = _dec(row.get("purchase_price"))
                if row.has("sale_price"):
                    product.sale_price = _dec(row.get("sale_price"))
                if row.has("min_stock"):
                    product.min_stock = _dec(row.get("min_stock"))
                if row.has("description"):
                    product.description = _clean(row.get("description"))
                if row.has("archived"):
                    product.is_active = _clean(row.get("archived")).lower() not in ("да", "yes", "1", "true")

                _truncate_fields(product)
                product.save()
            created += was_created
            updated += not was_created
        except Exception as exc:  # noqa: BLE001 — сообщаем о строке и идём дальше
            errors.append(f"Строка {n} («{name[:40]}»): {exc}")

    return created, updated, errors
