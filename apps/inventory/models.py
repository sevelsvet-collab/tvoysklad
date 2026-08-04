from decimal import Decimal

from django.db import models
from django.utils import timezone

from apps.core.constants import DOC_DRAFT, DOC_STATUS_CHOICES
from apps.core.models import DocumentNumber
from apps.inventory.posting import PostableMixin


class StockMovement(models.Model):
    """Строка движения товара по складу. Источник истины для остатков.

    quantity > 0 — приход, quantity < 0 — расход. cost — себестоимость единицы.
    Ссылка на документ хранится как (doc_type, doc_id) — обобщённо, без FK,
    чтобы движения не зависели от конкретных приложений документов.
    """

    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="movements")
    warehouse = models.ForeignKey("core.Warehouse", on_delete=models.PROTECT, related_name="movements")
    quantity = models.DecimalField("Количество (±)", max_digits=15, decimal_places=3)
    cost = models.DecimalField("Себестоимость ед.", max_digits=15, decimal_places=2, default=0)

    doc_type = models.CharField("Тип документа", max_length=32)
    doc_id = models.PositiveIntegerField("ID документа")
    doc_number = models.CharField("Номер документа", max_length=32, blank=True)
    date = models.DateField("Дата документа", default=timezone.localdate)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Движение по складу"
        verbose_name_plural = "Движения по складу"
        indexes = [
            models.Index(fields=["product", "warehouse"]),
            models.Index(fields=["doc_type", "doc_id"]),
            models.Index(fields=["date"]),
        ]
        ordering = ["date", "id"]

    def __str__(self):
        return f"{self.product} {self.quantity:+} @ {self.warehouse}"


class StockBalance(models.Model):
    """Кэш остатка: количество и средневзвешенная себестоимость. Пересчитывается из движений."""

    product = models.ForeignKey("catalog.Product", on_delete=models.CASCADE, related_name="balances")
    warehouse = models.ForeignKey("core.Warehouse", on_delete=models.CASCADE, related_name="balances")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3, default=0)
    avg_cost = models.DecimalField("Средняя себестоимость", max_digits=15, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Остаток"
        verbose_name_plural = "Остатки"
        constraints = [
            models.UniqueConstraint(fields=["product", "warehouse"], name="uniq_stock_balance"),
        ]

    def __str__(self):
        return f"{self.product} @ {self.warehouse}: {self.quantity}"

    @property
    def total_value(self):
        return (self.quantity * self.avg_cost).quantize(Decimal("0.01"))


class Transfer(PostableMixin, models.Model):
    """Перемещение товара между складами."""

    DOC_TYPE = "transfer"

    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    warehouse_from = models.ForeignKey(
        "core.Warehouse", on_delete=models.PROTECT, related_name="transfers_out", verbose_name="Склад-отправитель",
    )
    warehouse_to = models.ForeignKey(
        "core.Warehouse", on_delete=models.PROTECT, related_name="transfers_in", verbose_name="Склад-получатель",
    )
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField("Статус", max_length=16, choices=DOC_STATUS_CHOICES, default=DOC_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Перемещение"
        verbose_name_plural = "Перемещения"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Перемещение № {self.number} от {self.date:%d.%m.%Y}"

    def save(self, *args, **kwargs):
        if not self.number and self.organization_id:
            self.number = DocumentNumber.next_number(self.organization, self.DOC_TYPE)
        super().save(*args, **kwargs)

    def build_specs(self):
        from apps.inventory import services

        specs = []
        for line in self.lines.all():
            avg = services.current_avg_cost(line.product_id, self.warehouse_from_id)
            specs.append({
                "product_id": line.product_id, "warehouse_id": self.warehouse_from_id,
                "quantity": -line.quantity, "cost": avg,
            })
            specs.append({
                "product_id": line.product_id, "warehouse_id": self.warehouse_to_id,
                "quantity": line.quantity, "cost": avg,
            })
        return specs


class TransferLine(models.Model):
    transfer = models.ForeignKey(Transfer, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, verbose_name="Товар")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3, default=1)

    class Meta:
        verbose_name = "Строка перемещения"
        verbose_name_plural = "Строки перемещения"

    def __str__(self):
        return f"{self.product} × {self.quantity}"


class StockAdjustment(PostableMixin, models.Model):
    """Оприходование (income) или списание (expense) — корректировка остатков, инвентаризация."""

    KIND_INCOME = "income"
    KIND_EXPENSE = "expense"
    KIND_CHOICES = [
        (KIND_INCOME, "Оприходование"),
        (KIND_EXPENSE, "Списание"),
    ]

    kind = models.CharField("Вид", max_length=16, choices=KIND_CHOICES)
    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    warehouse = models.ForeignKey("core.Warehouse", on_delete=models.PROTECT, verbose_name="Склад")
    reason = models.CharField("Причина", max_length=255, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField("Статус", max_length=16, choices=DOC_STATUS_CHOICES, default=DOC_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Оприходование/Списание"
        verbose_name_plural = "Оприходования и списания"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.get_kind_display()} № {self.number} от {self.date:%d.%m.%Y}"

    # PostableMixin.post/unpost используют self.DOC_TYPE — отдаём динамически по виду
    DOC_TYPE = property(lambda self: f"adjustment_{self.kind}")

    def save(self, *args, **kwargs):
        if not self.number and self.organization_id:
            self.number = DocumentNumber.next_number(self.organization, f"adjustment_{self.kind}")
        super().save(*args, **kwargs)

    def build_specs(self):
        from apps.inventory import services

        specs = []
        for line in self.lines.all():
            if self.kind == self.KIND_INCOME:
                specs.append({
                    "product_id": line.product_id, "warehouse_id": self.warehouse_id,
                    "quantity": line.quantity, "cost": line.price,
                })
            else:
                avg = services.current_avg_cost(line.product_id, self.warehouse_id)
                specs.append({
                    "product_id": line.product_id, "warehouse_id": self.warehouse_id,
                    "quantity": -line.quantity, "cost": avg,
                })
        return specs


class AdjustmentLine(models.Model):
    adjustment = models.ForeignKey(StockAdjustment, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, verbose_name="Товар")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3, default=1)
    price = models.DecimalField("Цена (для оприходования)", max_digits=15, decimal_places=2, default=0)

    class Meta:
        verbose_name = "Строка оприходования/списания"
        verbose_name_plural = "Строки оприходования/списания"

    def __str__(self):
        return f"{self.product} × {self.quantity}"
