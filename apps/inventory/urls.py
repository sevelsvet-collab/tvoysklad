from django.urls import path

from . import views
from .models import StockAdjustment

INCOME = StockAdjustment.KIND_INCOME
EXPENSE = StockAdjustment.KIND_EXPENSE

urlpatterns = [
    path("stock/balances/", views.BalanceListView.as_view(), name="balance_list"),

    path("stock/transfers/", views.TransferListView.as_view(), name="transfer_list"),
    path("stock/transfers/new/", views.TransferCreateView.as_view(), name="transfer_create"),
    path("stock/transfers/<int:pk>/", views.TransferUpdateView.as_view(), name="transfer_edit"),
    path("stock/transfers/<int:pk>/post/", views.transfer_post, name="transfer_post"),
    path("stock/transfers/<int:pk>/unpost/", views.transfer_unpost, name="transfer_unpost"),
    path("stock/transfers/<int:pk>/delete/", views.transfer_delete, name="transfer_delete"),

    # Оприходования / Списания
    path("stock/income/", views.AdjustmentListView.as_view(kind=INCOME), name="adjustment_list_income"),
    path("stock/expense/", views.AdjustmentListView.as_view(kind=EXPENSE), name="adjustment_list_expense"),
    path("stock/adjustments/income/new/", views.AdjustmentCreateView.as_view(kind=INCOME), name="adjustment_create_income"),
    path("stock/adjustments/expense/new/", views.AdjustmentCreateView.as_view(kind=EXPENSE), name="adjustment_create_expense"),
    path("stock/adjustments/edit/<int:pk>/", views.AdjustmentUpdateView.as_view(), name="adjustment_edit"),
    path("stock/adjustments/<int:pk>/post/", views.adjustment_post, name="adjustment_post"),
    path("stock/adjustments/<int:pk>/unpost/", views.adjustment_unpost, name="adjustment_unpost"),
    path("stock/adjustments/<int:pk>/delete/", views.adjustment_delete, name="adjustment_delete"),
    path("stock/adjustments/<str:kind>/", views.AdjustmentListView.as_view(), name="adjustment_list"),
]
