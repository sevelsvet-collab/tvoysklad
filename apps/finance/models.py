from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection, models, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.constants import DOC_DRAFT, DOC_POSTED, DOC_STATUS_CHOICES
from apps.core.models import assign_document_number, current_time


class AccountQuerySet(models.QuerySet):
    """Массовые изменения касс:

    * смена организации — по правилу Account.save(): нельзя, пока по кассе
      есть платежи или корректировки;
    * изменение начального остатка — в той же транзакции пересчитывается
      история касс (корректировки остатков); блокировки: кассы → их
      корректировки (services);
    остальные поля — без ограничений (например, снятие «основного» в
    Account.save)."""

    def _check_moves(self, targets):
        """targets — {id кассы: id новой организации}. Кассы блокируются по
        возрастанию id (уровень 2), затем проверка по базе."""
        from . import services

        services.lock_accounts(targets)
        stored = dict(Account.objects.filter(pk__in=list(targets)).values_list("pk", "organization_id"))
        moved = [pk for pk, organization_id in targets.items() if stored.get(pk) not in (None, organization_id)]
        busy = Account.objects.filter(pk__in=moved).filter(
            models.Q(pk__in=Payment.objects.values("account_id"))
            | models.Q(pk__in=AccountCorrection._base_manager.values("account_id"))
        ).order_by("pk").first()
        if busy is not None:
            raise ValidationError({"organization": (
                f"По счёту «{busy}» есть платежи или корректировки, оформленные на прежнюю организацию. "
                "Заведите для другой организации отдельный счёт или кассу"
            )})

    @staticmethod
    def _organization_value(value):
        from . import services

        return services._plain(value, "организация", "касс и счетов")

    def update(self, **kwargs):
        from . import services

        keys = [key for key in ("organization", "organization_id") if key in kwargs]
        opening = "opening_balance" in kwargs
        if not keys and not opening:
            return super().update(**kwargs)
        target = self._organization_value(kwargs[keys[0]]) if keys else None
        with transaction.atomic():
            ids = services.locked_pks(self)                       # 2) кассы выборки, по возрастанию id
            if keys:
                self._check_moves({pk: target for pk in ids})
            if opening:
                services.lock_account_corrections(ids)           # 3) их корректировки
            result = Account._base_manager.filter(pk__in=ids).update(**kwargs)
            if opening:
                services.recalculate_accounts(ids)
            return result

    def bulk_update(self, objs, fields, batch_size=None):
        """Смена организации или начального остатка пакетом: кассы пакета,
        попавшие в эту выборку (её фильтр сохраняется), блокируются по
        возрастанию id; если организация меняется у кассы с платежами или
        корректировками — отказ всего пакета до записи. При изменении
        начального остатка блокируются и корректировки этих касс, а после
        записи пересчитывается их история. Запись — по строке напрямую
        (services.write_rows). Возвращает число действительно обновлённых касс."""
        from . import services

        resolved = services.bulk_update_fields(Account, fields)
        moves = any(field.name == "organization" for field in resolved)
        opening = any(field.name == "opening_balance" for field in resolved)
        if not moves and not opening:
            return super().bulk_update(objs, fields, batch_size=batch_size)
        objs = list(objs)
        if any(obj.pk is None for obj in objs):
            raise ValueError("All bulk_update() objects must have a primary key set.")
        by_pk = {obj.pk: obj for obj in objs}
        targets = {pk: self._organization_value(obj.organization_id) for pk, obj in by_pk.items()} if moves else {}
        if not by_pk:
            return 0
        with transaction.atomic():
            ids = services.locked_pks(self.filter(pk__in=list(by_pk)))      # 2) кассы пакета в выборке
            if moves:
                self._check_moves({pk: targets[pk] for pk in ids})
            if opening:
                services.lock_account_corrections(ids)                     # 3) их корректировки
            count = services.write_rows(Account, {pk: by_pk[pk] for pk in ids}, resolved)
            if opening:
                services.recalculate_accounts(ids)
            return count


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

    objects = AccountQuerySet.as_manager()

    class Meta:
        verbose_name = "Счёт/касса"
        verbose_name_plural = "Счета и кассы"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def _organization_change_error(self, lock=False):
        """Счёт или кассу нельзя передать другой организации, пока по нему
        есть платежи или корректировки: они остались бы оформлены на прежнюю
        организацию, а деньги числились бы у новой.

        lock=True (при сохранении, внутри транзакции) — строка счёта
        блокируется до проверки: платёж, который сохраняется одновременно,
        блокирует ту же строку, поэтому одна из операций дождётся другой
        и увидит её результат.
        """
        if not self.pk or self._state.adding:
            return None
        stored = Account.objects.filter(pk=self.pk)
        if lock and connection.features.has_select_for_update:
            stored = stored.select_for_update()
        stored = stored.values_list("organization_id", flat=True).first()
        if stored is None or stored == self.organization_id:
            return None
        if not (Payment.objects.filter(account=self).exists()
                or AccountCorrection._base_manager.filter(account_id=self.pk).exists()):
            return None
        return {"organization": (
            "По этому счёту есть платежи или корректировки, оформленные на прежнюю организацию. "
            "Заведите для другой организации отдельный счёт или кассу"
        )}

    def clean(self):
        errors = self._organization_change_error()
        if errors:
            raise ValidationError(errors)

    def _writes(self, update_fields, name):
        """Попадёт ли поле в запись. Django принимает в update_fields и имя
        поля, и имя столбца: «organization» и «organization_id»."""
        if update_fields is None:
            return True
        return name in {self._meta.get_field(field).name for field in update_fields}

    def save(self, *args, **kwargs):
        """Проверяется и учитывается то, что действительно будет записано:
        при update_fields поля вне списка берутся из базы, а не из объекта.

        Блокировки (services): касса (и соседи по «основному») → её
        корректировки. Если изменился начальный остаток, в той же транзакции
        пересчитывается история кассы — разницы корректировок."""
        from . import services

        update_fields = kwargs.get("update_fields")
        if update_fields is not None and not update_fields:
            return super().save(*args, **kwargs)          # Django ничего не запишет
        with transaction.atomic():
            existing = bool(self.pk) and not self._state.adding
            fields = ("organization_id", "kind", "is_default", "opening_balance")
            written = {name: getattr(self, name) for name in fields}
            stored = Account._base_manager.filter(pk=self.pk).values(*fields).first() if existing else None
            if stored is not None and update_fields is not None:
                for name, attname in (("organization", "organization_id"), ("kind", "kind"),
                                      ("is_default", "is_default"), ("opening_balance", "opening_balance")):
                    if not self._writes(update_fields, name):
                        written[attname] = stored[attname]
            accounts = {self.pk} if existing else set()
            if existing and written["is_default"]:
                # снятие «основного» у соседей трогает их строки — блокируем
                # всю группу сразу, по возрастанию id (уровень 2, см. services)
                siblings = Account.objects.filter(organization_id=written["organization_id"], kind=written["kind"])
                accounts.update(siblings.values_list("pk", flat=True))
            services.lock_accounts(accounts)                                   # 2) кассы
            if existing:
                stored = Account._base_manager.filter(pk=self.pk).values(*fields).first()   # под блокировкой
            opening_changed = stored is not None and written["opening_balance"] != stored["opening_balance"]
            if opening_changed:
                services.lock_account_corrections([self.pk])                  # 3) её корректировки
            if self._writes(update_fields, "organization"):
                errors = self._organization_change_error(lock=True)
                if errors:
                    raise ValidationError(errors)
            super().save(*args, **kwargs)
            if written["is_default"]:
                # «Основной» — один на организацию и вид: у каждой организации своя
                # основная касса и свой основной расчётный счёт.
                Account.objects.filter(organization_id=written["organization_id"], kind=written["kind"]) \
                    .exclude(pk=self.pk).update(is_default=False)
            if opening_changed:
                services.recalculate_accounts([self.pk])

    @classmethod
    def get_default(cls, kind=None, organization=None):
        """Касса или счёт по умолчанию.

        kind — отдельно наличные и безналичные; organization — только счета
        этой организации (платёж нельзя провести по чужому счёту).
        """
        qs = cls.objects.filter(is_active=True)
        if kind:
            qs = qs.filter(kind=kind)
        if organization is not None:
            qs = qs.filter(organization=organization)
        return qs.order_by("-is_default", "name", "pk").first()

    @classmethod
    def ensure_default(cls, kind=KIND_BANK, organization=None):
        """Счёт или касса нужного вида у нужной организации; если такого нет —
        заводит его именно для неё. Чужой счёт не подставляется.

        Без организации берётся организация по умолчанию. Возвращает
        (счёт, создан ли он только что); (None, False) — организаций нет вовсе.
        """
        from apps.core.models import Organization

        org = organization or Organization.get_default()
        if org is None:
            return None, False
        account = cls.get_default(kind, org)
        if account:
            return account, False
        name = "Основная касса" if kind == cls.KIND_CASH else "Расчётный счёт"
        account = cls.objects.create(organization=org, name=name, kind=kind, is_default=True)
        return account, True

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


class PaymentQuerySet(models.QuerySet):
    """Массовые операции с платежами — с теми же правилами, что и Payment.save().

    Контракт:

    * delete() — блокирует платежи → их кассы → корректировки этих касс →
      счета покупателям; удаляет, пересчитывает историю касс и оплату.
    * update(**поля) — только конкретные значения (число, строка, дата, объект
      модели, id, None). Выражения (F(), Case(), подзапросы) для полей, от
      которых зависят проверки (вид, статус, сумма, организация, касса,
      контрагент, счёт покупателю, назначение для взаиморасчётов),
      отклоняются ДО записи исключением UnsupportedBulkChange; так же —
      пустое назначение («не указано» определяется только при save()). Каждая строка проверяется так же, как при
      save(): совместимость связей и допустимость суммы — по тому, что
      окажется в базе, под блокировками в общем порядке (платежи → прежние и
      новые кассы → их корректировки → счета покупателям). Одна недопустимая
      строка — отказ всего пакета до записи. Снятие с проведения
      (update(status="draft")) разрешено всегда. После записи — пересчёт
      истории касс и оплаты счетов.
    * bulk_update(objs, fields) — блокировки всего пакета берутся заранее
      (платежи → все прежние и новые кассы → их корректировки → все прежние
      и новые счета покупателям), затем
      проверка каждой строки и запись; одна недопустимая строка — отказ
      всего пакета до записи. Фильтр QuerySet сохраняется, возвращается
      число обновлённых платежей.
    * bulk_create(objs) — каждая запись проверяется как при save()
      (пустое назначение для взаиморасчётов определяется так же);
      ignore_conflicts/update_conflicts не поддерживаются (отказ до записи):
      они записали бы или пропустили строки мимо проверок.

    Все методы работают внутри собственной точки сохранения: если вызывающий
    код перехватил исключение во внешней транзакции, она остаётся пригодной.
    """

    def _locked(self):
        """Блокирует выбранные платежи (по возрастанию id); возвращает их
        строки {id: значения} и id счетов, с которыми они связаны."""
        from . import services

        rows = self.order_by("pk")
        if connection.features.has_select_for_update:
            rows = rows.select_for_update()
        rows = {row["pk"]: row for row in rows.values("pk", *services.LINK_FIELDS)}
        return rows, {row["invoice_id"] for row in rows.values() if row["invoice_id"]}

    def delete(self):
        from . import services

        with transaction.atomic():
            rows, invoice_ids = self._locked()                                 # 1) платежи
            accounts = {row["account_id"] for row in rows.values()}
            services.lock_account_history(accounts)                            # 2) кассы, 3) корректировки
            services.lock_invoices(invoice_ids)                                # 4) счета покупателям
            result = Payment._base_manager.filter(pk__in=list(rows)).delete()
            services.recalculate_accounts(accounts)
            services.recompute_invoices(invoice_ids)
        return result

    @staticmethod
    def _checked_values(kwargs):
        """Новые значения проверяемых полей {столбец: значение}; выражения —
        отказ до записи."""
        from . import services

        checked = {}
        for name, value in kwargs.items():
            field = Payment._meta.get_field(name)
            if field.attname not in services.LINK_FIELDS:
                continue
            if hasattr(value, "resolve_expression"):
                raise services.UnsupportedBulkChange(
                    f"Массовое изменение поля «{field.verbose_name}» выражением не поддерживается: "
                    "укажите конкретное значение или сохраните платежи по одному"
                )
            if field.attname == "settlement_purpose" and not value:
                raise services.UnsupportedBulkChange(
                    "При массовом изменении назначение для взаиморасчётов указывается явно: "
                    "«не указано» определяется по счёту-основанию только при сохранении платежа по одному"
                )
            checked[field.attname] = value.pk if isinstance(value, models.Model) else value
        return checked

    def update(self, **kwargs):
        from . import services

        checked = self._checked_values(kwargs)        # до любых блокировок и записи
        with transaction.atomic():
            rows, before = self._locked()                                      # 1) платежи
            new_rows = {pk: {**row, **checked} for pk, row in rows.items()}
            accounts = {row["account_id"] for row in rows.values()} | {row["account_id"] for row in new_rows.values()}
            services.lock_account_history(accounts)                            # 2) кассы, 3) корректировки
            after = {row["invoice_id"] for row in new_rows.values() if row["invoice_id"]}
            services.lock_invoices(before | after)                             # 4) счета покупателям
            only_unposting = set(checked) == {"status"} and checked["status"] != DOC_POSTED
            if not only_unposting:
                for pk in sorted(new_rows):
                    values = {name: new_rows[pk][name] for name in services.LINK_FIELDS}
                    errors = {**services.link_errors(values), **services.amount_errors(values, rows[pk])}
                    if errors:
                        raise services.PaymentError(
                            {field: f"платёж id {pk}: {text}" for field, text in errors.items()}
                        )
            result = Payment._base_manager.filter(pk__in=list(rows)).update(**kwargs)
            services.recalculate_accounts(accounts)
            services.recompute_invoices(before | after)
        return result

    def bulk_update(self, objs, fields, batch_size=None):
        """Пакетное изменение с теми же правилами, что update(), но для всего
        пакета сразу:

        1) блокируются все платежи пакета, попавшие в этот QuerySet
           (его фильтр сохраняется; остальные объекты не изменяются);
        2) блокируется полный набор касс — будущих для каждой строки;
        3) блокируется полный набор счетов покупателям — прежних и будущих;
        внутри каждого уровня — по возрастанию id. Новых блокировок после
        этого не берётся: запись идёт напрямую, мимо update().
        Затем каждая строка проверяется по свежим данным (связи и сумма —
        будущие значения), пакет записывается и оплата пересчитывается —
        всё в одной транзакции; одна недопустимая строка — отказ всего пакета
        до записи. Возвращает число действительно обновлённых платежей.
        """
        from . import services

        objs = list(objs)
        if any(obj.pk is None for obj in objs):
            raise ValueError("bulk_update: у всех объектов должен быть первичный ключ")
        names = [Payment._meta.get_field(name).name for name in fields]
        if not objs or not names:
            return 0
        values_by_pk = {}
        for obj in objs:
            values = {name: getattr(obj, name) for name in names}
            checked = self._checked_values(values)        # выражения — отказ до записи
            values_by_pk[obj.pk] = (values, checked)
        with transaction.atomic():
            rows, before = self.filter(pk__in=list(values_by_pk))._locked()      # 1) платежи
            new_rows = {pk: {**row, **values_by_pk[pk][1]} for pk, row in rows.items()}
            accounts = {row["account_id"] for row in rows.values()} | {row["account_id"] for row in new_rows.values()}
            services.lock_account_history(accounts)                              # 2) кассы, 3) корректировки
            after = {row["invoice_id"] for row in new_rows.values() if row["invoice_id"]}
            services.lock_invoices(before | after)                               # 4) счета покупателям
            for pk in sorted(new_rows):
                checked = values_by_pk[pk][1]
                if set(checked) == {"status"} and checked["status"] != DOC_POSTED:
                    continue                                  # снятие с проведения разрешено всегда
                values = {name: new_rows[pk][name] for name in services.LINK_FIELDS}
                errors = {**services.link_errors(values), **services.amount_errors(values, rows[pk])}
                if errors:
                    raise services.PaymentError(
                        {field: f"платёж id {pk}: {text}" for field, text in errors.items()}
                    )
            for pk in sorted(new_rows):
                # прямая запись: блокировки уже взяты, проверка выполнена
                models.QuerySet.update(Payment.objects.filter(pk=pk), **values_by_pk[pk][0])
            services.recalculate_accounts(accounts)
            services.recompute_invoices(before | after)
        return len(new_rows)

    def bulk_create(self, objs, *args, **kwargs):
        """Каждая запись проверяется так же, как при save(); оплата счетов
        пересчитывается. Номера документов не присваиваются."""
        from . import services

        if kwargs.get("ignore_conflicts") or kwargs.get("update_conflicts"):
            raise services.UnsupportedBulkChange(
                "Массовое создание платежей с ignore_conflicts/update_conflicts не поддерживается: "
                "строки были бы записаны или пропущены мимо проверок"
            )
        objs = list(objs)
        with transaction.atomic():
            accounts = {obj.account_id for obj in objs}
            services.lock_account_history(accounts)                  # 2) кассы, 3) корректировки
            invoice_ids = {obj.invoice_id for obj in objs}
            services.lock_invoices(invoice_ids)                      # 4) счета покупателям
            for obj in objs:
                if not obj.settlement_purpose:                       # «не указано» — как при save()
                    obj.settlement_purpose = services.auto_purpose(services.link_values(obj))
                values = services.link_values(obj)
                errors = {**services.link_errors(values), **services.amount_errors(values)}
                if errors:
                    raise services.PaymentError(errors)
            result = super().bulk_create(objs, *args, **kwargs)
            services.recalculate_accounts(accounts)
            services.recompute_invoices(invoice_ids)
        return result


class Payment(models.Model):
    """Движение денег: входящий (приход) или исходящий (расход) платёж.

    Оплата связанного счёта покупателю пересчитывается при любом изменении
    платежа — см. apps/finance/services.py.
    """

    KIND_IN = "incoming"
    KIND_OUT = "outgoing"
    KIND_CHOICES = [
        (KIND_IN, "Входящий платёж"),
        (KIND_OUT, "Исходящий платёж"),
    ]

    # Назначение для взаиморасчётов — что оплачено: продажа или закупка
    # (apps/finance/settlements.py). Текстовое «Назначение платежа» (purpose)
    # не заменяет. Пустое значение у ещё не записанного платежа — «не
    # указано»: при записи определяется по счёту-основанию (services.auto_purpose);
    # явно выбранное назначение никогда не меняется молча — противоречие
    # направлению денег или счёту-основанию отклоняется ошибкой.
    PURPOSE_CUSTOMER_PAYMENT = "customer_payment"   # входящий: долг покупателя по продажам меньше
    PURPOSE_CUSTOMER_REFUND = "customer_refund"     # исходящий: вернули деньги покупателю — его долг больше
    PURPOSE_SUPPLIER_PAYMENT = "supplier_payment"   # исходящий: наш долг по закупкам меньше
    PURPOSE_SUPPLIER_REFUND = "supplier_refund"     # входящий: поставщик вернул деньги — наш долг больше
    PURPOSE_UNALLOCATED = "unallocated"             # любой: в стороны не входит, показывается отдельно
    PURPOSE_CHOICES = [
        (PURPOSE_CUSTOMER_PAYMENT, "Оплата от покупателя"),
        (PURPOSE_CUSTOMER_REFUND, "Возврат покупателю"),
        (PURPOSE_SUPPLIER_PAYMENT, "Оплата поставщику"),
        (PURPOSE_SUPPLIER_REFUND, "Возврат от поставщика"),
        (PURPOSE_UNALLOCATED, "Не распределено"),
    ]
    PURPOSES_BY_KIND = {
        KIND_IN: (PURPOSE_CUSTOMER_PAYMENT, PURPOSE_SUPPLIER_REFUND, PURPOSE_UNALLOCATED),
        KIND_OUT: (PURPOSE_SUPPLIER_PAYMENT, PURPOSE_CUSTOMER_REFUND, PURPOSE_UNALLOCATED),
    }

    kind = models.CharField("Вид", max_length=16, choices=KIND_CHOICES)
    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    time = models.TimeField("Время", default=current_time)
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
    settlement_purpose = models.CharField(
        "Назначение для взаиморасчётов", max_length=24, choices=PURPOSE_CHOICES, blank=True, default="",
        help_text="Что оплачено: продажа или закупка. «Не распределено» — показывается в расчётах отдельно",
    )
    status = models.CharField("Статус", max_length=16, choices=DOC_STATUS_CHOICES, default=DOC_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PaymentQuerySet.as_manager()

    class Meta:
        verbose_name = "Платёж"
        verbose_name_plural = "Платежи"
        ordering = ["-date", "-time", "-id"]
        constraints = [
            # Последняя граница для путей мимо модели: в базе назначение всегда
            # указано и не противоречит направлению денег. Проверки с понятными
            # сообщениями — services.purpose_errors.
            models.CheckConstraint(
                condition=(
                    models.Q(settlement_purpose__in=[
                        "customer_payment", "customer_refund", "supplier_payment", "supplier_refund", "unallocated",
                    ])
                    & ~models.Q(kind="incoming", settlement_purpose__in=["customer_refund", "supplier_payment"])
                    & ~models.Q(kind="outgoing", settlement_purpose__in=["customer_payment", "supplier_refund"])
                ),
                name="finance_payment_settlement_purpose_valid",
                violation_error_message="Назначение для взаиморасчётов не указано или не подходит к направлению платежа",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} № {self.number} от {self.date:%d.%m.%Y}"

    @property
    def DOC_TYPE(self):
        return f"payment_{self.kind}"

    def clean(self):
        """Совместимость связей, назначения и допустимость суммы — для форм и
        административной панели. «Не указано» в назначении для
        взаиморасчётов здесь же определяется по счёту-основанию."""
        from . import services

        if not self.settlement_purpose:
            self.settlement_purpose = services.auto_purpose(services.link_values(self))
        errors = services.consistency_errors(self)
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Сохраняет платёж и в той же транзакции пересчитывает оплату счетов,
        которых он касается: прежнего (из базы) и нового.

        Порядок (services): своя строка платежа → прежняя и новая касса →
        корректировки этих касс → счета покупателям, затем проверка связей по
        свежим данным из базы, запись, пересчёт истории касс (разницы
        корректировок) и оплаты счетов — в одной транзакции.
        Проверяется то, что действительно будет записано: при update_fields
        поля вне списка берутся из базы, а не из объекта.

        Несовместимые связи отклоняются (services.PaymentError) — кроме снятия
        с проведения: его нельзя запрещать, им исправляют старые ошибки.

        Назначение для взаиморасчётов: «не указано» (пусто) определяется по
        тому, что будет записано, — со счётом-основанием «оплата от
        покупателя», без него «не распределено». Указанное назначение не
        меняется: противоречие направлению денег или счёту — PaymentError.
        """
        from . import services

        assign_document_number(self, f"payment_{self.kind}", kind=self.kind)
        update_fields = kwargs.get("update_fields")
        with transaction.atomic():
            stored = None
            if self.pk and not self._state.adding:
                rows = Payment.objects.filter(pk=self.pk)
                if connection.features.has_select_for_update:
                    rows = rows.select_for_update()                      # 1) свой платёж
                stored = rows.values(*services.LINK_FIELDS).first()
            values = services.written_values(self, stored, update_fields)
            if not values["settlement_purpose"]:
                values["settlement_purpose"] = self.settlement_purpose = services.auto_purpose(values)
            invoice_ids = {values["invoice_id"], stored and stored["invoice_id"]}
            accounts = {values["account_id"], stored and stored["account_id"]}
            services.lock_account_history(accounts)                      # 2) кассы, 3) их корректировки
            services.lock_invoices(invoice_ids)                          # 4) счета покупателям
            only_unposting = (
                update_fields is not None and set(update_fields) == {"status"}
                and values["status"] != DOC_POSTED
            )
            if not only_unposting:
                errors = {
                    **services.link_errors(values),                      # свежие данные под блокировкой
                    **services.amount_errors(values, stored),            # сумма и статус, что будут записаны
                }
                if errors:
                    raise services.PaymentError(errors)
            super().save(*args, **kwargs)
            services.recalculate_accounts(accounts)                      # история касс
            services.recompute_invoices(invoice_ids)                     # сумма — после блокировок

    def delete(self, *args, **kwargs):
        from . import services

        with transaction.atomic():
            invoice_ids, accounts = {self.invoice_id}, {self.account_id}
            if self.pk:
                stored = Payment.objects.filter(pk=self.pk)
                if connection.features.has_select_for_update:
                    stored = stored.select_for_update()                  # 1) свой платёж
                for invoice_id, account_id in stored.values_list("invoice_id", "account_id"):
                    invoice_ids.add(invoice_id)
                    accounts.add(account_id)
            services.lock_account_history(accounts)                      # 2) касса, 3) её корректировки
            services.lock_invoices(invoice_ids)                          # 4) счета покупателям
            result = super().delete(*args, **kwargs)
            services.recalculate_accounts(accounts)
            services.recompute_invoices(invoice_ids)
        return result

    @property
    def is_posted(self):
        return self.status == DOC_POSTED

    def post(self):
        """Проводит платёж. Повторное проведение оплату счёта не удваивает:
        она каждый раз считается заново как сумма проведённых платежей."""
        self.status = DOC_POSTED
        self.save(update_fields=["status"])

    def unpost(self):
        self.status = DOC_DRAFT
        self.save(update_fields=["status"])

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
    """Пересчитывает «Оплачено» счёта = сумма проведённых входящих платежей по нему.

    Обычно вызывать не нужно: это делает сам платёж при любом изменении.
    """
    from . import services

    if not invoice:
        return
    with transaction.atomic():
        paid = services.recompute_invoices([invoice.pk])[invoice.pk]
    invoice.paid_amount = paid


class AccountCorrectionQuerySet(models.QuerySet):
    """Массовые операции с корректировками остатков — по правилам save():
    касса или счёт — своей организации, проверка по базе под блокировками;
    после записи — пересчёт истории касс. Порядок блокировок (services):
    прежние и новые кассы затронутых корректировок → все корректировки этих
    касс; строки перечитываются под блокировкой. Одна несовместимая строка —
    отказ всего пакета до записи. Снятие с проведения (update(status="draft"))
    и удаление разрешены всегда. Выражения для организации, кассы и статуса
    не поддерживаются (отказ до записи)."""

    # поля, от которых зависят проверки или история кассы
    _HISTORY = {"organization", "organization_id", "account", "account_id", "status", "actual_balance",
                "date", "time", "balance_before", "amount", "created_at"}

    def _checked(self, values):
        from . import services

        checked = {}
        for name, value in values.items():
            column = services._CORRECTION_NAMES.get(name)
            if column:
                checked[column] = services._plain(value, AccountCorrection._meta.get_field(name).verbose_name,
                                                  "корректировок остатков")
        return checked

    @staticmethod
    def _lock(pks, new_account_ids=()):
        """Кассы (уровень 2) — прежние для строк pks и новые — и все
        корректировки этих касс (уровень 3); строки перечитываются под
        блокировкой. Возвращает (id касс, {id: строка})."""
        from . import services

        pks = list(pks)
        accounts = set(AccountCorrection._base_manager.filter(pk__in=pks).values_list("account_id", flat=True))
        accounts.update(pk for pk in new_account_ids if pk)
        services.lock_account_history(accounts, pks)
        rows = {row["pk"]: row for row in AccountCorrection._base_manager.filter(pk__in=pks).values(
            "pk", "number", *services.CORRECTION_FIELDS)}
        if any(row["account_id"] not in accounts for row in rows.values()):
            raise services.CorrectionError({"__all__": services.CONCURRENT_CORRECTION_CHANGE})
        return accounts, rows

    def update(self, **kwargs):
        from . import services

        if not set(kwargs) & self._HISTORY:
            return super().update(**kwargs)
        checked = self._checked(kwargs)
        with transaction.atomic():
            accounts, rows = self._lock(self.values_list("pk", flat=True), [checked.get("account_id")])
            if checked:
                services.check_correction_links(
                    [({**{n: row[n] for n in services.CORRECTION_FIELDS}, **checked},
                      f"корректировка № {row['number']} (id {pk})") for pk, row in sorted(rows.items())],
                    update_fields=set(checked),
                )
            result = AccountCorrection._base_manager.filter(pk__in=list(rows)).update(**kwargs)
            services.recalculate_accounts(accounts)
            return result

    def bulk_update(self, objs, fields, batch_size=None):
        """С полями истории или связей: корректировки пакета в этой выборке
        (фильтр сохраняется) — блокировки, проверка, прямая запись по строке
        (services.write_rows), пересчёт истории касс. Возвращает число
        обновлённых записей."""
        from . import services

        resolved = services.bulk_update_fields(AccountCorrection, fields)
        if not {field.name for field in resolved} & self._HISTORY:
            return super().bulk_update(objs, fields, batch_size=batch_size)
        objs = list(objs)
        if any(obj.pk is None for obj in objs):
            raise ValueError("All bulk_update() objects must have a primary key set.")
        by_pk = {obj.pk: obj for obj in objs}
        if not by_pk:
            return 0
        checked = {pk: self._checked({field.name: getattr(obj, field.attname) for field in resolved})
                   for pk, obj in by_pk.items()}
        with transaction.atomic():
            pks = list(self.filter(pk__in=list(by_pk)).values_list("pk", flat=True))
            accounts, rows = self._lock(pks, [checked[pk].get("account_id") for pk in pks])
            link_rows = [({**{n: row[n] for n in services.CORRECTION_FIELDS}, **checked[pk]},
                          f"корректировка № {row['number']} (id {pk})")
                         for pk, row in sorted(rows.items()) if checked[pk]]
            if link_rows:
                services.check_correction_links(
                    link_rows, update_fields={name for pk in rows for name in checked[pk]})
            count = services.write_rows(AccountCorrection, {pk: by_pk[pk] for pk in rows}, resolved)
            services.recalculate_accounts(accounts)
            return count

    def bulk_create(self, objs, *args, **kwargs):
        from . import services

        if kwargs.get("ignore_conflicts") or kwargs.get("update_conflicts"):
            raise services.UnsupportedBulkChange(
                "Массовое создание корректировок с ignore_conflicts/update_conflicts не поддерживается: "
                "строки были бы записаны или пропущены мимо проверок"
            )
        objs = list(objs)
        with transaction.atomic():
            accounts = {obj.account_id for obj in objs}
            services.lock_account_history(accounts)                  # 2) кассы, 3) корректировки
            services.check_correction_links(
                [(services.correction_values(obj), f"корректировка № {obj.number or '—'}") for obj in objs],
                update_fields=None)
            result = super().bulk_create(objs, *args, **kwargs)
            services.recalculate_accounts(accounts)
            return result

    def delete(self):
        from . import services

        with transaction.atomic():
            accounts, rows = self._lock(self.values_list("pk", flat=True))
            result = AccountCorrection._base_manager.filter(pk__in=list(rows)).delete()
            services.recalculate_accounts(accounts)
            return result


class AccountCorrection(models.Model):
    """Корректировка остатка кассы или расчётного счёта: «в указанные дату и
    время фактически было actual_balance» (правило «остаток на момент»).

    actual_balance — введённый человеком факт, автоматически не меняется;
    balance_before — расчётный остаток непосредственно перед корректировкой
    (по документам раньше неё в общем порядке services.money_key);
    amount = actual_balance − balance_before: плюс — излишек, минус —
    недостача. Проведённая корректировка входит в остаток кассы суммой
    amount, поэтому сразу после неё остаток равен факту, а документы после
    неё меняют остаток от этого факта. Изменение предшествующей истории
    (платежи, начальный остаток, более ранние корректировки) пересчитывает
    разницу — services.recalculate_accounts в той же транзакции. У черновика
    balance_before и amount предварительные.
    """

    DOC_TYPE = "account_correction"

    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    time = models.TimeField("Время", default=current_time)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, verbose_name="Счёт/касса", related_name="corrections")
    balance_before = models.DecimalField("Расчётный остаток", max_digits=15, decimal_places=2, default=0)
    actual_balance = models.DecimalField("Фактический остаток", max_digits=15, decimal_places=2, default=0)
    amount = models.DecimalField("Сумма корректировки", max_digits=15, decimal_places=2, default=0)
    comment = models.CharField("Комментарий", max_length=512, blank=True)
    status = models.CharField("Статус", max_length=16, choices=DOC_STATUS_CHOICES, default=DOC_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AccountCorrectionQuerySet.as_manager()

    class Meta:
        verbose_name = "Корректировка остатка"
        verbose_name_plural = "Корректировки остатков"
        ordering = ["-date", "-time", "-id"]

    def __str__(self):
        return f"Корректировка остатка № {self.number} от {self.date:%d.%m.%Y}"

    def recalc(self):
        """Предварительный расчёт в памяти, по данным базы и без записи:
        расчётный остаток непосредственно перед моментом этой корректировки
        (её дата, время, момент создания) и разница. Начальный остаток
        кассы берётся из базы, а не из объекта кассы в памяти. Окончательные
        значения проведённой корректировки записывает save() — пересчётом
        истории кассы."""
        from . import services

        key = services.money_key(self.date, self.time, self.created_at, services.CORRECTION_RANK, self.pk)
        self.balance_before = services.balance_before(self.account_id, key, exclude_correction_pk=self.pk)
        self.amount = self.actual_balance - self.balance_before

    @property
    def is_surplus(self):
        return self.amount > 0

    def clean(self):
        """Касса или счёт — своей организации (по базе, а не по объекту
        кассы в памяти) — для форм и административной панели."""
        from . import services

        errors = services.correction_link_errors(services.correction_values(self))
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Сохраняет корректировку, если касса или счёт принадлежит её
        организации, и в той же транзакции пересчитывает историю касс —
        прежней и новой (разницы этой и остальных корректировок).

        Порядок блокировок (services): кассы — прежняя (из базы, прочитана без
        блокировки) и новая → все корректировки этих касс, включая эту →
        перечитать свою строку. Если её касса тем временем сменилась
        (одновременный перенос) — отказ, повторить. Затем проверка по данным
        базы, запись, пересчёт. Проверяется то, что действительно будет
        записано: при update_fields поля вне списка берутся из базы. Снятие с
        проведения (только статус «черновик») не проверяется: им исправляют
        старые несовместимые корректировки. Отказ — services.CorrectionError,
        ничего не записывается. balance_before и amount объекта после
        сохранения — значения из пересчёта.
        """
        from . import services

        assign_document_number(self, self.DOC_TYPE)
        update_fields = kwargs.get("update_fields")
        with transaction.atomic():
            existing = bool(self.pk) and not self._state.adding
            stored = None
            if existing:
                stored = AccountCorrection._base_manager.filter(pk=self.pk).values(*services.CORRECTION_FIELDS).first()
            values = services.written_correction_values(self, stored, update_fields)
            accounts = {values["account_id"], stored and stored["account_id"]}
            services.lock_account_history(accounts, [self.pk] if existing else [])   # 2) кассы, 3) корректировки
            if existing:
                fresh = AccountCorrection._base_manager.filter(pk=self.pk).values(*services.CORRECTION_FIELDS).first()
                if fresh is not None:
                    if fresh["account_id"] not in accounts:
                        raise services.CorrectionError({"__all__": services.CONCURRENT_CORRECTION_CHANGE})
                    values = services.written_correction_values(self, fresh, update_fields)
            services.check_correction_links([(values, None)], update_fields)
            super().save(*args, **kwargs)
            histories = services.recalculate_accounts(accounts)
            own = histories.get(values["account_id"], {}).get("corrections", {}).get(self.pk)
            if own is not None:
                self.balance_before, self.amount = own

    def delete(self, *args, **kwargs):
        """Удаление разрешено всегда; история кассы пересчитывается в той же
        транзакции. Блокировки: касса → её корректировки."""
        from . import services

        with transaction.atomic():
            account_id = AccountCorrection._base_manager.filter(pk=self.pk).values_list(
                "account_id", flat=True).first() or self.account_id
            services.lock_account_history([account_id], [self.pk])
            fresh = AccountCorrection._base_manager.filter(pk=self.pk).values_list("account_id", flat=True).first()
            if fresh is not None and fresh != account_id:
                raise services.CorrectionError({"__all__": services.CONCURRENT_CORRECTION_CHANGE})
            result = super().delete(*args, **kwargs)
            services.recalculate_accounts([account_id])
        return result

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
    """Корректировка взаиморасчётов с контрагентом: направление, сторона, сумма.

    Направление задаёт знак, сторона — какую задолженность он меняет
    (apps/finance/settlements.py):
    * «продажи»: «контрагент должен нам» увеличивает долг покупателя,
      «мы должны» — уменьшает;
    * «закупки»: «мы должны» увеличивает наш долг поставщику, «контрагент
      должен нам» — уменьшает;
    * «не распределено» (все старые корректировки): в стороны не входит,
      показывается отдельно и участвует только в справочной разнице.
    Взаимозачётом корректировка не является.
    """

    DOC_TYPE = "settlement_correction"

    DIR_THEY_OWE = "they_owe"   # увеличить долг контрагента перед нами
    DIR_WE_OWE = "we_owe"       # увеличить наш долг перед контрагентом
    DIRECTION_CHOICES = [
        (DIR_THEY_OWE, "Контрагент должен нам"),
        (DIR_WE_OWE, "Мы должны контрагенту"),
    ]

    SIDE_SALES = "sales"
    SIDE_PURCHASES = "purchases"
    SIDE_UNALLOCATED = "unallocated"
    SIDE_CHOICES = [
        (SIDE_SALES, "Продажи (расчёты с покупателем)"),
        (SIDE_PURCHASES, "Закупки (расчёты с поставщиком)"),
        (SIDE_UNALLOCATED, "Не распределено"),
    ]

    number = models.CharField("Номер", max_length=32, blank=True)
    date = models.DateField("Дата", default=timezone.localdate)
    time = models.TimeField("Время", default=current_time)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, verbose_name="Организация")
    counterparty = models.ForeignKey(
        "partners.Counterparty", on_delete=models.PROTECT, verbose_name="Контрагент",
        related_name="settlement_corrections",
    )
    direction = models.CharField("Направление", max_length=16, choices=DIRECTION_CHOICES, default=DIR_THEY_OWE)
    side = models.CharField("Сторона расчётов", max_length=16, choices=SIDE_CHOICES, default=SIDE_UNALLOCATED)
    amount = models.DecimalField("Сумма", max_digits=15, decimal_places=2, default=0)
    comment = models.CharField("Комментарий", max_length=512, blank=True)
    status = models.CharField("Статус", max_length=16, choices=DOC_STATUS_CHOICES, default=DOC_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Корректировка взаиморасчётов"
        verbose_name_plural = "Корректировки взаиморасчётов"
        ordering = ["-date", "-time", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(side__in=["sales", "purchases", "unallocated"]),
                name="finance_settlementcorrection_side_valid",
                violation_error_message="Укажите сторону расчётов: продажи, закупки или «не распределено»",
            ),
        ]

    def __str__(self):
        return f"Корректировка взаиморасчётов № {self.number} от {self.date:%d.%m.%Y}"

    @property
    def signed_amount(self):
        """Знак по направлению: >0 — контрагент должен нам, <0 — мы должны."""
        return self.amount if self.direction == self.DIR_THEY_OWE else -self.amount

    @property
    def settlement_effect(self):
        """(сторона, изменение её задолженности): по продажам «+» — покупатель
        должен нам больше; по закупкам «+» — мы должны поставщику больше; без
        стороны — signed_amount."""
        if self.side == self.SIDE_SALES:
            return self.SIDE_SALES, self.signed_amount
        if self.side == self.SIDE_PURCHASES:
            return self.SIDE_PURCHASES, -self.signed_amount
        return self.SIDE_UNALLOCATED, self.signed_amount

    def save(self, *args, **kwargs):
        assign_document_number(self, self.DOC_TYPE)
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
