from django.contrib import messages
from django.contrib.auth import get_user_model
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.generic import CreateView, ListView, TemplateView, UpdateView

from . import roles
from .forms import OrganizationForm, WarehouseForm
from .models import Organization, Warehouse
from .permissions import RoleRequiredMixin

User = get_user_model()


class DashboardView(RoleRequiredMixin, TemplateView):
    template_name = "core/dashboard.html"

    def get_context_data(self, **kwargs):
        from decimal import Decimal

        from django.utils import timezone

        from apps.finance.models import Account
        from apps.reports import services

        ctx = super().get_context_data(**kwargs)
        ctx["organizations"] = Organization.objects.filter(is_active=True)
        ctx["warehouses"] = Warehouse.objects.filter(is_active=True)
        ctx["money_total"] = sum(
            (a.balance for a in Account.objects.filter(is_active=True)), Decimal("0"),
        )

        today = timezone.localdate()
        month_start = today.replace(day=1)
        ctx["sales_month"] = services.sales_summary(month_start, today)
        ctx["top_products"] = services.top_products(month_start, today, limit=5)
        ctx["overdue"] = services.overdue_invoices()[:10]

        by_month = services.sales_by_month(6)
        ctx["chart_labels"] = [r["label"] for r in by_month]
        ctx["chart_values"] = [float(r["value"]) for r in by_month]
        return ctx


# ---------- Настройки: организации ----------

class OrganizationListView(RoleRequiredMixin, ListView):
    allowed_roles = [roles.ROLE_ADMIN]
    model = Organization
    template_name = "core/organization_list.html"
    context_object_name = "organizations"


class OrganizationCreateView(RoleRequiredMixin, CreateView):
    allowed_roles = [roles.ROLE_ADMIN]
    model = Organization
    form_class = OrganizationForm
    template_name = "core/organization_form.html"
    success_url = reverse_lazy("organization_list")

    def form_valid(self, form):
        messages.success(self.request, "Организация сохранена")
        return super().form_valid(form)


@method_decorator(xframe_options_sameorigin, name="dispatch")
class OrganizationUpdateView(RoleRequiredMixin, UpdateView):
    # разрешаем встраивание в модалку документа (тот же сайт)
    allowed_roles = [roles.ROLE_ADMIN]
    model = Organization
    form_class = OrganizationForm
    template_name = "core/organization_form.html"
    success_url = reverse_lazy("organization_list")

    def form_valid(self, form):
        messages.success(self.request, "Организация сохранена")
        return super().form_valid(form)

    def get_success_url(self):
        # Во встроенном режиме (в модалке документа) остаёмся на карточке
        if self.request.GET.get("embed"):
            return reverse("organization_edit", args=[self.object.pk]) + "?embed=1"
        return super().get_success_url()


# ---------- Настройки: склады ----------

class WarehouseListView(RoleRequiredMixin, ListView):
    allowed_roles = [roles.ROLE_ADMIN, roles.ROLE_STOREKEEPER]
    model = Warehouse
    template_name = "core/warehouse_list.html"
    context_object_name = "warehouses"


class WarehouseCreateView(RoleRequiredMixin, CreateView):
    allowed_roles = [roles.ROLE_ADMIN]
    model = Warehouse
    form_class = WarehouseForm
    template_name = "core/warehouse_form.html"
    success_url = reverse_lazy("warehouse_list")

    def form_valid(self, form):
        messages.success(self.request, "Склад сохранён")
        return super().form_valid(form)


class WarehouseUpdateView(RoleRequiredMixin, UpdateView):
    allowed_roles = [roles.ROLE_ADMIN]
    model = Warehouse
    form_class = WarehouseForm
    template_name = "core/warehouse_form.html"
    success_url = reverse_lazy("warehouse_list")

    def form_valid(self, form):
        messages.success(self.request, "Склад сохранён")
        return super().form_valid(form)


# ---------- Настройки: пользователи ----------

class UserListView(RoleRequiredMixin, ListView):
    allowed_roles = [roles.ROLE_ADMIN]
    model = User
    template_name = "core/user_list.html"
    context_object_name = "users"

    def get_queryset(self):
        return User.objects.prefetch_related("groups").order_by("username")
