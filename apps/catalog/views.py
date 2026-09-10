from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, FormView, ListView, UpdateView

from apps.core import roles
from apps.core.models import Organization
from apps.core.permissions import RoleRequiredMixin

from .forms import ImportForm, ProductForm, ProductGroupForm
from .importers import import_products
from .models import Product, ProductGroup, Unit

EDIT_ROLES = [roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_STOREKEEPER]


class ProductListView(RoleRequiredMixin, ListView):
    model = Product
    template_name = "catalog/product_list.html"
    context_object_name = "products"
    paginate_by = 50

    def get_queryset(self):
        qs = Product.objects.select_related("group", "unit")
        q = self.request.GET.get("q", "").strip()
        group_id = self.request.GET.get("group")
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(article__icontains=q) | Q(code__icontains=q) | Q(barcode__icontains=q))
        if group_id:
            group = ProductGroup.objects.filter(pk=group_id).first()
            if group:
                qs = qs.filter(group_id__in=group.descendant_ids())
        return qs

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
    pass


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
        messages.success(self.request, f"Импорт завершён: создано {created}, обновлено {updated}")
        for err in errors[:20]:
            messages.error(self.request, err)
        return redirect("catalog_import")
