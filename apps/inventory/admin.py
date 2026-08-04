from django.contrib import admin

from .models import (
    AdjustmentLine,
    StockAdjustment,
    StockBalance,
    StockMovement,
    Transfer,
    TransferLine,
)


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("date", "product", "warehouse", "quantity", "cost", "doc_type", "doc_number")
    list_filter = ("doc_type", "warehouse")
    search_fields = ("product__name", "doc_number")


@admin.register(StockBalance)
class StockBalanceAdmin(admin.ModelAdmin):
    list_display = ("product", "warehouse", "quantity", "avg_cost")
    list_filter = ("warehouse",)
    search_fields = ("product__name",)


class TransferLineInline(admin.TabularInline):
    model = TransferLine
    extra = 0


@admin.register(Transfer)
class TransferAdmin(admin.ModelAdmin):
    list_display = ("number", "date", "warehouse_from", "warehouse_to", "status")
    inlines = [TransferLineInline]


class AdjustmentLineInline(admin.TabularInline):
    model = AdjustmentLine
    extra = 0


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(admin.ModelAdmin):
    list_display = ("number", "kind", "date", "warehouse", "status")
    list_filter = ("kind", "status", "warehouse")
    inlines = [AdjustmentLineInline]
