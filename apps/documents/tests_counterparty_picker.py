"""Выбор контрагента в документе и карандаш его карточки — при id от 1000.

Воспроизведено 22.10.2026 на тестовой базе. Причина: при русской локали и
USE_THOUSAND_SEPARATOR=True шаблон выводит целое число с разделителем
разрядов — неразрывным пробелом (U+00A0). Идентификатор, выведенный в шаблоне
как {{ ... }} без unlocalize, превращался из 1540 в «1 540»:

- скрытое поле контрагента в открытом документе получало «1 540» —
  сохранение без изменений отвечало «Выберите корректный вариант…»;
- карандаш строил адрес /counterparties/1 540/ — 404 «Not Found»;
- подпись по id (API поиска, ?id=1 540) — 500;
- кнопки «Создать счёт…» в карточке контрагента вели на ?partner=1 540.

У 999 разделителя нет — он не был затронут; 1000 и 1540 — затронуты.

Тесты читают страницу, как браузер без скриптов: значения полей берутся из
HTML и отправляются обратно без изменений.
"""
import re
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.finance.models import Account, Payment, SettlementCorrection
from apps.partners.models import Contract, Counterparty
from apps.purchases.models import Receipt, SupplierReturn
from apps.sales.models import CustomerReturn, Invoice, Shipment

User = get_user_model()

IDS = (999, 1000, 1540)
NBSP = "\xa0"
INVALID_CHOICE = "Выберите корректный вариант"


class _FormFields(HTMLParser):
    """Поля одной формы страницы — то, что браузер отправит по кнопке.

    Поле относится к форме, если стоит внутри неё или ссылается на неё
    атрибутом form="..." (номер и дата документа — в заголовке страницы).
    Содержимое <template> (заготовка новой строки) браузер не отправляет."""

    def __init__(self, form_id):
        super().__init__(convert_charrefs=True)
        self.form_id = form_id
        self.inside = False
        self.in_template = 0
        self.data = []          # пары (имя, значение) в порядке страницы
        self._select = None     # [имя, multiple, варианты]
        self._option = None     # [value, selected, куски текста]
        self._textarea = None   # [имя, куски текста]

    def _belongs(self, attrs):
        if self.in_template or "disabled" in attrs:
            return False
        owner = attrs.get("form")
        return owner == self.form_id if owner else self.inside

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.inside = attrs.get("id") == self.form_id
            return
        if tag == "template":
            self.in_template += 1
            return
        if tag == "option" and self._select is not None:
            self._option = [attrs.get("value"), "selected" in attrs, []]
            self._select[2].append(self._option)
            return
        if not self._belongs(attrs):
            return
        name = attrs.get("name")
        if tag == "input" and name:
            kind = attrs.get("type", "text")
            if kind in ("submit", "button", "reset", "image", "file"):
                return
            if kind in ("checkbox", "radio"):
                if "checked" in attrs:
                    self.data.append((name, attrs.get("value", "on")))
                return
            self.data.append((name, attrs.get("value", "")))
        elif tag == "select" and name:
            self._select = [name, "multiple" in attrs, []]
        elif tag == "textarea" and name:
            self._textarea = [name, []]

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea[1].append(data)
        elif self._option is not None:
            self._option[2].append(data)

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "template":
            self.in_template -= 1
        elif tag == "option":
            self._option = None
        elif tag == "select" and self._select is not None:
            name, multiple, options = self._select
            chosen = [o for o in options if o[1]]
            if not chosen and not multiple and options:
                chosen = options[:1]
            for value, _selected, text in chosen:
                self.data.append((name, value if value is not None else "".join(text).strip()))
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            name, parts = self._textarea
            text = "".join(parts)
            self.data.append((name, text[1:] if text.startswith("\n") else text))
            self._textarea = None


def form_data(html, form_id="doc-form"):
    reader = _FormFields(form_id)
    reader.feed(html)
    data = {}
    for name, value in reader.data:
        data.setdefault(name, []).append(value)
    return data


class _Elements(HTMLParser):
    """Атрибуты элементов с заданным классом."""

    def __init__(self, css_class):
        super().__init__(convert_charrefs=True)
        self.css_class = css_class
        self.found = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.css_class in (attrs.get("class") or "").split():
            self.found.append(attrs)


def elements(html, css_class):
    reader = _Elements(css_class)
    reader.feed(html)
    return reader.found


def pencil_url(base, value):
    """Адрес карточки так же, как его строит static/js/entity-modal.js
    (buildUrl): «/0/» в конце заменяется на значение скрытого поля. Браузер
    кодирует неразрывный пробел как %C2%A0."""
    url = re.sub(r"/0/(?:$|\?)", "/" + value + "/", base, count=1) + "?embed=1"
    return quote(url, safe="/?=&%")


class PickerWorld(TestCase):
    """Контрагенты с id 999, 1000 и 1540 и по черновику каждого вида документа."""

    def setUp(self):
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.unit = Unit.objects.create(name="шт")
        self.product = Product.objects.create(name="Товар", unit=self.unit, sale_price=490)
        self.account = Account.objects.create(organization=self.org, name="Расчётный счёт", is_default=True)
        self.parties = {
            pk: Counterparty.objects.create(pk=pk, name=f"Контрагент №{pk}", partner_type=Counterparty.TYPE_BOTH)
            for pk in IDS
        }
        self.user = User.objects.create_user("m", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="m", password="pass12345")

    # вид документа → (адрес редактирования, поле контрагента, создать черновик)
    def kinds(self):
        def line_doc(model, field):
            def make(party):
                doc = model.objects.create(organization=self.org, warehouse=self.wh, **{field: party})
                doc.lines.create(product=self.product, quantity=Decimal("3"), price=Decimal("490"),
                                 vat_rate="none")
                return doc
            return make

        def payment(kind):
            def make(party):
                return Payment.objects.create(kind=kind, organization=self.org, account=self.account,
                                              counterparty=party, amount=Decimal("100"))
            return make

        def correction(party):
            return SettlementCorrection.objects.create(
                organization=self.org, counterparty=party, side=SettlementCorrection.SIDE_SALES,
                amount=Decimal("100"))

        return {
            "Счёт покупателю": ("invoice_edit", "customer", line_doc(Invoice, "customer")),
            "Отгрузка": ("shipment_edit", "customer", line_doc(Shipment, "customer")),
            "Возврат покупателя": ("customer_return_edit", "customer", line_doc(CustomerReturn, "customer")),
            "Приёмка": ("receipt_edit", "supplier", line_doc(Receipt, "supplier")),
            "Возврат поставщику": ("supplier_return_edit", "supplier", line_doc(SupplierReturn, "supplier")),
            "Входящий платёж": ("payment_edit", "counterparty", payment(Payment.KIND_IN)),
            "Исходящий платёж": ("payment_edit", "counterparty", payment(Payment.KIND_OUT)),
            "Корректировка взаиморасчётов": ("settlement_correction_edit", "counterparty", correction),
        }


class HiddenCounterpartyIdTests(PickerWorld):
    """Общее поле выбора контрагента (templates/documents/_ac_field.html)."""

    def test_settings_under_test(self):
        # Условия, в которых проявлялась ошибка, — не выключать ради тестов
        self.assertEqual(settings.LANGUAGE_CODE, "ru")
        self.assertTrue(settings.USE_THOUSAND_SEPARATOR)

    def test_hidden_id_is_plain_number(self):
        for title, (url_name, field, make) in self.kinds().items():
            for pk in IDS:
                with self.subTest(title, id=pk):
                    doc = make(self.parties[pk])
                    html = self.client.get(reverse(url_name, args=[doc.pk])).content.decode()
                    self.assertEqual(form_data(html)[field], [str(pk)])
                    self.assertIn(f"Контрагент №{pk}", html)   # имя видно в поле

    def test_resave_without_changes_keeps_counterparty_and_lines(self):
        """Открыли документ, ничего не меняли, нажали «Сохранить»."""
        for title, (url_name, field, make) in self.kinds().items():
            for pk in IDS:
                with self.subTest(title, id=pk):
                    doc = make(self.parties[pk])
                    url = reverse(url_name, args=[doc.pk])
                    data = form_data(self.client.get(url).content.decode())
                    data["action"] = ["save"]
                    resp = self.client.post(url, data)
                    self.assertNotContains(resp, INVALID_CHOICE, status_code=resp.status_code)
                    self.assertEqual(resp.status_code, 302)
                    doc.refresh_from_db()
                    self.assertEqual(getattr(doc, f"{field}_id"), pk)
                    if hasattr(doc, "lines"):
                        line = doc.lines.get()
                        self.assertEqual((line.product_id, line.quantity), (self.product.pk, Decimal("3")))

    def test_contract_card_hidden_id(self):
        for pk in IDS:
            with self.subTest(id=pk):
                contract = Contract.objects.create(counterparty=self.parties[pk], organization=self.org,
                                                   date=date(2026, 10, 1))
                html = self.client.get(reverse("contract_edit", args=[contract.pk])).content.decode()
                hidden = [a for a in elements(html, "ac-value") if a.get("name") == "counterparty"]
                self.assertEqual([a["value"] for a in hidden], [str(pk)])


class FormErrorKeepsInputTests(PickerWorld):
    """Ошибка формы не теряет выбранного контрагента и введённые строки."""

    def _new_invoice(self, pk, **extra):
        data = {
            "date": "2026-10-22", "time": "", "number": "", "organization": self.org.pk,
            "warehouse": self.wh.pk, "customer": str(pk), "contract": "", "due_date": "", "comment": "",
            "action": "save",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.product.pk, "lines-0-quantity": "7",
            "lines-0-price": "490", "lines-0-discount": "0", "lines-0-vat_rate": "none",
        }
        data.update(extra)
        return data

    def assertKept(self, resp, pk, quantity="7"):
        self.assertEqual(resp.status_code, 200)              # форма показана снова
        html = resp.content.decode()
        data = form_data(html)
        self.assertEqual(data["customer"], [str(pk)])
        self.assertIn(f'value="Контрагент №{pk}"', html)      # имя в видимом поле
        self.assertEqual(data["lines-0-product"], [str(self.product.pk)])
        self.assertEqual(data["lines-0-quantity"], [quantity])
        return data

    def test_header_error_keeps_counterparty_and_lines(self):
        for pk in IDS:
            with self.subTest(id=pk):
                resp = self.client.post(reverse("invoice_create"), self._new_invoice(pk, date="2026-13-45"))
                self.assertKept(resp, pk)
                self.assertFalse(Invoice.objects.exists())

    def test_line_error_keeps_counterparty_and_lines(self):
        for pk in IDS:
            with self.subTest(id=pk):
                resp = self.client.post(reverse("invoice_create"),
                                        self._new_invoice(pk, **{"lines-0-quantity": "7 шт"}))
                self.assertKept(resp, pk, quantity="7 шт")
                self.assertFalse(Invoice.objects.exists())

    def test_resubmit_after_error_saves(self):
        """Пользователь исправил ошибку и отправил ту же страницу ещё раз."""
        for pk in IDS:
            with self.subTest(id=pk):
                resp = self.client.post(reverse("invoice_create"), self._new_invoice(pk, date="2026-13-45"))
                data = form_data(resp.content.decode())
                data.update({"date": ["2026-10-22"], "action": ["save"]})
                resp = self.client.post(reverse("invoice_create"), data)
                self.assertEqual(resp.status_code, 302)
                invoice = Invoice.objects.get(customer_id=pk)
                self.assertEqual(invoice.lines.get().quantity, Decimal("7"))

    def test_existing_document_error_keeps_counterparty(self):
        for pk in IDS:
            with self.subTest(id=pk):
                invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh,
                                                 customer=self.parties[pk])
                url = reverse("invoice_edit", args=[invoice.pk])
                data = form_data(self.client.get(url).content.decode())
                data.update({"date": ["2026-13-45"], "action": ["save"]})
                resp = self.client.post(url, data)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(form_data(resp.content.decode())["customer"], [str(pk)])


class CounterpartyPencilTests(PickerWorld):
    """Карандаш у контрагента: адрес карточки и фактические коды ответа."""

    def _pencil(self, html):
        (button,) = elements(html, "ac-edit")
        return button["data-edit-base"]

    def test_pencil_opens_card_from_documents(self):
        docs = {
            "Счёт покупателю": ("invoice_edit", "customer", self.kinds()["Счёт покупателю"][2]),
            "Отгрузка": ("shipment_edit", "customer", self.kinds()["Отгрузка"][2]),
            "Договор": ("contract_edit", "counterparty",
                        lambda party: Contract.objects.create(counterparty=party, organization=self.org,
                                                              date=date(2026, 10, 1))),
        }
        for title, (url_name, field, make) in docs.items():
            for pk in IDS:
                with self.subTest(title, id=pk):
                    doc = make(self.parties[pk])
                    html = self.client.get(reverse(url_name, args=[doc.pk])).content.decode()
                    hidden = [a for a in elements(html, "ac-value") if a.get("name") == field]
                    url = pencil_url(self._pencil(html), hidden[0]["value"])
                    resp = self.client.get(url)
                    self.assertEqual(resp.status_code, 200, url)
                    self.assertContains(resp, f"Контрагент №{pk}")

    def test_card_status_codes(self):
        """Фактические коды: есть — 200, нет такого — 404, нет прав — 403,
        не вошёл — переход на вход."""
        card = reverse("counterparty_edit", args=[1540]) + "?embed=1"
        self.assertEqual(self.client.get(card).status_code, 200)

        missing = reverse("counterparty_edit", args=[99999]) + "?embed=1"
        self.assertEqual(self.client.get(missing).status_code, 404)

        archived = self.parties[1000]
        archived.is_active = False
        archived.save()
        self.assertEqual(self.client.get(reverse("counterparty_edit", args=[1000])).status_code, 200)

        # кладовщик открывает отгрузку, но карточки контрагентов ему недоступны
        keeper = User.objects.create_user("k", password="pass12345")
        keeper.groups.add(Group.objects.get(name=roles.ROLE_STOREKEEPER))
        shipment = self.kinds()["Отгрузка"][2](self.parties[1540])
        self.client.login(username="k", password="pass12345")
        self.assertEqual(self.client.get(reverse("shipment_edit", args=[shipment.pk])).status_code, 200)
        self.assertEqual(self.client.get(card).status_code, 403)

        self.client.logout()
        resp = self.client.get(card)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp["Location"])

    def test_localized_id_never_reaches_card(self):
        """Адрес с «1 540» — всегда 404 (так выглядела ошибка до исправления);
        исправление — не выводить такой id, а не принимать его."""
        url = pencil_url(reverse("counterparty_edit", args=[0]), f"1{NBSP}540")
        self.assertEqual(self.client.get(url).status_code, 404)


class CounterpartySearchByIdTests(PickerWorld):
    """Подпись выбранного контрагента по id (autocomplete.js, entity-modal.js)."""

    def test_search_by_id(self):
        url = reverse("api_counterparty_search")
        for pk in IDS:
            with self.subTest(id=pk):
                results = self.client.get(url, {"id": str(pk)}).json()["results"]
                self.assertEqual([r["id"] for r in results], [pk])

    def test_malformed_id_is_bad_request_not_500(self):
        url = reverse("api_counterparty_search")
        for bad in (f"1{NBSP}540", "1 540", "abc", "-1", "1.5"):
            with self.subTest(bad=bad):
                resp = self.client.get(url, {"id": bad})
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json()["results"], [])

    def test_missing_id_is_empty(self):
        resp = self.client.get(reverse("api_counterparty_search"), {"id": "99999"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["results"], [])


class CardLinksTests(PickerWorld):
    """Кнопки «Создать документ» в карточке контрагента (?partner=<id>)."""

    LINKS = {
        "invoice_create": "customer", "shipment_create": "customer", "customer_return_create": "customer",
        "payment_create_in": "counterparty", "receipt_create": "supplier",
        "supplier_return_create": "supplier", "payment_create_out": "counterparty",
        "contract_create": None,
    }

    def _links(self, html):
        found = {}
        for href in re.findall(r'href="([^"]*\?partner=[^"]*)"', html):
            path, query = href.split("?", 1)
            found[path] = parse_qs(query)["partner"]
        return found

    def test_links_carry_plain_id_and_prefill_document(self):
        for pk in IDS:
            html = self.client.get(reverse("counterparty_edit", args=[pk])).content.decode()
            links = self._links(html)
            for url_name, field in self.LINKS.items():
                with self.subTest(url_name, id=pk):
                    self.assertEqual(links[reverse(url_name)], [str(pk)])
                    if field:
                        page = self.client.get(reverse(url_name), {"partner": links[reverse(url_name)][0]})
                        self.assertEqual(form_data(page.content.decode())[field], [str(pk)])

    def test_invoice_from_card_saves(self):
        for pk in IDS:
            with self.subTest(id=pk):
                html = self.client.get(reverse("counterparty_edit", args=[pk])).content.decode()
                partner = self._links(html)[reverse("invoice_create")][0]
                page = self.client.get(reverse("invoice_create"), {"partner": partner}).content.decode()
                data = form_data(page)
                data.update({
                    "action": ["save"], "lines-TOTAL_FORMS": ["1"],
                    "lines-0-product": [str(self.product.pk)], "lines-0-quantity": ["2"],
                    "lines-0-price": ["490"], "lines-0-discount": ["0"], "lines-0-vat_rate": ["none"],
                })
                resp = self.client.post(reverse("invoice_create"), data)
                self.assertNotContains(resp, INVALID_CHOICE, status_code=resp.status_code)
                self.assertEqual(resp.status_code, 302)
                self.assertTrue(Invoice.objects.filter(customer_id=pk, lines__quantity=2).exists())


class ListCheckboxTests(PickerWorld):
    """Флажки в списках — id для массовых действий."""

    def test_counterparty_list_checkbox_and_archive(self):
        html = self.client.get(reverse("counterparty_list")).content.decode()
        values = sorted(a["value"] for a in elements(html, "list-check"))
        self.assertEqual(values, sorted(str(pk) for pk in IDS))
        resp = self.client.post(reverse("counterparty_bulk", args=["archive"]), {"ids": values})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Counterparty.objects.filter(pk__in=IDS, is_active=True).exists())

    def test_invoice_list_checkbox_and_bulk_delete(self):
        Invoice.objects.create(pk=1540, organization=self.org, warehouse=self.wh, customer=self.parties[999])
        html = self.client.get(reverse("invoice_list")).content.decode()
        values = [a["value"] for a in elements(html, "list-check")]
        self.assertEqual(values, ["1540"])
        resp = self.client.post(reverse("invoice_bulk_delete"), {"ids": values})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Invoice.objects.exists())

    def test_amounts_keep_thousand_separator(self):
        """Суммы и количества для человека — по-прежнему с разделителем разрядов."""
        invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.parties[1540])
        invoice.lines.create(product=self.product, quantity=Decimal("2500"), price=Decimal("490"),
                             vat_rate="none")
        html = self.client.get(reverse("invoice_list")).content.decode()
        self.assertIn(f"1{NBSP}225{NBSP}000,00", html)                 # 2 500 × 490
        html = self.client.get(reverse("invoice_edit", args=[invoice.pk])).content.decode()
        self.assertEqual(form_data(html)["lines-0-quantity"], ["2500.000"])  # поле ввода — без разделителя

    def test_payment_list_checkbox(self):
        Payment.objects.create(pk=1540, kind=Payment.KIND_IN, organization=self.org, account=self.account,
                               counterparty=self.parties[999], amount=Decimal("100"))
        html = self.client.get(reverse("payment_list")).content.decode()
        self.assertEqual([a["value"] for a in elements(html, "list-check")], ["1540"])


# Технический id в шаблоне — выражение, оканчивающееся на .pk, .id или _id,
# и значение поля формы (field.value / form.<поле>.value). Выводить их можно
# только через |unlocalize или внутри {% localize off %}.
TEMPLATE_VAR = re.compile(r"\{\{\s*([\w.]+)\s*((?:\|[^}]*)?)\}\}")
TECHNICAL = re.compile(r"(?:\.pk|\.id|(?<!auto)_id)$|^(?:field|form\.\w+)\.value$")   # auto_id — строка «id_…»
WIDGETS = {"form.id", "f.id"}   # {{ form.id }} — скрытый виджет формы, Django выводит его без локализации
SKIP_BLOCKS = re.compile(
    r"\{%\s*localize off\s*%\}.*?\{%\s*endlocalize\s*%\}"
    r"|\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}|\{#.*?#\}",
    re.S,
)


class TemplateTechnicalIdTests(TestCase):
    def test_no_localized_technical_ids_in_templates(self):
        root = Path(settings.BASE_DIR) / "templates"
        problems = []
        for path in sorted(root.rglob("*.html")):
            name = path.relative_to(root).as_posix()
            text = SKIP_BLOCKS.sub("", path.read_text(encoding="utf-8"))
            for match in TEMPLATE_VAR.finditer(text):
                var, filters = match.group(1), match.group(2)
                if var in WIDGETS or not TECHNICAL.search(var) or "unlocalize" in filters:
                    continue
                problems.append(f"{name}: {match.group(0)}")
        self.assertEqual(problems, [])

    def test_check_catches_raw_id(self):
        # проверка выше действительно ловит прежнюю запись поля выбора
        self.assertTrue(TECHNICAL.search("field.value"))
        self.assertTrue(TECHNICAL.search("object.pk"))
        self.assertTrue(TECHNICAL.search("doc.customer_id"))
        self.assertFalse(TECHNICAL.search("b.summary.value"))
