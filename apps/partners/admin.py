from django.contrib import admin

from apps.core.uploads import validate_scan

from .models import BankAccount, Contract, Counterparty


class BankAccountInline(admin.TabularInline):
    model = BankAccount
    extra = 0


class ContractInline(admin.TabularInline):
    model = Contract
    extra = 0

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "scan":
            kwargs["validators"] = [validate_scan]
        return super().formfield_for_dbfield(db_field, request, **kwargs)


@admin.register(Counterparty)
class CounterpartyAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "partner_type", "inn", "phone", "is_active")
    search_fields = ("name", "inn", "phone", "email")
    list_filter = ("partner_type", "kind", "is_active")
    inlines = [BankAccountInline, ContractInline]
