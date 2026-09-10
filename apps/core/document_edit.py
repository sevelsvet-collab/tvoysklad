"""Базовый CBV для документов с табличной частью (строками).

Обрабатывает форму-шапку + inline-формсет строк, кнопки «Сохранить» и
«Сохранить и провести». Товары в строках выбираются живым поиском
(apps.catalog.api), поэтому весь каталог в шаблон не передаётся.
"""
import json

from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect

from apps.inventory.services import StockError

from . import submit_once


class LineDocumentMixin:
    formset_class = None
    formset_prefix = "lines"

    def get_formset(self, data=None):
        return self.formset_class(data, prefix=self.formset_prefix, instance=self.object)

    def get_initial(self):
        initial = super().get_initial()
        # Предзаполнение контрагента при создании из карточки: ?partner=<pk>
        partner = self.request.GET.get("partner")
        if partner:
            fields = self.get_form_class().base_fields
            for name in ("customer", "supplier", "counterparty"):
                if name in fields:
                    initial[name] = partner
                    break
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if "formset" not in ctx:
            ctx["formset"] = self.get_formset(self.request.POST or None)
        ctx["submit_token"] = submit_once.new_token()
        # НДС строк берётся из организации документа. Карту ставок всех фирм
        # отдаём в JS — при смене организации ставки в строках пересчитываются.
        from apps.core.models import Organization

        org = self._document_organization() or Organization.get_default()
        ctx["default_vat_rate"] = org.line_vat_default if org else "20"
        ctx["org_vat_json"] = json.dumps({
            str(o.pk): {"charges": o.charges_vat, "rate": o.default_vat_rate}
            for o in Organization.objects.filter(is_active=True)
        })
        return ctx

    def _document_organization(self):
        obj = getattr(self, "object", None)
        if obj is not None and getattr(obj, "organization_id", None):
            return obj.organization
        return None

    def _apply_organization_vat(self, formset):
        """Неплательщик НДС не может выставить НДС: страхуем на сервере,
        даже если в строке вручную оставили 20%."""
        org = self._document_organization()
        if org is None or org.charges_vat:
            return
        field_names = {f.name for f in formset.model._meta.fields}
        if "vat_rate" in field_names:
            formset.model.objects.filter(**{formset.fk.name: self.object}).update(vat_rate="none")

    @transaction.atomic
    def form_valid(self, form):
        self.object = form.save(commit=False)
        formset = self.get_formset(self.request.POST)
        formset.instance = self.object
        if not formset.is_valid():
            return self.render_to_response(self.get_context_data(form=form, formset=formset))
        creating = self.object.pk is None
        if creating:
            duplicate = submit_once.claim(self.request)
            if duplicate:
                return duplicate   # повторная отправка той же формы — документ уже создан
        self.object.save()
        formset.instance = self.object
        formset.save()
        self._apply_organization_vat(formset)

        if self.request.POST.get("action") == "save_post":
            try:
                self.object.post()
                messages.success(self.request, f"{self.object} — проведён")
            except StockError as exc:
                messages.error(self.request, f"Сохранено, но не проведено: {exc}")
        else:
            messages.success(self.request, f"{self.object} — сохранён")
        url = self.get_edit_url()
        if creating:
            submit_once.remember(self.request, url)
        return redirect(url)

    def get_edit_url(self):
        return self.request.path
