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


class ProductCardFromDocumentTests(TestCase):
    """Карточка товара во всплывающем окне документа (?embed=1)."""

    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.product = Product.objects.create(name="Кабель HDMI", article="H-2", unit=self.unit)
        self.user = User.objects.create_user("manager", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="manager", password="pass12345")

    def _post_data(self, **extra):
        data = {
            "item_type": "service", "name": "Установка и настройка оборудования",
            "article": "", "code": "", "barcode": "", "unit": self.unit.pk,
            "vat_rate": "20", "purchase_price": "0", "sale_price": "1500",
            "min_stock": "0", "description": "", "is_active": "on",
        }
        data.update(extra)
        return data

    def test_search_by_id_returns_exact_product(self):
        Product.objects.create(name="Другой товар", unit=self.unit)
        resp = self.client.get(reverse("api_product_search"), {"id": self.product.pk})
        results = resp.json()["results"]
        self.assertEqual([r["id"] for r in results], [self.product.pk])
        self.assertEqual(results[0]["label"], "Кабель HDMI (H-2)")

    def test_search_by_id_finds_archived_product(self):
        """Архивный товар может стоять в старом документе — подпись нужна и ему."""
        self.product.is_active = False
        self.product.save()
        resp = self.client.get(reverse("api_product_search"), {"id": self.product.pk})
        self.assertEqual(len(resp.json()["results"]), 1)

    def test_create_form_prefilled_from_document_line(self):
        resp = self.client.get(reverse("product_create"), {
            "embed": "1", "name": "Установка оборудования",
            "price_source": "sale", "price": "1500",
        })
        form = resp.context["form"]
        self.assertEqual(form.initial["name"], "Установка оборудования")
        self.assertEqual(form.initial["sale_price"], Decimal("1500"))
        self.assertEqual(form.initial["unit"], self.unit.pk)
        self.assertNotContains(resp, 'class="topbar"')   # без верхнего меню

    def test_embed_create_stays_in_card_and_notifies_document(self):
        resp = self.client.post(reverse("product_create") + "?embed=1", self._post_data())
        product = Product.objects.get(name="Установка и настройка оборудования")
        self.assertTrue(product.is_service)
        self.assertRedirects(resp, reverse("product_edit", args=[product.pk]) + "?embed=1&saved=1")
        page = self.client.get(resp.url)
        self.assertContains(page, "entity-saved")
        self.assertContains(page, f"id: {product.pk}")

    def test_regular_create_returns_to_list(self):
        resp = self.client.post(reverse("product_create"), self._post_data())
        self.assertRedirects(resp, reverse("product_list"))

    def test_embed_edit_renames_product(self):
        resp = self.client.post(
            reverse("product_edit", args=[self.product.pk]) + "?embed=1",
            self._post_data(name="Кабель HDMI 2 м", item_type="product"),
        )
        self.assertEqual(resp.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, "Кабель HDMI 2 м")


def break_dimension(buf):
    """Портит размерность листа, как в экспорте МойСклада (<dimension ref="A1"/>).

    В экономном режиме openpyxl после этого видит одну ячейку на строку —
    ровно так импорт и ломался."""
    import re
    import zipfile

    src = zipfile.ZipFile(buf)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.startswith("xl/worksheets/sheet"):
                data = re.sub(rb'<dimension ref="[^"]*"/>', b'<dimension ref="A1"/>', data)
            dst.writestr(item, data)
    out.seek(0)
    return out


MOYSKLAD_HEADER = [
    "Группы", "UUID", "Тип", "Код", "Наименование", "Внешний код", "Артикул", "Единица измерения",
    "Цена: Цена продажи", "Валюта (Цена продажи)", "Закупочная цена", "Валюта (Закупочная цена)",
    "Неснижаемый остаток", "Штрихкод EAN13", "Штрихкод EAN8", "Описание", "НДС", "Архивный",
]


def moysklad_row(code, name, *, kind="Товар", unit="шт", sale="2500,00", purchase="1250,00",
                 ean13="", ean8="", vat="без НДС", archived="нет", group=None, description=None):
    return [group, "uuid-" + code, kind, code, name, "ext", None, unit, sale, "руб", purchase, "руб",
            None, ean13, ean8, description, vat, archived]


class MoySkladImportTests(TestCase):
    """Выгрузка товаров из МойСклад загружается как есть."""

    def _import(self, rows):
        return import_products(break_dimension(make_xlsx(rows, MOYSKLAD_HEADER)))

    def test_moysklad_file_with_broken_dimension_imports(self):
        created, updated, errors = self._import([
            moysklad_row("00435", "AirPods 2", ean13="2000996048633"),
            moysklad_row("01101", "Borofone BL8", sale="150,00", purchase="0,00"),
        ])
        self.assertEqual((created, updated, errors), (2, 0, []))
        airpods = Product.objects.get(code="00435")
        self.assertEqual(airpods.sale_price, Decimal("2500.00"))
        self.assertEqual(airpods.purchase_price, Decimal("1250.00"))
        self.assertEqual(airpods.barcode, "2000996048633")
        self.assertEqual(airpods.vat_rate, "none")
        self.assertEqual(airpods.unit.name, "шт")

    def test_same_names_with_different_codes_stay_separate(self):
        """В МойСклад бывают одноимённые товары — не склеиваем их по названию."""
        created, _, errors = self._import([
            moysklad_row("00001", "ОФД на 36 месяцев", kind="Услуга", unit=None),
            moysklad_row("00002", "ОФД на 36 месяцев", kind="Услуга", unit=None),
        ])
        self.assertEqual((created, errors), (2, []))
        self.assertEqual(Product.objects.filter(name="ОФД на 36 месяцев").count(), 2)
        self.assertTrue(all(p.is_service for p in Product.objects.all()))

    def test_reimport_updates_by_code(self):
        self._import([moysklad_row("00435", "AirPods 2")])
        created, updated, _ = self._import([moysklad_row("00435", "AirPods 2 Pro", sale="3000,00")])
        self.assertEqual((created, updated), (0, 1))
        product = Product.objects.get(code="00435")
        self.assertEqual((product.name, product.sale_price), ("AirPods 2 Pro", Decimal("3000.00")))

    def test_archived_imported_inactive(self):
        self._import([moysklad_row("00007", "Старый товар", archived="да")])
        self.assertFalse(Product.objects.get(code="00007").is_active)

    def test_first_of_several_barcodes(self):
        self._import([moysklad_row("00008", "Чехол", ean13="4660042756660,4660042756677")])
        self.assertEqual(Product.objects.get(code="00008").barcode, "4660042756660")

    def test_barcode_from_other_column_when_ean13_empty(self):
        self._import([moysklad_row("00009", "Мелочь", ean8="12345670")])
        self.assertEqual(Product.objects.get(code="00009").barcode, "12345670")

    def test_group_path_creates_nested_groups(self):
        self._import([moysklad_row("00010", "Тариф", group="Связь/Тарифы МТС")])
        group = Product.objects.get(code="00010").group
        self.assertEqual((group.name, group.parent.name), ("Тарифы МТС", "Связь"))

    def test_bad_row_does_not_break_import(self):
        created, _, errors = self._import([
            moysklad_row("00011", "Нормальный"),
            moysklad_row("00012", "Х" * 600),   # длиннее поля — обрежется, не упадёт
        ])
        self.assertEqual((created, errors), (2, []))
        self.assertEqual(len(Product.objects.get(code="00012").name), 512)

    def test_vat_rates_parsed_correctly(self):
        """Раньше «10» превращалось в «1», а «0» — в пустоту, и обе ставки становились 20%."""
        from apps.catalog.importers import _vat

        self.assertEqual([_vat(v) for v in ["20", "10", "0", "20%", "10.0", "без НДС", ""]],
                         ["20", "10", "0", "20", "10", "none", "20"])

    def test_header_not_found_gives_readable_error(self):
        created, updated, errors = import_products(make_xlsx([["a", "b"]], ["Колонка1", "Колонка2"]))
        self.assertEqual((created, updated), (0, 0))
        self.assertIn("Наименование", errors[0])
