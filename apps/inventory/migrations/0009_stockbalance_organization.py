"""Учёт по организациям: ключ остатка «организация + склад + товар».

Строки остатков к этому моменту удалены (0008), поэтому обязательное поле
добавляется к пустой таблице.

Обратный путь (откат): строки разных организаций одного склада и товара
сливаются в одну — количество и стоимость складываются, средняя =
стоимость ÷ количество. Это НЕ прежние суммы: средняя смешивается, а
принадлежность теряется. Прежний код пересчитает остаток товара по-своему
только при следующей операции с ним.
"""
from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


def merge_organizations(apps, schema_editor):
    balance_model = apps.get_model("inventory", "StockBalance")
    groups = {}
    for balance in balance_model.objects.order_by("product_id", "warehouse_id", "pk"):
        groups.setdefault((balance.product_id, balance.warehouse_id), []).append(balance)
    for rows in groups.values():
        keep = rows[0]
        quantity = sum((row.quantity for row in rows), Decimal("0"))
        value = sum((row.value or Decimal("0") for row in rows), Decimal("0"))
        average = (value / quantity).quantize(Decimal("0.01")) if quantity > 0 else Decimal("0")
        balance_model.objects.filter(pk=keep.pk).update(quantity=quantity, value=value, avg_cost=average)
        balance_model.objects.filter(pk__in=[row.pk for row in rows[1:]]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0008_fill_stock_organization"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="stockbalance", name="uniq_stock_balance"),
        migrations.AddField(
            model_name="stockbalance",
            name="organization",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                                    to="core.organization", verbose_name="Организация"),
        ),
        migrations.RunPython(migrations.RunPython.noop, merge_organizations),
        migrations.AlterField(
            model_name="stockbalance",
            name="organization",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+",
                                    to="core.organization", verbose_name="Организация"),
        ),
        migrations.AddConstraint(
            model_name="stockbalance",
            constraint=models.UniqueConstraint(fields=("organization", "product", "warehouse"),
                                               name="uniq_stock_balance_org"),
        ),
    ]
