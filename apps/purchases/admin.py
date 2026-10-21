from django.contrib import admin

from apps.inventory.admin_guard import StockDocumentAdmin

from .models import Receipt, ReceiptLine, SupplierReturn, SupplierReturnLine


class ReceiptLineInline(admin.TabularInline):
    model = ReceiptLine
    extra = 0


@admin.register(Receipt)
class ReceiptAdmin(StockDocumentAdmin):
    list_display = ("number", "date", "supplier", "warehouse", "status")
    list_filter = ("status", "warehouse")
    inlines = [ReceiptLineInline]


class SupplierReturnLineInline(admin.TabularInline):
    model = SupplierReturnLine
    extra = 0


@admin.register(SupplierReturn)
class SupplierReturnAdmin(StockDocumentAdmin):
    list_display = ("number", "date", "supplier", "warehouse", "status")
    list_filter = ("status", "warehouse")
    inlines = [SupplierReturnLineInline]
