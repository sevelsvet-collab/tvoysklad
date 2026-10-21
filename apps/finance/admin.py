from django.contrib import admin

from .models import Account, Payment


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "organization", "opening_balance", "is_default", "is_active")
    list_filter = ("kind", "is_active", "organization")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """Платежи в админке подчиняются тем же правилам, что и в программе.

    Сохранение, удаление одного платежа и «удалить выбранные» идут через
    Payment.save(), Payment.delete() и PaymentQuerySet.delete(): оплата счёта
    пересчитывается в той же транзакции. Несовместимые связи (чужой счёт или
    касса, чужой покупатель) и назначение для взаиморасчётов, не подходящее
    к направлению денег или счёту-основанию, отклоняет Payment.clean() —
    ошибкой в форме; пустое назначение определяется там же по счёту.
    """

    list_display = ("number", "kind", "date", "organization", "counterparty", "account", "invoice", "amount",
                    "settlement_purpose", "status")
    list_filter = ("kind", "status", "settlement_purpose", "organization", "account")
    search_fields = ("number", "counterparty__name", "purpose")
