"""Скан договора и выдача загруженных файлов.

Новый скан — только PDF или картинка с подходящим содержимым. Файл любого другого
типа, в том числе загруженный раньше, браузер получает на скачивание, а не как страницу.
Без входа в программу файлы не выдаются, сканы — только ролям со страницами договоров.
Данные только синтетические, файлы — во временной папке.

Браузерные тесты: условия запуска те же, что в documents.tests_browser_product_create.
"""
import io
import os
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.catalog.forms import ProductForm
from apps.catalog.models import Product
from apps.core import roles
from apps.core.forms import OrganizationForm
from apps.core.models import Organization
from apps.core.uploads import SCAN_ROLES, scan_problem, validate_scan
from apps.documents.tests_browser_product_create import BrowserWorld
from apps.partners.admin import ContractInline
from apps.partners.models import Contract, ContractTemplate, Counterparty
from apps.partners.views import EDIT_ROLES

User = get_user_model()

PDF = b"%PDF-1.4\n%%EOF\n"
PAGE = b"<!doctype html><title>t</title><script>window.__xss = 1;</script>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>window.__xss = 1;</script></svg>'
XHTML = b'<html xmlns="http://www.w3.org/1999/xhtml"><script>window.__xss = 1;</script></html>'


def image_bytes(image_format):
    out = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(out, image_format)
    return out.getvalue()


GIF_WITH_PAGE = image_bytes("GIF") + PAGE
GOOD_SCANS = {
    "скан.pdf": PDF,
    "СКАН.PDF": PDF,
    "scan.jpg": image_bytes("JPEG"),
    "scan.jpeg": image_bytes("JPEG"),
    "scan.png": image_bytes("PNG"),
    "scan.tif": image_bytes("TIFF"),
    "scan.tiff": image_bytes("TIFF"),
}
BAD_SCANS = {
    "a.html": PAGE,
    "a.HTM": PAGE,
    "a.svg": SVG,
    "a.xhtml": XHTML,
    "a.js": b"window.__xss = 1;",
    "a.docx": b"PK\x03\x04",
    "a": PDF,
    "a.pdf.html": PDF,
    "a.pdf": PAGE,                      # содержимое не соответствует расширению
    "a.png": PAGE,
    "a.jpg": image_bytes("PNG"),
    "a.tiff": image_bytes("JPEG"),
    "a.gif": image_bytes("GIF"),        # картинка, но для скана не предусмотрена
}


class WithoutMediaShortcut(StaticFilesHandler):
    """Живой сервер отдаёт media мимо адресов и middleware; проверяем настоящий путь."""

    def __init__(self, application):
        super().__init__(application.application)


class ScanWorld:
    def use_temp_media(self):
        self.media = Path(tempfile.mkdtemp())
        settings_override = override_settings(MEDIA_ROOT=str(self.media))
        settings_override.enable()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self.addCleanup(settings_override.disable)

    def make_world(self):
        self.org = Organization.objects.create(name="Организация", is_default=True)
        self.party = Counterparty.objects.create(name="Контрагент", partner_type=Counterparty.TYPE_BOTH)
        self.kind = ContractTemplate.objects.create(name="Вид", kind="supply")

    def contract_with_file(self, name, content):
        """Договор с файлом, записанным мимо формы: так выглядят загруженные раньше."""
        contract = Contract.objects.create(
            organization=self.org, counterparty=self.party, template=self.kind, date="2026-10-30")
        contract.scan.save(name, ContentFile(content))
        return contract

    def stored_files(self):
        return [path for path in self.media.rglob("*") if path.is_file()]


class ScanFormTests(ScanWorld, TestCase):
    def setUp(self):
        self.use_temp_media()
        self.make_world()
        user = User.objects.create_user("m", password="pass12345")
        user.groups.add(Group.objects.get_or_create(name=roles.ROLE_MANAGER)[0])
        self.client.force_login(user)

    def data(self, **extra):
        return {"template": self.kind.pk, "organization": self.org.pk, "counterparty": self.party.pk,
                "date": "2026-10-30", **extra}

    def test_good_scans_are_saved_whole(self):
        for name, content in GOOD_SCANS.items():
            with self.subTest(name):
                upload = SimpleUploadedFile(name, content)
                response = self.client.post(reverse("contract_create"), self.data(scan=upload))
                self.assertEqual(response.status_code, 302)
                contract = Contract.objects.latest("pk")
                with contract.scan.open("rb") as stored:
                    self.assertEqual(stored.read(), content)

    def test_bad_scans_are_rejected_with_message(self):
        for name, content in BAD_SCANS.items():
            with self.subTest(name):
                upload = SimpleUploadedFile(name, content)
                response = self.client.post(reverse("contract_create"), self.data(scan=upload))
                self.assertEqual(response.status_code, 200)
                self.assertIn("scan", response.context["form"].errors)
                self.assertContains(response, "Договор не сохранён")
                self.assertEqual(Contract.objects.count(), 0)
                self.assertEqual(self.stored_files(), [])

    def test_check_leaves_file_at_start(self):
        for name, content in GOOD_SCANS.items():
            with self.subTest(name):
                upload = SimpleUploadedFile(name, content)
                validate_scan(upload)
                self.assertEqual(upload.read(), content)

    def test_form_names_allowed_types(self):
        response = self.client.get(reverse("contract_create"))
        self.assertContains(response, 'accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff"')
        self.assertContains(response, "PDF, JPG, PNG или TIFF")

    def test_contract_with_old_file_can_be_saved_again(self):
        contract = self.contract_with_file("old.html", PAGE)
        response = self.client.post(reverse("contract_edit", args=[contract.pk]), self.data(comment="Правка"))
        self.assertEqual(response.status_code, 302)
        contract.refresh_from_db()
        self.assertEqual(contract.comment, "Правка")
        self.assertEqual(contract.scan.name, "contracts/old.html")

    def test_contract_with_missing_file_can_be_saved_again(self):
        contract = self.contract_with_file("gone.pdf", PDF)
        os.remove(contract.scan.path)
        response = self.client.post(reverse("contract_edit", args=[contract.pk]), self.data())
        self.assertEqual(response.status_code, 302)

    def test_rejected_replacement_keeps_previous_scan(self):
        contract = self.contract_with_file("good.pdf", PDF)
        upload = SimpleUploadedFile("bad.html", PAGE)
        response = self.client.post(reverse("contract_edit", args=[contract.pk]), self.data(scan=upload))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "bad.html")
        self.assertContains(response, "/media/contracts/good.pdf")
        contract.refresh_from_db()
        self.assertEqual(contract.scan.name, "contracts/good.pdf")
        self.assertEqual([path.name for path in self.stored_files()], ["good.pdf"])

    def test_scan_can_be_cleared(self):
        contract = self.contract_with_file("old.html", PAGE)
        response = self.client.post(reverse("contract_edit", args=[contract.pk]), self.data(**{"scan-clear": "on"}))
        self.assertEqual(response.status_code, 302)
        contract.refresh_from_db()
        self.assertFalse(contract.scan)

    def test_admin_inline_checks_new_file_too(self):
        inline = ContractInline(Counterparty, AdminSite())
        field = inline.formfield_for_dbfield(Contract._meta.get_field("scan"), RequestFactory().get("/"))
        with self.assertRaises(ValidationError):
            field.clean(SimpleUploadedFile("a.html", PAGE))
        self.assertEqual(field.clean(SimpleUploadedFile("a.pdf", PDF)).name, "a.pdf")


class PdfSignatureLimitTests(ScanWorld, TestCase):
    """Проверка PDF — только наличие «%PDF-» в первых 1024 байтах, не разбор файла.

    Такой файл форма принимает. Защита от него — при выдаче: тип application/pdf
    и запрет угадывания типа, браузер не откроет его как страницу."""

    def test_page_with_signature_passes_check_but_is_not_given_as_page(self):
        self.use_temp_media()
        content = b"%PDF-" + PAGE
        self.assertIsNone(scan_problem("a.pdf", io.BytesIO(content)))
        self.client.force_login(User.objects.create_superuser("root", password="pass12345"))
        (self.media / "a.pdf").write_bytes(content)
        response = self.client.get("/media/a.pdf")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        b"".join(response.streaming_content)
        response.close()

    def test_signature_after_first_kilobyte_is_rejected(self):
        self.assertIsNotNone(scan_problem("a.pdf", io.BytesIO(b" " * 1024 + b"%PDF-")))


class ImageFieldTests(TestCase):
    """Картинки товара и организации проверяет сама форма: это фиксируется тестом."""

    BAD = {"a.svg": SVG, "a.html": GIF_WITH_PAGE, "a.gif": PAGE}

    def test_product_image(self):
        for name, content in self.BAD.items():
            with self.subTest(name):
                form = ProductForm(data={}, files={"image": SimpleUploadedFile(name, content)})
                self.assertIn("image", form.errors)

    def test_organization_images(self):
        for field in ("logo", "signature", "stamp"):
            for name, content in self.BAD.items():
                with self.subTest(field=field, name=name):
                    form = OrganizationForm(data={}, files={field: SimpleUploadedFile(name, content)})
                    self.assertIn(field, form.errors)


def user_with_role(username, role):
    user = User.objects.create_user(username, password="pass12345")
    if role:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


class MediaRequests:
    def put(self, name, content=b"x"):
        path = self.media / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get(self, name, **headers):
        response = self.client.get(f"/media/{name}", headers=headers)
        if response.streaming:
            b"".join(response.streaming_content)
        response.close()
        return response


class MediaFileTests(ScanWorld, MediaRequests, TestCase):
    def setUp(self):
        self.use_temp_media()
        self.user = user_with_role("any", roles.ROLE_MANAGER)
        self.client.force_login(self.user)

    def test_without_login_files_are_not_given(self):
        self.client.logout()
        self.put("contracts/a.pdf", PDF)
        self.put("contracts/a.html", PAGE)
        for name in ("contracts/a.pdf", "contracts/a.html", "contracts/nothing.pdf", "../manage.py"):
            with self.subTest(name):
                response = self.get(name)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(reverse("login")))
                self.assertNotIn("Content-Disposition", response)

    def test_after_login_file_is_given_and_not_for_shared_caches(self):
        self.client.logout()
        self.put("contracts/a.pdf", PDF)
        self.assertEqual(self.get("contracts/a.pdf").status_code, 302)
        self.client.force_login(self.user)
        response = self.get("contracts/a.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-cache", response["Cache-Control"])

    def test_inactive_user_gets_no_file(self):
        self.put("contracts/a.pdf", PDF)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(self.get("contracts/a.pdf").status_code, 302)

    def test_safe_types_are_shown_with_exact_type(self):
        for name, expected in {
            "a.pdf": "application/pdf", "a.PDF": "application/pdf", "a.jpg": "image/jpeg", "a.jpeg": "image/jpeg",
            "a.png": "image/png", "a.gif": "image/gif", "a.webp": "image/webp", "a.bmp": "image/bmp",
            "a.tif": "image/tiff", "a.tiff": "image/tiff",
        }.items():
            with self.subTest(name):
                self.put(name)
                response = self.get(name)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], expected)
                self.assertNotIn("attachment", response["Content-Disposition"])
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_everything_else_is_a_download(self):
        names = ("a.html", "a.HTML", "a.htm", "a.xhtml", "a.xml", "a.svg", "a.SVG", "a.js", "a.mjs", "a.css",
                 "a.txt", "a.docx", "a.pdf.html", "a.html.gz", "a", ".htaccess", "договор.html")
        for name in names:
            with self.subTest(name):
                self.put(name)
                response = self.get(name)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/octet-stream")
                self.assertTrue(response["Content-Disposition"].startswith("attachment"))
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                self.assertNotIn("Content-Encoding", response)

    def test_download_keeps_file_name(self):
        self.put("contracts/договор.html")
        self.assertIn("%D0%B4%D0%BE%D0%B3", self.get("contracts/договор.html")["Content-Disposition"])

    def test_tricky_paths_do_not_change_the_decision(self):
        self.put("contracts/a.html", PAGE)
        self.put("contracts/b.pdf", PDF)
        for path in ("contracts/b.pdf/../a.html", "contracts/./a.html", "contracts//a.html",
                     "contracts/a.html/x.pdf/..", "contracts/a.html/."):
            with self.subTest(path):
                response = self.get(path)
                if response.status_code == 200:
                    self.assertEqual(response["Content-Type"], "application/octet-stream")

    def test_cached_copy_of_download_is_not_revalidated(self):
        self.put("a.html", PAGE)
        last_modified = self.get("a.html")["Last-Modified"]
        response = self.get("a.html", **{"If-Modified-Since": last_modified})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))

    def test_shown_file_still_uses_browser_cache(self):
        self.put("a.pdf", PDF)
        last_modified = self.get("a.pdf")["Last-Modified"]
        self.assertEqual(self.get("a.pdf", **{"If-Modified-Since": last_modified}).status_code, 304)

    def test_missing_file_and_outside_paths(self):
        self.assertEqual(self.get("nothing.pdf").status_code, 404)
        self.assertIn(self.get("../manage.py").status_code, (400, 404))
        self.assertIn(self.get("%2e%2e/manage.py").status_code, (400, 404))


class ScanRolesTests(ScanWorld, MediaRequests, TestCase):
    def setUp(self):
        self.use_temp_media()
        self.make_world()

    def login_as(self, role):
        username = f"user-{role}"
        self.client.force_login(User.objects.filter(username=username).first() or user_with_role(username, role))

    def test_roles_are_those_of_contract_pages(self):
        self.assertEqual(set(SCAN_ROLES), set(EDIT_ROLES))

    def test_roles_of_contract_pages_get_scan(self):
        contract = self.contract_with_file("scan.pdf", PDF)
        for role in SCAN_ROLES:
            with self.subTest(role):
                self.login_as(role)
                self.assertEqual(self.get(contract.scan.name).status_code, 200)
        self.client.force_login(User.objects.create_superuser("root", password="pass12345"))
        self.assertEqual(self.get(contract.scan.name).status_code, 200)

    def test_other_users_get_no_scan(self):
        for name, content in (("scan.pdf", PDF), ("old.html", PAGE)):
            contract = self.contract_with_file(name, content)
            for role in (roles.ROLE_STOREKEEPER, None):
                with self.subTest(name=name, role=role):
                    self.login_as(role)
                    response = self.get(contract.scan.name)
                    self.assertEqual(response.status_code, 403)
                    self.assertNotIn("Content-Disposition", response)
                    self.assertNotIn(b"%PDF", response.content)
                    self.assertEqual(self.client.get(reverse("contract_list")).status_code, 403)

    def test_refusal_does_not_tell_whether_file_exists(self):
        self.contract_with_file("scan.pdf", PDF)
        self.login_as(roles.ROLE_STOREKEEPER)
        for name in ("contracts/scan.pdf", "contracts/nothing.pdf", "contracts/", "contracts"):
            with self.subTest(name):
                self.assertEqual(self.get(name).status_code, 403)

    def test_browser_copy_is_not_confirmed_for_other_user(self):
        contract = self.contract_with_file("scan.pdf", PDF)
        self.login_as(roles.ROLE_MANAGER)
        last_modified = self.get(contract.scan.name)["Last-Modified"]
        self.assertEqual(self.get(contract.scan.name, **{"If-Modified-Since": last_modified}).status_code, 304)
        self.login_as(roles.ROLE_STOREKEEPER)
        self.assertEqual(self.get(contract.scan.name, **{"If-Modified-Since": last_modified}).status_code, 403)

    def test_other_spellings_of_address_give_no_scan(self):
        self.contract_with_file("scan.pdf", PDF)
        self.put("contracts/sub/inner.pdf", PDF)
        self.put("products/p.png")
        self.login_as(roles.ROLE_STOREKEEPER)
        for path in ("/contracts/scan.pdf", "./contracts/scan.pdf", "contracts//scan.pdf", "contracts/./scan.pdf",
                     "products/../contracts/scan.pdf", "nothing/../contracts/scan.pdf", "contracts/sub/inner.pdf",
                     "contracts/sub/../scan.pdf", "%2e/contracts/scan.pdf", "products%2F..%2Fcontracts%2Fscan.pdf"):
            with self.subTest(path):
                self.assertEqual(self.get(path).status_code, 403)
        # Эти написания ведут к тому же файлу только на части файловых систем
        for path in ("CONTRACTS/scan.pdf", "contracts%5Cscan.pdf", "contracts./scan.pdf", "contracts%20/scan.pdf"):
            with self.subTest(path):
                self.assertIn(self.get(path).status_code, (403, 404))

    def test_bad_addresses_do_not_break_the_check(self):
        self.login_as(roles.ROLE_STOREKEEPER)
        self.assertEqual(self.get("contracts/a%00.pdf").status_code, 404)
        self.assertEqual(self.get("a%00.pdf").status_code, 404)
        self.assertEqual(self.get("../manage.py").status_code, 400)

    def test_other_files_stay_open_to_logged_in_users(self):
        self.login_as(roles.ROLE_STOREKEEPER)
        for name in ("products/p.png", "org/logo.png", "contracts2/a.pdf", "contracts.pdf"):
            with self.subTest(name):
                self.put(name)
                self.assertEqual(self.get(name).status_code, 200)


class ProxyConfigTests(TestCase):
    def test_caddy_does_not_serve_media_past_the_application(self):
        caddyfile = Path(settings.BASE_DIR) / "docker" / "Caddyfile"
        if not caddyfile.exists():
            self.skipTest("docker/Caddyfile нет рядом с приложением (запуск в образе)")
        self.assertNotIn("file_server", caddyfile.read_text(encoding="utf-8"))


class CheckUploadsTests(ScanWorld, TestCase):
    def setUp(self):
        self.use_temp_media()
        self.make_world()

    def run_command(self):
        out = io.StringIO()
        call_command("check_uploads", stdout=out)
        return out.getvalue()

    def test_no_problems(self):
        self.contract_with_file("good.pdf", PDF)
        out = self.run_command()
        self.assertIn("Проверено сканов: 1", out)
        self.assertIn("Замечаний нет", out)

    def test_lists_problems_and_changes_nothing(self):
        wrong_type = self.contract_with_file("page.html", PAGE)
        wrong_content = self.contract_with_file("fake.pdf", PAGE)
        missing = self.contract_with_file("gone.pdf", PDF)
        os.remove(missing.scan.path)
        self.contract_with_file("good.pdf", PDF)
        before = sorted(path.name for path in self.stored_files())

        out = self.run_command()

        self.assertIn(f"(id {wrong_type.pk}): contracts/page.html — можно загрузить только", out)
        self.assertIn(f"(id {wrong_content.pk}): contracts/fake.pdf — содержимое", out)
        self.assertIn(f"(id {missing.pk}): contracts/gone.pdf — файла нет", out)
        self.assertNotIn("good.pdf", out)
        self.assertIn("Найдено: 3. Ничего не изменено.", out)
        self.assertEqual(sorted(path.name for path in self.stored_files()), before)
        wrong_type.refresh_from_db()
        self.assertEqual(wrong_type.scan.name, "contracts/page.html")


class ScanInBrowserTests(ScanWorld, BrowserWorld):
    static_handler = WithoutMediaShortcut

    def setUp(self):
        super().setUp()
        self.use_temp_media()
        self.kind = ContractTemplate.objects.create(name="Вид", kind="supply")

    def upload(self, name, content):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / name).write_bytes(content)
        self.open("contract_create")
        self.page.locator("#id_template").select_option(str(self.kind.pk))
        self.page.locator("#id_date").fill("2026-10-30")
        self.page.locator("#id_organization").select_option(str(self.org.pk))
        self.page.locator('input[name="counterparty"]').evaluate("(el, v) => { el.value = v; }", str(self.party.pk))
        self.page.locator("input[type=file][name=scan]").set_input_files(str(folder / name))
        self.page.locator("button.btn-success[type=submit]").first.click()
        self.page.wait_for_load_state("load")

    def scan_link(self):
        return self.page.locator('a[target="_blank"]', has_text="Загруженный файл")

    def test_page_is_rejected_on_upload(self):
        self.upload("a.html", PAGE)
        self.assertIn("Скан подписанного", self.page.locator(".alert-danger").inner_text())
        self.assertIn("Можно загрузить только", self.page.locator(".text-danger.small").first.inner_text())
        self.assertEqual(Contract.objects.count(), 0)
        self.assertEqual(self.stored_files(), [])
        self.assertIsNone(self.page.evaluate("window.__xss"))
        self.assertNoPageProblems()

    def test_pdf_and_picture_are_accepted_and_shown(self):
        for name, content, expected in (("a.pdf", PDF, "application/pdf"),
                                        ("a.png", image_bytes("PNG"), "image/png")):
            with self.subTest(name):
                self.upload(name, content)
                self.page.locator(".alert-success").wait_for()
                answer = self.page.request.get(self.live_server_url + self.scan_link().get_attribute("href"))
                self.assertEqual(answer.status, 200)
                self.assertEqual(answer.headers["content-type"], expected)
                self.assertNotIn("attachment", answer.headers["content-disposition"])
                self.assertEqual(answer.body(), content)
        self.page.goto(self.live_server_url + self.scan_link().get_attribute("href"))
        self.assertEqual(self.page.evaluate("document.contentType"), "image/png")

    def test_old_files_are_downloaded_and_do_not_run(self):
        for name, content in (("old.html", PAGE), ("old.htm", PAGE), ("old.svg", SVG), ("old.xhtml", XHTML)):
            with self.subTest(name):
                contract = self.contract_with_file(name, content)
                self.open("contract_edit", contract.pk)
                address = self.page.url
                href = self.scan_link().get_attribute("href")
                with self.page.expect_download() as download:
                    self.page.evaluate("url => { window.location.href = url; }", href)
                self.assertEqual(download.value.suggested_filename, name)
                self.assertEqual(self.page.url, address)
                self.assertIsNone(self.page.evaluate("window.__xss"))
                self.assertNoPageProblems()

    def test_visitor_without_login_gets_login_page(self):
        contract = self.contract_with_file("scan.pdf", PDF)
        visitor = self.browser.new_context(locale="ru-RU")
        self.addCleanup(visitor.close)
        page = visitor.new_page()
        answer = page.goto(self.live_server_url + contract.scan.url)
        self.assertIn(reverse("login"), page.url)
        self.assertEqual(page.locator("input[name=password]").count(), 1)
        self.assertIn("text/html", answer.headers["content-type"])

    def test_next_user_of_same_browser_does_not_see_scan(self):
        contract = self.contract_with_file("scan.png", image_bytes("PNG"))
        address = self.live_server_url + contract.scan.url
        page = self.context.new_page()
        self.assertEqual(page.goto(address).status, 200)
        self.client.force_login(user_with_role("k", roles.ROLE_STOREKEEPER))
        self.context.clear_cookies()
        self.context.add_cookies([{
            "name": "sessionid", "value": self.client.cookies["sessionid"].value, "url": self.live_server_url,
        }])
        self.assertEqual(page.goto(address).status, 403)

    def test_product_picture_is_shown_after_login(self):
        product = Product.objects.create(name="С картинкой", unit=self.unit)
        product.image.save("p.png", ContentFile(image_bytes("PNG")))
        self.open("product_edit", product.pk)
        picture = self.page.locator('img[src*="/media/"]')
        picture.wait_for()
        self.page.wait_for_function("document.querySelector('img[src*=\"/media/\"]').naturalWidth > 0")
        self.assertNoPageProblems()

    def test_old_file_does_not_run_when_embedded(self):
        contract = self.contract_with_file("old.svg", SVG)
        self.open("contract_edit", contract.pk)
        href = self.scan_link().get_attribute("href")
        self.page.evaluate("""url => {
          for (const tag of ['iframe', 'embed', 'object']) {
            const el = document.createElement(tag);
            el[tag === 'object' ? 'data' : 'src'] = url;
            document.body.appendChild(el);
          }
        }""", href)
        self.page.wait_for_timeout(500)
        self.assertIsNone(self.page.evaluate("window.__xss"))
        for frame in self.page.frames:
            self.assertIsNone(frame.evaluate("window.__xss"))
