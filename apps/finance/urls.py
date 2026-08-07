from django.urls import path

from . import views
from .models import Account, Payment

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

    # Корректировки
    path("money/corrections/", views.CorrectionListView.as_view(), name="correction_list"),
    path("money/corrections/cash/new/",
         views.AccountCorrectionCreateView.as_view(account_kind=Account.KIND_CASH), name="cash_correction_create"),
    path("money/corrections/bank/new/",
         views.AccountCorrectionCreateView.as_view(account_kind=Account.KIND_BANK), name="bank_correction_create"),
    path("money/corrections/account/<int:pk>/",
         views.AccountCorrectionUpdateView.as_view(), name="account_correction_edit"),
    path("money/corrections/account/<int:pk>/post/",
         views.account_correction_post, name="account_correction_post"),
    path("money/corrections/account/<int:pk>/unpost/",
         views.account_correction_unpost, name="account_correction_unpost"),
    path("money/corrections/account/<int:pk>/delete/",
         views.account_correction_delete, name="account_correction_delete"),
    path("money/corrections/settlement/new/",
         views.SettlementCorrectionCreateView.as_view(), name="settlement_correction_create"),
    path("money/corrections/settlement/<int:pk>/",
         views.SettlementCorrectionUpdateView.as_view(), name="settlement_correction_edit"),
    path("money/corrections/settlement/<int:pk>/post/",
         views.settlement_correction_post, name="settlement_correction_post"),
    path("money/corrections/settlement/<int:pk>/unpost/",
         views.settlement_correction_unpost, name="settlement_correction_unpost"),
    path("money/corrections/settlement/<int:pk>/delete/",
         views.settlement_correction_delete, name="settlement_correction_delete"),
]
