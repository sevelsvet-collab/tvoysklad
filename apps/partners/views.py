from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.generic import CreateView, FormView, ListView, UpdateView

from apps.core import roles
from apps.core.forms import ImportForm
from apps.core.pagination import PageSizeMixin
from apps.core.permissions import RoleRequiredMixin

from .documents import counterparty_documents
from .forms import BankAccountFormSet, ContactPersonFormSet, ContractFormSet, CounterpartyForm
from .importers import import_counterparties
from .models import Counterparty
from .services import BankLookupError, InnLookupError, lookup_bank, lookup_inn

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
        if "contact_formset" not in ctx:
            ctx["contact_formset"] = ContactPersonFormSet(instance=self.object, prefix="contacts")
        if self.object is not None:
            ctx["documents"] = counterparty_documents(self.object)
        return ctx

    def form_valid(self, form):
        is_create = self.object is None
        bank_formset = BankAccountFormSet(self.request.POST, instance=self.object, prefix="banks")
        contract_formset = ContractFormSet(self.request.POST, instance=self.object, prefix="contracts")
        contact_formset = ContactPersonFormSet(self.request.POST, instance=self.object, prefix="contacts")
        if not (bank_formset.is_valid() and contract_formset.is_valid() and contact_formset.is_valid()):
            return self.render_to_response(self.get_context_data(
                form=form, bank_formset=bank_formset,
                contract_formset=contract_formset, contact_formset=contact_formset,
            ))
        # При создании — проверка на дубликаты (по ИНН/телефону/названию),
        # пока пользователь не подтвердил «Всё равно создать».
        if is_create and self.request.POST.get("confirm_duplicate") != "1":
            duplicates = _find_duplicates(form)
            if duplicates:
                return self.render_to_response(self.get_context_data(
                    form=form, bank_formset=bank_formset,
                    contract_formset=contract_formset, contact_formset=contact_formset,
                    duplicates=duplicates,
                ))
        self.object = form.save()
        for fs in (bank_formset, contract_formset, contact_formset):
            fs.instance = self.object
            fs.save()
        messages.success(self.request, "Контрагент сохранён")
        # Остаёмся на карточке (не выбрасываем в список) — удобно дозаполнять
        url = reverse("counterparty_edit", args=[self.object.pk])
        if self.request.GET.get("embed"):
            url += "?embed=1"   # во встроенном режиме (в модалке документа) не теряем его
        return redirect(url)


def _find_duplicates(form):
    """Ищет уже существующих контрагентов с тем же ИНН, телефоном или названием."""
    inn = (form.cleaned_data.get("inn") or "").strip()
    phone = (form.cleaned_data.get("phone") or "").strip()
    name = (form.cleaned_data.get("name") or "").strip()
    query = Q()
    if inn:
        query |= Q(inn=inn)
    if phone:
        query |= Q(phone=phone)
    if name:
        query |= Q(name__iexact=name)
    if not query:
        return Counterparty.objects.none()
    return list(Counterparty.objects.filter(query)[:10])


class CounterpartyCreateView(CounterpartyEditBase, CreateView):
    pass


@method_decorator(xframe_options_sameorigin, name="dispatch")
class CounterpartyUpdateView(CounterpartyEditBase, UpdateView):
    # разрешаем встраивание в модалку документа (тот же сайт)
    pass


@login_required
def inn_lookup(request):
    """Подтягивает реквизиты по ИНН (DaData) для кнопки «Заполнить по ИНН»."""
    try:
        data = lookup_inn(request.GET.get("inn", ""))
        return JsonResponse({"ok": True, "data": data})
    except InnLookupError as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@login_required
def bank_lookup(request):
    """Подтягивает наименование банка и корр. счёт по БИК (DaData)."""
    try:
        data = lookup_bank(request.GET.get("bik", ""))
        return JsonResponse({"ok": True, "data": data})
    except BankLookupError as exc:
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
