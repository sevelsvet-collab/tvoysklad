from django.contrib import admin

from .models import Product, ProductGroup, Unit


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ("name", "okei_code")


@admin.register(ProductGroup)
class ProductGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "parent")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "article", "group", "unit", "sale_price", "is_active")
    search_fields = ("name", "article", "code", "barcode")
    list_filter = ("item_type", "group", "is_active")
