from django.contrib import admin

from .admin_guard import ReadOnlyAdmin, StockDocumentAdmin
from .models import (
    AdjustmentLine,
    StockAdjustment,
    StockBalance,
    StockMovement,
    Transfer,
    TransferLine,
)


@admin.register(StockMovement)
class StockMovementAdmin(ReadOnlyAdmin):
    list_display = ("date", "product", "warehouse", "organization", "quantity", "cost", "doc_type", "doc_number")
    list_filter = ("doc_type", "warehouse", "organization")
    search_fields = ("product__name", "doc_number")


@admin.register(StockBalance)
class StockBalanceAdmin(ReadOnlyAdmin):
    list_display = ("product", "warehouse", "organization", "quantity", "avg_cost", "value")
    list_filter = ("warehouse", "organization")
    search_fields = ("product__name",)


class TransferLineInline(admin.TabularInline):
    model = TransferLine
    extra = 0


@admin.register(Transfer)
class TransferAdmin(StockDocumentAdmin):
    list_display = ("number", "date", "warehouse_from", "warehouse_to", "status")
    inlines = [TransferLineInline]


class AdjustmentLineInline(admin.TabularInline):
    model = AdjustmentLine
    extra = 0


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(StockDocumentAdmin):
    list_display = ("number", "kind", "date", "warehouse", "status")
    list_filter = ("kind", "status", "warehouse")
    inlines = [AdjustmentLineInline]
