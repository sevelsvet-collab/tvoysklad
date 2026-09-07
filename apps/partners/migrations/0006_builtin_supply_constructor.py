"""Типовой конструктор «Договор поставки»: вопросы и готовые формулировки."""
from django.db import migrations


def create_constructor(apps, schema_editor):
    ContractTemplate = apps.get_model("partners", "ContractTemplate")
    ContractQuestion = apps.get_model("partners", "ContractQuestion")
    ContractOption = apps.get_model("partners", "ContractOption")
    from apps.partners.builtin_supply import INTRO, OUTRO, QUESTIONS

    # старые «сплошные» шаблоны из поставки больше не нужны
    ContractTemplate.objects.filter(is_builtin=True).delete()

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


def remove_constructor(apps, schema_editor):
    apps.get_model("partners", "ContractTemplate").objects.filter(is_builtin=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("partners", "0005_contract_place_contracttemplate_intro_and_more"),
    ]

    operations = [
        migrations.RunPython(create_constructor, remove_constructor),
    ]
