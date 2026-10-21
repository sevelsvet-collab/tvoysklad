"""Учёт по организациям: перенос данных.

* Организация складского движения и движения серийного номера — из
  документа-источника (вид + id документа). Если документа нет, организация
  остаётся пустой: это решает человек (manage.py stock_transition), по
  умолчанию никакая организация не ставится. Ввод номеров старого остатка
  (serial_stock) документа не имеет — организация пустая.
* Время и момент создания документа в движении номера — из документа (для
  ввода остатка — момент ввода).
* Организация номера — по его последнему движению, если номер на складе.
* Строки остатков (StockBalance) — кэш, посчитанный по старому ключу «склад
  + товар». Они удаляются: общий остаток не делится между организациями
  произвольно, новые остатки строит пересчёт истории (stock_transition
  --apply). До него статус перехода — «ожидает пересчёта», и складские
  операции отклоняются. В базе без движений статус сразу «готово».
"""
import datetime

from django.db import migrations
from django.utils import timezone

STOCK_SOURCES = {
    "receipt": ("purchases", "Receipt"), "supplier_return": ("purchases", "SupplierReturn"),
    "shipment": ("sales", "Shipment"), "customer_return": ("sales", "CustomerReturn"),
    "transfer": ("inventory", "Transfer"), "adjustment_income": ("inventory", "StockAdjustment"),
    "adjustment_expense": ("inventory", "StockAdjustment"),
}
SERIAL_SOURCES = {
    "receipt": ("purchases", "Receipt"), "supplierreturn": ("purchases", "SupplierReturn"),
    "shipment": ("sales", "Shipment"), "customerreturn": ("sales", "CustomerReturn"),
    "transfer": ("inventory", "Transfer"), "stockadjustment": ("inventory", "StockAdjustment"),
}
EARLIEST = datetime.datetime(1, 1, 1, tzinfo=datetime.timezone.utc)


def _documents(apps, sources, doc_type, ids):
    app_label, model_name = sources[doc_type]
    model = apps.get_model(app_label, model_name)
    return {row["pk"]: row for row in model.objects.filter(pk__in=ids).values(
        "pk", "organization_id", "time", "created_at")}


def fill(apps, schema_editor):
    movement_model = apps.get_model("inventory", "StockMovement")
    for doc_type in sorted(set(movement_model.objects.values_list("doc_type", flat=True))):
        if doc_type not in STOCK_SOURCES:
            continue
        ids = set(movement_model.objects.filter(doc_type=doc_type).values_list("doc_id", flat=True))
        by_org = {}
        for pk, row in _documents(apps, STOCK_SOURCES, doc_type, ids).items():
            by_org.setdefault(row["organization_id"], []).append(pk)
        for org_id, doc_ids in by_org.items():
            for start in range(0, len(doc_ids), 500):
                movement_model.objects.filter(doc_type=doc_type, doc_id__in=doc_ids[start:start + 500]).update(
                    organization_id=org_id)

    serial_movement_model = apps.get_model("inventory", "SerialMovement")
    for doc_type in sorted(set(serial_movement_model.objects.values_list("doc_type", flat=True))):
        rows = serial_movement_model.objects.filter(doc_type=doc_type)
        if doc_type not in SERIAL_SOURCES:      # ввод старого остатка: момент ввода, организация неизвестна
            for movement in rows.only("pk", "created_at"):
                local = timezone.localtime(movement.created_at) if movement.created_at else None
                serial_movement_model.objects.filter(pk=movement.pk).update(
                    time=local.time().replace(microsecond=0) if local else datetime.time(0, 0),
                    doc_created_at=movement.created_at)
            continue
        documents = _documents(apps, SERIAL_SOURCES, doc_type, set(rows.values_list("doc_id", flat=True)))
        for doc_id, row in documents.items():
            rows.filter(doc_id=doc_id).update(organization_id=row["organization_id"], time=row["time"],
                                              doc_created_at=row["created_at"])

    serial_model = apps.get_model("inventory", "SerialNumber")
    for serial in serial_model.objects.exclude(warehouse=None):
        last = sorted(serial_movement_model.objects.filter(serial_id=serial.pk),
                      key=lambda m: (m.date, m.time, m.doc_created_at or EARLIEST, m.doc_type, m.doc_id,
                                     m.quantity, m.pk))
        owner = last[-1].organization_id if last and last[-1].quantity > 0 else None
        serial_model.objects.filter(pk=serial.pk).update(organization_id=owner)

    apps.get_model("inventory", "StockBalance").objects.all().delete()
    state_model = apps.get_model("inventory", "StockLedgerState")
    ready = not movement_model.objects.exists()
    state_model.objects.update_or_create(pk=1, defaults={
        "status": "ready" if ready else "pending",
        "message": "" if ready else "Остатки и себестоимость нужно пересчитать по организациям: "
                                    "manage.py stock_transition --apply --i-have-a-backup",
        "finished_at": timezone.now() if ready else None,
    })


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0007_stock_organization"),
        ("purchases", "0005_receiptline_serial_numbers_and_more"),
        ("sales", "0008_return_line_link"),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
