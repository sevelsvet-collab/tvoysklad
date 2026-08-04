from django.urls import path

from . import views

urlpatterns = [
    path("sales/invoices/", views.InvoiceListView.as_view(), name="invoice_list"),
    path("sales/invoices/new/", views.InvoiceCreateView.as_view(), name="invoice_create"),
    path("sales/invoices/<int:pk>/", views.InvoiceUpdateView.as_view(), name="invoice_edit"),
    path("sales/invoices/<int:pk>/post/", views.invoice_post, name="invoice_post"),
    path("sales/invoices/<int:pk>/unpost/", views.invoice_unpost, name="invoice_unpost"),
    path("sales/invoices/<int:pk>/delete/", views.invoice_delete, name="invoice_delete"),
    path("sales/invoices/<int:pk>/ship/", views.invoice_to_shipment, name="invoice_to_shipment"),

    path("sales/shipments/", views.ShipmentListView.as_view(), name="shipment_list"),
    path("sales/shipments/new/", views.ShipmentCreateView.as_view(), name="shipment_create"),
    path("sales/shipments/<int:pk>/", views.ShipmentUpdateView.as_view(), name="shipment_edit"),
    path("sales/shipments/<int:pk>/post/", views.shipment_post, name="shipment_post"),
    path("sales/shipments/<int:pk>/unpost/", views.shipment_unpost, name="shipment_unpost"),
    path("sales/shipments/<int:pk>/delete/", views.shipment_delete, name="shipment_delete"),
    path("sales/shipments/<int:pk>/return/", views.shipment_to_return, name="shipment_to_return"),

    path("sales/returns/", views.CustomerReturnListView.as_view(), name="customer_return_list"),
    path("sales/returns/new/", views.CustomerReturnCreateView.as_view(), name="customer_return_create"),
    path("sales/returns/<int:pk>/", views.CustomerReturnUpdateView.as_view(), name="customer_return_edit"),
    path("sales/returns/<int:pk>/post/", views.customer_return_post, name="customer_return_post"),
    path("sales/returns/<int:pk>/unpost/", views.customer_return_unpost, name="customer_return_unpost"),
    path("sales/returns/<int:pk>/delete/", views.customer_return_delete, name="customer_return_delete"),
]
