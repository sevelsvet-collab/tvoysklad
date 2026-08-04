from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.catalog.models import Product, ProductGroup, Unit
from apps.core.models import Organization, Warehouse
from apps.finance.models import Account
from apps.partners.models import Counterparty

User = get_user_model()

DEFAULT_UNITS = [("шт", "796"), ("кг", "166"), ("г", "163"), ("л", "112"), ("м", "006"), ("упак", "778"), ("услуга", "")]


class Command(BaseCommand):
    help = "Создаёт демо-данные для разработки: администратора, организацию и склады"

    def handle(self, *args, **options):
        if not User.objects.filter(username="admin").exists():
            User.objects.create_superuser("admin", "admin@example.com", "admin123")
            self.stdout.write(self.style.SUCCESS("Создан суперпользователь admin / admin123 (смените пароль!)"))

        if not Organization.objects.exists():
            Organization.objects.create(
                name='ООО "Пример"',
                full_name='Общество с ограниченной ответственностью "Пример"',
                inn="7712345678", kpp="771201001", ogrn="1157746000000",
                legal_address="115114, г. Москва, ул. Примерная, д. 1",
                phone="+7 (495) 000-00-00", email="info@example.com",
                bank_name='АО "Банк Пример" г. Москва', bik="044525225",
                bank_account="40702810400000000001", corr_account="30101810400000000225",
                director_name="Иванов Иван Иванович",
                accountant_name="Петрова Мария Сергеевна",
                is_default=True,
            )
            self.stdout.write(self.style.SUCCESS("Создана демо-организация"))

        if not Warehouse.objects.exists():
            Warehouse.objects.create(name="Основной склад", address="г. Москва, ул. Складская, д. 5", is_default=True)
            Warehouse.objects.create(name="Розничный магазин", address="г. Москва, ул. Торговая, д. 12")
            self.stdout.write(self.style.SUCCESS("Созданы демо-склады"))

        for name, okei in DEFAULT_UNITS:
            Unit.objects.get_or_create(name=name, defaults={"okei_code": okei})

        if not Product.objects.exists():
            sht = Unit.objects.get(name="шт")
            electronics, _ = ProductGroup.objects.get_or_create(name="Электроника", parent=None)
            cables, _ = ProductGroup.objects.get_or_create(name="Кабели", parent=electronics)
            Product.objects.create(
                name="Ноутбук Lenovo IdeaPad 3", article="NB-001", group=electronics,
                unit=sht, purchase_price=35000, sale_price=45000, min_stock=2,
            )
            Product.objects.create(
                name="Кабель HDMI 2m", article="CAB-HDMI-2", group=cables,
                unit=sht, purchase_price=250, sale_price=490, min_stock=10,
            )
            Product.objects.create(
                name="Настройка оборудования", article="SRV-001", item_type=Product.TYPE_SERVICE,
                unit=Unit.objects.get(name="услуга"), sale_price=1500,
            )
            self.stdout.write(self.style.SUCCESS("Созданы демо-товары"))

        if not Counterparty.objects.exists():
            Counterparty.objects.create(
                name='ООО "Покупатель Плюс"', partner_type=Counterparty.TYPE_CUSTOMER,
                inn="7701234567", kpp="770101001", phone="+7 (495) 111-22-33",
                email="buy@example.com", legal_address="г. Москва, ул. Покупательская, д. 7",
                contact_person="Сидоров Пётр",
            )
            Counterparty.objects.create(
                name='ООО "Поставщик-Опт"', partner_type=Counterparty.TYPE_SUPPLIER,
                inn="7809876543", kpp="780901001", phone="+7 (812) 333-44-55",
                email="opt@example.com", legal_address="г. Санкт-Петербург, пр. Поставщиков, д. 21",
            )
            Counterparty.objects.create(
                name="ИП Смирнов А.В.", kind=Counterparty.KIND_ENTREPRENEUR,
                partner_type=Counterparty.TYPE_BOTH, inn="503209876543",
                phone="+7 (903) 555-66-77",
            )
            self.stdout.write(self.style.SUCCESS("Созданы демо-контрагенты"))

        if not Account.objects.exists():
            org = Organization.objects.filter(is_default=True).first() or Organization.objects.first()
            if org:
                Account.objects.create(organization=org, name="Расчётный счёт", kind=Account.KIND_BANK,
                                       bank_account="40702810400000000001", is_default=True)
                Account.objects.create(organization=org, name="Касса", kind=Account.KIND_CASH)
                self.stdout.write(self.style.SUCCESS("Созданы демо-счёт и касса"))

        self.stdout.write(self.style.SUCCESS("Готово"))
