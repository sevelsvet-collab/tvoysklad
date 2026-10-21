import datetime
from decimal import Decimal

from django.db import models
from django.utils import timezone

from apps.core.constants import DOC_DRAFT, DOC_STATUS_CHOICES
from apps.core.models import assign_document_number, current_time
from apps.inventory.posting import DocumentLineMixin, DocumentLineQuerySet, PostableMixin, PostableQuerySet


class StockMovement(models.Model):
    """Строка движения товара по складу. Источник истины для остатков.

    quantity > 0 — приход, quantity < 0 — расход.
    amount — сумма движения в копейках (всегда положительная): на неё меняется
    стоимость остатка. Это учётная величина. cost — цена единицы для показа
    (amount ÷ количество, округлённо); итоги из неё не считаются. У движений,
    созданных до появления amount, она пуста, пока товар не пересчитан.
    Ссылка на документ хранится как (doc_type, doc_id) — обобщённо, без FK,
    чтобы движения не зависели от конкретных приложений документов.

    Место движения в хронологии задают date, time и doc_created_at — копии
    полей документа. Они не зависят от момента проведения, поэтому
    перепроведение не меняет порядок операций (см. costing.ORDER).

    Учёт ведётся по ключу «организация + склад + товар»: склад — место
    хранения, товар на нём принадлежит организации документа. organization —
    копия организации документа-источника. Пустая — только у старых движений,
    чей источник не найден при переносе (требует решения человека,
    manage.py stock_transition).
    """

    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="movements")
    warehouse = models.ForeignKey("core.Warehouse", on_delete=models.PROTECT, related_name="movements")
    organization = models.ForeignKey(
        "core.Organization", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        verbose_name="Организация",
    )
    quantity = models.DecimalField("Количество (±)", max_digits=15, decimal_places=3)
    cost = models.DecimalField("Себестоимость ед.", max_digits=15, decimal_places=2, default=0)
    amount = models.DecimalField("Сумма движения", max_digits=18, decimal_places=2,
                                 null=True, blank=True)

    doc_type = models.CharField("Тип документа", max_length=32)
    doc_id = models.PositiveIntegerField("ID документа")
    doc_number = models.CharField("Номер документа", max_length=32, blank=True)
    date = models.DateField("Дата документа", default=timezone.localdate)
    time = models.TimeField("Время документа", default=datetime.time(0, 0))
    doc_created_at = models.DateTimeField("Документ создан", null=True, blank=True)
    line_id = models.PositiveIntegerField("ID строки документа", null=True, blank=True)
    # Расход сверх положительного остатка: сколько единиц ещё не покрыто
    # поступлениями. Их себестоимость не определена и в amount не входит.
    open_qty = models.DecimalField("Не покрыто поступлением", max_digits=15, decimal_places=3, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Движение по складу"
        verbose_name_plural = "Движения по складу"
        indexes = [
            models.Index(fields=["product", "warehouse"]),
            models.Index(fields=["product", "organization", "warehouse"], name="inventory_s_product_org_wh"),
            models.Index(fields=["doc_type", "doc_id"]),
            models.Index(fields=["date"]),
        ]
        ordering = ["date", "time", "doc_created_at", "doc_type", "doc_id", "id"]

    def __str__(self):
        return f"{self.product} {self.quantity:+} @ {self.warehouse}"


class DeficitCover(models.Model):
    """Как поступление покрыло дефицит расхода: какой расход, каким
    поступлением, сколько единиц и на какую сумму.

    Производная запись: её пишет пересчёт себестоимости (costing) и
    пересоздаёт при каждом изменении истории товара. Нужна, чтобы объяснить,
    из чего сложилась себестоимость отгрузки, проданной раньше поступления.
    """

    product = models.ForeignKey("catalog.Product", on_delete=models.CASCADE, related_name="deficit_covers")
    issue = models.ForeignKey(StockMovement, on_delete=models.CASCADE, related_name="deficit_covers",
                              verbose_name="Расход")
    cover = models.ForeignKey(StockMovement, on_delete=models.CASCADE, related_name="covered_deficits",
                              verbose_name="Покрывающее поступление")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3)
    amount = models.DecimalField("Сумма", max_digits=18, decimal_places=2)
    date = models.DateField("Дата покрытия")

    class Meta:
        verbose_name = "Покрытие дефицита"
        verbose_name_plural = "Покрытия дефицита"
        ordering = ["date", "id"]
        indexes = [models.Index(fields=["product"])]

    def __str__(self):
        return f"{self.issue} ← {self.cover}: {self.quantity} на {self.amount}"

    @property
    def cover_label(self):
        """Покрывающий документ словами: «приёмка № 5 от 03.01.2026 12:00»."""
        from .costing import _doc_label

        return _doc_label(self.cover)


class StockBalance(models.Model):
    """Кэш остатка организации на складе: количество и его стоимость.
    Пересчитывается из движений (costing).

    Ключ — «организация + склад + товар»: на одном складе могут лежать товары
    разных организаций, у каждой своя средняя. value — стоимость остатка в
    копейках, учётная величина: сумма приходов минус сумма расходов. avg_cost —
    средняя для показа (value ÷ количество, округлённо); при количестве ≤ 0
    стоимость 0, средняя не определена (0).
    """

    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, related_name="+",
                                     verbose_name="Организация")
    product = models.ForeignKey("catalog.Product", on_delete=models.CASCADE, related_name="balances")
    warehouse = models.ForeignKey("core.Warehouse", on_delete=models.CASCADE, related_name="balances")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3, default=0)
    avg_cost = models.DecimalField("Средняя себестоимость", max_digits=15, decimal_places=2, default=0)
    value = models.DecimalField("Стоимость остатка", max_digits=18, decimal_places=2, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Остаток"
        verbose_name_plural = "Остатки"
        constraints = [
            models.UniqueConstraint(fields=["organization", "product", "warehouse"], name="uniq_stock_balance_org"),
        ]

    def __str__(self):
        return f"{self.product} @ {self.warehouse} ({self.organization}): {self.quantity}"

    @property
    def total_value(self):
        """Стоимость остатка. Пока точная сумма не записана (остаток не
        пересчитывался после обновления) — прежняя оценка по средней."""
        if self.value is not None:
            return self.value
        return (self.quantity * self.avg_cost).quantize(Decimal("0.01"))


class Transfer(PostableMixin, models.Model):
    """Перемещение товара между складами."""

    DOC_TYPE = "transfer"

    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    time = models.TimeField("Время", default=current_time)
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

    objects = PostableQuerySet.as_manager()

    class Meta:
        verbose_name = "Перемещение"
        verbose_name_plural = "Перемещения"
        ordering = ["-date", "-time", "-id"]

    def __str__(self):
        return f"Перемещение № {self.number} от {self.date:%d.%m.%Y}"

    # оба склада — учётные реквизиты: расход стоит на отправителе, приход — на получателе
    STOCK_FIELDS = ("organization", "warehouse_from", "warehouse_to", "date", "time")

    def movement_warehouse_field(self, incoming):
        return "warehouse_to" if incoming else "warehouse_from"

    def assign_number(self):
        assign_document_number(self, self.DOC_TYPE)

    def build_specs(self):
        from apps.inventory import services

        # Себестоимость расхода и парного прихода считает costing по хронологии.
        specs = []
        for line in self.lines.order_by("pk"):
            specs.append({
                "product_id": line.product_id, "warehouse_id": self.warehouse_from_id,
                "quantity": -line.quantity, "cost": services.COST_BY_HISTORY, "line_id": line.pk,
            })
            specs.append({
                "product_id": line.product_id, "warehouse_id": self.warehouse_to_id,
                "quantity": line.quantity, "cost": services.COST_BY_HISTORY, "line_id": line.pk,
            })
        return specs


class TransferLine(DocumentLineMixin, models.Model):
    DOCUMENT_FIELD = "transfer"

    transfer = models.ForeignKey(Transfer, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, verbose_name="Товар")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3, default=1)
    serial_numbers = models.TextField("Серийные номера", blank=True, help_text="По одному номеру в строке")

    objects = DocumentLineQuerySet.as_manager()

    class Meta:
        verbose_name = "Строка перемещения"
        base_manager_name = "objects"   # связанный менеджер (add) — через защиту строк
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
    time = models.TimeField("Время", default=current_time)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    warehouse = models.ForeignKey("core.Warehouse", on_delete=models.PROTECT, verbose_name="Склад")
    reason = models.CharField("Причина", max_length=255, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField("Статус", max_length=16, choices=DOC_STATUS_CHOICES, default=DOC_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PostableQuerySet.as_manager()

    class Meta:
        verbose_name = "Оприходование/Списание"
        verbose_name_plural = "Оприходования и списания"
        ordering = ["-date", "-time", "-id"]

    def __str__(self):
        return f"{self.get_kind_display()} № {self.number} от {self.date:%d.%m.%Y}"

    # PostableMixin.post/unpost используют self.DOC_TYPE — отдаём динамически по виду
    DOC_TYPE = property(lambda self: f"adjustment_{self.kind}")
    DOC_TYPES = ("adjustment_income", "adjustment_expense")
    # вид задаёт тип движений и серию номеров (posting, правило 11)
    IDENTITY_FIELDS = ("kind",)
    NUMBERING_FIELDS = ("number", "organization", "kind")

    def numbering_scope(self):
        return {"kind": self.kind}

    def assign_number(self):
        assign_document_number(self, f"adjustment_{self.kind}", kind=self.kind)

    def build_specs(self):
        from apps.inventory import services

        specs = []
        for line in self.lines.order_by("pk"):
            if self.kind == self.KIND_INCOME:
                specs.append({     # оценка — по строке документа (costing, правило 1)
                    "product_id": line.product_id, "warehouse_id": self.warehouse_id,
                    "quantity": line.quantity, "cost": services.COST_BY_HISTORY, "line_id": line.pk,
                })
            else:
                specs.append({
                    "product_id": line.product_id, "warehouse_id": self.warehouse_id,
                    "quantity": -line.quantity, "cost": services.COST_BY_HISTORY, "line_id": line.pk,
                })
        return specs


class AdjustmentLine(DocumentLineMixin, models.Model):
    DOCUMENT_FIELD = "adjustment"

    adjustment = models.ForeignKey(StockAdjustment, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, verbose_name="Товар")
    quantity = models.DecimalField("Количество", max_digits=15, decimal_places=3, default=1)
    serial_numbers = models.TextField("Серийные номера", blank=True, help_text="По одному номеру в строке")
    price = models.DecimalField("Цена (для оприходования)", max_digits=15, decimal_places=2, default=0)

    objects = DocumentLineQuerySet.as_manager()

    class Meta:
        verbose_name = "Строка оприходования/списания"
        base_manager_name = "objects"   # связанный менеджер (add) — через защиту строк
        verbose_name_plural = "Строки оприходования/списания"

    def __str__(self):
        return f"{self.product} × {self.quantity}"


class SerialNumber(models.Model):
    """Серийный номер конкретной единицы товара: где она сейчас и чья.

    Состояние пересчитывается из журнала SerialMovement (как остатки из
    StockMovement): последний приход — лежит на складе у организации этого
    прихода, последний расход — продан, возвращён поставщику или списан.
    Номер — физическая единица: пока он лежит на складе, повторно принять
    его нельзя ни одной организации. organization пуста, когда номер не на
    складе или его принадлежность не определена при переносе (решает человек).
    """

    STATUS_IN_STOCK = "in_stock"
    STATUS_SOLD = "sold"
    STATUS_RETURNED = "returned"
    STATUS_WRITTEN_OFF = "written_off"
    STATUS_CHOICES = [
        (STATUS_IN_STOCK, "На складе"),
        (STATUS_SOLD, "Продан"),
        (STATUS_RETURNED, "Возвращён поставщику"),
        (STATUS_WRITTEN_OFF, "Списан"),
    ]

    product = models.ForeignKey(
        "catalog.Product", on_delete=models.CASCADE, related_name="serials", verbose_name="Товар",
    )
    number = models.CharField("Серийный номер", max_length=100, db_index=True)
    warehouse = models.ForeignKey(
        "core.Warehouse", on_delete=models.PROTECT, null=True, blank=True,
        related_name="serials", verbose_name="Склад",
    )
    organization = models.ForeignKey(
        "core.Organization", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        verbose_name="Организация",
    )
    status = models.CharField("Состояние", max_length=16, choices=STATUS_CHOICES, default=STATUS_IN_STOCK)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Серийный номер"
        verbose_name_plural = "Серийные номера"
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(fields=["product", "number"], name="uniq_product_serial"),
        ]

    def __str__(self):
        return f"{self.product} — {self.number}"


class SerialMovement(models.Model):
    """Движение серийного номера: какой документ, когда, на какой склад и
    какой организации (±1). Порядок истории — как у складских движений:
    дата, время, момент создания документа (ORDER)."""

    ORDER = ("date", "time", "doc_created_at", "doc_type", "doc_id", "quantity", "id")

    serial = models.ForeignKey(SerialNumber, on_delete=models.CASCADE, related_name="movements")
    doc_type = models.CharField("Документ", max_length=32)  # model_name документа или «serial_stock»
    doc_id = models.PositiveIntegerField("ID документа")
    doc_number = models.CharField("Номер документа", max_length=32, blank=True)
    date = models.DateField("Дата документа")
    time = models.TimeField("Время документа", default=datetime.time(0, 0))
    doc_created_at = models.DateTimeField("Документ создан", null=True, blank=True)
    organization = models.ForeignKey(
        "core.Organization", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        verbose_name="Организация",
    )
    warehouse = models.ForeignKey(
        "core.Warehouse", on_delete=models.PROTECT, related_name="serial_movements", verbose_name="Склад",
    )
    quantity = models.SmallIntegerField("Приход (+1) / расход (−1)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Движение серийного номера"
        verbose_name_plural = "Движения серийных номеров"
        ordering = ["date", "id"]
        indexes = [models.Index(fields=["doc_type", "doc_id"])]



class StockLedgerState(models.Model):
    """Явный статус перехода складского учёта на ключ «организация + склад +
    товар» (одна строка, id 1).

    После миграций статус «ожидает пересчёта», если в базе есть движения:
    остатки, себестоимость и покрытия ещё посчитаны по старому ключу или не
    построены. Пока статус не «готово», складские операции отклоняются, а
    отчёты показывают предупреждение (apps/inventory/transition.py).
    «Готово» ставит только полный успешный пересчёт: manage.py
    stock_transition --apply. Прерванный пересчёт оставляет «выполняется» —
    это тоже «не готово»; повторный запуск начинает заново.
    """

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_READY = "ready"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Ожидает пересчёта"),
        (STATUS_RUNNING, "Пересчёт выполняется или прерван"),
        (STATUS_READY, "Готово"),
    ]

    status = models.CharField("Состояние", max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    products_total = models.PositiveIntegerField("Товаров к пересчёту", default=0)
    products_done = models.PositiveIntegerField("Пересчитано товаров", default=0)
    started_at = models.DateTimeField("Пересчёт начат", null=True, blank=True)
    finished_at = models.DateTimeField("Пересчёт завершён", null=True, blank=True)
    message = models.TextField("Пояснение", blank=True)

    class Meta:
        verbose_name = "Состояние перехода складского учёта"
        verbose_name_plural = "Состояние перехода складского учёта"

    def __str__(self):
        return self.get_status_display()


class StockTransitionIssue(models.Model):
    """Нарушение, которое создал переход на ключ организации: остаток
    организации с запретом минуса уходит в минус там, где по складу в целом
    его не было (организация расходовала чужой товар).

    baseline — история остатка склада в целом (все организации) на момент
    перехода: [[дата, время, момент создания (UTC), вид документа, id, кол-во]].
    Нарушение действует, пока где-то в истории организации остаток
    отрицателен и меньше этой базы; тогда операции с товаром отклоняются
    (costing: transition_negative), кроме исправляющих, после которых
    нарушения нет целиком. Если организации разрешили минус, нарушение не
    действует — её дефицит учитывается как обычный.
    """

    product = models.ForeignKey("catalog.Product", on_delete=models.CASCADE, related_name="+")
    organization = models.ForeignKey("core.Organization", on_delete=models.CASCADE, related_name="+")
    warehouse = models.ForeignKey("core.Warehouse", on_delete=models.CASCADE, related_name="+")
    baseline = models.JSONField("История склада в целом при переходе", default=list)
    description = models.TextField("Описание")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Нарушение после перехода на учёт по организациям"
        verbose_name_plural = "Нарушения после перехода на учёт по организациям"
        constraints = [
            models.UniqueConstraint(fields=["product", "organization", "warehouse"], name="uniq_transition_issue"),
        ]
