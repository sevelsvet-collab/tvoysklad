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

    @property
    def is_posted(self):
        return self.status == DOC_POSTED

    @transaction.atomic
    def post(self, allow_negative=None):
        from apps.inventory import services

        services.clear_movements(self.DOC_TYPE, self.pk)  # чистый лист (идемпотентно)
        specs = self.build_specs()
        services.validate_stock(specs, allow_negative)
        services.create_movements(self.DOC_TYPE, self.pk, self.number, self.date, specs)
        self.status = DOC_POSTED
        self.save(update_fields=["status"])

    @transaction.atomic
    def unpost(self):
        from apps.inventory import services

        services.clear_movements(self.DOC_TYPE, self.pk)
        self.status = DOC_DRAFT
        self.save(update_fields=["status"])
