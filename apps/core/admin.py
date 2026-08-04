from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import DocumentNumber, Organization, User, Warehouse


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("Дополнительно", {"fields": ("middle_name", "phone")}),
    )


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "inn", "kpp", "vat_payer", "is_default", "is_active")


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ("name", "address", "is_default", "is_active")


@admin.register(DocumentNumber)
class DocumentNumberAdmin(admin.ModelAdmin):
    list_display = ("organization", "doc_type", "year", "last_number")
