from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, FormView, ListView, UpdateView

from apps.core import roles
from apps.core.forms import ImportForm
from apps.core.pagination import PageSizeMixin
from apps.core.permissions import RoleRequiredMixin

from .forms import BankAccountFormSet, ContractFormSet, CounterpartyForm
from .importers import import_counterparties
from .models import Counterparty
from .services import InnLookupError, lookup_inn

EDIT_ROLES = [roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_ACCOUNTANT]


class CounterpartyListView(PageSizeMixin, RoleRequiredMixin, ListView):
    model = Counterparty
    template_name = "partners/counterparty_list.html"
    context_object_name = "counterparties"

    def get_queryset(self):
        qs = Counterparty.objects.all()
        q = self.request.GET.get("q", "").strip()
        ptype = self.request.GET.get("type", "")
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(inn__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q))
        if ptype in (Counterparty.TYPE_CUSTOMER, Counterparty.TYPE_SUPPLIER):
            qs = qs.filter(partner_type__in=[ptype, Counterparty.TYPE_BOTH])
        return qs

    def get_template_names(self):
        # HTMX-запрос (живой поиск) получает только строки таблицы
        if self.request.headers.get("HX-Request"):
            return ["partners/_counterparty_rows.html"]
        return [self.template_name]


class CounterpartyEditBase(RoleRequiredMixin):
    """Общая логика создания/редактирования: форма + формсеты счетов и договоров."""

    allowed_roles = EDIT_ROLES
    model = Counterparty
    form_class = CounterpartyForm
    template_name = "partners/counterparty_form.html"
    success_url = reverse_lazy("counterparty_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if "bank_formset" not in ctx:
            ctx["bank_formset"] = BankAccountFormSet(instance=self.object, prefix="banks")
        if "contract_formset" not in ctx:
            ctx["contract_formset"] = ContractFormSet(instance=self.object, prefix="contracts")
        return ctx

    def form_valid(self, form):
        bank_formset = BankAccountFormSet(self.request.POST, instance=self.object, prefix="banks")
        contract_formset = ContractFormSet(self.request.POST, instance=self.object, prefix="contracts")
        if not (bank_formset.is_valid() and contract_formset.is_valid()):
            return self.render_to_response(self.get_context_data(
                form=form, bank_formset=bank_formset, contract_formset=contract_formset,
            ))
        self.object = form.save()
        bank_formset.instance = self.object
        contract_formset.instance = self.object
        bank_formset.save()
        contract_formset.save()
        messages.success(self.request, "Контрагент сохранён")
        return redirect(self.success_url)


class CounterpartyCreateView(CounterpartyEditBase, CreateView):
    pass


class CounterpartyUpdateView(CounterpartyEditBase, UpdateView):
    pass


@login_required
def inn_lookup(request):
    """Подтягивает реквизиты по ИНН (DaData) для кнопки «Заполнить по ИНН»."""
    try:
        data = lookup_inn(request.GET.get("inn", ""))
        return JsonResponse({"ok": True, "data": data})
    except InnLookupError as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


class PartnersImportView(RoleRequiredMixin, FormView):
    allowed_roles = EDIT_ROLES
    form_class = ImportForm
    template_name = "partners/import.html"

    def form_valid(self, form):
        created, updated, errors = import_counterparties(form.cleaned_data["file"])
        messages.success(self.request, f"Импорт завершён: создано {created}, обновлено {updated}")
        for err in errors[:20]:
            messages.error(self.request, err)
        return redirect("partners_import")
