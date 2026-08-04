from django.contrib import admin

from .models import (
    CustomerReturn,
    CustomerReturnLine,
    Invoice,
    InvoiceLine,
    Shipment,
    ShipmentLine,
)


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "date", "customer", "status", "paid_amount")
    list_filter = ("status",)
    inlines = [InvoiceLineInline]


class ShipmentLineInline(admin.TabularInline):
    model = ShipmentLine
    extra = 0


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ("number", "date", "customer", "warehouse", "status")
    list_filter = ("status", "warehouse")
    inlines = [ShipmentLineInline]


class CustomerReturnLineInline(admin.TabularInline):
    model = CustomerReturnLine
    extra = 0


@admin.register(CustomerReturn)
class CustomerReturnAdmin(admin.ModelAdmin):
    list_display = ("number", "date", "customer", "warehouse", "status")
    list_filter = ("status", "warehouse")
    inlines = [CustomerReturnLineInline]
