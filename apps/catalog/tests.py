import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from apps.core import roles

from .importers import import_products
from .models import Product, ProductGroup, Unit

User = get_user_model()


def make_xlsx(rows, header):
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


class GroupTreeTests(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.root = ProductGroup.objects.create(name="Электроника")
        self.child = ProductGroup.objects.create(name="Кабели", parent=self.root)
        Product.objects.create(name="Ноутбук", group=self.root, unit=self.unit)
        Product.objects.create(name="Кабель HDMI", group=self.child, unit=self.unit)
        Product.objects.create(name="Стол", unit=self.unit)

        self.user = User.objects.create_user("kladovshik", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_STOREKEEPER))
        self.client.login(username="kladovshik", password="pass12345")

    def test_descendant_ids(self):
        self.assertCountEqual(self.root.descendant_ids(), [self.root.pk, self.child.pk])

    def test_filter_by_root_group_includes_children(self):
        resp = self.client.get(reverse("product_list"), {"group": self.root.pk})
        self.assertContains(resp, "Ноутбук")
        self.assertContains(resp, "Кабель HDMI")
        self.assertNotContains(resp, "Стол")

    def test_search(self):
        resp = self.client.get(reverse("product_list"), {"q": "hdmi"})
        self.assertContains(resp, "Кабель HDMI")
        self.assertNotContains(resp, "Ноутбук")


class ProductSearchApiTests(TestCase):
    """Живой поиск товаров в строках документов."""

    def setUp(self):
        from apps.core.models import Organization, Warehouse
        from apps.inventory.models import StockAdjustment

        self.unit = Unit.objects.create(name="шт", okei_code="796")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.wh2 = Warehouse.objects.create(name="Второй")
        self.laptop = Product.objects.create(
            name="Ноутбук Lenovo", article="NB-001", unit=self.unit,
            purchase_price=35000, sale_price=45000,
        )
        self.cable = Product.objects.create(name="Кабель HDMI", article="CAB-1", unit=self.unit)
        self.service = Product.objects.create(
            name="Настройка", item_type=Product.TYPE_SERVICE, unit=self.unit, sale_price=1500,
        )
        # 7 ноутбуков на основном складе
        adj = StockAdjustment.objects.create(
            kind=StockAdjustment.KIND_INCOME, organization=self.org, warehouse=self.wh,
        )
        adj.lines.create(product=self.laptop, quantity=7, price=100)
        adj.post()

        self.user = User.objects.create_user("kl", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_STOREKEEPER))
        self.client.login(username="kl", password="pass12345")

    def test_search_by_name_and_article(self):
        by_name = self.client.get(reverse("api_product_search"), {"q": "ноут"}).json()["results"]
        self.assertEqual([r["name"] for r in by_name], ["Ноутбук Lenovo"])

        by_article = self.client.get(reverse("api_product_search"), {"q": "CAB-1"}).json()["results"]
        self.assertEqual([r["name"] for r in by_article], ["Кабель HDMI"])

    def test_search_returns_stock_for_warehouse(self):
        row = self.client.get(
            reverse("api_product_search"), {"q": "ноут", "warehouse": self.wh.pk},
        ).json()["results"][0]
        self.assertEqual(row["stock"], 7)

        other = self.client.get(
            reverse("api_product_search"), {"q": "ноут", "warehouse": self.wh2.pk},
        ).json()["results"][0]
        self.assertEqual(other["stock"], 0)

    def test_price_source_switches_purchase_sale(self):
        purchase = self.client.get(reverse("api_product_search"), {"q": "ноут"}).json()["results"][0]
        self.assertEqual(purchase["price"], "35000.00")

        sale = self.client.get(
            reverse("api_product_search"), {"q": "ноут", "price": "sale"},
        ).json()["results"][0]
        self.assertEqual(sale["price"], "45000.00")

    def test_service_has_no_stock(self):
        row = self.client.get(reverse("api_product_search"), {"q": "Настройка"}).json()["results"][0]
        self.assertIsNone(row["stock"])

    def test_quick_create_new_product(self):
        resp = self.client.post(reverse("api_product_quick_create"), {"name": "Шрек", "price_value": "250"})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["created"])
        product = Product.objects.get(name="Шрек")
        self.assertEqual(product.unit, self.unit)
        self.assertEqual(product.purchase_price, Decimal("250"))
        self.assertEqual(data["product"]["id"], product.pk)

    def test_quick_create_returns_existing_instead_of_duplicate(self):
        resp = self.client.post(reverse("api_product_quick_create"), {"name": "ноутбук lenovo"})
        data = resp.json()
        self.assertFalse(data["created"])
        self.assertEqual(data["product"]["id"], self.laptop.pk)
        self.assertEqual(Product.objects.filter(name__iexact="Ноутбук Lenovo").count(), 1)

    def test_quick_create_requires_name(self):
        resp = self.client.post(reverse("api_product_quick_create"), {"name": "  "})
        self.assertEqual(resp.status_code, 400)

    def test_quick_create_forbidden_for_accountant(self):
        buh = User.objects.create_user("buh", password="pass12345")
        buh.groups.add(Group.objects.get(name=roles.ROLE_ACCOUNTANT))
        self.client.login(username="buh", password="pass12345")
        resp = self.client.post(reverse("api_product_quick_create"), {"name": "Запрещено"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Product.objects.filter(name="Запрещено").exists())

    def test_search_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("api_product_search"), {"q": "ноут"})
        self.assertEqual(resp.status_code, 302)

    def test_barcode_scan_auto_selects_single_match(self):
        self.laptop.barcode = "4600051000057"
        self.laptop.save()
        data = self.client.get(reverse("api_product_search"), {"q": "4600051000057"}).json()
        self.assertEqual(data["auto_select"], self.laptop.pk)

    def test_barcode_no_auto_select_when_partial(self):
        self.laptop.barcode = "4600051000057"
        self.laptop.save()
        data = self.client.get(reverse("api_product_search"), {"q": "460005"}).json()
        self.assertIsNone(data["auto_select"])

    def test_product_stock_endpoint(self):
        data = self.client.get(
            reverse("api_product_stock"),
            {"ids": f"{self.laptop.pk},{self.cable.pk},{self.service.pk}", "warehouse": self.wh.pk},
        ).json()["stock"]
        self.assertEqual(data[str(self.laptop.pk)], {"stock": 7.0, "available": 7.0, "unit": "шт"})
        self.assertEqual(data[str(self.cable.pk)]["stock"], 0.0)
        self.assertIsNone(data[str(self.service.pk)]["stock"])  # услуга — без остатка


class ImportProductsTests(TestCase):
    def test_import_creates_and_updates(self):
        header = ["Наименование", "Тип", "Группа", "Артикул", "Ед. изм.", "Ставка НДС", "Закупочная цена", "Цена продажи"]
        file = make_xlsx([
            ["Ноутбук", "Товар", "Электроника", "NB-1", "шт", "20", "35000", "45000"],
            ["Доставка", "Услуга", "", "", "услуга", "Без НДС", "", "500"],
        ], header)
        created, updated, errors = import_products(file)
        self.assertEqual((created, updated, errors), (2, 0, []))

        nb = Product.objects.get(article="NB-1")
        self.assertEqual(nb.sale_price, 45000)
        self.assertEqual(nb.group.name, "Электроника")
        self.assertEqual(Product.objects.get(name="Доставка").item_type, Product.TYPE_SERVICE)

        # повторный импорт того же артикула — обновление, не дубль
        file2 = make_xlsx([["Ноутбук Pro", "Товар", "Электроника", "NB-1", "шт", "20", "40000", "52000"]], header)
        created, updated, errors = import_products(file2)
        self.assertEqual((created, updated), (0, 1))
        nb.refresh_from_db()
        self.assertEqual(nb.name, "Ноутбук Pro")
        self.assertEqual(nb.sale_price, 52000)
