import io
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from apps.core import roles

from .importers import import_counterparties
from .models import Counterparty
from .services import InnLookupError

User = get_user_model()


def make_xlsx(header, rows, blank_rows_before_header=0):
    wb = Workbook()
    ws = wb.active
    for _ in range(blank_rows_before_header):
        ws.append([])
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


class CounterpartyViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("manager", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="manager", password="pass12345")
        Counterparty.objects.create(name="Ромашка", inn="7701", partner_type=Counterparty.TYPE_CUSTOMER)
        Counterparty.objects.create(name="Опт-Снаб", inn="7802", partner_type=Counterparty.TYPE_SUPPLIER)

    def test_list_shows_all(self):
        resp = self.client.get(reverse("counterparty_list"))
        self.assertContains(resp, "Ромашка")
        self.assertContains(resp, "Опт-Снаб")

    def test_search_by_name(self):
        resp = self.client.get(reverse("counterparty_list"), {"q": "ромашка"})
        self.assertContains(resp, "Ромашка")
        self.assertNotContains(resp, "Опт-Снаб")

    def test_filter_by_type(self):
        resp = self.client.get(reverse("counterparty_list"), {"type": "supplier"})
        self.assertContains(resp, "Опт-Снаб")
        self.assertNotContains(resp, "Ромашка")

    def test_htmx_returns_partial(self):
        resp = self.client.get(reverse("counterparty_list"), HTTP_HX_REQUEST="true")
        self.assertNotContains(resp, "<html")
        self.assertContains(resp, "Ромашка")

    def test_inn_lookup_fills_data(self):
        fake = {
            "name": 'АО "СЕВТЕЛЕКОМ"', "full_name": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "СЕВАСТОПОЛЬ ТЕЛЕКОМ"',
            "kind": "legal", "inn": "9204569240", "kpp": "920401001",
            "ogrn": "1189204003229", "okpo": "28667467",
            "legal_address": "299011, Россия, г Севастополь, ул Генерала Петрова, 15",
            "director_name": "Иванов Иван",
        }
        with mock.patch("apps.partners.views.lookup_inn", return_value=fake):
            resp = self.client.get(reverse("inn_lookup"), {"inn": "9204569240"})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["data"]["kpp"], "920401001")

    def test_inn_lookup_error_reported(self):
        with mock.patch("apps.partners.views.lookup_inn", side_effect=InnLookupError("Не настроен ключ DaData")):
            resp = self.client.get(reverse("inn_lookup"), {"inn": "123"})
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("DaData", data["error"])

    def test_create_with_bank_account(self):
        data = {
            "name": "Новый Клиент", "kind": "legal", "partner_type": "customer",
            "full_name": "", "inn": "", "kpp": "", "ogrn": "",
            "legal_address": "", "actual_address": "", "phone": "", "email": "",
            "contact_person": "", "director_name": "", "comment": "", "is_active": "on",
            "banks-TOTAL_FORMS": "1", "banks-INITIAL_FORMS": "0",
            "banks-0-bank_name": "Тест-Банк", "banks-0-bik": "044525225",
            "banks-0-account": "40702810000000000001", "banks-0-corr_account": "",
            "contracts-TOTAL_FORMS": "1", "contracts-INITIAL_FORMS": "0",
            "contracts-0-organization": "", "contracts-0-number": "", "contracts-0-date": "", "contracts-0-name": "",
            "contacts-TOTAL_FORMS": "1", "contacts-INITIAL_FORMS": "0",
            "contacts-0-full_name": "", "contacts-0-position": "", "contacts-0-phone": "",
            "contacts-0-email": "", "contacts-0-comment": "",
        }
        data["contacts-0-full_name"] = "Иванов И.И."
        data["contacts-0-position"] = "Бухгалтер"
        data["contacts-0-phone"] = "+79990000000"
        resp = self.client.post(reverse("counterparty_create"), data)
        self.assertEqual(resp.status_code, 302)
        cp = Counterparty.objects.get(name="Новый Клиент")
        self.assertEqual(cp.bank_accounts.count(), 1)
        self.assertEqual(cp.bank_accounts.first().bank_name, "Тест-Банк")
        # остаёмся на карточке контрагента, а не в списке
        self.assertEqual(resp.url, reverse("counterparty_edit", args=[cp.pk]))
        # контактное лицо сохранилось
        self.assertEqual(cp.contacts.count(), 1)
        self.assertEqual(cp.contacts.first().position, "Бухгалтер")

    def _base_data(self, name, **extra):
        data = {
            "name": name, "kind": "legal", "partner_type": "customer",
            "full_name": "", "inn": "", "kpp": "", "ogrn": "",
            "legal_address": "", "actual_address": "", "phone": "", "email": "",
            "contact_person": "", "director_name": "", "comment": "", "is_active": "on",
            "banks-TOTAL_FORMS": "0", "banks-INITIAL_FORMS": "0",
            "contracts-TOTAL_FORMS": "0", "contracts-INITIAL_FORMS": "0",
            "contacts-TOTAL_FORMS": "0", "contacts-INITIAL_FORMS": "0",
        }
        data.update(extra)
        return data

    def test_duplicate_warning_on_matching_inn(self):
        Counterparty.objects.create(name="Ромашка", inn="7712345678", partner_type="customer")
        # попытка создать другого с тем же ИНН — предупреждение, не создаётся
        resp = self.client.post(reverse("counterparty_create"), self._base_data("Ромашка-2", inn="7712345678"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "уже есть в базе")
        self.assertFalse(Counterparty.objects.filter(name="Ромашка-2").exists())
        # подтверждение «всё равно создать» — создаётся
        resp2 = self.client.post(
            reverse("counterparty_create"),
            self._base_data("Ромашка-2", inn="7712345678", confirm_duplicate="1"),
        )
        self.assertEqual(resp2.status_code, 302)
        self.assertTrue(Counterparty.objects.filter(name="Ромашка-2").exists())

    def test_no_duplicate_warning_for_unique(self):
        resp = self.client.post(reverse("counterparty_create"), self._base_data("Уникальный Клиент"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Counterparty.objects.filter(name="Уникальный Клиент").exists())


class CounterpartySearchApiTests(TestCase):
    """Живой поиск контрагентов в документах."""

    def setUp(self):
        self.buyer = Counterparty.objects.create(
            name="ООО Ромашка", inn="7701234567", phone="+7 495 111-22-33",
            partner_type=Counterparty.TYPE_CUSTOMER,
        )
        self.supplier = Counterparty.objects.create(
            name="Опт-Снаб", inn="7809876543", partner_type=Counterparty.TYPE_SUPPLIER,
        )
        self.both = Counterparty.objects.create(name="Универсал", partner_type=Counterparty.TYPE_BOTH)
        Counterparty.objects.create(name="Архивный", partner_type=Counterparty.TYPE_CUSTOMER, is_active=False)

        self.user = User.objects.create_user("mgr", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="mgr", password="pass12345")

    def _names(self, **params):
        resp = self.client.get(reverse("api_counterparty_search"), params)
        return [r["name"] for r in resp.json()["results"]]

    def test_search_by_name_part(self):
        self.assertEqual(self._names(q="ромаш"), ["ООО Ромашка"])

    def test_search_by_inn_and_phone(self):
        self.assertEqual(self._names(q="7809876543"), ["Опт-Снаб"])
        self.assertEqual(self._names(q="111-22-33"), ["ООО Ромашка"])

    def test_type_filter_includes_both(self):
        customers = self._names(type="customer")
        self.assertIn("ООО Ромашка", customers)
        self.assertIn("Универсал", customers)
        self.assertNotIn("Опт-Снаб", customers)

        suppliers = self._names(type="supplier")
        self.assertIn("Опт-Снаб", suppliers)
        self.assertIn("Универсал", suppliers)
        self.assertNotIn("ООО Ромашка", suppliers)

    def test_archived_excluded(self):
        self.assertNotIn("Архивный", self._names())

    def test_details_contain_inn(self):
        row = self.client.get(reverse("api_counterparty_search"), {"q": "ромаш"}).json()["results"][0]
        self.assertIn("ИНН 7701234567", row["details"])

    def test_quick_create(self):
        resp = self.client.post(
            reverse("api_counterparty_quick_create"), {"name": "Васин Владимир", "type": "customer"},
        )
        data = resp.json()
        self.assertTrue(data["created"])
        cp = Counterparty.objects.get(name="Васин Владимир")
        self.assertEqual(cp.partner_type, Counterparty.TYPE_CUSTOMER)
        self.assertEqual(data["counterparty"]["id"], cp.pk)

    def test_quick_create_returns_existing(self):
        resp = self.client.post(reverse("api_counterparty_quick_create"), {"name": "ооо ромашка"})
        data = resp.json()
        self.assertFalse(data["created"])
        self.assertEqual(data["counterparty"]["id"], self.buyer.pk)

    def test_quick_create_forbidden_for_storekeeper(self):
        kl = User.objects.create_user("kl", password="pass12345")
        kl.groups.add(Group.objects.get(name=roles.ROLE_STOREKEEPER))
        self.client.login(username="kl", password="pass12345")
        resp = self.client.post(reverse("api_counterparty_quick_create"), {"name": "Нельзя"})
        self.assertEqual(resp.status_code, 403)

    def test_quick_create_by_inn_fills_requisites(self):
        fake = {
            "name": 'АО "СЕВТЕЛЕКОМ"', "full_name": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "СЕВАСТОПОЛЬ ТЕЛЕКОМ"',
            "kind": Counterparty.KIND_LEGAL, "inn": "9204569240", "kpp": "920401001",
            "ogrn": "1189204003229", "okpo": "28667467",
            "legal_address": "299011, г Севастополь", "director_name": "Иванов И.",
        }
        with mock.patch("apps.partners.api.lookup_inn", return_value=fake):
            resp = self.client.post(
                reverse("api_counterparty_quick_create"), {"name": "9204569240", "type": "supplier"},
            )
        data = resp.json()
        self.assertTrue(data["created"])
        self.assertTrue(data["by_inn"])
        cp = Counterparty.objects.get(inn="9204569240")
        self.assertEqual(cp.name, 'АО "СЕВТЕЛЕКОМ"')
        self.assertEqual(cp.kpp, "920401001")
        self.assertEqual(cp.partner_type, Counterparty.TYPE_SUPPLIER)

    def test_quick_create_by_inn_dedup(self):
        cp = Counterparty.objects.create(name="Старое", inn="7712345678", partner_type=Counterparty.TYPE_CUSTOMER)
        resp = self.client.post(reverse("api_counterparty_quick_create"), {"name": "7712345678"})
        data = resp.json()
        self.assertFalse(data["created"])
        self.assertEqual(data["counterparty"]["id"], cp.pk)

    def test_quick_create_by_inn_fallback_when_lookup_fails(self):
        from apps.partners.services import InnLookupError

        with mock.patch("apps.partners.api.lookup_inn", side_effect=InnLookupError("Нет ключа DaData")):
            resp = self.client.post(reverse("api_counterparty_quick_create"), {"name": "5044156440"})
        data = resp.json()
        self.assertTrue(data["created"])
        self.assertFalse(data["by_inn"])
        cp = Counterparty.objects.get(inn="5044156440")
        self.assertEqual(cp.name, "5044156440")


MOYSKLAD_HEADER = [
    "UUID", "Группы", "Код", "Наименование", "Внешний код", "Полное наименование",
    "Юридический адрес", "ИНН", "КПП", "ОКПО", "Телефон", "E-mail",
    "БИК", "Банк", "К/с", "Р/с", "ОГРН", "Тип контрагента", "Статус", "Архивный",
]


class MoySkladImportTests(TestCase):
    def _row(self, **kwargs):
        row = dict.fromkeys(MOYSKLAD_HEADER, None)
        row.update(kwargs)
        return [row[h] for h in MOYSKLAD_HEADER]

    def test_import_moysklad_export_with_bank(self):
        file = make_xlsx(MOYSKLAD_HEADER, [
            self._row(**{
                "Наименование": 'АО "СЕВТЕЛЕКОМ"',
                "Полное наименование": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "СЕВАСТОПОЛЬ ТЕЛЕКОМ"',
                "Юридический адрес": "299011, Россия, г Севастополь",
                "ИНН": "9204569240", "КПП": "920401001", "ОКПО": "28667467",
                "БИК": "043510107", "Банк": 'АБ "РОССИЯ"',
                "К/с": "30101810835100000107", "Р/с": "40702810816282006919",
                "ОГРН": "1189204003229",
                "Тип контрагента": "Юридическое лицо. Россия", "Архивный": "нет",
            }),
            self._row(**{
                "Наименование": "Аблязизов Сервер Эдемович",
                "Телефон": "+79788704198",
                "Тип контрагента": "Физическое лицо. Россия", "Архивный": "да",
            }),
        ])
        created, updated, errors = import_counterparties(file)
        self.assertEqual((created, updated, errors), (2, 0, []))

        org = Counterparty.objects.get(inn="9204569240")
        self.assertEqual(org.kind, Counterparty.KIND_LEGAL)
        self.assertEqual(org.okpo, "28667467")
        self.assertEqual(org.ogrn, "1189204003229")
        account = org.bank_accounts.get()
        self.assertEqual(account.account, "40702810816282006919")
        self.assertEqual(account.bik, "043510107")

        person = Counterparty.objects.get(name="Аблязизов Сервер Эдемович")
        self.assertEqual(person.kind, Counterparty.KIND_PERSON)
        self.assertFalse(person.is_active)

    def test_reimport_updates_by_inn_and_keeps_partner_type(self):
        cp = Counterparty.objects.create(name="Старое имя", inn="9204569240",
                                         partner_type=Counterparty.TYPE_SUPPLIER)
        file = make_xlsx(MOYSKLAD_HEADER, [
            self._row(**{"Наименование": 'АО "СЕВТЕЛЕКОМ"', "ИНН": "9204569240",
                         "Тип контрагента": "Юридическое лицо. Россия"}),
        ])
        created, updated, errors = import_counterparties(file)
        self.assertEqual((created, updated), (0, 1))
        cp.refresh_from_db()
        self.assertEqual(cp.name, 'АО "СЕВТЕЛЕКОМ"')
        # тип «поставщик» не затёрт — в файле МойСклад этой колонки нет
        self.assertEqual(cp.partner_type, Counterparty.TYPE_SUPPLIER)

    def test_header_not_on_first_row(self):
        file = make_xlsx(["Наименование", "ИНН"], [["Тест", "111"]], blank_rows_before_header=3)
        created, updated, errors = import_counterparties(file)
        self.assertEqual((created, updated, errors), (1, 0, []))

    def test_no_header_gives_readable_error(self):
        file = make_xlsx(["Колонка1", "Колонка2"], [["a", "b"]])
        created, updated, errors = import_counterparties(file)
        self.assertEqual(created, 0)
        self.assertIn("Наименование", errors[0])
