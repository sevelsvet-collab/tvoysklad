"""Взаиморасчёты: «прежний отчёт / новый расчёт» и документы для ручного
уточнения. Только чтение — ничего не записывает и ключей исправления не имеет.

    python manage.py check_settlements

Показывает:

1. По каждому контрагенту — сальдо прежнего отчёта (до 18.10.2026: счета
   вместо отгрузок, все организации вместе, все входящие деньги — оплата
   продаж, все исходящие — оплата закупок) и новый расчёт по парам
   «организация + контрагент»: по продажам, по закупкам, не распределено,
   справочная разница, окончательно или предварительно.
2. Проведённые документы для ручного уточнения: платежи «не распределено»
   и корректировки взаиморасчётов без стороны — с подсказкой по товарным
   документам пары. Подсказка ничего не меняет: назначение ставит человек в
   карточке платежа (поле «Назначение для взаиморасчётов»), сторону — в
   карточке корректировки.
3. Платежи с назначением, которое не подходит к направлению денег или к
   счёту-основанию (любой статус). Такие связи остаются от старых данных;
   их исправляют вручную.
"""
from django.core.management.base import BaseCommand

from apps.finance import services, settlements
from apps.finance.models import Payment
from apps.finance.settlements import money


def _state(item):
    return "окончательно" if item.final else "предварительно"


class Command(BaseCommand):
    help = "Взаиморасчёты: прежний отчёт и новый расчёт, документы для уточнения (только чтение)."

    def handle(self, *args, **options):
        out = self.stdout.write
        out("Взаиморасчёты: прежний отчёт и новый расчёт (только чтение, ничего не записывается).")

        pairs = settlements.settlement_pairs()
        by_counterparty = {}
        for item in pairs:
            by_counterparty.setdefault(item.counterparty.pk, []).append(item)
        previous = {row["cp"].pk: row for row in settlements.previous_report()}
        counterparties = {item.counterparty.pk: item.counterparty for item in pairs}
        counterparties.update({pk: row["cp"] for pk, row in previous.items()})

        out("")
        out(f"1. Прежний отчёт → новый расчёт. Контрагентов: {len(counterparties)}")
        for pk in sorted(counterparties, key=lambda key: (counterparties[key].name, key)):
            cp = counterparties[pk]
            row = previous.get(pk)
            if row is None:
                old = "строки не было"
            else:
                side = "нам должны" if row["balance"] > 0 else "мы должны" if row["balance"] < 0 else "ноль"
                old = (f"сальдо {money(row['balance'])} ({side}): счета {money(row['sales'])}, "
                       f"получено {money(row['paid_in'])}, приёмки {money(row['purch'])}, "
                       f"уплачено {money(row['paid_out'])}")
            out(f"  {cp} (id {pk}) — прежний отчёт: {old}")
            for item in by_counterparty.get(pk, []):
                out(f"      {item.organization}: по продажам {money(item.sales)}, по закупкам {money(item.purchases)}, "
                    f"не распределено: получено {money(item.unallocated_in)}, уплачено {money(item.unallocated_out)}, "
                    f"корректировки без стороны {len(item.unsided_corrections)} "
                    f"(итог {money(item.unsided)}); разница {money(item.difference)} — {_state(item)}")
                if item.invoiced:
                    out(f"        справочно: выставлено счетов {money(item.invoiced)}, оплачено по счетам "
                        f"{money(item.invoice_paid)}, отгружено по счетам {money(item.invoice_shipped)}")

        documents = [(item, doc) for item in pairs for doc in item.unallocated_docs]
        out("")
        out(f"2. Документы для ручного уточнения: {len(documents)}")
        for item, doc in documents:
            if doc["kind"] == "payment":
                kind = Payment.KIND_IN if doc["effect"] == "получено" else Payment.KIND_OUT
                hint = settlements.purpose_hint(item, kind)
                what = "назначение «Не распределено»"
            else:
                hint = "укажите сторону: продажи или закупки"
                what = "без стороны"
            out(f"  {doc['type_label']} № {doc['number']} от {doc['date']:%d.%m.%Y} (id {doc['pk']}), "
                f"{item.counterparty}, {item.organization}: {doc['effect']} {money(doc['amount'])} — {what}; "
                f"подсказка: {hint}")

        incompatible = []
        for payment in Payment.objects.select_related("counterparty", "organization").order_by("pk"):
            errors = services.purpose_errors(services.link_values(payment))
            if errors:
                incompatible.append((payment, errors["settlement_purpose"]))
        out("")
        out(f"3. Платежи с несовместимым назначением (не исправляются автоматически): {len(incompatible)}")
        for payment, text in incompatible:
            out(f"  {payment} (id {payment.pk}), {payment.counterparty}, {payment.get_status_display().lower()}: "
                f"{text}")

        out("")
        out(f"Итого: строк прежнего отчёта — {len(previous)}, пар в новом расчёте — {len(pairs)} "
            f"(предварительных — {sum(1 for item in pairs if not item.final)}), документов для уточнения — "
            f"{len(documents)}, несовместимых назначений — {len(incompatible)}. Ничего не изменено.")
