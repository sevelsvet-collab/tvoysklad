from django.urls import path

from . import views

urlpatterns = [
    path("purchases/receipts/", views.ReceiptListView.as_view(), name="receipt_list"),
    path("purchases/receipts/new/", views.ReceiptCreateView.as_view(), name="receipt_create"),
    path("purchases/receipts/<int:pk>/", views.ReceiptUpdateView.as_view(), name="receipt_edit"),
    path("purchases/receipts/<int:pk>/post/", views.receipt_post, name="receipt_post"),
    path("purchases/receipts/<int:pk>/unpost/", views.receipt_unpost, name="receipt_unpost"),
    path("purchases/receipts/<int:pk>/delete/", views.receipt_delete, name="receipt_delete"),
    path("purchases/receipts/<int:pk>/return/", views.receipt_to_return, name="receipt_to_return"),

    path("purchases/returns/", views.SupplierReturnListView.as_view(), name="supplier_return_list"),
    path("purchases/returns/new/", views.SupplierReturnCreateView.as_view(), name="supplier_return_create"),
    path("purchases/returns/<int:pk>/", views.SupplierReturnUpdateView.as_view(), name="supplier_return_edit"),
    path("purchases/returns/<int:pk>/post/", views.supplier_return_post, name="supplier_return_post"),
    path("purchases/returns/<int:pk>/unpost/", views.supplier_return_unpost, name="supplier_return_unpost"),
    path("purchases/returns/<int:pk>/delete/", views.supplier_return_delete, name="supplier_return_delete"),
]
