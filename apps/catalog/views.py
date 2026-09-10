from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import ProtectedError
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, FormView, ListView, UpdateView

from apps.core import roles
from apps.core.bulk import back, selected_ids
from apps.core.listing import ChoiceFilter, FilteredListMixin, TextFilter
from apps.core.models import Organization
from apps.core.permissions import RoleRequiredMixin, role_required

from .forms import ImportForm, ProductForm, ProductGroupForm
from .importers import import_products
from .models import Product, ProductGroup, Unit

EDIT_ROLES = [roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_STOREKEEPER]


def _by_activity(qs, value):
    """«Показывать»: как в МойСклад — по умолчанию только обычные (не архивные)."""
    return {"active": qs.filter(is_active=True), "archived": qs.filter(is_active=False)}.get(value, qs)


def _by_serials(qs, value):
    return {"yes": qs.filter(track_serials=True), "no": qs.filter(track_serials=False)}.get(value, qs)


class ProductListView(FilteredListMixin, RoleRequiredMixin, ListView):
    model = Product
    template_name = "catalog/product_list.html"
    context_object_name = "products"
    search_fields = ("name", "article", "code", "barcode")
    search_placeholder = "Наименование, код, артикул или штрихкод"
    keep_params = ("group",)
    list_filters = [
        TextFilter("name", "Наименование"),
        TextFilter("description", "Описание"),
        TextFilter("article", "Артикул"),
        TextFilter("code", "Код"),
        TextFilter("barcode", "Штрихкод"),
        ChoiceFilter("item_type", "Тип", choices=Product.TYPE_CHOICES),
        ChoiceFilter("show", "Показывать", choices=[("active", "Только обычные"), ("archived", "Только архивные"),
                                                    ("all", "Все")],
                     empty_label=None, default="active", apply=_by_activity),
        ChoiceFilter("serials", "Серийные номера", choices=[("yes", "С учётом"), ("no", "Без учёта")],
                     apply=_by_serials),
    ]
    bulk_actions = [
        {"label": "В архив", "url": "product_bulk", "kwargs": {"action": "archive"}, "icon": "bi-archive"},
        {"label": "Вернуть из архива", "url": "product_bulk", "kwargs": {"action": "restore"},
         "icon": "bi-arrow-counterclockwise"},
        {"label": "Удалить", "url": "product_bulk", "kwargs": {"action": "delete"}, "icon": "bi-trash",
         "danger": True, "ok": "Удалить",
         "confirm": "Удалить выбранные товары? Товары, которые есть в документах, удалить нельзя — "
                    "их лучше отправить в архив."},
    ]

    def get_queryset(self):
        qs = Product.objects.select_related("group", "unit")
        group_id = self.request.GET.get("group")
        if group_id:
            group = ProductGroup.objects.filter(pk=group_id).first()
            if group:
                qs = qs.filter(group_id__in=group.descendant_ids())
        return self.filter_queryset(qs)

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["catalog/_product_rows.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["groups"] = ProductGroup.objects.filter(parent=None).prefetch_related("children__children")
        ctx["active_group"] = self.request.GET.get("group", "")
        return ctx


class ProductEditBase(RoleRequiredMixin):
    """Карточка товара. Во всплывающем окне документа (?embed=1) после
    сохранения остаёмся в карточке — страница сообщает документу id товара."""

    allowed_roles = EDIT_ROLES
    model = Product
    form_class = ProductForm
    template_name = "catalog/product_form.html"
    success_url = reverse_lazy("product_list")

    def form_valid(self, form):
        messages.success(self.request, "Товар сохранён")
        return super().form_valid(form)

    def get_success_url(self):
        if self.request.GET.get("embed"):
            return reverse("product_edit", args=[self.object.pk]) + "?embed=1&saved=1"
        return super().get_success_url()


def _price_or_none(raw):
    try:
        return Decimal(str(raw).replace(",", ".")) if raw else None
    except (InvalidOperation, ValueError):
        return None


class ProductCreateView(ProductEditBase, CreateView):
    def get_initial(self):
        """Предзаполнение из строки документа: наименование, тип и цена."""
        initial = super().get_initial()
        params = self.request.GET
        if params.get("name"):
            initial["name"] = params["name"].strip()[:512]
        if params.get("item_type") in dict(Product.TYPE_CHOICES):
            initial["item_type"] = params["item_type"]
        price = _price_or_none(params.get("price"))
        if price:
            field = "sale_price" if params.get("price_source") == "sale" else "purchase_price"
            initial[field] = price
        unit = Unit.objects.filter(name="шт").first()
        if unit:
            initial.setdefault("unit", unit.pk)
        org = Organization.get_default()
        if org:
            initial.setdefault("vat_rate", org.line_vat_default)
        return initial


class ProductUpdateView(ProductEditBase, UpdateView):
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if self.object.track_serials:
            ctx["serial_stock"] = _serial_stock(self.object)
            ctx["serial_stock_missing"] = [r for r in ctx["serial_stock"] if r["without"] > 0]
        return ctx


def _serial_stock(product):
    """По складам: остаток, сколько единиц уже с номерами и сколько без."""
    from django.db.models import Count

    from apps.inventory.models import SerialNumber, StockBalance

    numbered = dict(
        SerialNumber.objects.filter(product=product).exclude(warehouse=None)
        .values_list("warehouse").annotate(n=Count("id"))
    )
    rows = []
    for balance in StockBalance.objects.filter(product=product).select_related("warehouse"):
        with_numbers = numbered.get(balance.warehouse_id, 0)
        rows.append({
            "warehouse": balance.warehouse, "quantity": balance.quantity,
            "numbered": with_numbers, "without": max(balance.quantity - with_numbers, 0),
        })
    return rows


class GroupCreateView(RoleRequiredMixin, CreateView):
    allowed_roles = EDIT_ROLES
    model = ProductGroup
    form_class = ProductGroupForm
    template_name = "catalog/group_form.html"
    success_url = reverse_lazy("product_list")


class GroupUpdateView(RoleRequiredMixin, UpdateView):
    allowed_roles = EDIT_ROLES
    model = ProductGroup
    form_class = ProductGroupForm
    template_name = "catalog/group_form.html"
    success_url = reverse_lazy("product_list")


class CatalogImportView(RoleRequiredMixin, FormView):
    allowed_roles = EDIT_ROLES
    form_class = ImportForm
    template_name = "catalog/import.html"

    def form_valid(self, form):
        created, updated, errors = import_products(form.cleaned_data["file"])
        if created or updated:
            messages.success(self.request, f"Импорт завершён: создано {created}, обновлено {updated}")
        elif not errors:
            messages.warning(self.request, "В файле не нашлось ни одного товара с наименованием")
        for err in errors[:20]:
            messages.error(self.request, err)
        if len(errors) > 20:
            messages.error(self.request, f"…и ещё ошибок: {len(errors) - 20}")
        return redirect("catalog_import")


@require_POST
@role_required(*EDIT_ROLES)
def product_bulk(request, action):
    """Отмеченные товары: в архив, из архива или удалить (если их нет в документах)."""
    ids = selected_ids(request)
    products = Product.objects.filter(pk__in=ids)
    if not ids:
        messages.warning(request, "Ничего не выбрано")
    elif action in ("archive", "restore"):
        count = products.update(is_active=action == "restore")
        label = "Возвращено из архива" if action == "restore" else "Отправлено в архив"
        messages.success(request, f"{label}: {count}")
    elif action == "delete":
        deleted, used = 0, []
        for product in products:
            try:
                product.delete()
                deleted += 1
            except ProtectedError:
                used.append(product.name)
        if deleted:
            messages.success(request, f"Удалено товаров: {deleted}")
        if used:
            messages.warning(request, "Есть в документах, поэтому не удалены — отправьте их в архив: "
                                      + ", ".join(used[:5]) + (" …" if len(used) > 5 else ""))
    return back(request, reverse("product_list"))
