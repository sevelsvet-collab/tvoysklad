"""Типовой конструктор «Договор купли-продажи товара».

Создаёт шаблон с 15 разделами: состояние товара (новый / б/у / восстановленный),
осмотр до покупки, объём гарантии, переход права собственности и рисков,
заверения об обстоятельствах и налоговая оговорка.

Ранее заключённые договоры не меняются — их текст хранится снимком.
"""
from django.db import migrations


def create_sale(apps, schema_editor):
    ContractTemplate = apps.get_model("partners", "ContractTemplate")
    ContractQuestion = apps.get_model("partners", "ContractQuestion")
    ContractOption = apps.get_model("partners", "ContractOption")
    Contract = apps.get_model("partners", "Contract")
    from apps.partners.builtin_sale import INTRO, OUTRO, QUESTIONS

    # старый шаблон купли-продажи (без конструктора) убираем из выбора
    for template in ContractTemplate.objects.filter(kind="sale"):
        if Contract.objects.filter(template=template).exists():
            template.is_active = False
            template.is_builtin = False
            template.name = f"{template.name} (старая версия)"
            template.save()
        else:
            template.delete()

    template = ContractTemplate.objects.create(
        name="Договор купли-продажи (конструктор)",
        kind="sale",
        title="ДОГОВОР КУПЛИ-ПРОДАЖИ ТОВАРА",
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


def remove_sale(apps, schema_editor):
    ContractTemplate = apps.get_model("partners", "ContractTemplate")
    ContractTemplate.objects.filter(
        kind="sale", is_builtin=True, name="Договор купли-продажи (конструктор)",
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("partners", "0009_contract_goods_condition_contract_goods_details"),
    ]

    operations = [
        migrations.RunPython(create_sale, remove_sale),
    ]
