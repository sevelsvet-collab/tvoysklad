from django import forms

from apps.core.forms import BootstrapFormMixin, ImportForm  # noqa: F401 — реэкспорт для views

from .models import Product, ProductGroup


class ProductForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "item_type", "group", "name", "article", "code", "barcode",
            "unit", "vat_rate", "purchase_price", "sale_price", "min_stock",
            "description", "image", "is_active", "track_serials",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def clean(self):
        cleaned = super().clean()
        # у услуг нет складского движения — и серийных номеров тоже
        if cleaned.get("item_type") == Product.TYPE_SERVICE:
            cleaned["track_serials"] = False
        return cleaned


class ProductGroupForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ProductGroup
        fields = ["name", "parent"]
