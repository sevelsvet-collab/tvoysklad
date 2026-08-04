from django.urls import path

from . import api, views

urlpatterns = [
    path("api/counterparties/search/", api.counterparty_search, name="api_counterparty_search"),
    path("api/counterparties/quick-create/", api.counterparty_quick_create, name="api_counterparty_quick_create"),
    path("counterparties/", views.CounterpartyListView.as_view(), name="counterparty_list"),
    path("counterparties/new/", views.CounterpartyCreateView.as_view(), name="counterparty_create"),
    path("counterparties/<int:pk>/", views.CounterpartyUpdateView.as_view(), name="counterparty_edit"),
    path("counterparties/import/", views.PartnersImportView.as_view(), name="partners_import"),
    path("counterparties/inn-lookup/", views.inn_lookup, name="inn_lookup"),
]
