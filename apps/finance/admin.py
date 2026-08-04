from django.contrib import admin

from .models import Account, Payment


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "organization", "opening_balance", "is_default", "is_active")
    list_filter = ("kind", "is_active")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("number", "kind", "date", "counterparty", "account", "amount", "status")
    list_filter = ("kind", "status", "account")
    search_fields = ("number", "counterparty__name", "purpose")
