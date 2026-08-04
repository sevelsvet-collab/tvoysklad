from django.contrib.auth.models import AbstractUser
from django.db import models, transaction
from django.utils import timezone


class User(AbstractUser):
    middle_name = models.CharField("Отчество", max_length=150, blank=True)
    phone = models.CharField("Телефон", max_length=32, blank=True)

    class Meta(AbstractUser.Meta):
        verbose_name = "Пользователь"
        verbose_name_plural = "Пользователи"

    @property
    def role_names(self):
        return list(self.groups.values_list("name", flat=True))

    def has_role(self, *names):
        if self.is_superuser:
            return True
        return self.groups.filter(name__in=names).exists()

    @property
    def display_name(self):
        full = f"{self.first_name} {self.last_name}".strip()
        return full or self.username


class Organization(models.Model):
    """Своё юрлицо/ИП, от имени которого выставляются документы."""

    name = models.CharField("Краткое наименование", max_length=255)
    full_name = models.CharField("Полное наименование", max_length=512, blank=True)
    inn = models.CharField("ИНН", max_length=12, blank=True)
    kpp = models.CharField("КПП", max_length=9, blank=True)
    ogrn = models.CharField("ОГРН/ОГРНИП", max_length=15, blank=True)
    legal_address = models.CharField("Юридический адрес", max_length=512, blank=True)
    actual_address = models.CharField("Фактический адрес", max_length=512, blank=True)
    phone = models.CharField("Телефон", max_length=64, blank=True)
    email = models.EmailField("E-mail", blank=True)

    bank_name = models.CharField("Банк", max_length=255, blank=True)
    bik = models.CharField("БИК", max_length=9, blank=True)
    bank_account = models.CharField("Расчётный счёт", max_length=20, blank=True)
    corr_account = models.CharField("Корр. счёт", max_length=20, blank=True)

    director_name = models.CharField("Руководитель (ФИО)", max_length=255, blank=True)
    director_position = models.CharField("Должность руководителя", max_length=255, blank=True, default="Генеральный директор")
    accountant_name = models.CharField("Главный бухгалтер (ФИО)", max_length=255, blank=True)

    signature = models.ImageField("Подпись (картинка)", upload_to="org/", blank=True, null=True)
    stamp = models.ImageField("Печать (картинка)", upload_to="org/", blank=True, null=True)
    logo = models.ImageField("Логотип", upload_to="org/", blank=True, null=True)

    vat_payer = models.BooleanField("Плательщик НДС", default=True)
    is_default = models.BooleanField("Организация по умолчанию", default=False)
    is_active = models.BooleanField("Действующая", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Организация"
        verbose_name_plural = "Организации"
        ordering = ["-is_default", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.is_default:
            Organization.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)
        super().save(*args, **kwargs)

    @classmethod
    def get_default(cls):
        return cls.objects.filter(is_active=True).order_by("-is_default", "name").first()


class Warehouse(models.Model):
    name = models.CharField("Наименование", max_length=255)
    address = models.CharField("Адрес", max_length=512, blank=True)
    is_default = models.BooleanField("Склад по умолчанию", default=False)
    is_active = models.BooleanField("Действующий", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Склад"
        verbose_name_plural = "Склады"
        ordering = ["-is_default", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.is_default:
            Warehouse.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)
        super().save(*args, **kwargs)

    @classmethod
    def get_default(cls):
        return cls.objects.filter(is_active=True).order_by("-is_default", "name").first()


class DocumentNumber(models.Model):
    """Счётчик номеров документов: свой ряд на организацию, тип документа и год."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    doc_type = models.CharField("Тип документа", max_length=32)
    year = models.PositiveIntegerField("Год")
    last_number = models.PositiveIntegerField("Последний номер", default=0)

    class Meta:
        verbose_name = "Нумератор документов"
        verbose_name_plural = "Нумераторы документов"
        constraints = [
            models.UniqueConstraint(fields=["organization", "doc_type", "year"], name="uniq_doc_number"),
        ]

    def __str__(self):
        return f"{self.organization} / {self.doc_type} / {self.year}: {self.last_number}"

    @classmethod
    def next_number(cls, organization, doc_type):
        """Выдаёт следующий номер вида '00001' атомарно."""
        year = timezone.localdate().year
        with transaction.atomic():
            counter, _ = cls.objects.select_for_update().get_or_create(
                organization=organization, doc_type=doc_type, year=year,
            )
            counter.last_number += 1
            counter.save(update_fields=["last_number"])
            return f"{counter.last_number:05d}"
