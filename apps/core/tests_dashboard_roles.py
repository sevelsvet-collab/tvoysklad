from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from apps.core import roles
from apps.core.views import DashboardView


class DashboardRolesTests(TestCase):
    def make_user(self, *names, superuser=False):
        user = get_user_model().objects.create_user(
            username=f"user-{get_user_model().objects.count()}", is_superuser=superuser,
        )
        for name in names:
            user.groups.add(Group.objects.get(name=name))
        self.client.force_login(user)
        return user

    def test_keeper_and_unassigned_never_build_financial_context(self):
        for names in [(roles.ROLE_STOREKEEPER,), ()]:
            with self.subTest(names=names):
                self.make_user(*names)
                with patch.object(DashboardView, "get_context_data", side_effect=AssertionError("Financial data read")):
                    response = self.client.get(reverse("dashboard"))
                    if names:
                        self.assertRedirects(response, reverse("task_mine"))
                    else:
                        self.assertEqual(response.status_code, 403)

    def test_financial_roles_keep_dashboard(self):
        for name in [roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_ACCOUNTANT]:
            with self.subTest(role=name):
                self.make_user(name)
                response = self.client.get(reverse("dashboard"))
                self.assertEqual(response.status_code, 200)
                self.assertIn("money_total", response.context)
                self.assertContains(response, "chart-values")

    def test_superuser_and_combined_roles(self):
        self.make_user(superuser=True)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.make_user(roles.ROLE_STOREKEEPER, roles.ROLE_MANAGER)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_anonymous_still_requires_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("login") + "?next=/", fetch_redirect_response=False)

    def test_keeper_navigation_and_reports(self):
        self.make_user(roles.ROLE_STOREKEEPER)
        response = self.client.get(reverse("task_mine"))
        self.assertNotContains(response, 'bi-graph-up')
        for name in ["report_sales", "report_cashflow"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_role_removal_takes_effect_on_next_request(self):
        user = self.make_user(roles.ROLE_MANAGER)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        user.groups.clear()
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 403)
