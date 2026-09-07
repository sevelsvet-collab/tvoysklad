"""Предмет из карточки договора включаем по умолчанию.

Необязательные пункты без явного варианта по умолчанию теперь в договор
не попадают. Для «Уточнить предмет поставки» это нежелательно — предмет
должен быть в тексте, поэтому помечаем вариант как выбранный по умолчанию.
"""
from django.db import migrations


def set_default(apps, schema_editor):
    ContractOption = apps.get_model("partners", "ContractOption")
    ContractOption.objects.filter(
        question__template__kind="supply",
        question__title="Уточнить предмет поставки",
    ).update(is_default=True)


def unset_default(apps, schema_editor):
    ContractOption = apps.get_model("partners", "ContractOption")
    ContractOption.objects.filter(
        question__template__kind="supply",
        question__title="Уточнить предмет поставки",
    ).update(is_default=False)


class Migration(migrations.Migration):
    dependencies = [
        ("partners", "0010_sale_constructor"),
    ]

    operations = [
        migrations.RunPython(set_default, unset_default),
    ]
