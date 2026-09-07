from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, FormView, ListView, UpdateView

from apps.core import roles
from apps.core.forms import ImportForm
from apps.core.pagination import PageSizeMixin
from apps.core.permissions import RoleRequiredMixin

from .contracts import PLACEHOLDER_GROUPS, build_contract_text, default_option, sync_answers
from .documents import counterparty_documents
from .export import contract_docx, contract_filename, contract_pdf
from .forms import (
    BankAccountFormSet, ContactPersonFormSet, ContractForm, ContractTemplateForm, CounterpartyForm,
)
from .importers import import_counterparties
from .models import Contract, ContractTemplate, Counterparty
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
        if "contact_formset" not in ctx:
            ctx["contact_formset"] = ContactPersonFormSet(instance=self.object, prefix="contacts")
        if self.object is not None:
            ctx["documents"] = counterparty_documents(self.object)
            ctx["contracts"] = self.object.contracts.select_related("template", "organization")
        return ctx

    def form_valid(self, form):
        is_create = self.object is None
        bank_formset = BankAccountFormSet(self.request.POST, instance=self.object, prefix="banks")
        contact_formset = ContactPersonFormSet(self.request.POST, instance=self.object, prefix="contacts")
        if not (bank_formset.is_valid() and contact_formset.is_valid()):
            return self.render_to_response(self.get_context_data(
                form=form, bank_formset=bank_formset, contact_formset=contact_formset,
            ))
        # При создании — проверка на дубликаты (по ИНН/телефону/названию),
        # пока пользователь не подтвердил «Всё равно создать».
        if is_create and self.request.POST.get("confirm_duplicate") != "1":
            duplicates = _find_duplicates(form)
            if duplicates:
                return self.render_to_response(self.get_context_data(
                    form=form, bank_formset=bank_formset,
                    contact_formset=contact_formset, duplicates=duplicates,
                ))
        self.object = form.save()
        for fs in (bank_formset, contact_formset):
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


@login_required
def bank_lookup(request):
    """Подтягивает наименование банка и корр. счёт по БИК (DaData)."""
    try:
        data = lookup_bank(request.GET.get("bik", ""))
        return JsonResponse({"ok": True, "data": data})
    except BankLookupError as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


# ---------- Договоры ----------

def _build_sections(template, contract=None):
    """Вопросы шаблона по разделам с уже вычисленным выбором.

    Для нового договора (contract=None) отмечаются варианты по умолчанию —
    вопросы видны сразу, до сохранения.
    """
    answers = {}
    if contract and contract.pk:
        answers = {a.question_id: a for a in contract.answers.select_related("option")}

    sections, current = [], None
    for question in template.questions.prefetch_related("options"):
        answer = answers.get(question.id)
        if answer is None:
            default = default_option(question)
            selected = str(default.pk) if default else "none"
            custom_text = ""
        elif answer.is_skipped:
            selected, custom_text = "none", ""
        elif answer.custom_text:
            selected, custom_text = "custom", answer.custom_text
        else:
            selected = str(answer.option_id) if answer.option_id else ""
            custom_text = ""

        if current is None or current["title"] != question.section:
            current = {"title": question.section, "questions": []}
            sections.append(current)
        current["questions"].append({
            "question": question, "selected": selected, "custom_text": custom_text,
        })
    return sections


@login_required
def contract_questions(request):
    """Блок вопросов конструктора — подгружается при выборе вида договора."""
    template = ContractTemplate.objects.filter(pk=request.GET.get("template")).first()
    sections = _build_sections(template) if template else []
    return render(request, "partners/_contract_questions.html",
                  {"sections": sections, "template": template})


def _save_answers(contract, post):
    """Сохраняет выбор пользователя по каждому вопросу конструктора."""
    from .models import ContractOption

    for answer in contract.answers.select_related("question"):
        choice = post.get(f"q_{answer.question_id}")
        if choice is None:
            continue
        custom_text = post.get(f"custom_{answer.question_id}", "").strip()
        if choice == "none":
            answer.option, answer.custom_text, answer.is_skipped = None, "", True
        elif choice == "custom":
            answer.option, answer.custom_text, answer.is_skipped = None, custom_text, False
        else:
            option = ContractOption.objects.filter(pk=choice, question_id=answer.question_id).first()
            if not option:
                continue
            answer.option, answer.custom_text, answer.is_skipped = option, "", False
            answer.body_snapshot = option.body   # снимок: правка шаблона не изменит договор
        answer.save()


class ContractListView(PageSizeMixin, RoleRequiredMixin, ListView):
    allowed_roles = EDIT_ROLES
    model = Contract
    template_name = "partners/contract_list.html"
    context_object_name = "contracts"

    def get_queryset(self):
        qs = Contract.objects.select_related("counterparty", "organization", "template")
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(number__icontains=q) | Q(name__icontains=q)
                | Q(counterparty__name__icontains=q) | Q(subject__icontains=q)
            )
        return qs


class ContractEditBase(RoleRequiredMixin):
    allowed_roles = EDIT_ROLES
    model = Contract
    form_class = ContractForm
    template_name = "partners/contract_form.html"

    def get_initial(self):
        initial = super().get_initial()
        partner = self.request.GET.get("partner")  # создание из карточки контрагента
        if partner:
            initial["counterparty"] = partner
        if not (self.object and self.object.pk):
            # Вид договора подставляем сразу — иначе конструктор пуст и непонятно,
            # куда вводить условия. Выбранный вид показан над списком вопросов.
            template = self.request.GET.get("template")
            if not template:
                first = ContractTemplate.objects.filter(is_active=True).first()
                template = first.pk if first else None
            if template:
                initial["template"] = template
        return initial

    def _current_template(self, ctx):
        """Шаблон, вопросы которого показываем: сохранённый, выбранный или единственный."""
        contract = self.object
        if contract and contract.pk and contract.template:
            return contract.template
        form = ctx.get("form")
        raw = None
        if form is not None:
            raw = form.data.get("template") if form.is_bound else form.initial.get("template")
        if not raw:
            return None
        return ContractTemplate.objects.filter(pk=raw).first()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        contract = self.object
        template = self._current_template(ctx)
        if template:
            saved = contract if (contract and contract.pk) else None
            ctx["sections"] = _build_sections(template, saved)
            ctx["template"] = template
            if saved:
                ctx["preview"] = build_contract_text(contract)
        return ctx

    def form_valid(self, form):
        self.object = form.save()
        sync_answers(self.object)          # добавит вопросы нового шаблона
        _save_answers(self.object, self.request.POST)
        messages.success(self.request, "Договор сохранён")
        return redirect("contract_edit", pk=self.object.pk)


class ContractCreateView(ContractEditBase, CreateView):
    pass


class ContractUpdateView(ContractEditBase, UpdateView):
    pass


@login_required
def contract_download(request, pk, fmt="pdf"):
    """Выгрузка договора: PDF или Word."""
    contract = get_object_or_404(
        Contract.objects.select_related("organization", "counterparty", "template"), pk=pk,
    )
    if not contract.template:
        messages.error(request, "Сначала выберите шаблон договора")
        return redirect("contract_edit", pk=pk)

    if fmt == "docx":
        data = contract_docx(contract)
        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        filename = contract_filename(contract, "docx")
    else:
        data = contract_pdf(contract, request=request)
        content_type = "application/pdf"
        filename = contract_filename(contract, "pdf")

    response = HttpResponse(data, content_type=content_type)
    disposition = "attachment" if fmt == "docx" or request.GET.get("download") else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{quote(filename)}"'
    return response


@require_POST
def contract_delete(request, pk):
    contract = get_object_or_404(Contract, pk=pk)
    counterparty_pk = contract.counterparty_id
    contract.delete()
    messages.info(request, "Договор удалён")
    if request.POST.get("back") == "counterparty":
        return redirect("counterparty_edit", pk=counterparty_pk)
    return redirect("contract_list")


# ---------- Шаблоны договоров ----------

class ContractTemplateListView(RoleRequiredMixin, ListView):
    allowed_roles = EDIT_ROLES
    model = ContractTemplate
    template_name = "partners/contract_template_list.html"
    context_object_name = "templates"


class ContractTemplateEditBase(RoleRequiredMixin):
    allowed_roles = EDIT_ROLES
    model = ContractTemplate
    form_class = ContractTemplateForm
    template_name = "partners/contract_template_form.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["placeholder_groups"] = PLACEHOLDER_GROUPS
        return ctx

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request, "Шаблон сохранён")
        return redirect("contract_template_edit", pk=self.object.pk)


class ContractTemplateCreateView(ContractTemplateEditBase, CreateView):
    pass


class ContractTemplateUpdateView(ContractTemplateEditBase, UpdateView):
    pass


@require_POST
def contract_template_delete(request, pk):
    get_object_or_404(ContractTemplate, pk=pk).delete()
    messages.info(request, "Шаблон удалён")
    return redirect("contract_template_list")


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
