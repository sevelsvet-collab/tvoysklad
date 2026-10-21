"""Базовый CBV для документов с табличной частью (строками).

Обрабатывает форму-шапку + inline-формсет строк, кнопки «Сохранить» и
«Сохранить и провести». Товары в строках выбираются живым поиском
(apps.catalog.api), поэтому весь каталог в шаблон не передаётся.
"""
import json

from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect

from apps.inventory.posting import PostableMixin
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

    def form_valid(self, form):
        self.object = form.save(commit=False)
        formset = self.get_formset(self.request.POST)
        formset.instance = self.object
        if not formset.is_valid():
            return self.render_to_response(self.get_context_data(form=form, formset=formset))
        try:
            with transaction.atomic():
                return self._save_document(form, formset)
        except StockError as exc:
            # Проведённый документ нельзя изменить так: откатилось всё — шапка,
            # строки, движения. Форма остаётся с введёнными значениями.
            messages.error(self.request, f"Изменения не сохранены: {exc}")
            self.object = self.get_object()
            return self.render_to_response(self.get_context_data(form=form, formset=formset))

    def _save_document(self, form, formset):
        """Сохраняет шапку и строки. Проведённый складской документ сразу
        перепроводится: движения, остатки и серийные номера обновляются в той же
        транзакции, статус остаётся «проведён». StockError отсюда означает
        полный откат (см. form_valid)."""
        creating = self.object.pk is None
        was_posted = False
        if creating:
            duplicate = submit_once.claim(self.request)
            if duplicate:
                return duplicate   # повторная отправка той же формы — документ уже создан
            self._write(formset)
        elif isinstance(self.object, PostableMixin):
            # Тот же механизм, что у change_organization(): документ блокируется,
            # статус — из базы (форма могла быть открыта давно); у проведённого
            # товары блокируются до изменения строк, после записи он
            # перепроводится (обе истории при смене организации). Не получилось —
            # StockError и откат всего сохранения.
            with self.object.editing(products=self._line_products(formset)) as was_posted:
                self._write(formset)
        else:
            self._write(formset)

        if was_posted:
            messages.success(self.request, f"{self.object} — сохранён и перепроведён")
        elif self.request.POST.get("action") == "save_post":
            try:
                self.object.post()
                messages.success(self.request, f"{self.object} — проведён")
            except StockError as exc:
                # черновик сохранён, проведение откатилось своей вложенной транзакцией
                messages.error(self.request, f"Сохранено, но не проведено: {exc}")
        else:
            messages.success(self.request, f"{self.object} — сохранён")
        url = self.get_edit_url()
        if creating:
            submit_once.remember(self.request, url)
        return redirect(url)

    def _write(self, formset):
        self.object.save()
        formset.instance = self.object
        formset.save()
        self._apply_organization_vat(formset)

    def _line_products(self, formset):
        """Товары, которые форма запишет в строки. Их, как и товары строк и
        движений документа, блокирует editing() до изменения строк: пересчёт
        себестоимости, запущенный другой операцией, обновляет строки этого
        документа под блокировкой товара. Если бы форма сначала меняла строки,
        а товар блокировала потом, две операции ждали бы друг друга.
        """
        products = set()
        for line_form in formset.forms:
            product = getattr(line_form, "cleaned_data", {}).get("product")
            if product is not None:
                products.add(product.pk)
        return products

    def get_edit_url(self):
        return self.request.path
