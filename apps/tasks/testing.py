"""Общий набор данных для тестов задач: сотрудники всех ролей и настоящие
документы каждого вида, созданные обычными методами программы."""
from decimal import Decimal

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.finance.models import Account, Payment
from apps.inventory.models import StockAdjustment
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt, SupplierReturn
from apps.sales.models import CustomerReturn, Invoice, Shipment

from . import services

User = get_user_model()
PASSWORD = "pass-for-tests-only"

# Таблицы учёта: задачи не должны менять ни одной их строки
ACCOUNTING_MODELS = (
    "inventory.StockMovement", "inventory.StockBalance", "inventory.SerialNumber", "inventory.SerialMovement",
    "inventory.DeficitCover", "inventory.StockAdjustment", "inventory.AdjustmentLine", "inventory.StockLedgerState",
    "inventory.Transfer", "inventory.TransferLine",
    "sales.Invoice", "sales.InvoiceLine", "sales.Shipment", "sales.ShipmentLine",
    "sales.CustomerReturn", "sales.CustomerReturnLine",
    "purchases.Receipt", "purchases.ReceiptLine", "purchases.SupplierReturn", "purchases.SupplierReturnLine",
    "finance.Account", "finance.Payment", "finance.AccountCorrection", "finance.SettlementCorrection",
    "partners.Counterparty", "core.DocumentNumber",
)


def accounting_state():
    state = {}
    for label in ACCOUNTING_MODELS:
        model = apps.get_model(label)
        state[label] = list(model._base_manager.order_by("pk").values())
    return state


class TaskWorld:
    def make_people(self):
        groups = {name: Group.objects.get_or_create(name=name)[0] for name in roles.ALL_ROLES}

        def person(username, first, last, *role_names, **extra):
            user = User.objects.create_user(username, password=PASSWORD, first_name=first, last_name=last,
                                            **extra)
            user.groups.add(*[groups[name] for name in role_names])
            return user

        self.admin = person("admin1", "Анна", "Админова", roles.ROLE_ADMIN)
        self.manager = person("manager1", "Марк", "Менеджеров", roles.ROLE_MANAGER)
        self.manager2 = person("manager2", "Мария", "Вторая", roles.ROLE_MANAGER)
        self.keeper = person("keeper1", "Кирилл", "Кладовщиков", roles.ROLE_STOREKEEPER)
        self.accountant = person("accountant1", "Белла", "Бухгалтерова", roles.ROLE_ACCOUNTANT)
        self.nobody = person("nobody1", "Без", "Роли")
        self.boss = person("boss1", "Суп", "Пользователь", is_superuser=True, is_staff=True)
        self.groups = groups

    def make_documents(self):
        self.org = Organization.objects.create(name="Организация", is_default=True)
        self.wh = Warehouse.objects.create(name="Склад", is_default=True)
        self.product = Product.objects.create(name="Кабель", unit=Unit.objects.create(name="шт"),
                                              purchase_price=10)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.account = Account.objects.create(organization=self.org, name="Р/с", is_default=True)

        stock = StockAdjustment.objects.create(kind=StockAdjustment.KIND_INCOME, organization=self.org,
                                               warehouse=self.wh)
        stock.lines.create(product=self.product, quantity=100, price=10)
        stock.post()

        self.invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        self.invoice.lines.create(product=self.product, quantity=2, price=50, vat_rate="none")
        self.invoice.post()
        self.shipment = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                                invoice=self.invoice)
        self.shipment.lines.create(product=self.product, quantity=2, price=50, vat_rate="none")
        self.shipment.post()
        self.receipt = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        self.receipt.lines.create(product=self.product, quantity=5, price=10, vat_rate="none")
        self.receipt.post()
        self.customer_return = CustomerReturn.objects.create(organization=self.org, warehouse=self.wh,
                                                             customer=self.customer, shipment=self.shipment)
        self.customer_return.lines.create(product=self.product, quantity=1, price=50, vat_rate="none")
        self.supplier_return = SupplierReturn.objects.create(organization=self.org, warehouse=self.wh,
                                                             supplier=self.supplier, receipt=self.receipt)
        self.supplier_return.lines.create(product=self.product, quantity=1, price=10, vat_rate="none")
        self.payment = Payment.objects.create(kind=Payment.KIND_IN, organization=self.org, account=self.account,
                                              counterparty=self.customer, invoice=self.invoice,
                                              amount=Decimal("100"),
                                              settlement_purpose=Payment.PURPOSE_CUSTOMER_PAYMENT)
        self.payment.post()
        self.linked = {
            "counterparty": self.customer, "invoice": self.invoice, "shipment": self.shipment,
            "customer_return": self.customer_return, "receipt": self.receipt,
            "supplier_return": self.supplier_return, "payment": self.payment,
        }

    def make_world(self):
        self.make_people()
        self.make_documents()

    def new_task(self, actor, assignee, title="Позвонить покупателю", link=None, **kwargs):
        code, obj = (link, self.linked[link]) if isinstance(link, str) else (link or ("", None))
        data = services.NewTask(title=title, assignee=assignee, link_code=code,
                                link_id=obj.pk if obj is not None else None, **kwargs)
        return services.create_task(actor, data)

    def login(self, user):
        self.client.force_login(user)
