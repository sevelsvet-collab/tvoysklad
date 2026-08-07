from django.urls import path

from . import views

urlpatterns = [
    path("print/invoice/<int:pk>/payment/", views.invoice_payment, name="print_invoice_payment"),
    path("print/invoice/<int:pk>/payment-qr/", views.invoice_payment, {"with_qr": True}, name="print_invoice_payment_qr"),
    path("send/invoice/<int:pk>/email/", views.invoice_send_email, name="send_invoice_email"),
    path("send/invoice/<int:pk>/email-qr/", views.invoice_send_email, {"with_qr": True}, name="send_invoice_email_qr"),
    path("print/shipment/<int:pk>/torg12/", views.shipment_torg12, name="print_shipment_torg12"),
    path("print/shipment/<int:pk>/act/", views.shipment_act, name="print_shipment_act"),
    path("print/shipment/<int:pk>/upd/", views.shipment_upd, name="print_shipment_upd"),
]
