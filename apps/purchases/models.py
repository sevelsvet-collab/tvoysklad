from decimal import Decimal

from django.db import models
from django.utils import timezone

from apps.core.constants import (
    DOC_DRAFT,
    DOC_STATUS_CHOICES,
    VAT_20,
    VAT_CHOICES,
    line_total,
    vat_amount,
)
from apps.core.models import DocumentNumber
from apps.inventory.posting import PostableMixin


class Receipt(PostableMixin, models.Model):
    """Приёмка — поступление товара от поставщика на склад."""

    DOC_TYPE = "receipt"

    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    warehouse = models.ForeignKey("core.Warehouse", on_delete=models.PROTECT, verbose_name="Склад")
    supplier = models.ForeignKey(
        "partners.Counterparty", on_delete=models.PROTECT, verbose_name="Поставщик", related_name="receipts",
    )
    contract = models.ForeignKey(
        "partners.Contract", on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Договор",
    )
    supplier_invoice = models.CharField("Номер счёта поставщика", max_length=64, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField("Статус", max_length=16, choices=DOC_STATUS_CHOICES, default=DOC_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Приёмка"
        verbose_name_plural = "Приёмки"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Приёмка № {self.number} от {self.date:%d.%m.%Y}"

    def save(self, *args, **kwargs):
        if not self.number and self.organization_id:
            self.number = DocumentNumber.next_number(self.organization, self.DOC_TYPE)
        super().save(*args, **kwargs)

    def build_specs(self):
        return [
            {
                "product_id": line.product_id, "warehouse_id": self.warehouse_id,
                "quantity": line.quantity, "cost": line.price,
            }
            for line in self.lines.all()
        ]

    @property
    def total(self):
        return sum((line.total for line in self.lines.all()), Decimal("0"))

    @property
    def vat_total(self):
        return sum((line.vat_total for line in self.lines.all()), Decimal("0"))

    @property
    def related_docs(self):
        from django.urls import reverse
        docs = []
        for r in self.returns.all():
            docs.append({
                "type_label": "Возврат поставщику", "icon": "bi-arrow-return-right",
                "number": r.number, "date": r.date, "amount": r.total,
                "is_posted": r.is_posted, "url": reverse("supplier_return_edit", args=[r.pk]),
            })
        return docs


class ReceiptLine(models.Model):
    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, verbose_name="Товар")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3, default=1)
    price = models.DecimalField("Цена", max_digits=15, decimal_places=2, default=0)
    discount = models.DecimalField("Скидка, %", max_digits=6, decimal_places=3, default=0)
    vat_rate = models.CharField("Ставка НДС", max_length=8, choices=VAT_CHOICES, default=VAT_20)

    class Meta:
        verbose_name = "Строка приёмки"
        verbose_name_plural = "Строки приёмки"

    def __str__(self):
        return f"{self.product} × {self.quantity}"

    @property
    def total(self):
        return line_total(self.quantity, self.price, self.discount)

    @property
    def vat_total(self):
        return vat_amount(self.total, self.vat_rate)


class SupplierReturn(PostableMixin, models.Model):
    """Возврат поставщику — товар уходит со склада обратно поставщику (расход)."""

    DOC_TYPE = "supplier_return"

    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    warehouse = models.ForeignKey("core.Warehouse", on_delete=models.PROTECT, verbose_name="Со склада")
    supplier = models.ForeignKey(
        "partners.Counterparty", on_delete=models.PROTECT, verbose_name="Поставщик", related_name="supplier_returns",
    )
    receipt = models.ForeignKey(
        Receipt, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name="Приёмка-основание", related_name="returns",
    )
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField("Статус", max_length=16, choices=DOC_STATUS_CHOICES, default=DOC_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Возврат поставщику"
        verbose_name_plural = "Возвраты поставщикам"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Возврат поставщику № {self.number} от {self.date:%d.%m.%Y}"

    def save(self, *args, **kwargs):
        if not self.number and self.organization_id:
            self.number = DocumentNumber.next_number(self.organization, self.DOC_TYPE)
        super().save(*args, **kwargs)

    def build_specs(self):
        from apps.inventory import services

        specs = []
        for line in self.lines.all():
            avg = services.current_avg_cost(line.product_id, self.warehouse_id)
            specs.append({
                "product_id": line.product_id, "warehouse_id": self.warehouse_id,
                "quantity": -line.quantity, "cost": avg,
            })
        return specs

    @property
    def total(self):
        return sum((line.total for line in self.lines.all()), Decimal("0"))

    @property
    def vat_total(self):
        return sum((line.vat_total for line in self.lines.all()), Decimal("0"))

    @property
    def related_docs(self):
        from django.urls import reverse
        docs = []
        if self.receipt_id:
            r = self.receipt
            docs.append({
                "type_label": "Приёмка", "icon": "bi-box-arrow-in-down",
                "number": r.number, "date": r.date, "amount": r.total,
                "is_posted": r.is_posted, "url": reverse("receipt_edit", args=[r.pk]),
                "is_source": True,
            })
        return docs


class SupplierReturnLine(models.Model):
    document = models.ForeignKey(SupplierReturn, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, verbose_name="Товар")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3, default=1)
    price = models.DecimalField("Цена", max_digits=15, decimal_places=2, default=0)
    discount = models.DecimalField("Скидка, %", max_digits=6, decimal_places=3, default=0)
    vat_rate = models.CharField("Ставка НДС", max_length=8, choices=VAT_CHOICES, default=VAT_20)

    class Meta:
        verbose_name = "Строка возврата поставщику"
        verbose_name_plural = "Строки возврата поставщику"

    def __str__(self):
        return f"{self.product} × {self.quantity}"

    @property
    def total(self):
        return line_total(self.quantity, self.price, self.discount)

    @property
    def vat_total(self):
        return vat_amount(self.total, self.vat_rate)
