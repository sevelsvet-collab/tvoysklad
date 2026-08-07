"""Базовый CBV для документов с табличной частью (строками).

Обрабатывает форму-шапку + inline-формсет строк, кнопки «Сохранить» и
«Сохранить и провести». Товары в строках выбираются живым поиском
(apps.catalog.api), поэтому весь каталог в шаблон не передаётся.
"""
from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect

from apps.inventory.services import StockError


class LineDocumentMixin:
    formset_class = None
    formset_prefix = "lines"

    def get_formset(self, data=None):
        return self.formset_class(data, prefix=self.formset_prefix, instance=self.object)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if "formset" not in ctx:
            ctx["formset"] = self.get_formset(self.request.POST or None)
        # НДС по умолчанию из организации документа
        obj = getattr(self, "object", None)
        org = None
        if obj and obj.pk and getattr(obj, "organization_id", None):
            org = obj.organization
        if not org:
            from apps.core.models import Organization
            org = Organization.get_default()
        ctx["default_vat_rate"] = org.default_vat_rate if org else "20"
        return ctx

    @transaction.atomic
    def form_valid(self, form):
        self.object = form.save(commit=False)
        formset = self.get_formset(self.request.POST)
        formset.instance = self.object
        if not formset.is_valid():
            return self.render_to_response(self.get_context_data(form=form, formset=formset))
        self.object.save()
        formset.instance = self.object
        formset.save()

        if self.request.POST.get("action") == "save_post":
            try:
                self.object.post()
                messages.success(self.request, f"{self.object} — проведён")
            except StockError as exc:
                messages.error(self.request, f"Сохранено, но не проведено: {exc}")
        else:
            messages.success(self.request, f"{self.object} — сохранён")
        return redirect(self.get_edit_url())

    def get_edit_url(self):
        return self.request.path
