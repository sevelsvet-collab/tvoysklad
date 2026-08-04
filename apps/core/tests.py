from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from . import roles
from .models import DocumentNumber, Organization, Warehouse

User = get_user_model()


class DocumentNumberTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Тест")

    def test_sequential_numbers(self):
        self.assertEqual(DocumentNumber.next_number(self.org, "invoice"), "00001")
        self.assertEqual(DocumentNumber.next_number(self.org, "invoice"), "00002")

    def test_separate_series_per_doc_type(self):
        DocumentNumber.next_number(self.org, "invoice")
        self.assertEqual(DocumentNumber.next_number(self.org, "shipment"), "00001")

    def test_separate_series_per_organization(self):
        other = Organization.objects.create(name="Другая")
        DocumentNumber.next_number(self.org, "invoice")
        self.assertEqual(DocumentNumber.next_number(other, "invoice"), "00001")


class DefaultFlagTests(TestCase):
    def test_single_default_organization(self):
        a = Organization.objects.create(name="А", is_default=True)
        b = Organization.objects.create(name="Б", is_default=True)
        a.refresh_from_db()
        self.assertFalse(a.is_default)
        self.assertTrue(b.is_default)
        self.assertEqual(Organization.get_default(), b)

    def test_single_default_warehouse(self):
        a = Warehouse.objects.create(name="А", is_default=True)
        b = Warehouse.objects.create(name="Б", is_default=True)
        a.refresh_from_db()
        self.assertFalse(a.is_default)
        self.assertTrue(b.is_default)


class RoleAccessTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user("manager", password="pass12345")
        self.manager.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.admin = User.objects.create_user("boss", password="pass12345")
        self.admin.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp.url)

    def test_manager_can_see_dashboard(self):
        self.client.login(username="manager", password="pass12345")
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_manager_cannot_open_settings(self):
        self.client.login(username="manager", password="pass12345")
        self.assertEqual(self.client.get(reverse("organization_list")).status_code, 403)

    def test_admin_role_can_open_settings(self):
        self.client.login(username="boss", password="pass12345")
        self.assertEqual(self.client.get(reverse("organization_list")).status_code, 200)
