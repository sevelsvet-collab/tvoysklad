"""Списки как в МойСклад: быстрый поиск, панель фильтров, пагинация снизу
и действия с отмеченными строками.

Представление объявляет, по каким полям искать, какие фильтры показать
и какие действия доступны для отмеченных строк. Шаблон выводит всё это
общими блоками partials/_list_toolbar.html и partials/_pagination.html.

Прежние параметры адреса (q, status, kind, type, per_page, page) работают
как раньше — старые ссылки и закладки не ломаются.
"""
from datetime import date
from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse

from .constants import DOC_STATUS_CHOICES
from .pagination import PageSizeMixin


class TextFilter:
    """Текстовое поле: «содержит» без учёта регистра."""

    kind = "text"

    def __init__(self, name, label, field=None, placeholder=""):
        self.name, self.label = name, label
        self.field = field or name
        self.placeholder = placeholder

    def raw(self, params):
        return params.get(self.name, "").strip()

    def apply(self, qs, params):
        value = self.raw(params)
        return qs.filter(**{f"{self.field}__icontains": value}) if value else qs

    def bind(self, params):
        value = self.raw(params)
        return {"f": self, "value": value, "active": bool(value)}


class ChoiceFilter(TextFilter):
    """Выпадающий список. choices — пары (значение, подпись) или функция,
    которая их возвращает (для справочников: организации, склады…)."""

    kind = "select"

    def __init__(self, name, label, field=None, choices=(), empty_label="Все", default="", apply=None):
        super().__init__(name, label, field)
        self._choices = choices
        self.empty_label = empty_label
        self.default = default
        self._apply = apply

    def choices(self):
        items = self._choices() if callable(self._choices) else self._choices
        return [(str(value), label) for value, label in items]

    def raw(self, params):
        return params.get(self.name, self.default).strip()

    def apply(self, qs, params):
        value = self.raw(params)
        if not value:
            return qs
        if self._apply:
            return self._apply(qs, value)
        if value not in dict(self.choices()):
            return qs  # постороннее значение из адресной строки — не фильтруем
        return qs.filter(**{self.field: value})

    def bind(self, params):
        value = self.raw(params)
        return {"f": self, "value": value, "choices": self.choices(),
                "active": value not in ("", self.default)}


class PeriodFilter(TextFilter):
    """Период «с — по»; в панели — быстрые ссылки: вчера, сегодня, неделя, месяц."""

    kind = "period"

    def __init__(self, name="date", label="Период", field="date"):
        super().__init__(name, label, field)

    @staticmethod
    def _date(raw):
        try:
            return date.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    def apply(self, qs, params):
        start = self._date(params.get(f"{self.name}_from", ""))
        end = self._date(params.get(f"{self.name}_to", ""))
        if start:
            qs = qs.filter(**{f"{self.field}__gte": start})
        if end:
            qs = qs.filter(**{f"{self.field}__lte": end})
        return qs

    def bind(self, params):
        start = params.get(f"{self.name}_from", "")
        end = params.get(f"{self.name}_to", "")
        return {"f": self, "from": start, "to": end,
                "active": bool(self._date(start) or self._date(end))}


# ---------- Справочники для фильтров ----------

def organizations():
    from .models import Organization

    return Organization.objects.filter(is_active=True).values_list("pk", "name")


def warehouses():
    from .models import Warehouse

    return Warehouse.objects.filter(is_active=True).values_list("pk", "name")


def document_filters(partner_field=None, partner_label="Контрагент", status_choices=DOC_STATUS_CHOICES,
                     warehouse_field="warehouse", warehouse_label="Склад"):
    """Типовой набор фильтров документа: период, контрагент, статус,
    организация, склад."""
    filters = [PeriodFilter()]
    if partner_field:
        filters.append(TextFilter("partner", partner_label, f"{partner_field}__name", placeholder="Наименование"))
    filters.append(ChoiceFilter("status", "Статус", choices=status_choices))
    filters.append(ChoiceFilter("organization", "Организация", "organization_id", organizations))
    if warehouse_field:
        filters.append(ChoiceFilter("warehouse", warehouse_label, f"{warehouse_field}_id", warehouses))
    return filters


def delete_action(url_name, what="документы"):
    """Действие «Удалить» для отмеченных документов (с подтверждением)."""
    return {
        "label": "Удалить", "url": url_name, "icon": "bi-trash", "danger": True, "ok": "Удалить",
        "confirm": f"Удалить выбранные {what}? Проведённые сначала будут сняты с проведения. "
                   "Отменить удаление нельзя.",
    }


class FilteredListMixin(PageSizeMixin):
    """Поиск, фильтры и массовые действия для ListView.

    get_queryset представления возвращает self.filter_queryset(базовый_qs).
    """

    search_fields = ()
    list_filters = ()
    bulk_actions = ()        # [{"label", "url" (имя маршрута), "kwargs", "confirm", "danger", "icon"}]
    keep_params = ()         # параметры, которые не сбрасывает «Очистить» (например, группа товаров)
    search_placeholder = "Поиск"

    def get_list_filters(self):
        return list(self.list_filters)

    def filter_queryset(self, qs):
        params = self.request.GET
        q = params.get("q", "").strip()
        if q and self.search_fields:
            cond = Q()
            for field in self.search_fields:
                cond |= Q(**{f"{field}__icontains": q})
            qs = qs.filter(cond)
        for flt in self.get_list_filters():
            qs = flt.apply(qs, params)
        return qs

    def get_bulk_actions(self):
        return [{**a, "url": reverse(a["url"], kwargs=a.get("kwargs"))} for a in self.bulk_actions]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        params = self.request.GET
        bound = [flt.bind(params) for flt in self.get_list_filters()]
        ctx.update({
            "list_filters": bound,
            "filters_active": sum(1 for b in bound if b["active"]),
            "bulk_actions": self.get_bulk_actions(),
            "search_placeholder": self.search_placeholder,
            "keep_query": urlencode({k: params[k] for k in self.keep_params if params.get(k)}),
        })
        return ctx
