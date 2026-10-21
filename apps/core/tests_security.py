"""Доступ к операциям, меняющим данные.

Подтверждённая уязвимость: гость брал CSRF-токен со страницы входа и отправлял
POST на /sales/invoices/<pk>/post/ — счёт становился «выставлен». Проверка CSRF
была, проверки пользователя не было.

Здесь каждая операция проверяется четырьмя способами: гость с действительным
CSRF-токеном, пользователь с неподходящей ролью, GET-запрос и разрешённая роль.
После отказа база должна остаться прежней.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.db import transaction
from django.test import Client, TestCase
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.constants import DOC_POSTED
from apps.core.models import Organization, Warehouse
from apps.finance.models import Account, AccountCorrection, Payment, SettlementCorrection
from apps.inventory.models import (
    AdjustmentLine, SerialNumber, StockAdjustment, StockBalance, StockMovement, Transfer, TransferLine,
)
from apps.partners.models import Contract, ContractTemplate, Counterparty
from apps.purchases.models import Receipt, ReceiptLine, SupplierReturn, SupplierReturnLine
from apps.sales.models import CustomerReturn, CustomerReturnLine, Invoice, InvoiceLine, Shipment, ShipmentLine

User = get_user_model()

ADMIN, MANAGER, STOREKEEPER, ACCOUNTANT = (
    roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_STOREKEEPER, roles.ROLE_ACCOUNTANT,
)

WATCHED = [
    Invoice, InvoiceLine, Shipment, ShipmentLine, CustomerReturn, CustomerReturnLine,
    Receipt, ReceiptLine, SupplierReturn, SupplierReturnLine,
    Transfer, TransferLine, StockAdjustment, AdjustmentLine, StockMovement, StockBalance, SerialNumber,
    Payment, AccountCorrection, SettlementCorrection, Account,
    Contract, ContractTemplate, Counterparty, Product,
]


class SecurityBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.unit = Unit.objects.create(name="шт")
        cls.org = Organization.objects.create(name="Орг", is_default=True)
        cls.wh = Warehouse.objects.create(name="Основной", is_default=True)
        cls.wh2 = Warehouse.objects.create(name="Магазин")
        cls.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        cls.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        cls.product = Product.objects.create(name="Товар", unit=cls.unit)
        cls.serial_product = Product.objects.create(name="Телефон", unit=cls.unit, track_serials=True)
        cls.account = Account.objects.create(organization=cls.org, name="Р/с", is_default=True)

        # приход, чтобы расходные документы могли провестись
        stock = Receipt.objects.create(organization=cls.org, warehouse=cls.wh, supplier=cls.supplier)
        stock.lines.create(product=cls.product, quantity=100, price=10)
        stock.post()

        doc = dict(organization=cls.org, warehouse=cls.wh)
        cls.invoice = Invoice.objects.create(customer=cls.customer, **doc)
        cls.invoice.lines.create(product=cls.product, quantity=1, price=100)
        cls.shipment = Shipment.objects.create(customer=cls.customer, **doc)
        cls.shipment.lines.create(product=cls.product, quantity=1, price=100)
        cls.customer_return = CustomerReturn.objects.create(customer=cls.customer, **doc)
        cls.customer_return.lines.create(product=cls.product, quantity=1, price=100)
        cls.receipt = Receipt.objects.create(supplier=cls.supplier, **doc)
        cls.receipt.lines.create(product=cls.product, quantity=1, price=10)
        cls.supplier_return = SupplierReturn.objects.create(supplier=cls.supplier, **doc)
        cls.supplier_return.lines.create(product=cls.product, quantity=1, price=10)
        cls.transfer = Transfer.objects.create(organization=cls.org, warehouse_from=cls.wh, warehouse_to=cls.wh2)
        cls.transfer.lines.create(product=cls.product, quantity=1)
        cls.adjustment = StockAdjustment.objects.create(kind=StockAdjustment.KIND_INCOME, **doc)
        cls.adjustment.lines.create(product=cls.product, quantity=1, price=10)
        cls.payment = Payment.objects.create(
            kind=Payment.KIND_IN, organization=cls.org, account=cls.account,
            counterparty=cls.customer, invoice=cls.invoice, amount=Decimal("50"),
        )
        cls.account_correction = AccountCorrection.objects.create(
            organization=cls.org, account=cls.account, actual_balance=Decimal("10"), amount=Decimal("10"),
        )
        cls.settlement_correction = SettlementCorrection.objects.create(
            organization=cls.org, counterparty=cls.customer, amount=Decimal("5"),
        )
        cls.contract_template = ContractTemplate.objects.create(name="Свой шаблон")
        cls.contract = Contract.objects.create(counterparty=cls.customer, organization=cls.org)

        cls.users = {}
        for index, role in enumerate(roles.ALL_ROLES):
            user = User.objects.create_user(f"user{index}", password="pass12345")
            user.groups.add(Group.objects.get(name=role))
            cls.users[role] = user

    # ---------- помощники ----------

    def snapshot(self):
        """Полное содержимое всех таблиц, которые могут затронуть операции."""
        return {model.__name__: list(model.objects.order_by("pk").values()) for model in WATCHED}

    def as_role(self, role):
        client = Client()
        client.force_login(self.users[role])
        return client

    def operations(self):
        """(название, адрес, данные POST, роль с доступом, роль без доступа)."""
        ids = lambda obj: {"ids": [obj.pk]}  # noqa: E731
        one = lambda name, obj: reverse(name, args=[obj.pk])  # noqa: E731
        return [
            # Продажи
            ("invoice_post", one("invoice_post", self.invoice), {}, MANAGER, STOREKEEPER),
            ("invoice_unpost", one("invoice_unpost", self.invoice), {}, MANAGER, ACCOUNTANT),
            ("invoice_delete", one("invoice_delete", self.invoice), {}, MANAGER, STOREKEEPER),
            ("invoice_bulk_delete", reverse("invoice_bulk_delete"), ids(self.invoice), MANAGER, STOREKEEPER),
            ("invoice_to_shipment", one("invoice_to_shipment", self.invoice), {}, STOREKEEPER, ACCOUNTANT),
            ("shipment_post", one("shipment_post", self.shipment), {}, STOREKEEPER, ACCOUNTANT),
            ("shipment_unpost", one("shipment_unpost", self.shipment), {}, MANAGER, ACCOUNTANT),
            ("shipment_delete", one("shipment_delete", self.shipment), {}, MANAGER, ACCOUNTANT),
            ("shipment_bulk_delete", reverse("shipment_bulk_delete"), ids(self.shipment), STOREKEEPER, ACCOUNTANT),
            ("shipment_to_return", one("shipment_to_return", self.shipment), {}, MANAGER, ACCOUNTANT),
            ("customer_return_post", one("customer_return_post", self.customer_return), {}, MANAGER, ACCOUNTANT),
            ("customer_return_unpost", one("customer_return_unpost", self.customer_return), {}, MANAGER, ACCOUNTANT),
            ("customer_return_delete", one("customer_return_delete", self.customer_return), {}, MANAGER, ACCOUNTANT),
            ("customer_return_bulk_delete", reverse("customer_return_bulk_delete"), ids(self.customer_return),
             MANAGER, ACCOUNTANT),
            # Закупки
            ("receipt_post", one("receipt_post", self.receipt), {}, STOREKEEPER, ACCOUNTANT),
            ("receipt_unpost", one("receipt_unpost", self.receipt), {}, MANAGER, ACCOUNTANT),
            ("receipt_delete", one("receipt_delete", self.receipt), {}, MANAGER, ACCOUNTANT),
            ("receipt_bulk_delete", reverse("receipt_bulk_delete"), ids(self.receipt), STOREKEEPER, ACCOUNTANT),
            ("receipt_to_return", one("receipt_to_return", self.receipt), {}, MANAGER, ACCOUNTANT),
            ("supplier_return_post", one("supplier_return_post", self.supplier_return), {}, MANAGER, ACCOUNTANT),
            ("supplier_return_unpost", one("supplier_return_unpost", self.supplier_return), {}, MANAGER, ACCOUNTANT),
            ("supplier_return_delete", one("supplier_return_delete", self.supplier_return), {}, MANAGER, ACCOUNTANT),
            ("supplier_return_bulk_delete", reverse("supplier_return_bulk_delete"), ids(self.supplier_return),
             STOREKEEPER, ACCOUNTANT),
            # Склад
            ("transfer_post", one("transfer_post", self.transfer), {}, STOREKEEPER, MANAGER),
            ("transfer_unpost", one("transfer_unpost", self.transfer), {}, STOREKEEPER, MANAGER),
            ("transfer_delete", one("transfer_delete", self.transfer), {}, STOREKEEPER, ACCOUNTANT),
            ("transfer_bulk_delete", reverse("transfer_bulk_delete"), ids(self.transfer), STOREKEEPER, MANAGER),
            ("adjustment_post", one("adjustment_post", self.adjustment), {}, STOREKEEPER, MANAGER),
            ("adjustment_unpost", one("adjustment_unpost", self.adjustment), {}, STOREKEEPER, MANAGER),
            ("adjustment_delete", one("adjustment_delete", self.adjustment), {}, STOREKEEPER, ACCOUNTANT),
            ("adjustment_bulk_delete", reverse("adjustment_bulk_delete"), ids(self.adjustment), STOREKEEPER, MANAGER),
            # номера остатка вводятся для организации: «склад:организация» (учёт по организациям)
            ("product_serial_stock", one("product_serial_stock", self.serial_product),
             {"place": f"{self.wh.pk}:{self.org.pk}", "numbers": "SN-1"}, STOREKEEPER, ACCOUNTANT),
            # Деньги
            ("payment_post", one("payment_post", self.payment), {}, ACCOUNTANT, STOREKEEPER),
            ("payment_unpost", one("payment_unpost", self.payment), {}, MANAGER, STOREKEEPER),
            ("payment_delete", one("payment_delete", self.payment), {}, ACCOUNTANT, STOREKEEPER),
            ("payment_bulk_delete", reverse("payment_bulk_delete"), ids(self.payment), ACCOUNTANT, STOREKEEPER),
            ("invoice_to_payment", one("invoice_to_payment", self.invoice), {}, ACCOUNTANT, STOREKEEPER),
            ("invoice_to_cash_order", one("invoice_to_cash_order", self.invoice), {}, MANAGER, STOREKEEPER),
            ("account_correction_post", one("account_correction_post", self.account_correction), {},
             ACCOUNTANT, STOREKEEPER),
            ("account_correction_unpost", one("account_correction_unpost", self.account_correction), {},
             ACCOUNTANT, STOREKEEPER),
            ("account_correction_delete", one("account_correction_delete", self.account_correction), {},
             ACCOUNTANT, STOREKEEPER),
            ("settlement_correction_post", one("settlement_correction_post", self.settlement_correction), {},
             ACCOUNTANT, STOREKEEPER),
            ("settlement_correction_unpost", one("settlement_correction_unpost", self.settlement_correction), {},
             ACCOUNTANT, STOREKEEPER),
            ("settlement_correction_delete", one("settlement_correction_delete", self.settlement_correction), {},
             ACCOUNTANT, STOREKEEPER),
            # Контрагенты и договоры
            ("contract_delete", one("contract_delete", self.contract), {}, ACCOUNTANT, STOREKEEPER),
            ("contract_bulk_delete", reverse("contract_bulk_delete"), ids(self.contract), MANAGER, STOREKEEPER),
            ("contract_template_delete", one("contract_template_delete", self.contract_template), {},
             MANAGER, STOREKEEPER),
            ("counterparty_bulk", reverse("counterparty_bulk", args=["archive"]), ids(self.customer),
             ACCOUNTANT, STOREKEEPER),
            ("counterparty_quick_create", reverse("api_counterparty_quick_create"), {"name": "Новый"},
             MANAGER, STOREKEEPER),
            # Товары
            ("product_bulk_archive", reverse("product_bulk", args=["archive"]), ids(self.product),
             STOREKEEPER, ACCOUNTANT),
            ("product_bulk_delete", reverse("product_bulk", args=["delete"]), ids(self.serial_product),
             MANAGER, ACCOUNTANT),
            ("product_quick_create", reverse("api_product_quick_create"), {"name": "Новый товар"},
             STOREKEEPER, ACCOUNTANT),
        ]


class AnonymousAccessTests(SecurityBase):
    def csrf_client(self):
        """Гость с настоящим CSRF-токеном, полученным на странице входа."""
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("login"))
        return client, client.cookies["csrftoken"].value

    def test_confirmed_scenario_invoice_post(self):
        """Тот самый сценарий: токен со страницы входа → POST на проведение счёта."""
        client, token = self.csrf_client()
        resp = client.post(reverse("invoice_post", args=[self.invoice.pk]), {"csrfmiddlewaretoken": token})
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp.url)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.STATUS_DRAFT)

    def test_every_operation_rejects_guest_with_valid_csrf(self):
        client, token = self.csrf_client()
        before = self.snapshot()
        for name, url, data, _allowed, _denied in self.operations():
            with self.subTest(operation=name):
                resp = client.post(url, {**data, "csrfmiddlewaretoken": token})
                self.assertEqual(resp.status_code, 302, name)   # CSRF пройден, но пользователя нет
                self.assertIn(reverse("login"), resp.url, name)
                self.assertEqual(self.snapshot(), before, f"{name}: гость изменил данные")

    def test_create_and_import_forms_reject_guest(self):
        client, token = self.csrf_client()
        before = self.snapshot()
        for name in ["invoice_create", "shipment_create", "customer_return_create", "receipt_create",
                     "supplier_return_create", "transfer_create", "adjustment_create_income",
                     "payment_create_in", "product_create", "group_create", "counterparty_create",
                     "contract_create", "catalog_import", "partners_import", "organization_create"]:
            with self.subTest(view=name):
                resp = client.post(reverse(name), {"csrfmiddlewaretoken": token, "name": "X"})
                self.assertEqual(resp.status_code, 302, name)
                self.assertIn(reverse("login"), resp.url, name)
        self.assertEqual(self.snapshot(), before)

    def test_guest_cannot_send_invoice_email(self):
        client, token = self.csrf_client()
        resp = client.post(reverse("send_invoice_email", args=[self.invoice.pk]), {
            "csrfmiddlewaretoken": token, "to_email": "x@example.com", "subject": "Счёт", "message": "Текст",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp.url)
        self.assertEqual(mail.outbox, [])


class WrongRoleTests(SecurityBase):
    def test_every_operation_forbidden_for_wrong_role(self):
        before = self.snapshot()
        for name, url, data, _allowed, denied in self.operations():
            with self.subTest(operation=name, role=denied):
                resp = self.as_role(denied).post(url, data)
                self.assertEqual(resp.status_code, 403, f"{name} доступна роли {denied}")
                self.assertEqual(self.snapshot(), before, f"{name}: роль {denied} изменила данные")

    def test_wrong_role_cannot_send_invoice_email(self):
        resp = self.as_role(STOREKEEPER).post(reverse("send_invoice_email", args=[self.invoice.pk]), {
            "to_email": "x@example.com", "subject": "Счёт", "message": "Текст",
        })
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(mail.outbox, [])


class GetIsReadOnlyTests(SecurityBase):
    def test_get_never_changes_data(self):
        """GET на операцию не выполняет её даже у пользователя с доступом."""
        before = self.snapshot()
        for name, url, data, allowed, _denied in self.operations():
            with self.subTest(operation=name):
                resp = self.as_role(allowed).get(url, data)
                self.assertEqual(resp.status_code, 405, name)
                self.assertEqual(self.snapshot(), before, f"{name}: GET изменил данные")


class AllowedRoleTests(SecurityBase):
    def test_every_operation_available_to_allowed_role(self):
        """Разрешённая роль не получает ни отказа, ни отправки на вход."""
        for name, url, data, allowed, _denied in self.operations():
            with self.subTest(operation=name, role=allowed):
                savepoint = transaction.savepoint()     # операции разрушительны — каждую откатываем
                resp = self.as_role(allowed).post(url, data)
                self.assertIn(resp.status_code, (200, 302), name)
                if resp.status_code == 302:
                    self.assertNotIn(reverse("login"), resp.url, name)
                transaction.savepoint_rollback(savepoint)

    def test_manager_posts_invoice(self):
        self.as_role(MANAGER).post(reverse("invoice_post", args=[self.invoice.pk]))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.STATUS_ISSUED)

    def test_storekeeper_posts_receipt(self):
        self.as_role(STOREKEEPER).post(reverse("receipt_post", args=[self.receipt.pk]))
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status, DOC_POSTED)

    def test_accountant_posts_payment(self):
        self.as_role(ACCOUNTANT).post(reverse("payment_post", args=[self.payment.pk]))
        self.payment.refresh_from_db()
        self.invoice.refresh_from_db()
        self.assertEqual(self.payment.status, DOC_POSTED)
        self.assertEqual(self.invoice.paid_amount, Decimal("50"))

    def test_single_delete(self):
        self.as_role(MANAGER).post(reverse("shipment_delete", args=[self.shipment.pk]))
        self.assertFalse(Shipment.objects.filter(pk=self.shipment.pk).exists())
        self.as_role(ACCOUNTANT).post(reverse("contract_delete", args=[self.contract.pk]))
        self.assertFalse(Contract.objects.filter(pk=self.contract.pk).exists())

    def test_bulk_delete(self):
        self.as_role(MANAGER).post(reverse("invoice_bulk_delete"), {"ids": [self.invoice.pk]})
        self.assertFalse(Invoice.objects.filter(pk=self.invoice.pk).exists())
        self.as_role(STOREKEEPER).post(reverse("transfer_bulk_delete"), {"ids": [self.transfer.pk]})
        self.assertFalse(Transfer.objects.filter(pk=self.transfer.pk).exists())

    def test_admin_can_do_everything(self):
        for name, url, data, _allowed, _denied in self.operations():
            with self.subTest(operation=name):
                savepoint = transaction.savepoint()
                resp = self.as_role(ADMIN).post(url, data)
                self.assertIn(resp.status_code, (200, 302), name)
                transaction.savepoint_rollback(savepoint)
