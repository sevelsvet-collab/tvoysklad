"""Учёт по организациям: поля и таблицы (без данных).

Организация у складского движения, у серийного номера и его движения; время
и момент создания документа в движении номера (порядок истории номера);
статус перехода и нарушения, созданные переходом. Данные — 0008, ключ
остатков — 0009.
"""
import datetime

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_submittoken"),
        ("catalog", "0001_initial"),
        ("inventory", "0006_deficit_cover"),
    ]

    operations = [
        migrations.AddField(
            model_name="stockmovement",
            name="organization",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name="+", to="core.organization", verbose_name="Организация"),
        ),
        migrations.AddIndex(
            model_name="stockmovement",
            index=models.Index(fields=["product", "organization", "warehouse"], name="inventory_s_product_org_wh"),
        ),
        migrations.AddField(
            model_name="serialnumber",
            name="organization",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name="+", to="core.organization", verbose_name="Организация"),
        ),
        migrations.AddField(
            model_name="serialmovement",
            name="organization",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name="+", to="core.organization", verbose_name="Организация"),
        ),
        migrations.AddField(
            model_name="serialmovement",
            name="time",
            field=models.TimeField(default=datetime.time(0, 0), verbose_name="Время документа"),
        ),
        migrations.AddField(
            model_name="serialmovement",
            name="doc_created_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Документ создан"),
        ),
        migrations.CreateModel(
            name="StockLedgerState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending", "Ожидает пересчёта"),
                                                     ("running", "Пересчёт выполняется или прерван"),
                                                     ("ready", "Готово")],
                                            default="pending", max_length=16, verbose_name="Состояние")),
                ("products_total", models.PositiveIntegerField(default=0, verbose_name="Товаров к пересчёту")),
                ("products_done", models.PositiveIntegerField(default=0, verbose_name="Пересчитано товаров")),
                ("started_at", models.DateTimeField(blank=True, null=True, verbose_name="Пересчёт начат")),
                ("finished_at", models.DateTimeField(blank=True, null=True, verbose_name="Пересчёт завершён")),
                ("message", models.TextField(blank=True, verbose_name="Пояснение")),
            ],
            options={
                "verbose_name": "Состояние перехода складского учёта",
                "verbose_name_plural": "Состояние перехода складского учёта",
            },
        ),
        migrations.CreateModel(
            name="StockTransitionIssue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("baseline", models.JSONField(default=list, verbose_name="История склада в целом при переходе")),
                ("description", models.TextField(verbose_name="Описание")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+",
                                                   to="core.organization")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+",
                                              to="catalog.product")),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+",
                                                to="core.warehouse")),
            ],
            options={
                "verbose_name": "Нарушение после перехода на учёт по организациям",
                "verbose_name_plural": "Нарушения после перехода на учёт по организациям",
                "constraints": [models.UniqueConstraint(fields=("product", "organization", "warehouse"),
                                                        name="uniq_transition_issue")],
            },
        ),
    ]
