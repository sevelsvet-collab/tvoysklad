from django.db import models


class Counterparty(models.Model):
    KIND_LEGAL = "legal"
    KIND_ENTREPRENEUR = "entrepreneur"
    KIND_PERSON = "person"
    KIND_CHOICES = [
        (KIND_LEGAL, "Юридическое лицо"),
        (KIND_ENTREPRENEUR, "Индивидуальный предприниматель"),
        (KIND_PERSON, "Физическое лицо"),
    ]

    TYPE_CUSTOMER = "customer"
    TYPE_SUPPLIER = "supplier"
    TYPE_BOTH = "both"
    TYPE_CHOICES = [
        (TYPE_CUSTOMER, "Покупатель"),
        (TYPE_SUPPLIER, "Поставщик"),
        (TYPE_BOTH, "Покупатель и поставщик"),
    ]

    name = models.CharField("Наименование", max_length=255)
    full_name = models.CharField("Полное наименование", max_length=512, blank=True)
    kind = models.CharField("Форма", max_length=16, choices=KIND_CHOICES, default=KIND_LEGAL)
    partner_type = models.CharField("Тип", max_length=16, choices=TYPE_CHOICES, default=TYPE_CUSTOMER)

    inn = models.CharField("ИНН", max_length=12, blank=True, db_index=True)
    kpp = models.CharField("КПП", max_length=9, blank=True)
    ogrn = models.CharField("ОГРН/ОГРНИП", max_length=15, blank=True)
    okpo = models.CharField("ОКПО", max_length=14, blank=True)

    legal_address = models.CharField("Юридический адрес", max_length=512, blank=True)
    actual_address = models.CharField("Фактический адрес", max_length=512, blank=True)
    phone = models.CharField("Телефон", max_length=64, blank=True)
    email = models.EmailField("E-mail", blank=True)
    contact_person = models.CharField("Контактное лицо", max_length=255, blank=True)
    director_name = models.CharField("Руководитель (ФИО, для документов)", max_length=255, blank=True)

    comment = models.TextField("Комментарий", blank=True)
    is_active = models.BooleanField("Действующий", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Контрагент"
        verbose_name_plural = "Контрагенты"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def requisites_line(self):
        parts = [self.full_name or self.name]
        if self.inn:
            parts.append(f"ИНН {self.inn}")
        if self.kpp:
            parts.append(f"КПП {self.kpp}")
        if self.legal_address:
            parts.append(self.legal_address)
        return ", ".join(parts)


class BankAccount(models.Model):
    counterparty = models.ForeignKey(Counterparty, on_delete=models.CASCADE, related_name="bank_accounts")
    bank_name = models.CharField("Банк", max_length=255)
    bik = models.CharField("БИК", max_length=9, blank=True)
    account = models.CharField("Расчётный счёт", max_length=20)
    corr_account = models.CharField("Корр. счёт", max_length=20, blank=True)
    is_default = models.BooleanField("Основной", default=False)

    class Meta:
        verbose_name = "Банковский счёт"
        verbose_name_plural = "Банковские счета"

    def __str__(self):
        return f"{self.bank_name} {self.account}"


class Contract(models.Model):
    counterparty = models.ForeignKey(Counterparty, on_delete=models.CASCADE, related_name="contracts")
    organization = models.ForeignKey(
        "core.Organization", on_delete=models.PROTECT, verbose_name="Организация",
        null=True, blank=True,
    )
    number = models.CharField("Номер", max_length=64)
    date = models.DateField("Дата", null=True, blank=True)
    name = models.CharField("Название", max_length=255, blank=True)

    class Meta:
        verbose_name = "Договор"
        verbose_name_plural = "Договоры"
        ordering = ["-date"]

    def __str__(self):
        base = f"№ {self.number}"
        if self.date:
            base += f" от {self.date:%d.%m.%Y}"
        return base
