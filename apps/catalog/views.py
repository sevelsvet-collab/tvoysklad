from django.contrib import messages
from django.db.models import Q
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, FormView, ListView, UpdateView

from apps.core import roles
from apps.core.permissions import RoleRequiredMixin

from .forms import ImportForm, ProductForm, ProductGroupForm
from .importers import import_products
from .models import Product, ProductGroup

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


class ProductCreateView(RoleRequiredMixin, CreateView):
    allowed_roles = EDIT_ROLES
    model = Product
    form_class = ProductForm
    template_name = "catalog/product_form.html"
    success_url = reverse_lazy("product_list")

    def form_valid(self, form):
        messages.success(self.request, "Товар сохранён")
        return super().form_valid(form)


class ProductUpdateView(RoleRequiredMixin, UpdateView):
    allowed_roles = EDIT_ROLES
    model = Product
    form_class = ProductForm
    template_name = "catalog/product_form.html"
    success_url = reverse_lazy("product_list")

    def form_valid(self, form):
        messages.success(self.request, "Товар сохранён")
        return super().form_valid(form)


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
