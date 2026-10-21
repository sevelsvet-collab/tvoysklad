"""Документы-источники складских движений: вид движения → (приложение, модель).

Общая таблица для диагностики перехода на учёт по организациям (transition)."""

STOCK_SOURCES = {
    "receipt": ("purchases", "Receipt"), "supplier_return": ("purchases", "SupplierReturn"),
    "shipment": ("sales", "Shipment"), "customer_return": ("sales", "CustomerReturn"),
    "transfer": ("inventory", "Transfer"), "adjustment_income": ("inventory", "StockAdjustment"),
    "adjustment_expense": ("inventory", "StockAdjustment"),
}
