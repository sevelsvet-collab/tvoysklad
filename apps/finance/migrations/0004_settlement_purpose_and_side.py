"""Назначение платежа для взаиморасчётов и сторона корректировки взаиморасчётов.

Перенос старых данных — только однозначное, ничего не угадывается:
* входящий платёж с действительной ссылкой на счёт покупателю (счёт той же
  организации и того же покупателя) — «оплата от покупателя»;
* все остальные платежи — «не распределено» (в том числе со ссылкой на
  счёт, несовместимой с платежом: такие показывает диагностика
  check_payments / check_settlements);
* все корректировки взаиморасчётов — «не распределено» (без стороны).
Тип контрагента, текст назначения и наличие закупок не используются.
"""
from django.db import migrations, models


def fill_settlement_purpose(apps, schema_editor):
    payment_model = apps.get_model("finance", "Payment")
    invoice_model = apps.get_model("sales", "Invoice")
    invoices = {
        row["pk"]: (row["organization_id"], row["customer_id"])
        for row in invoice_model.objects.values("pk", "organization_id", "customer_id")
    }
    customer_payments = []
    rows = payment_model.objects.filter(kind="incoming", invoice_id__isnull=False).values(
        "pk", "invoice_id", "organization_id", "counterparty_id")
    for row in rows:
        if invoices.get(row["invoice_id"]) == (row["organization_id"], row["counterparty_id"]):
            customer_payments.append(row["pk"])
    payment_model.objects.update(settlement_purpose="unallocated")
    for start in range(0, len(customer_payments), 500):
        payment_model.objects.filter(pk__in=customer_payments[start:start + 500]).update(
            settlement_purpose="customer_payment")
    apps.get_model("finance", "SettlementCorrection").objects.update(side="unallocated")


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0003_alter_accountcorrection_options_and_more"),
        ("sales", "0008_return_line_link"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="settlement_purpose",
            field=models.CharField(
                blank=True,
                choices=[
                    ("customer_payment", "Оплата от покупателя"),
                    ("customer_refund", "Возврат покупателю"),
                    ("supplier_payment", "Оплата поставщику"),
                    ("supplier_refund", "Возврат от поставщика"),
                    ("unallocated", "Не распределено"),
                ],
                default="",
                help_text="Что оплачено: продажа или закупка. «Не распределено» — показывается в расчётах отдельно",
                max_length=24,
                verbose_name="Назначение для взаиморасчётов",
            ),
        ),
        migrations.AddField(
            model_name="settlementcorrection",
            name="side",
            field=models.CharField(
                choices=[
                    ("sales", "Продажи (расчёты с покупателем)"),
                    ("purchases", "Закупки (расчёты с поставщиком)"),
                    ("unallocated", "Не распределено"),
                ],
                default="unallocated",
                max_length=16,
                verbose_name="Сторона расчётов",
            ),
        ),
        migrations.RunPython(fill_settlement_purpose, migrations.RunPython.noop),
    ]
