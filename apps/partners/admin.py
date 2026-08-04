from django.contrib import admin

from .models import BankAccount, Contract, Counterparty


class BankAccountInline(admin.TabularInline):
    model = BankAccount
    extra = 0


class ContractInline(admin.TabularInline):
    model = Contract
    extra = 0


@admin.register(Counterparty)
class CounterpartyAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "partner_type", "inn", "phone", "is_active")
    search_fields = ("name", "inn", "phone", "email")
    list_filter = ("partner_type", "kind", "is_active")
    inlines = [BankAccountInline, ContractInline]
