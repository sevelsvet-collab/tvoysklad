"""Развёрнутый конструктор «Договор поставки» — та же глубина, что и купля-продажа.

15 разделов вместо 11: заявки и периоды поставки, отгрузочная разнарядка,
восполнение недопоставки, выборка товара, ответственное хранение, возвратная
тара, заверения об обстоятельствах, налоговая оговорка, отказ от договора
по статье 523 ГК РФ, исчисление убытков по статье 524 ГК РФ.

Ранее заключённые договоры не меняются — их текст хранится снимком, а старый
шаблон, если по нему есть договоры, помечается как старая версия.
"""
from django.db import migrations


def rebuild(apps, schema_editor):
    ContractTemplate = apps.get_model("partners", "ContractTemplate")
    ContractQuestion = apps.get_model("partners", "ContractQuestion")
    ContractOption = apps.get_model("partners", "ContractOption")
    Contract = apps.get_model("partners", "Contract")
    from apps.partners.builtin_supply import INTRO, OUTRO, QUESTIONS

    for template in ContractTemplate.objects.filter(is_builtin=True, kind="supply"):
        if Contract.objects.filter(template=template).exists():
            template.is_active = False
            template.is_builtin = False
            template.name = f"{template.name} (старая версия)"
            template.save()
        else:
            template.delete()

    template = ContractTemplate.objects.create(
        name="Договор поставки (конструктор)",
        kind="supply",
        title="ДОГОВОР ПОСТАВКИ",
        intro=INTRO,
        outro=OUTRO,
        is_builtin=True,
        is_active=True,
    )
    for q_order, (section, title, hint, options, allow_none) in enumerate(QUESTIONS, start=1):
        question = ContractQuestion.objects.create(
            template=template, section=section, order=q_order * 10,
            title=title, help_text=hint, allow_none=allow_none, allow_custom=True,
        )
        for o_order, (label, body, is_default) in enumerate(options, start=1):
            ContractOption.objects.create(
                question=question, order=o_order * 10, label=label, body=body, is_default=is_default,
            )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("partners", "0012_alter_contract_penalty_rate"),
    ]

    operations = [
        migrations.RunPython(rebuild, noop),
    ]
