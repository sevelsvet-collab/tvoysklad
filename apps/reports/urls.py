from django.urls import path

from . import views

urlpatterns = [
    path("reports/sales/", views.SalesReportView.as_view(), name="report_sales"),
    path("reports/cashflow/", views.CashflowReportView.as_view(), name="report_cashflow"),
]
