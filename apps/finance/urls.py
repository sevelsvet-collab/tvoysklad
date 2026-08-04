from django.urls import path

from . import views
from .models import Payment

urlpatterns = [
    # Счета и кассы
    path("money/accounts/", views.AccountListView.as_view(), name="account_list"),
    path("money/accounts/new/", views.AccountCreateView.as_view(), name="account_create"),
    path("money/accounts/<int:pk>/", views.AccountUpdateView.as_view(), name="account_edit"),

    # Платежи
    path("money/payments/", views.PaymentListView.as_view(), name="payment_list"),
    path("money/payments/incoming/new/", views.PaymentCreateView.as_view(kind=Payment.KIND_IN), name="payment_create_in"),
    path("money/payments/outgoing/new/", views.PaymentCreateView.as_view(kind=Payment.KIND_OUT), name="payment_create_out"),
    path("money/payments/<int:pk>/", views.PaymentUpdateView.as_view(), name="payment_edit"),
    path("money/payments/<int:pk>/post/", views.payment_post, name="payment_post"),
    path("money/payments/<int:pk>/unpost/", views.payment_unpost, name="payment_unpost"),
    path("money/payments/<int:pk>/delete/", views.payment_delete, name="payment_delete"),
    path("money/invoices/<int:pk>/pay/", views.invoice_to_payment, name="invoice_to_payment"),

    # Взаиморасчёты
    path("money/settlements/", views.SettlementsView.as_view(), name="settlements"),
]
