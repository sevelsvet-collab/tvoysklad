"""Взаиморасчёты с контрагентами: прежний отчёт, предложение и программа — настоящими документами.

Только временная база; рабочую скрипт не трогает (проверяет имя и что база
пустая):

    DATABASE_URL=sqlite:///<временный файл> python -X utf8 docs/proposals/settlements_current.py

Для каждого сценария печатаются документы, строка прежнего отчёта
«Взаиморасчёты» (до 18.10.2026; формула сохранена в
apps/finance/settlements.previous_report), показатели счетов покупателю
(выставлено, оплачено по счёту, отгружено по счёту), расчёт по уточнённому
предложению (proposed_settlements — отдельная функция этого файла, код
программы не использует; правила — docs/settlements-proposal.md, независимая
модель — settlements_model.py) и новый расчёт программы
(apps/finance/settlements.settlement_pairs). Предложение и программа должны
совпасть; пары, где есть только выставленный счёт, программа показывает с
нулями (счёт задолженность не создаёт), предложение — не показывает.

Платежи здесь создаются без явного назначения для взаиморасчётов — как старые
при переходе: однозначно определяется только входящий платёж со ссылкой на
счёт покупателю («оплата от покупателя»), остальные — «не распределено».
Корректировки взаиморасчётов — «без стороны». Ожидаемые числа записаны
константами: при расхождении скрипт останавливается.

Разбор платежей существующей базы (только чтение, для копии данных):

    DATABASE_URL=... python -X utf8 docs/proposals/settlements_current.py --existing
"""
import os
import sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()
from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402

name = str(settings.DATABASES["default"]["NAME"])
assert os.path.abspath(name) != os.path.abspath(os.path.join(ROOT, "db.sqlite3")) and name != "sklad", \
    "только временная база: рабочую db.sqlite3 и базу sklad скрипт не использует"

from apps.catalog.models import Product, Unit  # noqa: E402
from apps.core.constants import DOC_POSTED  # noqa: E402
from apps.core.models import Organization, Warehouse  # noqa: E402
from apps.finance.models import Account, Payment, SettlementCorrection  # noqa: E402
from apps.finance import settlements  # noqa: E402
from apps.inventory.models import StockAdjustment  # noqa: E402
from apps.partners.documents import counterparty_documents  # noqa: E402
from apps.partners.models import Counterparty  # noqa: E402
from apps.purchases.models import Receipt, SupplierReturn  # noqa: E402
from apps.sales.models import CustomerReturn, Invoice, Shipment  # noqa: E402

D = Decimal
ZERO = D("0")
LOG = []


def money(value):
    return f"{D(value).quantize(D('0.01')):f}"


# ---------------------------------------------------------------------------
# Документы — обычными методами программы
# ---------------------------------------------------------------------------

class World:
    def __init__(self):
        call_command("migrate", "--noinput", verbosity=0)
        assert not Organization.objects.exists(), "база не пустая — нужна новая временная база"
        self.org_a = Organization.objects.create(name="Организация А", is_default=True)
        self.org_b = Organization.objects.create(name="Организация Б")
        self.bank = {
            self.org_a.pk: Account.objects.create(organization=self.org_a, name="Р/с А", is_default=True),
            self.org_b.pk: Account.objects.create(organization=self.org_b, name="Р/с Б", is_default=True),
        }
        self.wh = Warehouse.objects.create(name="Склад", is_default=True)
        self.product = Product.objects.create(name="Товар", unit=Unit.objects.create(name="шт"), purchase_price=10)
        stock = StockAdjustment.objects.create(kind=StockAdjustment.KIND_INCOME, organization=self.org_a,
                                               warehouse=self.wh)
        stock.lines.create(product=self.product, quantity=10000, price=10)
        stock.post()                                           # товар на складе — оприходованием, не закупкой

    def party(self, label, kind=Counterparty.TYPE_CUSTOMER):
        return Counterparty.objects.create(name=label, partner_type=kind)

    def _org(self, org):
        return org or self.org_a

    def invoice(self, cp, amount, org=None):
        inv = Invoice.objects.create(organization=self._org(org), warehouse=self.wh, customer=cp)
        inv.lines.create(product=self.product, quantity=1, price=amount, vat_rate="none")
        inv.post()
        LOG.append(f"счёт № {inv.number} ({inv.organization}) выставлен на {money(amount)}")
        return inv

    def ship(self, cp, qty, price, invoice=None, org=None):
        doc = Shipment.objects.create(organization=self._org(org), warehouse=self.wh, customer=cp, invoice=invoice)
        doc.lines.create(product=self.product, quantity=qty, price=price, vat_rate="none")
        doc.post()
        basis = f" по счёту № {invoice.number}" if invoice else " без счёта"
        LOG.append(f"отгрузка № {doc.number} ({doc.organization}){basis}: {qty} × {price} = {money(doc.total)}")
        return doc

    def customer_return(self, cp, qty, price, basis=None, org=None):
        doc = CustomerReturn.objects.create(organization=self._org(org), warehouse=self.wh, customer=cp,
                                            shipment=basis)
        doc.lines.create(product=self.product, quantity=qty, price=price, vat_rate="none")
        doc.post()
        how = f"по отгрузке № {basis.number}" if basis else "без основания"
        LOG.append(f"возврат покупателя № {doc.number} {how}: {qty} × {price} = {money(doc.total)}")
        return doc

    def receipt(self, cp, qty, price, org=None):
        doc = Receipt.objects.create(organization=self._org(org), warehouse=self.wh, supplier=cp)
        doc.lines.create(product=self.product, quantity=qty, price=price, vat_rate="none")
        doc.post()
        LOG.append(f"приёмка № {doc.number}: {qty} × {price} = {money(doc.total)}")
        return doc

    def supplier_return(self, cp, qty, price, basis, org=None):
        doc = SupplierReturn.objects.create(organization=self._org(org), warehouse=self.wh, supplier=cp,
                                            receipt=basis)
        doc.lines.create(product=self.product, quantity=qty, price=price, vat_rate="none")
        doc.post()
        LOG.append(f"возврат поставщику № {doc.number} по приёмке № {basis.number}: {money(doc.total)}")
        return doc

    def pay(self, cp, kind, amount, org=None, invoice=None):
        org = self._org(org)
        payment = Payment.objects.create(kind=kind, organization=org, account=self.bank[org.pk], counterparty=cp,
                                         invoice=invoice, amount=amount, status=DOC_POSTED)
        what = "получено от контрагента" if kind == Payment.KIND_IN else "уплачено контрагенту"
        link = f" по счёту № {invoice.number}" if invoice else ""
        LOG.append(f"{what} {money(amount)} ({org}){link}")
        return payment

    def correction(self, cp, direction, amount, org=None):
        doc = SettlementCorrection.objects.create(organization=self._org(org), counterparty=cp, direction=direction,
                                                  amount=amount)
        doc.post()
        LOG.append(f"корректировка взаиморасчётов № {doc.number}: «{doc.get_direction_display()}» {money(amount)}")
        return doc


# ---------------------------------------------------------------------------
# Предлагаемая модель (docs/settlements-proposal.md) — отдельно от кода отчёта
# ---------------------------------------------------------------------------

def _total(docs):
    return sum((doc.total for doc in docs), ZERO)


CUSTOMER_PAYMENT = "оплата от покупателя"
UNALLOCATED = "не распределено"


def legacy_purpose(payment):
    """Назначение старого платежа — только однозначное: входящий платёж со
    ссылкой на счёт покупателю — оплата от покупателя; остальное не угадывается."""
    if payment.kind == Payment.KIND_IN and payment.invoice_id:
        return CUSTOMER_PAYMENT
    return UNALLOCATED


def proposed_settlements(cp):
    """Расчёт по паре «наша организация + контрагент» из проведённых
    документов: по продажам, по закупкам, не распределено, корректировки без
    стороны, справочная разница, окончательно ли."""
    docs = {
        "shipments": Shipment.objects.filter(customer=cp, status=DOC_POSTED),
        "customer_returns": CustomerReturn.objects.filter(customer=cp, status=DOC_POSTED),
        "receipts": Receipt.objects.filter(supplier=cp, status=DOC_POSTED),
        "supplier_returns": SupplierReturn.objects.filter(supplier=cp, status=DOC_POSTED),
        "payments": Payment.objects.filter(counterparty=cp, status=DOC_POSTED),
        "corrections": SettlementCorrection.objects.filter(counterparty=cp, status=DOC_POSTED),
    }
    org_ids = set()
    for queryset in docs.values():
        org_ids.update(queryset.values_list("organization_id", flat=True))
    rows = []
    for org in Organization.objects.filter(pk__in=org_ids).order_by("name"):
        of = {key: queryset.filter(organization=org) for key, queryset in docs.items()}
        paid_by_customer = sum((p.amount for p in of["payments"] if legacy_purpose(p) == CUSTOMER_PAYMENT), ZERO)
        unallocated_in = sum((p.amount for p in of["payments"]
                              if p.kind == Payment.KIND_IN and legacy_purpose(p) == UNALLOCATED), ZERO)
        unallocated_out = sum((p.amount for p in of["payments"] if p.kind == Payment.KIND_OUT), ZERO)
        unsided = sum((c.signed_amount for c in of["corrections"]), ZERO)
        sales = _total(of["shipments"]) - _total(of["customer_returns"]) - paid_by_customer
        purchases = _total(of["receipts"]) - _total(of["supplier_returns"])
        difference = sales - purchases - unallocated_in + unallocated_out + unsided
        # окончательно — только если нет ни одного нераспределённого документа:
        # суммы таких документов могут взаимно гаситься (исправлено 18.10.2026,
        # прежде проверялись только суммы)
        unallocated_docs = [p for p in of["payments"] if legacy_purpose(p) == UNALLOCATED]
        final = not (unallocated_docs or of["corrections"].exists())
        notes = []
        if unallocated_in or unallocated_out:
            notes.append(f"не распределено: получено {money(unallocated_in)}, уплачено {money(unallocated_out)}")
        if unsided:
            notes.append(f"корректировки без стороны {money(unsided)}")
        if final and sales < 0:
            notes.append(f"аванс или переплата покупателя {money(-sales)}")
        if final and purchases < 0:
            notes.append(f"аванс поставщику {money(-purchases)}")
        notes.append("окончательно" if final else "стороны предварительные, разница — справочно")
        rows.append({"org": org, "sales": sales, "purchases": purchases, "unallocated_in": unallocated_in,
                     "unallocated_out": unallocated_out, "unsided": unsided, "difference": difference,
                     "final": final, "notes": notes})
    return rows


# ---------------------------------------------------------------------------
# Показ и проверка
# ---------------------------------------------------------------------------

def current_row(cp):
    """Строка прежнего отчёта (до 18.10.2026) для контрагента."""
    for row in settlements.previous_report():
        if row["cp"].pk == cp.pk:
            return row
    return None


def show(title, cp, current, proposed):
    """current — ожидаемое сальдо отчёта (None — строки нет); proposed —
    {организация: (по продажам, по закупкам, разница, окончательно ли)}."""
    global LOG
    print(f"\n  {title}")
    for line in LOG:
        print(f"    • {line}")
    LOG = []
    row = current_row(cp)
    if row is None:
        print("    прежний отчёт: строки нет (как будто расчётов нет)")
        got_current = None
    else:
        print(f"    прежний отчёт: продажи (счета) {money(row['sales'])}, оплатил нам {money(row['paid_in'])}, "
              f"возврат нам {money(row['cust_return'])}, закупки {money(row['purch'])}, "
              f"оплатили ему {money(row['paid_out'])}, возврат ему {money(row['supp_return'])}, "
              f"корректировка {money(row['correction'])} → сальдо {money(row['balance'])}")
        got_current = money(row["balance"])
    for inv in Invoice.objects.filter(customer=cp, status=Invoice.STATUS_ISSUED).order_by("pk"):
        print(f"    счёт № {inv.number} ({inv.organization}): выставлен {money(inv.total)}, "
              f"оплачено по счёту {money(inv.paid_amount)} ({inv.payment_status_display.lower()}), "
              f"отгружено по счёту {money(inv.shipped_amount)}")
    got_proposed = {}
    for item in proposed_settlements(cp):
        got_proposed[item["org"].name] = (money(item["sales"]), money(item["purchases"]),
                                          money(item["difference"]), item["final"])
        print(f"    предложение, {item['org']}: по продажам (он должен) {money(item['sales'])}, по закупкам "
              f"(мы должны) {money(item['purchases'])}, разница {money(item['difference'])}; "
              f"{'; '.join(item['notes'])}")
    want_current = None if current is None else money(current)
    want_proposed = {org: (money(s), money(p), money(d), final) for org, (s, p, d, final) in proposed.items()}
    assert (got_current, got_proposed) == (want_current, want_proposed), \
        f"{title}: получено {(got_current, got_proposed)}, ожидалось {(want_current, want_proposed)}"
    got_program = {}
    for item in settlements.settlement_pairs(counterparty=cp):
        values = (money(item.sales), money(item.purchases), money(item.difference), item.final)
        if item.organization.name not in want_proposed:
            # только выставленный счёт: задолженности нет — проверяется отдельно
            assert item.invoiced and values == ("0.00", "0.00", "0.00", True), (title, item.organization, values)
            print(f"    программа, {item.organization}: только счёт — задолженности нет ({item.status.lower()})")
            continue
        got_program[item.organization.name] = values
        print(f"    программа, {item.organization}: {item.status.lower()}; " + " ".join(item.explanations))
    assert got_program == want_proposed, f"{title}: программа {got_program}, предложение {want_proposed}"


def card(cp):
    """Карточка контрагента: вкладка «Документы» — сводка тем же расчётом и список документов."""
    docs = counterparty_documents(cp)
    kinds = sorted({doc["type_label"] for doc in docs})
    pairs = settlements.settlement_pairs(counterparty=cp)
    summary = "; ".join(f"{item.organization}: {item.status.lower()}, по продажам {money(item.sales)}, "
                        f"по закупкам {money(item.purchases)}" for item in pairs)
    print(f"    карточка контрагента: сводка — {summary}; документов {len(docs)} ({', '.join(kinds)})")


def main():
    w = World()
    A, B = "Организация А", "Организация Б"
    IN, OUT = Payment.KIND_IN, Payment.KIND_OUT

    print("Сценарий 1. Счёт выставлен, отгрузки и оплаты нет.")
    cp = w.party("Покупатель 1")
    w.invoice(cp, 1000)
    show("после выставления счёта", cp, current="1000", proposed={})

    print("\nСценарий 2. Отгрузка без счёта.")
    cp = w.party("Покупатель 2")
    w.ship(cp, 6, 100)
    show("после отгрузки", cp, current=None, proposed={A: ("600", "0", "600", True)})

    print("\nСценарий 3. Счёт на 1000, частичная отгрузка на 400.")
    cp = w.party("Покупатель 3")
    inv = w.invoice(cp, 1000)
    w.ship(cp, 4, 100, invoice=inv)
    show("после отгрузки 400", cp, current="1000", proposed={A: ("400", "0", "400", True)})

    print("\nСценарий 4. Несколько отгрузок по одному счёту.")
    cp = w.party("Покупатель 4")
    inv = w.invoice(cp, 1000)
    w.ship(cp, 4, 100, invoice=inv)
    show("первая отгрузка 400", cp, current="1000", proposed={A: ("400", "0", "400", True)})
    w.ship(cp, 6, 100, invoice=inv)
    show("вторая отгрузка 600", cp, current="1000", proposed={A: ("1000", "0", "1000", True)})
    w.ship(cp, 2, 100, invoice=inv)
    show("третья отгрузка 200 — сверх счёта", cp, current="1000", proposed={A: ("1200", "0", "1200", True)})

    print("\nСценарий 5. Аванс до отгрузки, частичная оплата и переплата (платежи по счёту).")
    cp = w.party("Покупатель 5")
    inv = w.invoice(cp, 1000)
    w.pay(cp, IN, 1000, invoice=inv)
    show("аванс 1000 по счёту, отгрузок нет", cp, current="0", proposed={A: ("-1000", "0", "-1000", True)})
    w.ship(cp, 4, 100, invoice=inv)
    show("отгружено 400", cp, current="0", proposed={A: ("-600", "0", "-600", True)})
    w.ship(cp, 6, 100, invoice=inv)
    show("отгружено ещё 600", cp, current="0", proposed={A: ("0", "0", "0", True)})
    cp = w.party("Покупатель 5б")
    inv = w.invoice(cp, 1000)
    w.ship(cp, 10, 100, invoice=inv)
    w.pay(cp, IN, 300, invoice=inv)
    show("отгружено 1000, оплачено 300", cp, current="700", proposed={A: ("700", "0", "700", True)})
    w.pay(cp, IN, 900, invoice=inv)
    show("оплачено ещё 900 — переплата", cp, current="-200", proposed={A: ("-200", "0", "-200", True)})

    print("\nСценарий 6. Возврат покупателя с основанием и без него, затем возврат денег.")
    cp = w.party("Покупатель 6")
    inv = w.invoice(cp, 1000)
    basis = w.ship(cp, 10, 100, invoice=inv)
    w.pay(cp, IN, 1000, invoice=inv)
    show("отгружено и оплачено 1000", cp, current="0", proposed={A: ("0", "0", "0", True)})
    w.customer_return(cp, 3, 100, basis=basis)
    w.customer_return(cp, 2, 100)
    show("возвраты товара 300 (по отгрузке) и 200 (без основания)", cp, current="-500",
         proposed={A: ("-500", "0", "-500", True)})
    w.pay(cp, OUT, 500)
    show("вернули покупателю деньги 500 (исходящий платёж, назначение не указано)", cp, current="0",
         proposed={A: ("-500", "0", "0", False)})
    card(cp)

    print("\nСценарий 7. Один контрагент и две наши организации.")
    cp = w.party("Покупатель 7")
    inv = w.invoice(cp, 1000, org=w.org_a)
    w.ship(cp, 10, 100, invoice=inv, org=w.org_a)
    w.pay(cp, IN, 1000, org=w.org_b)
    show("должен А 1000, заплатил Б 1000 (без счёта)", cp, current="0",
         proposed={A: ("1000", "0", "1000", True), B: ("0", "0", "-1000", False)})

    print("\nСценарий 8. Контрагент — и покупатель, и поставщик.")
    cp = w.party("Покупатель-поставщик 8", kind=Counterparty.TYPE_BOTH)
    inv = w.invoice(cp, 1000)
    w.ship(cp, 10, 100, invoice=inv)
    w.receipt(cp, 7, 100)
    w.pay(cp, IN, 400, invoice=inv)
    w.pay(cp, OUT, 200)
    show("он должен за товар 600; мы ему 700, уплачено 200 без назначения", cp, current="100",
         proposed={A: ("600", "700", "100", False)})

    print("\nСценарий 9. Приёмка, аванс поставщику, возврат поставщику и деньги от него.")
    cp = w.party("Поставщик 9", kind=Counterparty.TYPE_SUPPLIER)
    w.pay(cp, OUT, 1000)
    show("аванс поставщику 1000 (назначение не указано)", cp, current="1000",
         proposed={A: ("0", "0", "1000", False)})
    basis = w.receipt(cp, 10, 100)
    show("приёмка 1000", cp, current="0", proposed={A: ("0", "1000", "0", False)})
    w.supplier_return(cp, 2, 100, basis)
    show("возврат поставщику 200", cp, current="200", proposed={A: ("0", "800", "200", False)})
    w.pay(cp, IN, 200)
    show("поставщик вернул деньги 200", cp, current="0", proposed={A: ("0", "800", "0", False)})

    print("\nСценарий 10. Корректировки взаиморасчётов, отмена документов, повторный расчёт.")
    cp = w.party("Покупатель 10")
    inv = w.invoice(cp, 1000)
    shipment = w.ship(cp, 10, 100, invoice=inv)
    they_owe = w.correction(cp, SettlementCorrection.DIR_THEY_OWE, 100)
    show("корректировка «он должен» 100", cp, current="1100", proposed={A: ("1000", "0", "1100", False)})
    w.correction(cp, SettlementCorrection.DIR_WE_OWE, 30)
    show("корректировка «мы должны» 30", cp, current="1070", proposed={A: ("1000", "0", "1070", False)})
    they_owe.unpost()
    LOG.append("корректировка «он должен» 100 снята с проведения")
    show("после отмены корректировки", cp, current="970", proposed={A: ("1000", "0", "970", False)})
    Shipment.objects.get(pk=shipment.pk).unpost()
    LOG.append("отгрузка снята с проведения")
    show("после отмены отгрузки", cp, current="970", proposed={A: ("0", "0", "-30", False)})
    Invoice.objects.get(pk=inv.pk).unpost()
    LOG.append("счёт возвращён в черновик")
    show("после возврата счёта в черновик", cp, current="-30", proposed={A: ("0", "0", "-30", False)})
    show("повторный расчёт без изменений", cp, current="-30", proposed={A: ("0", "0", "-30", False)})
    card(cp)

    print("\nВсе сценарии сошлись с записанными значениями.")


def existing_report():
    """Только чтение: разбор старых платежей и корректировок по парам."""
    total = {"однозначно": 0, "уточнить": 0, "корректировок без стороны": 0}
    pairs = {}
    for payment in Payment.objects.filter(status=DOC_POSTED).select_related("organization", "counterparty"):
        pairs.setdefault((payment.organization, payment.counterparty), []).append(payment)
    for (org, cp), payments in sorted(pairs.items(), key=lambda item: (item[0][0].name, item[0][1].name)):
        has_sales = Shipment.objects.filter(customer=cp, organization=org, status=DOC_POSTED).exists() or \
            CustomerReturn.objects.filter(customer=cp, organization=org, status=DOC_POSTED).exists()
        has_purchases = Receipt.objects.filter(supplier=cp, organization=org, status=DOC_POSTED).exists() or \
            SupplierReturn.objects.filter(supplier=cp, organization=org, status=DOC_POSTED).exists()
        print(f"\n{org} + {cp}: документы продаж — {'есть' if has_sales else 'нет'}, закупок — "
              f"{'есть' if has_purchases else 'нет'}")
        for payment in sorted(payments, key=lambda p: (p.date, p.pk)):
            purpose = legacy_purpose(payment)
            if purpose == CUSTOMER_PAYMENT:
                total["однозначно"] += 1
                why = f"по счёту № {payment.invoice.number} — оплата от покупателя"
            else:
                total["уточнить"] += 1
                if payment.kind == Payment.KIND_IN:
                    hint = "вероятно, оплата от покупателя" if has_sales and not has_purchases else \
                        "вероятно, возврат от поставщика" if has_purchases and not has_sales else "неясно"
                else:
                    hint = "вероятно, оплата поставщику" if has_purchases and not has_sales else \
                        "вероятно, возврат покупателю" if has_sales and not has_purchases else "неясно"
                why = f"уточнить (подсказка: {hint})"
            kind = "входящий" if payment.kind == Payment.KIND_IN else "исходящий"
            print(f"  {kind} № {payment.number} от {payment.date:%d.%m.%Y} на {money(payment.amount)}: {why}")
    for correction in SettlementCorrection.objects.filter(status=DOC_POSTED):
        total["корректировок без стороны"] += 1
        print(f"\nкорректировка взаиморасчётов № {correction.number} ({correction.organization} + "
              f"{correction.counterparty}): «{correction.get_direction_display()}» {money(correction.amount)} — "
              "сторона не указана")
    print(f"\nИтого платежей: однозначно {total['однозначно']}, уточнить {total['уточнить']}; "
          f"корректировок без стороны: {total['корректировок без стороны']}")


if __name__ == "__main__":
    if "--existing" in sys.argv:
        existing_report()
    else:
        main()
