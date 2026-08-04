from django.urls import path

from . import api, views

urlpatterns = [
    path("api/products/search/", api.product_search, name="api_product_search"),
    path("api/products/stock/", api.product_stock, name="api_product_stock"),
    path("api/products/quick-create/", api.product_quick_create, name="api_product_quick_create"),
    path("products/", views.ProductListView.as_view(), name="product_list"),
    path("products/new/", views.ProductCreateView.as_view(), name="product_create"),
    path("products/<int:pk>/", views.ProductUpdateView.as_view(), name="product_edit"),
    path("products/groups/new/", views.GroupCreateView.as_view(), name="group_create"),
    path("products/groups/<int:pk>/", views.GroupUpdateView.as_view(), name="group_edit"),
    path("products/import/", views.CatalogImportView.as_view(), name="catalog_import"),
]
