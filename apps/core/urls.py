from django.urls import path

from . import views

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),

    path("settings/organizations/", views.OrganizationListView.as_view(), name="organization_list"),
    path("settings/organizations/new/", views.OrganizationCreateView.as_view(), name="organization_create"),
    path("settings/organizations/<int:pk>/", views.OrganizationUpdateView.as_view(), name="organization_edit"),

    path("settings/warehouses/", views.WarehouseListView.as_view(), name="warehouse_list"),
    path("settings/warehouses/new/", views.WarehouseCreateView.as_view(), name="warehouse_create"),
    path("settings/warehouses/<int:pk>/", views.WarehouseUpdateView.as_view(), name="warehouse_edit"),

    path("settings/users/", views.UserListView.as_view(), name="user_list"),
]
