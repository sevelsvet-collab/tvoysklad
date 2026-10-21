"""Складской учёт по организациям: что делает программа — настоящими документами.

С 18.10.2026 учёт по организациям реализован: программа должна совпадать с
режимом модели «организация + склад + товар» (предложение). До реализации
программа совпадала с режимом «склад + товар» — вывод тех запусков сохранён в
org_stock_current_before_output.txt. Модель не менялась.

Только временная база (скрипт проверяет имя и что база пустая):

    DATABASE_URL=sqlite:///<временный файл> python -X utf8 docs/proposals/org_stock_current.py

Каждая операция выполняется в программе (создание и проведение документов,
снятие с проведения, смена организации с перепроведением — как в форме) и
параллельно в независимой модели docs/proposals/org_stock_model.py в двух
режимах: «склад + товар» (прежнее поведение) и «организация + склад + товар»
(предложение). Скрипт останавливается, если программа расходится с режимом
EXPECTED (сейчас — предложение).

В конце — прототип диагностики переноса (только чтение) на этих же данных:
организация каждого движения по документу-источнику, документы без
источника, и что изменится после разделения: себестоимость отгрузок,
остатки по организациям, новые дефициты и нарушения, прибыль по месяцам.
Тот же разбор для копии данных (только чтение, база должна быть уже
перенесена на новую схему кодом программы):

    DATABASE_URL=... python -X utf8 docs/proposals/org_stock_current.py --existing
"""
import datetime
import os
import sys
from collections import defaultdict
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "docs", "proposals"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()
from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402

name = str(settings.DATABASES["default"]["NAME"])
assert os.path.abspath(name) != os.path.abspath(os.path.join(ROOT, "db.sqlite3")) and name != "sklad", \
    "только временная база или копия: рабочую db.sqlite3 и базу sklad скрипт не использует"

from django.apps import apps  # noqa: E402
from django.core.exceptions import ValidationError  # noqa: E402

import org_stock_model as model  # noqa: E402
from apps.catalog.models import Product, Unit  # noqa: E402
from apps.core.constants import DOC_POSTED  # noqa: E402
from apps.core.models import Organization, Warehouse  # noqa: E402
from apps.inventory import costing  # noqa: E402
from apps.inventory.models import SerialMovement, SerialNumber, StockBalance, StockMovement, Transfer  # noqa: E402
from apps.inventory.services import StockError  # noqa: E402
from apps.partners.models import Counterparty  # noqa: E402
from apps.purchases.models import Receipt, SupplierReturn  # noqa: E402
from apps.sales.models import CustomerReturn, Shipment, ShipmentLine  # noqa: E402

D = Decimal
MODES = (model.CURRENT, model.TARGET)
EXPECTED = model.TARGET          # с какой моделью обязана совпадать программа (до 18.10.2026 — CURRENT)


def show(value):
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        return f"{value.normalize():f}" if value == value.to_integral() else f"{value:f}"
    if isinstance(value, tuple):
        return " / ".join(show(v) for v in value)
    if isinstance(value, dict):
        if not value:
            return "остатков нет"
        return "; ".join(f"{k if isinstance(k, str) else '+'.join(k)}: {show(v)}" for k, v in sorted(value.items(),
                                                                                                    key=str))
    return str(value)


class Twin:
    """Мир программы и две модели; каждая операция — во всех трёх."""

    def __init__(self):
        call_command("migrate", "--noinput", verbosity=0)
        assert not Organization.objects.exists(), "база не пустая — нужна новая временная база"
        self.orgs = {"А": Organization.objects.create(name="А", is_default=True),
                     "Б": Organization.objects.create(name="Б")}
        self.wh = {name: Warehouse.objects.create(name=name, is_default=name == "W") for name in ("W", "W1", "W2")}
        self.unit = Unit.objects.create(name="шт")
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.clock = datetime.datetime(2026, 10, 1, 8, 0)
        self.mismatches = []

    def scenario(self, title, negative, serials=False):
        print(f"\n{title}")
        print(f"  минус разрешён: А — {'да' if negative['А'] else 'нет'}, Б — {'да' if negative['Б'] else 'нет'}")
        for org_name, allowed in negative.items():
            Organization.objects.filter(pk=self.orgs[org_name].pk).update(allow_negative_stock=allowed)
        self.product = Product.objects.create(name=title[:40], unit=self.unit, track_serials=serials)
        self.books = {mode: model.Book(mode, negative) for mode in MODES}
        self.serial_books = {mode: model.Serials(mode) for mode in MODES}
        print("  | Операция | Программа | Модель «склад + товар» (прежнее) | Модель «организация + склад + товар» |")
        print("  |---|---|---|---|")

    def _when(self):
        self.clock += datetime.timedelta(minutes=10)
        return {"date": self.clock.date(), "time": self.clock.time()}

    def _org(self, name):
        return Organization.objects.get(pk=self.orgs[name].pk)

    def _program(self, action):
        try:
            return action(), None
        except (StockError, ValidationError) as exc:
            return None, str(exc).split("\n")[0][:110]

    def _models(self, make, serial_step=None):
        result = {}
        for mode in MODES:
            try:
                if serial_step:
                    serial_step(self.serial_books[mode])
                doc = make(mode)
                result[mode] = (doc, None)
            except model.Refused as exc:
                result[mode] = (None, str(exc))
        return result

    def _row(self, label, program_error, models, detail=None):
        def outcome(error):
            return "проведено" if error is None else "отклонено"

        current = models[EXPECTED][1]
        if (program_error is None) != (current is None):
            self.mismatches.append(f"{label}: программа {outcome(program_error)}, модель {outcome(current)}")
        cells = [outcome(program_error), outcome(models[model.CURRENT][1]), outcome(models[model.TARGET][1])]
        if detail:
            cells = [f"{cell}; {extra}" for cell, extra in zip(cells, detail)]
        print(f"  | {label} | " + " | ".join(cells) + " |")

    # ---------- документы ----------

    def receipt(self, org, wh, qty, price, serial=""):
        def program():
            doc = Receipt.objects.create(organization=self._org(org), warehouse=self.wh[wh], supplier=self.supplier,
                                         **self._when())
            doc.lines.create(product=self.product, quantity=qty, price=price, vat_rate="none", serial_numbers=serial)
            doc.post()
            return doc
        doc, error = self._program(program)
        models = self._models(lambda mode: self.books[mode].post(model.Doc("receipt", org, wh, qty, qty * price)),
                              (lambda book: book.receive(serial, org, wh)) if serial else None)
        self._row(f"{org}: приёмка {qty} × {price} на {wh}", error, models)
        return {"program": doc, **{mode: models[mode][0] for mode in MODES}}

    def ship(self, org, wh, qty, price=300, serial=""):
        def program():
            doc = Shipment.objects.create(organization=self._org(org), warehouse=self.wh[wh], customer=self.customer,
                                          **self._when())
            doc.lines.create(product=self.product, quantity=qty, price=price, vat_rate="none", serial_numbers=serial)
            doc.post()
            return doc
        doc, error = self._program(program)
        models = self._models(lambda mode: self.books[mode].post(model.Doc("ship", org, wh, qty)),
                              (lambda book: book.ship(serial, org, wh)) if serial else None)
        detail = None
        if error is None and all(models[mode][1] is None for mode in MODES):
            line = ShipmentLine.objects.get(shipment=doc)
            detail = (f"себест. {show(line.cost_amount)}, не покр. {show(line.cost_open_qty)}",
                      *(f"себест. {show(models[mode][0].cost)}, не покр. {show(models[mode][0].open)}"
                        for mode in MODES))
            self._compare(f"{org}: отгрузка — себестоимость", (line.cost_amount, line.cost_open_qty),
                          (models[EXPECTED][0].cost, models[EXPECTED][0].open))
        self._row(f"{org}: отгрузка {qty} с {wh}", error, models, detail)
        return {"program": doc, **{mode: models[mode][0] for mode in MODES}}

    def transfer(self, org, wh_from, wh_to, qty, serial=""):
        def program():
            doc = Transfer.objects.create(organization=self._org(org), warehouse_from=self.wh[wh_from],
                                          warehouse_to=self.wh[wh_to], **self._when())
            doc.lines.create(product=self.product, quantity=qty, serial_numbers=serial)
            doc.post()
            return doc
        doc, error = self._program(program)
        models = self._models(
            lambda mode: self.books[mode].post(model.Doc("transfer", org, wh_from, qty, wh_to=wh_to)),
            (lambda book: book.move(serial, org, wh_from, wh_to)) if serial else None)
        self._row(f"{org}: перемещение {qty} {wh_from} → {wh_to}", error, models)
        return {"program": doc, **{mode: models[mode][0] for mode in MODES}}

    def customer_return(self, org, wh, qty, basis=None, price=300):
        def program():
            doc = CustomerReturn.objects.create(organization=self._org(org), warehouse=self.wh[wh],
                                                customer=self.customer,
                                                shipment=basis["program"] if basis else None, **self._when())
            doc.lines.create(product=self.product, quantity=qty, price=price, vat_rate="none")
            doc.post()
            return doc
        doc, error = self._program(program)
        models = self._models(lambda mode: self.books[mode].post(
            model.Doc("customer_return", org, wh, qty, basis=basis[mode] if basis else None)))
        detail = None
        if error is None and all(models[mode][1] is None for mode in MODES):
            amount = StockMovement.objects.get(doc_type="customer_return", doc_id=doc.pk).amount
            detail = (f"сумма {show(amount)}", *(f"сумма {show(models[mode][0].in_amount)}" for mode in MODES))
            self._compare(f"{org}: возврат покупателя — сумма", amount, models[EXPECTED][0].in_amount)
        how = "по отгрузке" if basis else "без основания"
        self._row(f"{org}: возврат покупателя {qty} {how}", error, models, detail)
        return {"program": doc, **{mode: models[mode][0] for mode in MODES}}

    def supplier_return(self, org, wh, qty, basis):
        def program():
            doc = SupplierReturn.objects.create(organization=self._org(org), warehouse=self.wh[wh],
                                                supplier=self.supplier, receipt=basis["program"], **self._when())
            doc.lines.create(product=self.product, quantity=qty, price=100, vat_rate="none")
            doc.post()
            return doc
        doc, error = self._program(program)
        models = self._models(lambda mode: self.books[mode].post(model.Doc("supplier_return", org, wh, qty)))
        detail = None
        if error is None and all(models[mode][1] is None for mode in MODES):
            amount = StockMovement.objects.get(doc_type="supplier_return", doc_id=doc.pk).amount
            detail = (f"списано {show(amount)}", *(f"списано {show(models[mode][0].cost)}" for mode in MODES))
            self._compare(f"{org}: возврат поставщику — списано", amount, models[EXPECTED][0].cost)
        self._row(f"{org}: возврат поставщику {qty}", error, models, detail)
        return {"program": doc, **{mode: models[mode][0] for mode in MODES}}

    def unpost(self, doc, label):
        _, error = self._program(lambda: type(doc["program"]).objects.get(pk=doc["program"].pk).unpost())
        models = {}
        for mode in MODES:
            try:
                self.books[mode].unpost(doc[mode])
                models[mode] = (doc[mode], None)
            except model.Refused as exc:
                models[mode] = (None, str(exc))
        self._row(label, error, models)

    def change_org(self, doc, org, label):
        def program():
            # тот же механизм, что у формы: сохранение и перепроведение — одна транзакция
            type(doc["program"]).objects.get(pk=doc["program"].pk).change_organization(self._org(org))
        _, error = self._program(program)
        models = {}
        for mode in MODES:
            try:
                self.books[mode].change_org(doc[mode], org)
                models[mode] = (doc[mode], None)
            except model.Refused as exc:
                models[mode] = (None, str(exc))
        self._row(label, error, models)

    # ---------- итог сценария ----------

    def _compare(self, what, program, current):
        if program != current:
            self.mismatches.append(f"{what}: программа {program!r}, модель {current!r}")

    def balances(self):
        rows = StockBalance.objects.filter(product=self.product).select_related("warehouse", "organization")
        program = {(b.organization.name, b.warehouse.name): (b.quantity, b.value) for b in rows}
        before = self.books[model.CURRENT].balances()
        target = self.books[EXPECTED].balances()
        self._compare("остатки", program, {k: (q, v) for k, (q, v) in target.items()})
        print(f"  Остатки: программа {show(program)}; модель {show(target)}; прежнее поведение {show(before)}")
        if self.product.track_serials:
            for serial in SerialNumber.objects.filter(product=self.product):
                moves = ", ".join(f"{m.doc_type} {m.warehouse.name} {m.quantity:+d}"
                                  for m in serial.movements.select_related("warehouse").order_by("id"))
                print(f"  Номер {serial.number}: склад {serial.warehouse or '—'}, организация "
                      f"{serial.organization or '—'}, состояние «{serial.get_status_display()}»; движения номера: "
                      f"{moves}")
                print(f"  Модель: номер лежит у {self.serial_books[EXPECTED].where.get(serial.number)}")
                self._compare("номер", (serial.organization.name if serial.organization else None,
                                        serial.warehouse.name if serial.warehouse else None),
                              self.serial_books[EXPECTED].where.get(serial.number) or (None, None))


def run_scenarios():
    t = Twin()
    t.scenario("1. Б (минус запрещён) отгружает товар А", {"А": False, "Б": False})
    t.receipt("А", "W", 1, 100)
    t.ship("Б", "W", 1)
    t.ship("А", "W", 1)
    t.balances()

    t.scenario("2. Б (минус разрешён): её дефицит не покрывается товаром А", {"А": False, "Б": True})
    t.receipt("А", "W", 1, 100)
    t.ship("Б", "W", 1)
    t.receipt("Б", "W", 1, 80)
    t.balances()

    t.scenario("3. А и Б покупают один товар по разным ценам на общий склад", {"А": False, "Б": False})
    t.receipt("А", "W", 10, 100)
    t.receipt("Б", "W", 10, 200)
    t.ship("А", "W", 5)
    t.ship("Б", "W", 2)
    t.balances()

    t.scenario("4. Поступление Б покрывает только её дефицит", {"А": True, "Б": True})
    t.ship("А", "W", 2)
    ship_b = t.ship("Б", "W", 3)
    t.receipt("Б", "W", 3, 100)
    line = ShipmentLine.objects.get(shipment=ship_b["program"])
    print(f"  после приёмки Б: отгрузка Б в программе — себестоимость {show(line.cost_amount)}, не покрыто "
          f"{show(line.cost_open_qty)}")
    t.receipt("А", "W", 2, 120)
    t.balances()

    t.scenario("5. Возвраты на общем складе", {"А": False, "Б": False})
    t.receipt("А", "W", 10, 100)
    receipt_b = t.receipt("Б", "W", 10, 200)
    ship_a = t.ship("А", "W", 5)
    t.customer_return("А", "W", 2, basis=ship_a)
    t.customer_return("Б", "W", 1)
    t.supplier_return("Б", "W", 2, basis=receipt_b)
    t.customer_return("Б", "W", 1, basis=ship_a)
    t.balances()

    t.scenario("6. Перемещения: товар А уходит к Б через перемещение", {"А": False, "Б": False})
    t.receipt("А", "W1", 5, 100)
    t.transfer("Б", "W1", "W2", 5)
    t.ship("Б", "W2", 5)
    t.transfer("А", "W1", "W2", 3)
    t.balances()

    for negative_b in (False, True):
        t.scenario(f"7{'а' if not negative_b else 'б'}. Отмена поступления А после отгрузки Б",
                   {"А": False, "Б": negative_b})
        receipt = t.receipt("А", "W", 1, 100)
        t.ship("Б", "W", 1)
        t.unpost(receipt, "А: снять свою приёмку с проведения")
        t.balances()

    t.scenario("8. Изменение организации проведённой приёмки", {"А": False, "Б": False})
    receipt = t.receipt("А", "W", 1, 100)
    t.ship("А", "W", 1)
    t.change_org(receipt, "Б", "приёмка: организация А → Б, перепроведение")
    t.balances()

    t.scenario("9. Серийный номер: Б перемещает и продаёт номер А", {"А": False, "Б": False}, serials=True)
    t.receipt("А", "W1", 1, 100, serial="SN-1")
    t.transfer("Б", "W1", "W2", 1, serial="SN-1")
    t.ship("Б", "W2", 1, serial="SN-1")
    t.balances()

    print()
    if t.mismatches:
        print("РАСХОЖДЕНИЯ программы с моделью:")
        for line in t.mismatches:
            print("  ", line)
        sys.exit(1)
    print(f"Программа во всех операциях совпала с моделью «{EXPECTED}» (проведение/отказ, себестоимость, "
          "суммы возвратов, остатки, номера).")


# ---------------------------------------------------------------------------
# Прототип диагностики переноса (только чтение)
# ---------------------------------------------------------------------------

SOURCES = {
    "receipt": ("purchases", "Receipt"), "supplier_return": ("purchases", "SupplierReturn"),
    "shipment": ("sales", "Shipment"), "customer_return": ("sales", "CustomerReturn"),
    "transfer": ("inventory", "Transfer"), "adjustment_income": ("inventory", "StockAdjustment"),
    "adjustment_expense": ("inventory", "StockAdjustment"),
}
ISSUES = {"shipment": "ship", "supplier_return": "supplier_return", "adjustment_expense": "write_off"}
INCOMES = {"receipt", "adjustment_income"}


def resolve_organizations():
    """{(тип, id документа): (организация, статус)} и список требующих решения."""
    found, problems = {}, []
    keys = set(StockMovement.objects.values_list("doc_type", "doc_id"))
    by_type = defaultdict(set)
    for doc_type, doc_id in keys:
        by_type[doc_type].add(doc_id)
    for doc_type, ids in sorted(by_type.items()):
        if doc_type not in SOURCES:
            problems.extend(f"движения документа неизвестного вида «{doc_type}» id {doc_id}" for doc_id in sorted(ids))
            continue
        source = apps.get_model(*SOURCES[doc_type])
        rows = {pk: (org, status) for pk, org, status in
                source.objects.filter(pk__in=ids).values_list("pk", "organization_id", "status")}
        for doc_id in sorted(ids):
            if doc_id not in rows:
                problems.append(f"{costing.DOC_TITLES.get(doc_type, doc_type)} id {doc_id}: документа нет, а движения "
                                "есть — организацию не определить")
                continue
            org, status = rows[doc_id]
            if status != DOC_POSTED:
                problems.append(f"{costing.DOC_TITLES.get(doc_type, doc_type)} id {doc_id}: документ не проведён, а "
                                "движения есть — какой организации они принадлежат, решает человек")
            found[(doc_type, doc_id)] = (org, status)
    entries = SerialMovement.objects.filter(doc_type="serial_stock")
    for movement in entries.select_related("serial", "warehouse"):
        problems.append(f"серийный номер {movement.serial.number}: введён как остаток склада «{movement.warehouse}» "
                        f"{movement.date:%d.%m.%Y} без документа — организация номера не определена")
    return found, problems


def analyze():
    """Что изменится после разделения: по каждому товару проигрываются его
    движения в модели в обоих режимах; режим «сейчас» сверяется с записанным."""
    organizations, problems = resolve_organizations()
    names = dict(Organization.objects.values_list("pk", "name"))
    flags = dict(Organization.objects.values_list("name", "allow_negative_stock"))
    stores = dict(Warehouse.objects.values_list("pk", "name"))
    print("\nДиагностика переноса (прототип, только чтение)")
    print(f"  движений: {StockMovement.objects.count()}, документов-источников: {len(organizations)}, "
          f"организаций: {len(set(org for org, _ in organizations.values()))}")
    print(f"  требует решения (источник отсутствует или неоднозначен): {len(problems)}")
    for line in problems:
        print("   —", line)
    changed_lines, new_problems, sanity = [], [], [0, 0]
    profit = defaultdict(lambda: D("0"))
    for product_id in sorted(set(StockMovement.objects.values_list("product_id", flat=True))):
        movements = list(StockMovement.objects.filter(product_id=product_id).order_by(*costing.ORDER))
        orgs_by_pair = defaultdict(set)
        for m in movements:
            org = organizations.get((m.doc_type, m.doc_id), (None, None))[0]
            orgs_by_pair[m.warehouse_id].add(org)
        books = {mode: model.Book(mode, flags) for mode in MODES}
        docs, transfers, shipments = {}, {}, defaultdict(dict)
        for m in movements:
            org_id = organizations.get((m.doc_type, m.doc_id), (None, None))[0]
            org, wh = names.get(org_id, "?"), stores[m.warehouse_id]
            made = None
            if m.doc_type in INCOMES and m.quantity > 0:
                amount = m.amount if m.amount is not None else costing.implied_amount(m.quantity, m.cost)
                made = {mode: model.Doc("receipt", org, wh, m.quantity, amount) for mode in MODES}
            elif m.doc_type in ISSUES and m.quantity < 0:
                made = {mode: model.Doc(ISSUES[m.doc_type], org, wh, -m.quantity) for mode in MODES}
                if m.doc_type == "shipment":
                    shipments[m.doc_id][m.line_id] = made
            elif m.doc_type == "transfer" and m.quantity < 0:
                made = {mode: model.Doc("transfer", org, wh, -m.quantity) for mode in MODES}
                transfers.setdefault(m.doc_id, []).append(made)
            elif m.doc_type == "transfer" and m.quantity > 0:
                pending = transfers.get(m.doc_id) or []
                for item in pending:
                    if item[model.CURRENT].wh_to is None:
                        for mode in MODES:
                            item[mode].wh_to = wh
                        break
                continue
            elif m.doc_type == "customer_return" and m.quantity > 0:
                ret = CustomerReturn.objects.filter(pk=m.doc_id).first()
                line = ret.lines.filter(pk=m.line_id).first() if ret and m.line_id else None
                candidates = shipments.get(ret.shipment_id, {}) if ret and ret.shipment_id else {}
                basis = None
                if line is not None and line.shipment_line_id in candidates:
                    basis = candidates[line.shipment_line_id]
                elif len(candidates) == 1:
                    basis = next(iter(candidates.values()))
                elif candidates:
                    problems.append(f"возврат покупателя id {m.doc_id}: строка отгрузки не определена — "
                                    "в прототипе не разбирается")
                fallback = line.fallback_cost if line is not None and line.fallback_cost is not None else m.cost
                made = {mode: model.Doc("customer_return", org, wh, m.quantity,
                                        basis=basis[mode] if basis else None, fallback=fallback) for mode in MODES}
            if made is None:
                continue
            for mode in MODES:
                made[mode].posted = True
                books[mode].docs.append(made[mode])
            docs[m.pk] = made
        results = {mode: books[mode].replay() for mode in MODES}
        for pk, made in docs.items():
            m = next(x for x in movements if x.pk == pk)
            stored = m.amount if m.amount is not None else costing.implied_amount(m.quantity, m.cost)
            current = made[EXPECTED].cost if m.quantity < 0 else made[EXPECTED].in_amount
            sanity[0] += 1
            sanity[1] += stored == current
            if stored != current:
                print(f"   ! {costing._doc_label(m)}, движение #{m.pk}: записано {show(stored)}, модель {show(current)}")
            if m.doc_type == "shipment":
                before, after = made[model.CURRENT], made[model.TARGET]
                if (before.cost, before.open) != (after.cost, after.open):
                    changed_lines.append((m, before.cost, before.open, after.cost, after.open))
                    profit[m.date.strftime("%m.%Y")] -= after.cost - before.cost
            if m.doc_type == "customer_return":
                delta = made[model.TARGET].in_amount - made[model.CURRENT].in_amount
                if delta:
                    profit[m.date.strftime("%m.%Y")] += delta
        # новые отрицательные остатки у организаций с запретом минуса и новые дефициты
        _, current_series, _ = results[model.CURRENT]
        _, target_series, target_problems = results[model.TARGET]
        for text in target_problems:
            new_problems.append(f"товар {product_id}: нарушение — {text}")
        for key, points in target_series.items():
            negative = [qty for qty in points.values() if qty < 0]
            if negative and not flags.get(key[0], False):
                worst = min(negative)
                was = min([qty for qty in current_series.get(key[1], {}).values()] or [D("0")])
                new_problems.append(f"товар {product_id}, {key[0]} на «{key[1]}»: остаток уходил бы в "
                                    f"{worst:f} при запрете минуса (сейчас на складе в целом минимум {was:f})")
        mixed = {wh: orgs for wh, orgs in orgs_by_pair.items() if len(orgs) > 1}
        if mixed:
            print(f"  товар {product_id}: на складах {', '.join(stores[w] for w in mixed)} история нескольких "
                  "организаций")
    print(f"  модель «{EXPECTED}» совпала с записанными суммами движений: {sanity[1]} из {sanity[0]}")
    print(f"  отгрузки, у которых изменится себестоимость или непокрытое количество: {len(changed_lines)}")
    for m, cost_before, open_before, cost_after, open_after in changed_lines:
        print(f"   — {costing._doc_label(m)}, движение #{m.pk}: себестоимость {show(cost_before)} → {show(cost_after)}, "
              f"не покрыто {show(open_before)} → {show(open_after)}")
    print(f"  новые нарушения (минус у организации, которой он запрещён; перемещение чужого товара): "
          f"{len(new_problems)}")
    for line in new_problems:
        print("   —", line)
    print("  изменение прибыли «Продажи и прибыль» по месяцам (знак — как изменится прибыль): "
          + (", ".join(f"{month}: {show(value)}" for month, value in sorted(profit.items())) or "нет"))


if __name__ == "__main__":
    if "--existing" in sys.argv:
        analyze()
    else:
        run_scenarios()
        analyze()
