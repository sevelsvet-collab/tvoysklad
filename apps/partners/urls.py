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
    path("counterparties/bank-lookup/", views.bank_lookup, name="bank_lookup"),

    # Договоры
    path("contracts/", views.ContractListView.as_view(), name="contract_list"),
    path("contracts/new/", views.ContractCreateView.as_view(), name="contract_create"),
    path("contracts/questions/", views.contract_questions, name="contract_questions"),
    path("contracts/<int:pk>/", views.ContractUpdateView.as_view(), name="contract_edit"),
    path("contracts/<int:pk>/pdf/", views.contract_download, {"fmt": "pdf"}, name="contract_pdf"),
    path("contracts/<int:pk>/docx/", views.contract_download, {"fmt": "docx"}, name="contract_docx"),
    path("contracts/<int:pk>/delete/", views.contract_delete, name="contract_delete"),
    path("contracts/bulk-delete/", views.contract_bulk_delete, name="contract_bulk_delete"),

    # Шаблоны договоров
    path("contract-templates/", views.ContractTemplateListView.as_view(), name="contract_template_list"),
    path("contract-templates/new/", views.ContractTemplateCreateView.as_view(), name="contract_template_create"),
    path("contract-templates/<int:pk>/", views.ContractTemplateUpdateView.as_view(), name="contract_template_edit"),
    path("contract-templates/<int:pk>/delete/", views.contract_template_delete, name="contract_template_delete"),
]
