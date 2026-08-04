from datetime import date

from django.utils import timezone
from django.views.generic import TemplateView

from apps.core import roles
from apps.core.permissions import RoleRequiredMixin

from . import services

REPORT_ROLES = [roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_ACCOUNTANT]


def _period(request):
    """Возвращает (date_from, date_to) из GET; по умолчанию — текущий месяц."""
    today = timezone.localdate()
    default_from = today.replace(day=1)

    def parse(name, default):
        raw = request.GET.get(name)
        if raw:
            try:
                return date.fromisoformat(raw)
            except ValueError:
                pass
        return default

    return parse("date_from", default_from), parse("date_to", today)


class SalesReportView(RoleRequiredMixin, TemplateView):
    allowed_roles = REPORT_ROLES
    template_name = "reports/sales.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        date_from, date_to = _period(self.request)
        ctx["date_from"] = date_from
        ctx["date_to"] = date_to
        ctx["summary"] = services.sales_summary(date_from, date_to)
        ctx["top_products"] = services.top_products(date_from, date_to)
        ctx["top_customers"] = services.top_customers(date_from, date_to)
        by_month = services.sales_by_month(6)
        ctx["chart_labels"] = [r["label"] for r in by_month]
        ctx["chart_values"] = [float(r["value"]) for r in by_month]
        return ctx


class CashflowReportView(RoleRequiredMixin, TemplateView):
    allowed_roles = REPORT_ROLES
    template_name = "reports/cashflow.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        date_from, date_to = _period(self.request)
        ctx["date_from"] = date_from
        ctx["date_to"] = date_to
        ctx["summary"] = services.cashflow_summary(date_from, date_to)
        ctx["by_account"] = services.cashflow_by_account(date_from, date_to)
        income, outcome = services.cashflow_by_month(6)
        ctx["chart_labels"] = [r["label"] for r in income]
        ctx["chart_income"] = [float(r["value"]) for r in income]
        ctx["chart_outcome"] = [float(r["value"]) for r in outcome]
        return ctx
