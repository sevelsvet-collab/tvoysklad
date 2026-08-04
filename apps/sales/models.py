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

CENTS = Decimal("0.01")


class Invoice(models.Model):
    """Счёт покупателю — основа цепочки продажи. Остатки не двигает.

    Статус проведения: черновик → выставлен. Оплата подтягивается из модуля 5
    (paid_amount), отгрузка — из связанных проведённых отгрузок.
    """

    STATUS_DRAFT = "draft"
    STATUS_ISSUED = "issued"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_ISSUED, "Выставлен"),
    ]

    DOC_TYPE = "invoice"

    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    warehouse = models.ForeignKey("core.Warehouse", on_delete=models.PROTECT, verbose_name="Со склада")
    customer = models.ForeignKey(
        "partners.Counterparty", on_delete=models.PROTECT, verbose_name="Покупатель", related_name="invoices",
    )
    contract = models.ForeignKey(
        "partners.Contract", on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Договор",
    )
    due_date = models.DateField("Планируемая дата оплаты", null=True, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    paid_amount = models.DecimalField("Оплачено", max_digits=15, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Счёт покупателю"
        verbose_name_plural = "Счета покупателям"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Счёт № {self.number} от {self.date:%d.%m.%Y}"

    def save(self, *args, **kwargs):
        if not self.number and self.organization_id:
            self.number = DocumentNumber.next_number(self.organization, self.DOC_TYPE)
        super().save(*args, **kwargs)

    # Совместимость с LineDocumentMixin/тулбаром (duck-typing вместо PostableMixin — остатки не трогаем)
    @property
    def is_posted(self):
        return self.status == self.STATUS_ISSUED

    def post(self, allow_negative=None):
        self.status = self.STATUS_ISSUED
        self.save(update_fields=["status"])

    def unpost(self):
        self.status = self.STATUS_DRAFT
        self.save(update_fields=["status"])

    @property
    def total(self):
        return sum((line.total for line in self.lines.all()), Decimal("0"))

    @property
    def vat_total(self):
        return sum((line.vat_total for line in self.lines.all()), Decimal("0"))

    @property
    def shipped_amount(self):
        total = Decimal("0")
        for shipment in self.shipments.filter(status="posted"):
            total += shipment.total
        return total

    @property
    def payment_status(self):
        if self.paid_amount <= 0:
            return "not_paid"
        if self.paid_amount < self.total:
            return "partial"
        return "paid"

    @property
    def payment_status_display(self):
        return {"not_paid": "Не оплачен", "partial": "Частично", "paid": "Оплачен"}[self.payment_status]

    @property
    def related_docs(self):
        from django.urls import reverse
        docs = []
        for s in self.shipments.all():
            docs.append({
                "type_label": "Отгрузка", "icon": "bi-truck",
                "number": s.number, "date": s.date, "amount": s.total,
                "is_posted": s.is_posted, "url": reverse("shipment_edit", args=[s.pk]),
            })
        for p in self.payments.all():
            docs.append({
                "type_label": p.get_kind_display(), "icon": "bi-cash-coin",
                "number": p.number, "date": p.date, "amount": p.amount,
                "is_posted": p.is_posted, "url": reverse("payment_edit", args=[p.pk]),
            })
        return docs


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, verbose_name="Товар")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3, default=1)
    price = models.DecimalField("Цена", max_digits=15, decimal_places=2, default=0)
    discount = models.DecimalField("Скидка, %", max_digits=6, decimal_places=3, default=0)
    vat_rate = models.CharField("Ставка НДС", max_length=8, choices=VAT_CHOICES, default=VAT_20)

    class Meta:
        verbose_name = "Строка счёта"
        verbose_name_plural = "Строки счёта"

    def __str__(self):
        return f"{self.product} × {self.quantity}"

    @property
    def total(self):
        return line_total(self.quantity, self.price, self.discount)

    @property
    def vat_total(self):
        return vat_amount(self.total, self.vat_rate)


class Shipment(PostableMixin, models.Model):
    """Отгрузка — выдача товара покупателю. Списывает остатки, фиксирует себестоимость и прибыль."""

    DOC_TYPE = "shipment"

    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    warehouse = models.ForeignKey("core.Warehouse", on_delete=models.PROTECT, verbose_name="Со склада")
    customer = models.ForeignKey(
        "partners.Counterparty", on_delete=models.PROTECT, verbose_name="Покупатель", related_name="shipments",
    )
    invoice = models.ForeignKey(
        Invoice, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Счёт-основание", related_name="shipments",
    )
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField("Статус", max_length=16, choices=DOC_STATUS_CHOICES, default=DOC_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Отгрузка"
        verbose_name_plural = "Отгрузки"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Отгрузка № {self.number} от {self.date:%d.%m.%Y}"

    def save(self, *args, **kwargs):
        if not self.number and self.organization_id:
            self.number = DocumentNumber.next_number(self.organization, self.DOC_TYPE)
        super().save(*args, **kwargs)

    def build_specs(self):
        from apps.inventory import services

        specs = []
        for line in self.lines.all():
            avg = services.current_avg_cost(line.product_id, self.warehouse_id)
            if line.cost_price != avg:
                line.cost_price = avg
                line.save(update_fields=["cost_price"])
            specs.append({
                "product_id": line.product_id, "warehouse_id": self.warehouse_id,
                "quantity": -line.quantity, "cost": avg,
            })
        return specs

    @property
    def total(self):
        return sum((line.total for line in self.lines.all()), Decimal("0"))

    @property
    def cost_total(self):
        return sum((line.cost_total for line in self.lines.all()), Decimal("0"))

    @property
    def profit_total(self):
        return self.total - self.cost_total

    @property
    def related_docs(self):
        from django.urls import reverse
        docs = []
        if self.invoice_id:
            i = self.invoice
            docs.append({
                "type_label": "Счёт покупателю", "icon": "bi-file-text",
                "number": i.number, "date": i.date, "amount": i.total,
                "is_posted": i.is_posted, "url": reverse("invoice_edit", args=[i.pk]),
                "is_source": True,
            })
        for r in self.returns.all():
            docs.append({
                "type_label": "Возврат покупателя", "icon": "bi-arrow-return-left",
                "number": r.number, "date": r.date, "amount": r.total,
                "is_posted": r.is_posted, "url": reverse("customer_return_edit", args=[r.pk]),
            })
        return docs


class ShipmentLine(models.Model):
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, verbose_name="Товар")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3, default=1)
    price = models.DecimalField("Цена", max_digits=15, decimal_places=2, default=0)
    discount = models.DecimalField("Скидка, %", max_digits=6, decimal_places=3, default=0)
    vat_rate = models.CharField("Ставка НДС", max_length=8, choices=VAT_CHOICES, default=VAT_20)
    cost_price = models.DecimalField("Себестоимость ед.", max_digits=15, decimal_places=2, default=0)

    class Meta:
        verbose_name = "Строка отгрузки"
        verbose_name_plural = "Строки отгрузки"

    def __str__(self):
        return f"{self.product} × {self.quantity}"

    @property
    def total(self):
        return line_total(self.quantity, self.price, self.discount)

    @property
    def cost_total(self):
        return (self.quantity * self.cost_price).quantize(CENTS)

    @property
    def profit(self):
        return self.total - self.cost_total

    @property
    def vat_total(self):
        return vat_amount(self.total, self.vat_rate)


class CustomerReturn(PostableMixin, models.Model):
    """Возврат от покупателя — товар возвращается на склад (приход)."""

    DOC_TYPE = "customer_return"

    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    warehouse = models.ForeignKey("core.Warehouse", on_delete=models.PROTECT, verbose_name="На склад")
    customer = models.ForeignKey(
        "partners.Counterparty", on_delete=models.PROTECT, verbose_name="Покупатель", related_name="customer_returns",
    )
    shipment = models.ForeignKey(
        Shipment, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name="Отгрузка-основание", related_name="returns",
    )
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField("Статус", max_length=16, choices=DOC_STATUS_CHOICES, default=DOC_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Возврат покупателя"
        verbose_name_plural = "Возвраты покупателей"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Возврат покупателя № {self.number} от {self.date:%d.%m.%Y}"

    def save(self, *args, **kwargs):
        if not self.number and self.organization_id:
            self.number = DocumentNumber.next_number(self.organization, self.DOC_TYPE)
        super().save(*args, **kwargs)

    def build_specs(self):
        from apps.inventory import services

        specs = []
        for line in self.lines.all():
            # Товар возвращается в запас по текущей средней (при нулевом остатке — по закупочной)
            cost = services.current_avg_cost(line.product_id, self.warehouse_id) or line.product.purchase_price
            specs.append({
                "product_id": line.product_id, "warehouse_id": self.warehouse_id,
                "quantity": line.quantity, "cost": cost,
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
        if self.shipment_id:
            s = self.shipment
            docs.append({
                "type_label": "Отгрузка", "icon": "bi-truck",
                "number": s.number, "date": s.date, "amount": s.total,
                "is_posted": s.is_posted, "url": reverse("shipment_edit", args=[s.pk]),
                "is_source": True,
            })
        return docs


class CustomerReturnLine(models.Model):
    document = models.ForeignKey(CustomerReturn, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, verbose_name="Товар")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3, default=1)
    price = models.DecimalField("Цена", max_digits=15, decimal_places=2, default=0)
    discount = models.DecimalField("Скидка, %", max_digits=6, decimal_places=3, default=0)
    vat_rate = models.CharField("Ставка НДС", max_length=8, choices=VAT_CHOICES, default=VAT_20)

    class Meta:
        verbose_name = "Строка возврата покупателя"
        verbose_name_plural = "Строки возврата покупателя"

    def __str__(self):
        return f"{self.product} × {self.quantity}"

    @property
    def total(self):
        return line_total(self.quantity, self.price, self.discount)

    @property
    def vat_total(self):
        return vat_amount(self.total, self.vat_rate)
