from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty
from apps.sales.models import Invoice, Shipment

from .money import amount_to_words, amount_short
from .pdf import render_pdf

User = get_user_model()


class AmountToWordsTests(TestCase):
    def test_basic(self):
        self.assertEqual(amount_to_words(Decimal("1234.56")),
                         "Одна тысяча двести тридцать четыре рубля 56 копеек")

    def test_one_ruble(self):
        self.assertEqual(amount_to_words(Decimal("1.01")), "Один рубль 01 копейка")

    def test_five_rubles_zero_kopecks(self):
        self.assertEqual(amount_to_words(Decimal("5.00")), "Пять рублей 00 копеек")

    def test_declension_edge_112(self):
        # 112 → «сто двенадцать рублей» (не рубль)
        self.assertTrue(amount_to_words(Decimal("112.00")).endswith("рублей 00 копеек"))

    def test_amount_short(self):
        self.assertEqual(amount_short(Decimal("900.00")), "900 руб. 00 коп.")


class PdfRenderTests(TestCase):
    def test_render_pdf_returns_pdf_bytes(self):
        html = "<html><body><h1>Тест кириллица</h1></body></html>"
        pdf, engine = render_pdf(html)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(engine, ("weasyprint", "playwright", "xhtml2pdf"))


class PrintViewsTests(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт", okei_code="796")
        self.org = Organization.objects.create(
            name='ООО "Пример"', full_name='ООО "Пример"', inn="7712345678", kpp="771201001",
            bank_name="Банк", bik="044525225", bank_account="40702810400000000001",
            corr_account="30101810400000000225", director_name="Иванов И.И.", is_default=True,
        )
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.customer = Counterparty.objects.create(name="Покупатель", inn="7701234567", partner_type=Counterparty.TYPE_CUSTOMER)
        self.product = Product.objects.create(name="Товар", unit=self.unit)

        self.user = User.objects.create_user("m", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="m", password="pass12345")

        self.invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        self.invoice.lines.create(product=self.product, quantity=6, price=150, vat_rate="20")
        self.shipment = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        self.shipment.lines.create(product=self.product, quantity=6, price=150, vat_rate="20")

    def test_invoice_payment_html(self):
        resp = self.client.get(reverse("print_invoice_payment", args=[self.invoice.pk]), {"fmt": "html"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Счёт на оплату")
        self.assertContains(resp, "Девятьсот рублей 00 копеек")

    def test_invoice_payment_pdf(self):
        resp = self.client.get(reverse("print_invoice_payment", args=[self.invoice.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertTrue(resp.content.startswith(b"%PDF"))

    def test_torg12_html(self):
        resp = self.client.get(reverse("print_shipment_torg12", args=[self.shipment.pk]), {"fmt": "html"})
        self.assertContains(resp, "Товарная накладная")

    def test_act_html(self):
        resp = self.client.get(reverse("print_shipment_act", args=[self.shipment.pk]), {"fmt": "html"})
        self.assertContains(resp, "Акт")

    def test_upd_html(self):
        resp = self.client.get(reverse("print_shipment_upd", args=[self.shipment.pk]), {"fmt": "html"})
        self.assertContains(resp, "Универсальный передаточный документ")

    def test_all_forms_pdf(self):
        for name in ("print_shipment_torg12", "print_shipment_act", "print_shipment_upd"):
            resp = self.client.get(reverse(name, args=[self.shipment.pk]))
            self.assertTrue(resp.content.startswith(b"%PDF"), f"{name} не вернул PDF")

    def test_invoice_payment_qr_html(self):
        resp = self.client.get(reverse("print_invoice_payment_qr", args=[self.invoice.pk]), {"fmt": "html"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Оплата по QR")
        self.assertContains(resp, "data:image/png;base64,")  # QR встроен

    def test_invoice_payment_without_qr_has_no_qr(self):
        resp = self.client.get(reverse("print_invoice_payment", args=[self.invoice.pk]), {"fmt": "html"})
        self.assertNotContains(resp, "Оплата по QR")

    def test_send_email_modal_prefills_customer_email(self):
        self.customer.email = "buyer@example.com"
        self.customer.save(update_fields=["email"])
        resp = self.client.get(reverse("send_invoice_email", args=[self.invoice.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "buyer@example.com")
        self.assertContains(resp, "Счёт на оплату")

    def test_send_email_posts_and_attaches_pdf(self):
        resp = self.client.post(
            reverse("send_invoice_email_qr", args=[self.invoice.pk]),
            {"to_email": "a@example.com, b@example.com", "subject": "Счёт", "message": "Текст"},
        )
        self.assertRedirects(resp, reverse("invoice_edit", args=[self.invoice.pk]))
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["a@example.com", "b@example.com"])
        self.assertEqual(len(msg.attachments), 1)
        name, content, mime = msg.attachments[0]
        self.assertTrue(name.endswith(".pdf"))
        self.assertEqual(mime, "application/pdf")
        self.assertTrue(content.startswith(b"%PDF"))

    def test_send_email_invalid_address_shows_error(self):
        resp = self.client.post(
            reverse("send_invoice_email", args=[self.invoice.pk]),
            {"to_email": "не-email", "subject": "Счёт", "message": "Текст"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)


class PaymentQrTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name='ООО "Пример"', full_name='ООО "Пример"', inn="7712345678", kpp="771201001",
            bank_name='АО "Банк"', bik="044525225", bank_account="40702810400000000001",
            corr_account="30101810400000000225", is_default=True,
        )
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)

    def test_payload_is_valid_st00012(self):
        from decimal import Decimal
        from apps.documents.qr import build_payment_payload
        payload = build_payment_payload(self.org, self.invoice, Decimal("600.00"))
        self.assertTrue(payload.startswith("ST00012|"))
        self.assertIn("PersonalAcc=40702810400000000001", payload)
        self.assertIn("BIC=044525225", payload)
        self.assertIn("Sum=60000", payload)  # рубли → копейки
        self.assertIn(f"Purpose=Оплата по счёту № {self.invoice.number}", payload)

    def test_no_bank_details_returns_none(self):
        from decimal import Decimal
        from apps.documents.qr import build_payment_payload
        org = Organization.objects.create(name="Без реквизитов")
        inv = Invoice.objects.create(organization=org, warehouse=self.wh, customer=self.customer)
        self.assertIsNone(build_payment_payload(org, inv, Decimal("100")))

    def test_qr_data_uri_generated(self):
        from decimal import Decimal
        from apps.documents.qr import invoice_qr_data_uri
        uri = invoice_qr_data_uri(self.org, self.invoice, Decimal("600"))
        self.assertTrue(uri.startswith("data:image/png;base64,"))
