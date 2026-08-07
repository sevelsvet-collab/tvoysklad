from decimal import Decimal

from django.db import models
from django.db.models import Sum
from django.utils import timezone

from apps.core.constants import DOC_DRAFT, DOC_POSTED, DOC_STATUS_CHOICES
from apps.core.models import DocumentNumber


class Account(models.Model):
    """Касса или расчётный счёт организации — где хранятся деньги."""

    KIND_CASH = "cash"
    KIND_BANK = "bank"
    KIND_CHOICES = [
        (KIND_CASH, "Касса"),
        (KIND_BANK, "Расчётный счёт"),
    ]

    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    name = models.CharField("Наименование", max_length=255)
    kind = models.CharField("Тип", max_length=8, choices=KIND_CHOICES, default=KIND_BANK)
    bank_account = models.CharField("Номер счёта", max_length=32, blank=True)
    opening_balance = models.DecimalField("Начальный остаток", max_digits=15, decimal_places=2, default=0)
    is_default = models.BooleanField("Основной", default=False)
    is_active = models.BooleanField("Действующий", default=True)

    class Meta:
        verbose_name = "Счёт/касса"
        verbose_name_plural = "Счета и кассы"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            Account.objects.exclude(pk=self.pk).update(is_default=False)

    @classmethod
    def get_default(cls):
        return cls.objects.filter(is_default=True, is_active=True).first() or cls.objects.filter(is_active=True).first()

    def balance_at(self, exclude_correction_pk=None):
        """Остаток = начальный + приходы − расходы + проведённые корректировки.

        exclude_correction_pk исключает конкретную корректировку из суммы —
        нужно при расчёте её собственной разницы (чтобы не учитывать себя).
        """
        agg = Payment.objects.filter(account=self, status=DOC_POSTED).aggregate(
            income=Sum("amount", filter=models.Q(kind=Payment.KIND_IN)),
            outcome=Sum("amount", filter=models.Q(kind=Payment.KIND_OUT)),
        )
        income = agg["income"] or Decimal("0")
        outcome = agg["outcome"] or Decimal("0")
        corr_qs = AccountCorrection.objects.filter(account=self, status=DOC_POSTED)
        if exclude_correction_pk:
            corr_qs = corr_qs.exclude(pk=exclude_correction_pk)
        corr = corr_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")
        return self.opening_balance + income - outcome + corr

    @property
    def balance(self):
        return self.balance_at()


class Payment(models.Model):
    """Движение денег: входящий (приход) или исходящий (расход) платёж."""

    KIND_IN = "incoming"
    KIND_OUT = "outgoing"
    KIND_CHOICES = [
        (KIND_IN, "Входящий платёж"),
        (KIND_OUT, "Исходящий платёж"),
    ]

    kind = models.CharField("Вид", max_length=16, choices=KIND_CHOICES)
    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, verbose_name="Счёт/касса")
    counterparty = models.ForeignKey(
        "partners.Counterparty", on_delete=models.PROTECT, verbose_name="Контрагент", related_name="payments",
    )
    invoice = models.ForeignKey(
        "sales.Invoice", on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name="Счёт-основание", related_name="payments",
    )
    amount = models.DecimalField("Сумма", max_digits=15, decimal_places=2, default=0)
    purpose = models.CharField("Назначение платежа", max_length=512, blank=True)
    status = models.CharField("Статус", max_length=16, choices=DOC_STATUS_CHOICES, default=DOC_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Платёж"
        verbose_name_plural = "Платежи"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.get_kind_display()} № {self.number} от {self.date:%d.%m.%Y}"

    @property
    def DOC_TYPE(self):
        return f"payment_{self.kind}"

    def save(self, *args, **kwargs):
        if not self.number and self.organization_id:
            self.number = DocumentNumber.next_number(self.organization, f"payment_{self.kind}")
        super().save(*args, **kwargs)

    @property
    def is_posted(self):
        return self.status == DOC_POSTED

    def post(self):
        self.status = DOC_POSTED
        self.save(update_fields=["status"])
        recompute_invoice_paid(self.invoice)

    def unpost(self):
        self.status = DOC_DRAFT
        self.save(update_fields=["status"])
        recompute_invoice_paid(self.invoice)

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
        return docs


def recompute_invoice_paid(invoice):
    """Пересчитывает «Оплачено» счёта = сумма проведённых входящих платежей по нему."""
    if not invoice:
        return
    paid = Payment.objects.filter(
        invoice=invoice, kind=Payment.KIND_IN, status=DOC_POSTED,
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0")
    if invoice.paid_amount != paid:
        invoice.paid_amount = paid
        invoice.save(update_fields=["paid_amount"])


class AccountCorrection(models.Model):
    """Корректировка остатка кассы или расчётного счёта.

    Пользователь вводит фактический остаток, система хранит разницу (amount)
    как разовую проводку — она входит в Account.balance при проведении.
    """

    DOC_TYPE = "account_correction"

    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, verbose_name="Счёт/касса", related_name="corrections")
    balance_before = models.DecimalField("Расчётный остаток", max_digits=15, decimal_places=2, default=0)
    actual_balance = models.DecimalField("Фактический остаток", max_digits=15, decimal_places=2, default=0)
    amount = models.DecimalField("Сумма корректировки", max_digits=15, decimal_places=2, default=0)
    comment = models.CharField("Комментарий", max_length=512, blank=True)
    status = models.CharField("Статус", max_length=16, choices=DOC_STATUS_CHOICES, default=DOC_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Корректировка остатка"
        verbose_name_plural = "Корректировки остатков"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Корректировка остатка № {self.number} от {self.date:%d.%m.%Y}"

    def recalc(self):
        """Пересчитывает разницу от текущего остатка счёта (без учёта себя)."""
        self.balance_before = self.account.balance_at(exclude_correction_pk=self.pk)
        self.amount = self.actual_balance - self.balance_before

    def save(self, *args, **kwargs):
        if not self.number and self.organization_id:
            self.number = DocumentNumber.next_number(self.organization, self.DOC_TYPE)
        super().save(*args, **kwargs)

    @property
    def is_posted(self):
        return self.status == DOC_POSTED

    def post(self):
        self.status = DOC_POSTED
        self.save(update_fields=["status"])

    def unpost(self):
        self.status = DOC_DRAFT
        self.save(update_fields=["status"])


class SettlementCorrection(models.Model):
    """Корректировка взаиморасчётов с контрагентом (направление + сумма)."""

    DOC_TYPE = "settlement_correction"

    DIR_THEY_OWE = "they_owe"   # увеличить долг контрагента перед нами
    DIR_WE_OWE = "we_owe"       # увеличить наш долг перед контрагентом
    DIRECTION_CHOICES = [
        (DIR_THEY_OWE, "Контрагент должен нам"),
        (DIR_WE_OWE, "Мы должны контрагенту"),
    ]

    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    counterparty = models.ForeignKey(
        "partners.Counterparty", on_delete=models.PROTECT, verbose_name="Контрагент",
        related_name="settlement_corrections",
    )
    direction = models.CharField("Направление", max_length=16, choices=DIRECTION_CHOICES, default=DIR_THEY_OWE)
    amount = models.DecimalField("Сумма", max_digits=15, decimal_places=2, default=0)
    comment = models.CharField("Комментарий", max_length=512, blank=True)
    status = models.CharField("Статус", max_length=16, choices=DOC_STATUS_CHOICES, default=DOC_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Корректировка взаиморасчётов"
        verbose_name_plural = "Корректировки взаиморасчётов"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Корректировка взаиморасчётов № {self.number} от {self.date:%d.%m.%Y}"

    @property
    def signed_amount(self):
        """Знаковый вклад в сальдо: >0 — контрагент должен нам, <0 — мы должны."""
        return self.amount if self.direction == self.DIR_THEY_OWE else -self.amount

    def save(self, *args, **kwargs):
        if not self.number and self.organization_id:
            self.number = DocumentNumber.next_number(self.organization, self.DOC_TYPE)
        super().save(*args, **kwargs)

    @property
    def is_posted(self):
        return self.status == DOC_POSTED

    def post(self):
        self.status = DOC_POSTED
        self.save(update_fields=["status"])

    def unpost(self):
        self.status = DOC_DRAFT
        self.save(update_fields=["status"])
