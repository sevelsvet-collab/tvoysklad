"""Миксин проведения документов: черновик ⇄ проведён.

Проведение создаёт движения по складу, отмена — удаляет их.
Документ обязан задать DOC_TYPE и метод build_specs().
"""
from django.db import transaction

from apps.core.constants import DOC_DRAFT, DOC_POSTED


class PostableMixin:
    DOC_TYPE = None  # переопределяется в модели документа

    def build_specs(self):
        """Возвращает список движений: dict(product_id, warehouse_id, quantity, cost)."""
        raise NotImplementedError

    def get_allow_negative(self):
        """Разрешить продажи в минус? Переопределяется в моделях с полем organization."""
        from django.conf import settings
        return getattr(settings, "ALLOW_NEGATIVE_STOCK", False)

    @property
    def is_posted(self):
        return self.status == DOC_POSTED

    @transaction.atomic
    def post(self, allow_negative=None):
        from apps.inventory import serials, services

        services.clear_movements(self.DOC_TYPE, self.pk)  # чистый лист (идемпотентно)
        serials.clear(self)
        specs = self.build_specs()
        if allow_negative is None:
            allow_negative = self.get_allow_negative()
        services.validate_stock(specs, allow_negative)
        services.create_movements(self.DOC_TYPE, self.pk, self.number, self.date, specs)
        serials.apply(self)  # серийные номера: проверка и движение (StockError — откат)
        self.status = DOC_POSTED
        self.save(update_fields=["status"])

    @transaction.atomic
    def unpost(self):
        from apps.inventory import serials, services

        services.clear_movements(self.DOC_TYPE, self.pk)
        serials.clear(self)
        self.status = DOC_DRAFT
        self.save(update_fields=["status"])
