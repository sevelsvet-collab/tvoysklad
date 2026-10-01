"""Публичные страницы и сведения о владельце на странице входа.

Нужны, чтобы Google не принимал форму входа за поддельную
(предупреждение «Обманные страницы»).
"""
from django.test import TestCase
from django.urls import reverse

from .models import Organization


class PublicPagesTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name='ООО Пример', full_name='Общество с ограниченной ответственностью Пример',
            inn="7712345678", phone="+7 495 000-00-00", email="info@example.ru", is_default=True,
        )

    def test_about_open_without_login(self):
        resp = self.client.get(reverse("about"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "служебная система складского и товарного учёта")
        self.assertContains(resp, self.org.full_name)
        self.assertContains(resp, "7712345678")
        self.assertContains(resp, "самостоятельной регистрации на сайте нет")

    def test_privacy_open_without_login(self):
        resp = self.client.get(reverse("privacy"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Политика конфиденциальности")
        self.assertContains(resp, "не передаются третьим лицам")

    def test_login_page_shows_owner_and_links(self):
        resp = self.client.get(reverse("login"))
        self.assertContains(resp, self.org.full_name)
        self.assertContains(resp, "ИНН 7712345678")
        self.assertContains(resp, "info@example.ru")
        self.assertContains(resp, "Доступ только для сотрудников")
        self.assertContains(resp, reverse("about"))
        self.assertContains(resp, reverse("privacy"))

    def test_login_page_without_organization(self):
        """Организации ещё нет — страница входа всё равно открывается."""
        Organization.objects.all().delete()
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)

    def test_robots_txt(self):
        resp = self.client.get("/robots.txt")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/plain; charset=utf-8")
        body = resp.content.decode()
        self.assertIn("Allow: /login/", body)
        self.assertIn("Disallow: /", body)

    def test_work_pages_still_require_login(self):
        resp = self.client.get(reverse("invoice_list"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp.url)
