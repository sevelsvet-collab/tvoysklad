"""Скачивание договора в Word и PDF — те же права, что у страниц договора.

Раньше обработчик требовал только входа: кладовщик, которому страницы договоров
отвечают 403, по прямому адресу получал сам договор с реквизитами сторон.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from apps.core import roles
from apps.core.models import Organization
from apps.partners.models import Contract, ContractTemplate, Counterparty
from apps.partners.views import EDIT_ROLES

User = get_user_model()

FORMATS = {"contract_pdf": b"%PDF", "contract_docx": b"PK"}


class ContractDownloadRightsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Организация", full_name="ООО Организация", is_default=True)
        cls.party = Counterparty.objects.create(name="Контрагент", partner_type=Counterparty.TYPE_BOTH)
        cls.kind = ContractTemplate.objects.create(name="Вид", kind="supply")
        cls.contract = Contract.objects.create(
            organization=cls.org, counterparty=cls.party, template=cls.kind, date="2026-10-30")
        cls.no_template = Contract.objects.create(organization=cls.org, counterparty=cls.party, date="2026-10-30")

    def login_as(self, role):
        user = User.objects.create_user(f"user-{role}", password="pass12345")
        if role:
            user.groups.add(Group.objects.get(name=role))
        self.client.force_login(user)
        return user

    def urls(self, contract=None):
        return {name: reverse(name, args=[(contract or self.contract).pk]) for name in FORMATS}

    def test_same_roles_as_contract_pages(self):
        for role in EDIT_ROLES:
            self.login_as(role)
            with self.subTest(role):
                self.assertEqual(self.client.get(reverse("contract_edit", args=[self.contract.pk])).status_code, 200)
                for name, start in FORMATS.items():
                    response = self.client.get(self.urls()[name])
                    self.assertEqual(response.status_code, 200, name)
                    self.assertTrue(response.content.startswith(start), name)

    def test_superuser_downloads(self):
        self.client.force_login(User.objects.create_superuser("root", password="pass12345"))
        for name, start in FORMATS.items():
            with self.subTest(name):
                self.assertTrue(self.client.get(self.urls()[name]).content.startswith(start))

    def test_regular_download_is_kept(self):
        """Печать открывается в браузере, «Скачать» и Word — файлом."""
        self.login_as(roles.ROLE_MANAGER)
        pdf = self.client.get(self.urls()["contract_pdf"])
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf["Content-Disposition"].startswith("inline"))
        saved = self.client.get(self.urls()["contract_pdf"], {"download": "1"})
        self.assertTrue(saved["Content-Disposition"].startswith("attachment"))
        word = self.client.get(self.urls()["contract_docx"])
        self.assertIn("wordprocessingml", word["Content-Type"])
        self.assertTrue(word["Content-Disposition"].startswith("attachment"))

    def test_buttons_lead_to_working_downloads(self):
        self.login_as(roles.ROLE_ACCOUNTANT)
        page = self.client.get(reverse("contract_edit", args=[self.contract.pk]))
        for url in self.urls().values():
            with self.subTest(url):
                self.assertContains(page, f'href="{url}"')
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_other_users_get_403(self):
        for role in (roles.ROLE_STOREKEEPER, None):
            self.login_as(role)
            self.assertEqual(self.client.get(reverse("contract_list")).status_code, 403)
            for query in ({}, {"download": "1"}):
                for name, url in self.urls().items():
                    with self.subTest(role=role, name=name, query=query):
                        response = self.client.get(url, query)
                        self.assertEqual(response.status_code, 403)
                        self.assertNotIn("Content-Disposition", response)
                        for start in FORMATS.values():
                            self.assertFalse(response.content.startswith(start))

    def test_refusal_does_not_tell_whether_contract_exists(self):
        self.login_as(roles.ROLE_STOREKEEPER)
        for name in FORMATS:
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name, args=[999999])).status_code, 403)
                self.assertEqual(self.client.get(self.urls(self.no_template)[name]).status_code, 403)

    def test_anonymous_goes_to_login(self):
        for name, url in self.urls().items():
            with self.subTest(name):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(reverse("login")))

    def test_disabled_user_goes_to_login(self):
        """Сессия отключённого пользователя больше не действует, даже с нужной ролью."""
        user = self.login_as(roles.ROLE_MANAGER)
        self.assertEqual(self.client.get(self.urls()["contract_docx"]).status_code, 200)
        user.is_active = False
        user.save()
        for name, url in self.urls().items():
            with self.subTest(name):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(reverse("login")))

    def test_allowed_user_contract_states(self):
        self.login_as(roles.ROLE_MANAGER)
        self.assertEqual(self.client.get(reverse("contract_pdf", args=[999999])).status_code, 404)
        response = self.client.get(self.urls(self.no_template)["contract_pdf"])
        self.assertRedirects(response, reverse("contract_edit", args=[self.no_template.pk]),
                             fetch_redirect_response=False)
