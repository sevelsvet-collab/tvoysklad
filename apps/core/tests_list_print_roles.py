"""Списки и печатные формы — тем же ролям, что карточка документа.

Решение владельца (10.11.2026): кто не может открыть карточку документа, не
видит его и в списке, и в печатной форме — она раскрывает документ целиком.
Таблица «маршрут × роль» сверяет каждый список и печатную форму с карточкой
для всех ролей, суперпользователя и пользователя без роли. Меню не ведёт на
страницы, которые пользователю закрыты.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.finance.models import Account, Payment
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt, SupplierReturn
from apps.sales.models import CustomerReturn, Invoice, Shipment

User = get_user_model()
NO_ROLE = "без роли"


class ListPrintRolesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        org = Organization.objects.create(name="Орг", is_default=True)
        wh = Warehouse.objects.create(name="Склад", is_default=True)
        product = Product.objects.create(name="Изделие", unit=Unit.objects.create(name="шт"))
        party = Counterparty.objects.create(name="Контрагент", partner_type=Counterparty.TYPE_BOTH)
        cash = Account.objects.create(organization=org, name="Касса", kind=Account.KIND_CASH, is_default=True)
        doc = dict(organization=org, warehouse=wh)
        invoice = Invoice.objects.create(customer=party, **doc)
        invoice.lines.create(product=product, quantity=1, price=Decimal("100"), vat_rate="none")
        shipment = Shipment.objects.create(customer=party, **doc)
        shipment.lines.create(product=product, quantity=1, price=Decimal("100"), vat_rate="none")
        customer_return = CustomerReturn.objects.create(customer=party, **doc)
        receipt = Receipt.objects.create(supplier=party, **doc)
        supplier_return = SupplierReturn.objects.create(supplier=party, **doc)
        payment = Payment.objects.create(kind=Payment.KIND_IN, organization=org, account=cash, counterparty=party,
                                         amount=Decimal("100"), settlement_purpose=Payment.PURPOSE_UNALLOCATED)
        # (карточка, список, печатные формы)
        cls.documents = {
            "счёт": (("invoice_edit", invoice.pk), "invoice_list",
                     [("print_invoice_payment", invoice.pk), ("print_invoice_payment_qr", invoice.pk)]),
            "отгрузка": (("shipment_edit", shipment.pk), "shipment_list",
                         [("print_shipment_torg12", shipment.pk), ("print_shipment_act", shipment.pk),
                          ("print_shipment_upd", shipment.pk)]),
            "возврат покупателя": (("customer_return_edit", customer_return.pk), "customer_return_list", []),
            "приёмка": (("receipt_edit", receipt.pk), "receipt_list", []),
            "возврат поставщику": (("supplier_return_edit", supplier_return.pk), "supplier_return_list", []),
            "платёж": (("payment_edit", payment.pk), "payment_list", [("print_payment_cash_order", payment.pk)]),
            "контрагент": (("counterparty_edit", party.pk), "counterparty_list", []),
        }
        cls.users = {}
        for role in roles.ALL_ROLES:
            user = User.objects.create_user(f"u-{len(cls.users)}", password="x")
            user.groups.add(Group.objects.get(name=role))
            cls.users[role] = user
        cls.users[NO_ROLE] = User.objects.create_user("no-role", password="x")
        cls.users["суперпользователь"] = User.objects.create_user("super", password="x", is_superuser=True)

    def as_user(self, name):
        client = Client()
        client.force_login(self.users[name])
        return client

    @staticmethod
    def allowed(status):
        if status not in (200, 403):
            raise AssertionError(f"неожиданный ответ {status}")
        return status == 200

    def test_list_and_print_match_card_for_every_role(self):
        table = []
        for name in self.users:
            client = self.as_user(name)
            for title, ((card, pk), list_name, prints) in self.documents.items():
                card_open = self.allowed(client.get(reverse(card, args=[pk])).status_code)
                list_open = self.allowed(client.get(reverse(list_name)).status_code)
                self.assertEqual(list_open, card_open, f"{name}: список «{title}»")
                for print_name, print_pk in prints:
                    response = client.get(reverse(print_name, args=[print_pk]), {"fmt": "html"})
                    self.assertEqual(self.allowed(response.status_code), card_open, f"{name}: {print_name}")
                table.append((name, title, card_open))
        # пользователь без роли не видит ни одного документа; суперпользователь — все
        self.assertFalse(any(open_ for name, _, open_ in table if name == NO_ROLE))
        self.assertTrue(all(open_ for name, _, open_ in table if name == "суперпользователь"))

    def test_expected_roles(self):
        """Решение владельца в явном виде: кто что видит."""
        expected = {
            "счёт": {roles.ROLE_ADMIN, roles.ROLE_MANAGER},
            "отгрузка": {roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_STOREKEEPER},
            "возврат покупателя": {roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_STOREKEEPER},
            "приёмка": {roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_STOREKEEPER},
            "возврат поставщику": {roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_STOREKEEPER},
            "платёж": {roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_ACCOUNTANT},
            "контрагент": {roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_ACCOUNTANT},
        }
        for title, (_card, list_name, prints) in self.documents.items():
            for role in roles.ALL_ROLES:
                status = self.as_user(role).get(reverse(list_name)).status_code
                self.assertEqual(status == 200, role in expected[title], f"{role}: {title}")

    def test_guest_goes_to_login(self):
        client = Client()
        for title, ((card, pk), list_name, prints) in self.documents.items():
            for url in [reverse(list_name)] + [reverse(name, args=[pk_]) for name, pk_ in prints]:
                response = client.get(url)
                self.assertEqual(response.status_code, 302, url)
                self.assertIn(reverse("login"), response["Location"], url)

    def test_menu_never_leads_to_forbidden_page(self):
        for name in self.users:
            client = self.as_user(name)
            page = client.get(reverse("dashboard"))
            self.assertEqual(page.status_code, 200, name)
            links = set()
            for section in page.context["nav_sections"]:
                for item in section["submenu"]:
                    if item["url"]:
                        links.add(item["url"])
                if section["url"]:
                    links.add(section["url"])
            for url in sorted(links):
                self.assertNotEqual(client.get(url).status_code, 403, f"{name}: {url}")

    def test_sales_menu_leads_storekeeper_to_shipments(self):
        page = self.as_user(roles.ROLE_STOREKEEPER).get(reverse("dashboard"))
        sales = next(s for s in page.context["nav_sections"] if s["code"] == "sales")
        self.assertEqual(sales["url"], reverse("shipment_list"))
        self.assertNotIn(reverse("invoice_list"), [item["url"] for item in sales["submenu"]])
        codes = [s["code"] for s in page.context["nav_sections"]]
        self.assertNotIn("money", codes)            # платежи, кассы, взаиморасчёты кладовщику закрыты
