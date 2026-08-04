"""JSON API каталога для «живого» поиска товаров в документах."""
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from apps.core import roles
from apps.core.permissions import role_required

from .models import Product, ProductGroup, Unit

SEARCH_LIMIT = 15
EDIT_ROLES = [roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_STOREKEEPER]


def _stock_map(product_ids, warehouse_id=None):
    """{product_id: остаток} — по конкретному складу или суммарно по всем."""
    from apps.inventory.models import StockBalance

    qs = StockBalance.objects.filter(product_id__in=product_ids)
    if warehouse_id:
        qs = qs.filter(warehouse_id=warehouse_id)
    return {
        row["product_id"]: row["total"]
        for row in qs.values("product_id").annotate(total=Sum("quantity"))
    }


def _serialize(product, stock, price_source):
    price = product.sale_price if price_source == "sale" else product.purchase_price
    stock_val = float(stock) if stock is not None else None
    return {
        "id": product.pk,
        "name": product.name,
        "article": product.article,
        "unit": str(product.unit) if product.unit_id else "",
        "price": str(price),
        "stock": stock_val,
        # Доступно = остаток − резерв. Резервов пока нет → равно остатку.
        "available": stock_val,
        "is_service": product.is_service,
        "label": f"{product.name}" + (f" ({product.article})" if product.article else ""),
    }


@login_required
def product_search(request):
    """Подсказки по названию/артикулу/коду/штрихкоду с остатком на складе.

    Если запрос — точный штрихкод/код единственного товара, возвращаем auto_select:
    сканер штрихкодов «набирает» код и жмёт Enter — товар подставится сам.
    """
    q = request.GET.get("q", "").strip()
    warehouse_id = request.GET.get("warehouse") or None
    price_source = request.GET.get("price", "purchase")

    qs = Product.objects.filter(is_active=True).select_related("unit")
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(article__icontains=q)
            | Q(code__icontains=q) | Q(barcode__icontains=q)
        )
    products = list(qs.order_by("name")[:SEARCH_LIMIT])

    stocks = _stock_map([p.pk for p in products], warehouse_id)
    results = [
        _serialize(p, None if p.is_service else stocks.get(p.pk, Decimal("0")), price_source)
        for p in products
    ]

    auto_select = None
    if q.isdigit() and len(q) >= 6:
        exact = [p for p in products if p.barcode == q or p.code == q]
        if len(exact) == 1:
            auto_select = exact[0].pk

    return JsonResponse({
        "results": results,
        "can_create": request.user.has_role(*EDIT_ROLES),
        "auto_select": auto_select,
    })


@login_required
def product_stock(request):
    """Остатки для набора товаров на складе — для показа в строках документа."""
    raw = request.GET.get("ids", "")
    ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    warehouse_id = request.GET.get("warehouse") or None

    stocks = _stock_map(ids, warehouse_id)
    data = {}
    for product in Product.objects.filter(id__in=ids).select_related("unit"):
        stock = None if product.is_service else float(stocks.get(product.pk, Decimal("0")))
        data[str(product.pk)] = {
            "stock": stock,
            "available": stock,  # без резервов равно остатку
            "unit": str(product.unit) if product.unit_id else "",
        }
    return JsonResponse({"stock": data})


@require_POST
@login_required
@role_required(*EDIT_ROLES)
def product_quick_create(request):
    """Создаёт товар «на лету» из строки документа (только наименование и цена)."""
    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"ok": False, "error": "Введите наименование товара"}, status=400)

    existing = Product.objects.filter(name__iexact=name, is_active=True).select_related("unit").first()
    if existing:
        price_source = request.POST.get("price", "purchase")
        stocks = _stock_map([existing.pk], request.POST.get("warehouse") or None)
        return JsonResponse({
            "ok": True, "created": False,
            "product": _serialize(existing, stocks.get(existing.pk, Decimal("0")), price_source),
        })

    unit = Unit.objects.filter(name="шт").first() or Unit.objects.first()
    if unit is None:
        unit = Unit.objects.create(name="шт", okei_code="796")

    group = None
    group_id = request.POST.get("group")
    if group_id:
        group = ProductGroup.objects.filter(pk=group_id).first()

    def _decimal(raw):
        try:
            return Decimal(str(raw).replace(",", ".")) if raw else Decimal("0")
        except (ValueError, ArithmeticError):
            return Decimal("0")

    price = _decimal(request.POST.get("price_value"))
    price_source = request.POST.get("price", "purchase")
    product = Product.objects.create(
        name=name, unit=unit, group=group,
        sale_price=price if price_source == "sale" else Decimal("0"),
        purchase_price=price if price_source != "sale" else Decimal("0"),
    )
    return JsonResponse({
        "ok": True, "created": True,
        "product": _serialize(product, Decimal("0"), price_source),
    })
