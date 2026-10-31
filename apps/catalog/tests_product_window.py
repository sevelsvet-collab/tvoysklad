"""Серверная часть создания товара из строки документа.

Сам сценарий (окно → выбор в строке → цена → сохранение) проверяется в браузере:
apps/documents/tests_browser_product_create.py. Здесь — то, что отдаёт сервер:
сообщение окна, метка окна, цена по виду документа, технические id карточки.
"""
import json
import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.inventory.models import StockBalance

from .models import Product, Unit

User = get_user_model()
IDS = (999, 1000, 1540)
SAVED_DATA = re.compile(r'<script id="entity-saved-data" type="application/json">(.*?)</script>', re.S)


class WindowWorld(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.user = User.objects.create_user("m", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="m", password="pass12345")

    def product(self, pk=1540, **fields):
        return Product.objects.create(pk=pk, name=fields.pop("name", f"Товар {pk}"), unit=self.unit, **fields)

    def create_data(self, **extra):
        data = {
            "item_type": Product.TYPE_CHOICES[0][0], "name": "Новый товар", "unit": self.unit.pk,
            "vat_rate": "20", "purchase_price": "10", "sale_price": "20", "min_stock": "0", "is_active": "on",
        }
        data.update(extra)
        return data

    def saved_message(self, html):
        found = SAVED_DATA.search(html)
        return json.loads(found.group(1)) if found else None


class SavedMessageTests(WindowWorld):
    def test_id_is_json_number_for_each_id(self):
        for pk in IDS:
            with self.subTest(id=pk):
                self.product(pk)
                url = reverse("product_edit", args=[pk]) + "?embed=1&saved=1&modal=abc-1"
                html = self.client.get(url).content.decode()
                self.assertEqual(self.saved_message(html), {
                    "type": "entity-saved", "model": "product", "id": pk,
                    "name": f"Товар {pk}", "token": "abc-1",
                })
                # прежняя запись давала в скрипте «id: 1 540»
                self.assertNotIn("id: ", html[html.index("entity-saved-data"):])

    def test_name_cannot_break_out_of_script(self):
        self.product(name='</script><b>"Имя"</b>')
        html = self.client.get(reverse("product_edit", args=[1540]) + "?embed=1&saved=1").content.decode()
        self.assertNotIn("</script><b>", html)
        self.assertEqual(self.saved_message(html)["name"], '</script><b>"Имя"</b>')

    def test_no_message_outside_embed_or_before_save(self):
        self.product()
        card = reverse("product_edit", args=[1540])
        for query in ("", "?saved=1", "?embed=1", "?embed=1&modal=abc-1"):
            with self.subTest(query):
                html = self.client.get(card + query).content.decode()
                self.assertIsNone(self.saved_message(html))


class ModalTokenTests(WindowWorld):
    def test_token_goes_through_create_redirect(self):
        resp = self.client.post(reverse("product_create") + "?embed=1&modal=abc-1", self.create_data())
        product = Product.objects.get(name="Новый товар")
        self.assertRedirects(
            resp, reverse("product_edit", args=[product.pk]) + "?embed=1&saved=1&modal=abc-1",
            fetch_redirect_response=False)

    def test_foreign_token_is_dropped(self):
        for bad in ('"><script>', "a b", "a" * 65):
            with self.subTest(bad=bad):
                resp = self.client.post(reverse("product_create"),
                                        self.create_data(name=f"Товар {bad[:3]}"),
                                        QUERY_STRING=f"embed=1&modal={bad}")
                self.assertEqual(resp.status_code, 302)
                self.assertNotIn("modal=", resp["Location"])

    def test_form_error_stays_in_window_without_message(self):
        resp = self.client.post(reverse("product_create") + "?embed=1&modal=abc-1",
                                self.create_data(sale_price="99999999999999999", article="АРТ-1"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIsNone(self.saved_message(html))
        self.assertIn('value="АРТ-1"', html)
        self.assertIn('value="99999999999999999"', html)
        self.assertFalse(Product.objects.filter(name="Новый товар").exists())

    def test_form_error_on_saved_page_is_not_reported_as_saved(self):
        self.product()
        url = reverse("product_edit", args=[1540]) + "?embed=1&saved=1&modal=abc-1"
        resp = self.client.post(url, self.create_data(sale_price="99999999999999999"))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.saved_message(resp.content.decode()))


class PriceSourceApiTests(WindowWorld):
    def _price(self, pk, source):
        resp = self.client.get(reverse("api_product_search"), {"id": str(pk), "price": source})
        (item,) = resp.json()["results"]
        self.assertEqual(item["id"], pk)
        return item["price"]

    def test_price_by_document_kind(self):
        for pk in IDS:
            with self.subTest(id=pk):
                self.product(pk, purchase_price=Decimal("100.25"), sale_price=Decimal("200.75"))
                self.assertEqual(self._price(pk, "sale"), "200.75")
                self.assertEqual(self._price(pk, "purchase"), "100.25")

    def test_zero_price_is_sent(self):
        self.product(sale_price=Decimal("0"), purchase_price=Decimal("5"))
        self.assertEqual(Decimal(self._price(1540, "sale")), Decimal("0"))


class CardTechnicalIdsTests(WindowWorld):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(pk=1200, name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(pk=1300, name="Склад", is_default=True)
        self.phone = self.product(track_serials=True)
        StockBalance.objects.create(product=self.phone, warehouse=self.wh, organization=self.org,
                                    quantity=Decimal("2"))
        self.make_ready()

    def make_ready(self):
        from apps.inventory import transition

        if not transition.is_ready():
            self.skipTest("Учёт по организациям не отмечен как готовый в тестовой базе")

    def test_serial_history_link_and_place(self):
        html = self.client.get(reverse("product_edit", args=[1540])).content.decode()
        self.assertIn(f'{reverse("report_serials")}?product=1540"', html)
        self.assertIn('value="1300:1200"', html)
        self.assertNotIn("\xa0540", html)

    def test_token_survives_serial_form(self):
        url = reverse("product_edit", args=[1540]) + "?embed=1&modal=abc-1"
        html = self.client.get(url).content.decode()
        action = f'{reverse("product_serial_stock", args=[1540])}?embed=1&modal=abc-1"'
        self.assertIn(action, html)
        resp = self.client.post(
            reverse("product_serial_stock", args=[1540]) + "?embed=1&modal=abc-1",
            {"place": "1300:1200", "numbers": "SN-1"})
        self.assertEqual(resp["Location"], url + "#serials")
