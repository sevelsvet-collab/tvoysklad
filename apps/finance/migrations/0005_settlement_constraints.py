"""Ограничения базы для назначения платежа и стороны корректировки.

Отдельной миграцией после переноса данных (0004): на PostgreSQL изменение
строк и ALTER TABLE той же таблицы в одной транзакции не смешиваются.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0004_settlement_purpose_and_side"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(settlement_purpose__in=[
                        "customer_payment", "customer_refund", "supplier_payment", "supplier_refund", "unallocated",
                    ])
                    & ~models.Q(kind="incoming", settlement_purpose__in=["customer_refund", "supplier_payment"])
                    & ~models.Q(kind="outgoing", settlement_purpose__in=["customer_payment", "supplier_refund"])
                ),
                name="finance_payment_settlement_purpose_valid",
                violation_error_message="Назначение для взаиморасчётов не указано или не подходит к направлению платежа",
            ),
        ),
        migrations.AddConstraint(
            model_name="settlementcorrection",
            constraint=models.CheckConstraint(
                condition=models.Q(side__in=["sales", "purchases", "unallocated"]),
                name="finance_settlementcorrection_side_valid",
                violation_error_message="Укажите сторону расчётов: продажи, закупки или «не распределено»",
            ),
        ),
    ]
