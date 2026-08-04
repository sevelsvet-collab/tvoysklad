from django.urls import path

from . import views

urlpatterns = [
    path("print/invoice/<int:pk>/payment/", views.invoice_payment, name="print_invoice_payment"),
    path("print/shipment/<int:pk>/torg12/", views.shipment_torg12, name="print_shipment_torg12"),
    path("print/shipment/<int:pk>/act/", views.shipment_act, name="print_shipment_act"),
    path("print/shipment/<int:pk>/upd/", views.shipment_upd, name="print_shipment_upd"),
]
