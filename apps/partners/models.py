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


class ContractTemplate(models.Model):
    """Шаблон типового договора. Готовые заводятся при миграции, их можно
    править; можно добавлять свои. В тексте используются метки-переменные
    (см. apps/partners/contracts.py)."""

    KIND_SUPPLY = "supply"
    KIND_SALE = "sale"
    KIND_SERVICE = "service"
    KIND_OTHER = "other"
    KIND_CHOICES = [
        (KIND_SUPPLY, "Договор поставки"),
        (KIND_SALE, "Договор купли-продажи"),
        (KIND_SERVICE, "Договор оказания услуг"),
        (KIND_OTHER, "Другое"),
    ]

    name = models.CharField("Название шаблона", max_length=255)
    kind = models.CharField("Вид договора", max_length=16, choices=KIND_CHOICES, default=KIND_OTHER)
    title = models.CharField("Заголовок документа", max_length=255, blank=True)
    intro = models.TextField("Преамбула (шапка договора)", blank=True)
    outro = models.TextField("Реквизиты и подписи", blank=True)
    body = models.TextField("Текст договора (устар.)", blank=True)
    is_active = models.BooleanField("Действующий", default=True)
    is_builtin = models.BooleanField("Типовой (из поставки)", default=False)

    class Meta:
        verbose_name = "Шаблон договора"
        verbose_name_plural = "Шаблоны договоров"
        ordering = ["name"]

    def __str__(self):
        return self.name


class ContractQuestion(models.Model):
    """Вопрос конструктора договора: «Порядок оплаты товара» и т.п.

    Пользователь выбирает один из готовых вариантов формулировки — сам текст
    договора писать не нужно.
    """

    template = models.ForeignKey(
        ContractTemplate, on_delete=models.CASCADE, related_name="questions", verbose_name="Шаблон",
    )
    section = models.CharField("Раздел договора", max_length=255, blank=True)
    order = models.PositiveIntegerField("Порядок", default=0)
    title = models.CharField("Вопрос", max_length=255)
    help_text = models.CharField("Подсказка", max_length=512, blank=True)
    allow_none = models.BooleanField("Разрешить «Пункт не предусмотрен»", default=True)
    allow_custom = models.BooleanField("Разрешить «Свой вариант»", default=True)

    class Meta:
        verbose_name = "Вопрос договора"
        verbose_name_plural = "Вопросы договора"
        ordering = ["order", "id"]

    def __str__(self):
        return self.title


class ContractOption(models.Model):
    """Готовая формулировка — вариант ответа на вопрос конструктора."""

    question = models.ForeignKey(
        ContractQuestion, on_delete=models.CASCADE, related_name="options", verbose_name="Вопрос",
    )
    order = models.PositiveIntegerField("Порядок", default=0)
    label = models.CharField("Вариант (что видит пользователь)", max_length=255)
    body = models.TextField("Текст в договор")
    is_default = models.BooleanField("Выбран по умолчанию", default=False)

    class Meta:
        verbose_name = "Вариант формулировки"
        verbose_name_plural = "Варианты формулировок"
        ordering = ["order", "id"]

    def __str__(self):
        return self.label


class ContractAnswer(models.Model):
    """Выбор пользователя по конкретному договору."""

    contract = models.ForeignKey(
        "Contract", on_delete=models.CASCADE, related_name="answers", verbose_name="Договор",
    )
    question = models.ForeignKey(ContractQuestion, on_delete=models.CASCADE, verbose_name="Вопрос")
    option = models.ForeignKey(
        ContractOption, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Вариант",
    )
    custom_text = models.TextField("Свой вариант", blank=True)
    is_skipped = models.BooleanField("Пункт не предусмотрен", default=False)
    # Снимок текста на момент оформления: правка шаблона не меняет уже
    # заключённые договоры
    body_snapshot = models.TextField("Текст пункта", blank=True)

    class Meta:
        verbose_name = "Ответ по договору"
        verbose_name_plural = "Ответы по договору"
        ordering = ["question__order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["contract", "question"], name="uniq_contract_question"),
        ]

    def __str__(self):
        return f"{self.question}: {self.option or ('свой вариант' if self.custom_text else '—')}"

    @property
    def text(self):
        """Текст пункта для сборки договора."""
        if self.is_skipped:
            return ""
        if self.custom_text.strip():
            return self.custom_text.strip()
        return self.body_snapshot or (self.option.body if self.option else "")


class Contract(models.Model):
    DOC_TYPE = "contract"

    counterparty = models.ForeignKey(Counterparty, on_delete=models.CASCADE, related_name="contracts")
    organization = models.ForeignKey(
        "core.Organization", on_delete=models.PROTECT, verbose_name="Организация",
        null=True, blank=True,
    )
    template = models.ForeignKey(
        ContractTemplate, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name="Шаблон договора", related_name="contracts",
    )
    number = models.CharField("Номер", max_length=64, blank=True)
    date = models.DateField("Дата", null=True, blank=True)
    place = models.CharField("Место подписания", max_length=255, blank=True)
    name = models.CharField("Название", max_length=255, blank=True)
    subject = models.TextField("Предмет договора", blank=True)

    # Срок действия
    valid_from = models.DateField("Действует с", null=True, blank=True)
    valid_to = models.DateField("Действует по", null=True, blank=True)
    is_perpetual = models.BooleanField("Бессрочный", default=False)
    auto_renew = models.BooleanField("Автопролонгация", default=False)

    # Сумма и оплата
    amount = models.DecimalField("Сумма договора", max_digits=15, decimal_places=2, null=True, blank=True)
    payment_delay_days = models.PositiveIntegerField("Отсрочка платежа, дней", null=True, blank=True)
    prepayment_percent = models.DecimalField(
        "Предоплата, %", max_digits=5, decimal_places=2, null=True, blank=True,
    )

    # Условия поставки и претензий (подставляются в текст договора)
    delivery_place = models.CharField("Место поставки", max_length=512, blank=True)
    delivery_days = models.PositiveIntegerField("Срок поставки, дней", null=True, blank=True)
    penalty_rate = models.DecimalField(
        "Неустойка, % в день", max_digits=5, decimal_places=3, null=True, blank=True, default=0.1,
    )
    penalty_cap_percent = models.DecimalField(
        "Предельная неустойка, % от суммы", max_digits=5, decimal_places=2, null=True, blank=True, default=10,
    )
    claim_days = models.PositiveIntegerField("Срок ответа на претензию, дней", null=True, blank=True, default=10)
    warranty_months = models.PositiveIntegerField("Гарантия, месяцев", null=True, blank=True)

    # Состояние товара — важно для купли-продажи техники и оборудования
    CONDITION_NEW = "new"
    CONDITION_USED = "used"
    CONDITION_REFURBISHED = "refurbished"
    CONDITION_DISPLAY = "display"
    CONDITION_DEFECT = "defect"
    CONDITION_CHOICES = [
        (CONDITION_NEW, "Новый"),
        (CONDITION_USED, "Бывший в употреблении"),
        (CONDITION_REFURBISHED, "Восстановленный"),
        (CONDITION_DISPLAY, "Витринный образец"),
        (CONDITION_DEFECT, "С недостатками (уценённый)"),
    ]
    goods_condition = models.CharField(
        "Состояние товара", max_length=16, choices=CONDITION_CHOICES, blank=True,
    )
    goods_details = models.TextField(
        "Сведения о товаре", blank=True,
        help_text="Марка, модель, серийный/заводской номер, год выпуска, наработка, комплектация",
    )

    scan = models.FileField("Скан подписанного", upload_to="contracts/", blank=True)
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        verbose_name = "Договор"
        verbose_name_plural = "Договоры"
        ordering = ["-date", "-id"]

    def __str__(self):
        base = f"№ {self.number}"
        if self.date:
            base += f" от {self.date:%d.%m.%Y}"
        return base

    def save(self, *args, **kwargs):
        if not self.number and self.organization_id:
            from apps.core.models import DocumentNumber

            self.number = DocumentNumber.next_number(self.organization, self.DOC_TYPE)
        super().save(*args, **kwargs)

    @property
    def validity_display(self):
        if self.is_perpetual:
            return "бессрочный"
        if self.valid_from and self.valid_to:
            return f"с {self.valid_from:%d.%m.%Y} по {self.valid_to:%d.%m.%Y}"
        if self.valid_to:
            return f"по {self.valid_to:%d.%m.%Y}"
        if self.valid_from:
            return f"с {self.valid_from:%d.%m.%Y}"
        return "—"

    @property
    def is_expired(self):
        from django.utils import timezone

        return bool(self.valid_to and not self.is_perpetual and self.valid_to < timezone.localdate())

    # Развёрнутые формулировки состояния — подставляются в текст договора
    CONDITION_PHRASES = {
        CONDITION_NEW: (
            "новым, не бывшим в эксплуатации, не восстановленным и не собранным "
            "из восстановленных составных частей"
        ),
        CONDITION_USED: (
            "бывшим в употреблении и имеющим следы нормального эксплуатационного износа, "
            "не препятствующие использованию товара по назначению"
        ),
        CONDITION_REFURBISHED: (
            "восстановленным (прошедшим предпродажную подготовку с заменой отдельных "
            "составных частей) и полностью работоспособным"
        ),
        CONDITION_DISPLAY: (
            "витринным (демонстрационным) образцом, не находившимся в эксплуатации, "
            "но имеющим возможные незначительные внешние следы демонстрации"
        ),
        CONDITION_DEFECT: (
            "имеющим оговорённые Сторонами недостатки, с учётом которых определена "
            "цена товара"
        ),
    }

    @property
    def condition_phrase(self):
        return self.CONDITION_PHRASES.get(self.goods_condition, "")


class ContactPerson(models.Model):
    """Контактное лицо контрагента (бухгалтер, кладовщик и т.п.)."""

    counterparty = models.ForeignKey(
        Counterparty, on_delete=models.CASCADE, related_name="contacts", verbose_name="Контрагент",
    )
    full_name = models.CharField("ФИО", max_length=255)
    position = models.CharField("Должность", max_length=128, blank=True)
    phone = models.CharField("Телефон", max_length=64, blank=True)
    email = models.EmailField("E-mail", blank=True)
    comment = models.CharField("Комментарий", max_length=255, blank=True)

    class Meta:
        verbose_name = "Контактное лицо"
        verbose_name_plural = "Контактные лица"
        ordering = ["id"]

    def __str__(self):
        parts = [self.full_name]
        if self.position:
            parts.append(f"({self.position})")
        return " ".join(parts)
