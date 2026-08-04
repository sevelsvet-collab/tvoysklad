from django.db import models

from apps.core.constants import VAT_20, VAT_CHOICES


class Unit(models.Model):
    name = models.CharField("Обозначение", max_length=32, unique=True)
    okei_code = models.CharField("Код ОКЕИ", max_length=8, blank=True)

    class Meta:
        verbose_name = "Единица измерения"
        verbose_name_plural = "Единицы измерения"
        ordering = ["name"]

    def __str__(self):
        return self.name


class ProductGroup(models.Model):
    name = models.CharField("Наименование", max_length=255)
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True,
        related_name="children", verbose_name="Родительская группа",
    )

    class Meta:
        verbose_name = "Группа товаров"
        verbose_name_plural = "Группы товаров"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def descendant_ids(self):
        """id группы и всех вложенных групп."""
        ids = [self.pk]
        for child in self.children.all():
            ids.extend(child.descendant_ids())
        return ids


class Product(models.Model):
    TYPE_PRODUCT = "product"
    TYPE_SERVICE = "service"
    TYPE_CHOICES = [
        (TYPE_PRODUCT, "Товар"),
        (TYPE_SERVICE, "Услуга"),
    ]

    item_type = models.CharField("Тип", max_length=16, choices=TYPE_CHOICES, default=TYPE_PRODUCT)
    group = models.ForeignKey(
        ProductGroup, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="products", verbose_name="Группа",
    )
    name = models.CharField("Наименование", max_length=512)
    article = models.CharField("Артикул", max_length=64, blank=True, db_index=True)
    code = models.CharField("Код", max_length=64, blank=True, db_index=True)
    barcode = models.CharField("Штрихкод", max_length=64, blank=True, db_index=True)
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, verbose_name="Ед. изм.")
    vat_rate = models.CharField("Ставка НДС", max_length=8, choices=VAT_CHOICES, default=VAT_20)

    purchase_price = models.DecimalField("Закупочная цена", max_digits=15, decimal_places=2, default=0)
    sale_price = models.DecimalField("Цена продажи", max_digits=15, decimal_places=2, default=0)
    min_stock = models.DecimalField("Неснижаемый остаток", max_digits=15, decimal_places=3, default=0)

    description = models.TextField("Описание", blank=True)
    image = models.ImageField("Изображение", upload_to="products/", blank=True, null=True)
    is_active = models.BooleanField("Действующий", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Товар"
        verbose_name_plural = "Товары"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_service(self):
        return self.item_type == self.TYPE_SERVICE
