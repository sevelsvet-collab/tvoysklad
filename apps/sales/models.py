from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection, models, transaction
from django.utils import timezone

from apps.core.constants import (
    DOC_DRAFT,
    DOC_STATUS_CHOICES,
    VAT_20,
    VAT_CHOICES,
    line_total,
    vat_amount,
)
from apps.core.models import assign_document_number, current_time
from apps.inventory.posting import DocumentLineMixin, DocumentLineQuerySet, PostableMixin, PostableQuerySet

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
    time = models.TimeField("Время", default=current_time)
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
        ordering = ["-date", "-time", "-id"]

    def __str__(self):
        return f"Счёт № {self.number} от {self.date:%d.%m.%Y}"

    def _changed_parties(self, lock=False, fields=None):
        """Какие из сторон счёта (организация, покупатель) отличаются от
        сохранённых в базе. fields — какие поля будут записаны (None — все).

        lock=True (при сохранении, внутри транзакции) — строка счёта
        блокируется до проверки платежей: сохранение платежа блокирует ту же
        строку, поэтому одна операция дождётся другой и увидит её результат.
        """
        if not self.pk or self._state.adding:
            return []
        if fields is not None and not ({"organization", "customer"} & set(fields)):
            return []
        rows = Invoice.objects.filter(pk=self.pk)
        if lock and connection.features.has_select_for_update:
            rows = rows.select_for_update()
        stored = rows.values("organization_id", "customer_id").first()
        if not stored:
            return []
        changed = []
        if stored["organization_id"] != self.organization_id and (fields is None or "organization" in fields):
            changed.append("organization")
        if stored["customer_id"] != self.customer_id and (fields is None or "customer" in fields):
            changed.append("customer")
        return changed

    def _linked_payments_error(self, lock=False, fields=None):
        """Организацию и покупателя счёта нельзя менять, пока с ним связаны
        платежи (и проведённые, и черновики): платёж остался бы привязан
        к счёту другой организации или другого покупателя. Сначала платежи
        нужно отвязать от счёта или удалить."""
        changed = self._changed_parties(lock=lock, fields=fields)
        if not changed:
            return None
        payments = list(self.payments.order_by("pk")[:5])
        if not payments:
            return None
        names = ", ".join(f"№ {p.number} от {p.date:%d.%m.%Y}" for p in payments)
        text = (
            f"Со счётом связаны платежи ({names}). Сначала отвяжите их от счёта или удалите — "
            "иначе платёж окажется привязан к счёту другой организации или другого покупателя"
        )
        return {name: text for name in changed}

    def clean(self):
        errors = self._linked_payments_error()
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        assign_document_number(self, self.DOC_TYPE)
        with transaction.atomic():
            errors = self._linked_payments_error(lock=True, fields=kwargs.get("update_fields"))
            if errors:
                raise ValidationError(errors)
            if self.pk and not self._state.adding and kwargs.get("update_fields") is None:
                # «Оплачено» ведёт только модуль платежей (finance.services): обычное
                # сохранение счёта не должно затирать его значением, прочитанным раньше.
                kwargs["update_fields"] = [
                    f.name for f in self._meta.concrete_fields if not f.primary_key and f.name != "paid_amount"
                ]
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
    serial_numbers = models.TextField("Серийные номера", blank=True, help_text="По одному номеру в строке")
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
    time = models.TimeField("Время", default=current_time)
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

    objects = PostableQuerySet.as_manager()

    class Meta:
        verbose_name = "Отгрузка"
        verbose_name_plural = "Отгрузки"
        ordering = ["-date", "-time", "-id"]

    def __str__(self):
        return f"Отгрузка № {self.number} от {self.date:%d.%m.%Y}"

    def assign_number(self):
        assign_document_number(self, self.DOC_TYPE)

    def build_specs(self):
        from apps.inventory import services

        # Себестоимость (движения и ShipmentLine.cost_price) проставляет costing:
        # по средней на складе перед отгрузкой в хронологии документов.
        specs = []
        for line in self.lines.order_by("pk"):
            if line.product.is_service:  # услуги не имеют складского учёта
                continue
            specs.append({
                "product_id": line.product_id, "warehouse_id": self.warehouse_id,
                "quantity": -line.quantity, "cost": services.COST_BY_HISTORY, "line_id": line.pk,
            })
        return specs

    def get_allow_negative(self):
        return self.organization.allow_negative_stock

    @property
    def total(self):
        return sum((line.total for line in self.lines.all()), Decimal("0"))

    @property
    def cost_total(self):
        return sum((line.cost_total for line in self.lines.all()), Decimal("0"))

    @property
    def cost_open_qty(self):
        """Сколько единиц отгрузки ещё без себестоимости (продано раньше поступления)."""
        return sum((line.cost_open_qty for line in self.lines.all()), Decimal("0"))

    @property
    def cost_is_preliminary(self):
        return self.cost_open_qty > 0

    def deficit_covers(self):
        """Чем покрыта себестоимость единиц, проданных раньше поступления."""
        from apps.inventory.models import DeficitCover

        return (DeficitCover.objects.filter(issue__doc_type=self.DOC_TYPE, issue__doc_id=self.pk)
                .select_related("cover", "product").order_by("cover__date", "cover__time", "pk"))

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


class ShipmentLine(DocumentLineMixin, models.Model):
    DOCUMENT_FIELD = "shipment"
    # себестоимость строки проставляет пересчёт (costing) — это не реквизиты строки
    DERIVED_FIELDS = ("cost_price", "cost_open_qty", "cost_amount")

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, verbose_name="Товар")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3, default=1)
    serial_numbers = models.TextField("Серийные номера", blank=True, help_text="По одному номеру в строке")
    price = models.DecimalField("Цена", max_digits=15, decimal_places=2, default=0)
    discount = models.DecimalField("Скидка, %", max_digits=6, decimal_places=3, default=0)
    vat_rate = models.CharField("Ставка НДС", max_length=8, choices=VAT_CHOICES, default=VAT_20)
    # cost_amount — себестоимость строки в копейках (учётная величина, её
    # проставляет costing при проведении); cost_price — цена единицы для показа.
    cost_price = models.DecimalField("Себестоимость ед.", max_digits=15, decimal_places=2, default=0)
    # Продано раньше поступления: столько единиц ещё без себестоимости; пока
    # их больше нуля, себестоимость и прибыль строки предварительные.
    cost_open_qty = models.DecimalField("Себестоимость не определена, кол-во", max_digits=15,
                                        decimal_places=3, default=0, editable=False)
    cost_amount = models.DecimalField("Себестоимость строки", max_digits=18, decimal_places=2,
                                      null=True, blank=True, editable=False)

    objects = DocumentLineQuerySet.as_manager()

    class Meta:
        verbose_name = "Строка отгрузки"
        base_manager_name = "objects"   # связанный менеджер (add) — через защиту строк
        verbose_name_plural = "Строки отгрузки"

    def __str__(self):
        return f"{self.product} × {self.quantity}"

    @property
    def total(self):
        return line_total(self.quantity, self.price, self.discount)

    @property
    def cost_total(self):
        """Себестоимость строки. Пока точная сумма не записана (строка не
        пересчитывалась после обновления) — прежняя оценка по цене единицы."""
        if self.cost_amount is not None:
            return self.cost_amount
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
    # основание — учётный реквизит: меняется только в editing() с перепроведением (posting, правило 12)
    STOCK_FIELDS = ("organization", "warehouse", "date", "time", "shipment")
    BASIS_FIELDS = ("shipment",)

    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    time = models.TimeField("Время", default=current_time)
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

    objects = PostableQuerySet.as_manager()

    class Meta:
        verbose_name = "Возврат покупателя"
        verbose_name_plural = "Возвраты покупателей"
        ordering = ["-date", "-time", "-id"]

    def __str__(self):
        return f"Возврат покупателя № {self.number} от {self.date:%d.%m.%Y}"

    def assign_number(self):
        assign_document_number(self, self.DOC_TYPE)

    def save(self, *args, **kwargs):
        # одна транзакция: отказ в очистке ссылок строк (правило 10) откатывает и шапку
        with transaction.atomic():
            super().save(*args, **kwargs)
            # Сменили основание — ссылки строк на строки прежней отгрузки больше не верны.
            # «shipment» и «shipment_id» в update_fields — одно и то же поле.
            if self._writes(kwargs.get("update_fields"), "shipment"):
                self.lines.filter(shipment_line__isnull=False).exclude(
                    shipment_line__shipment_id=self.shipment_id).update(shipment_line=None)

    def build_specs(self):
        from apps.inventory import services

        # Себестоимость возврата считает costing: по исходной отгрузке, а без
        # основания — по правилу 5 из apps/inventory/costing.py.
        from apps.inventory.costing import FALLBACK_CARD, round_cost

        specs = []
        for line in self.lines.order_by("pk"):
            if line.product.is_service:
                continue
            if self.shipment_id and line.shipment_line_id is None:
                # Строка отгрузки не выбрана, но определяется однозначно —
                # запоминаем её: позже добавленная в отгрузку строка того же
                # товара не сделает возврат неоднозначным. Если по отгрузке
                # уже есть возвраты без строки, строка не назначается.
                source = self._only_shipment_line(line)
                if source is not None:
                    line.shipment_line = source
                    line.save(update_fields=["shipment_line"])
            if line.fallback_cost is None:
                # Первое проведение строки: запоминаем закупочную цену карточки.
                # Дальше пересчёт берёт её отсюда, а не из карточки товара;
                # отмена и повторное проведение значение не меняют.
                line.fallback_cost = round_cost(line.product.purchase_price or 0)
                line.fallback_source = FALLBACK_CARD
                line.save(update_fields=["fallback_cost", "fallback_source"])
            specs.append({
                "product_id": line.product_id, "warehouse_id": self.warehouse_id,
                "quantity": line.quantity, "cost": services.COST_BY_HISTORY, "line_id": line.pk,
            })
        return specs

    def _only_shipment_line(self, line):
        """Строка основания, из которой однозначно возвращается товар, —
        правило return_limits.only_shipment_line. None — выбирает пользователь."""
        from apps.inventory import return_limits

        return return_limits.only_shipment_line(self.shipment, line, document_id=self.pk)

    def get_allow_negative(self):
        return self.organization.allow_negative_stock

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


class CustomerReturnLine(DocumentLineMixin, models.Model):
    DOCUMENT_FIELD = "document"

    document = models.ForeignKey(CustomerReturn, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, verbose_name="Товар")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3, default=1)
    serial_numbers = models.TextField("Серийные номера", blank=True, help_text="По одному номеру в строке")
    price = models.DecimalField("Цена", max_digits=15, decimal_places=2, default=0)
    discount = models.DecimalField("Скидка, %", max_digits=6, decimal_places=3, default=0)
    vat_rate = models.CharField("Ставка НДС", max_length=8, choices=VAT_CHOICES, default=VAT_20)

    # Запасная оценка возврата без основания, когда на складе нет истории
    # остатков: закупочная цена карточки на момент первого проведения
    # (source="card") либо фактическая стоимость старого движения, перенесённая
    # миграцией (source="legacy"). См. apps/inventory/costing.py, правило 5.
    fallback_cost = models.DecimalField(
        "Зафиксированная исходная стоимость", max_digits=15, decimal_places=2,
        null=True, blank=True, editable=False,
    )
    fallback_source = models.CharField("Источник исходной стоимости", max_length=16, blank=True, editable=False)
    # Строка отгрузки-основания, из которой возвращается товар. По ней
    # гасится дефицит и восстанавливается себестоимость (costing). Пустая —
    # строку определяют однозначно (одна строка товара в основании) или
    # возврат неоднозначен (диагностика check_costs).
    shipment_line = models.ForeignKey(
        "sales.ShipmentLine", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="return_lines", verbose_name="Строка отгрузки",
    )

    objects = DocumentLineQuerySet.as_manager()

    class Meta:
        verbose_name = "Строка возврата покупателя"
        base_manager_name = "objects"   # связанный менеджер (add) — через защиту строк
        verbose_name_plural = "Строки возврата покупателя"

    def __str__(self):
        return f"{self.product} × {self.quantity}"

    def save(self, *args, **kwargs):
        # В строке заменили товар — зафиксированная стоимость относилась к прежнему.
        if self.pk and self.fallback_cost is not None and kwargs.get("update_fields") is None:
            stored = type(self).objects.filter(pk=self.pk).values_list("product_id", flat=True).first()
            if stored is not None and stored != self.product_id:
                self.fallback_cost = None
                self.fallback_source = ""
        if self.shipment_line_id and kwargs.get("update_fields") is None:
            # Строка отгрузки с другим товаром к этой строке не относится.
            if self.shipment_line.product_id != self.product_id:
                self.shipment_line = None
        super().save(*args, **kwargs)

    @property
    def total(self):
        return line_total(self.quantity, self.price, self.discount)

    @property
    def vat_total(self):
        return vat_amount(self.total, self.vat_rate)
