import tempfile
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Category, Closing, Expense, Payment, Period, Statement, Unit


class BillingWorkflowTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user("gestor", is_staff=True)
        self.client.force_login(self.actor)
        self.units = list(Unit.objects.order_by("code"))
        self.categories = {c.name: c for c in Category.objects.all()}
        self.client.post(reverse("period_create"), {"month": "2026-09", "due_on": "2026-10-10"})
        self.period = Period.objects.get()
        self.detail = reverse("period_detail", args=[self.period.pk])
        self.close_url = reverse("period_close", args=[self.period.pk])

    def expense(self, category, description, amount, *, participants=4, weights=(), attachment=None):
        data = {
            "category": self.categories[category].pk, "description": description, "amount": amount,
            "due_on": "2026-09-30", "rule": "percent" if weights else "equal",
            "override_reason": "Critério ilustrativo do teste",
        }
        for i, unit in enumerate(self.units[:participants]):
            data[f"participates_{unit.pk}"] = "on"
            if weights:
                data[f"percentage_{unit.pk}"] = weights[i]
        if category == "Água":
            data.update(rule="measured", invoice_volume=str(participants),
                        consumed_from="2026-09-01", consumed_to="2026-09-30")
            for unit in self.units[:participants]:
                for key, reading in (("previous", "0000,00"), ("current", "0001,00"),
                                     ("hot_previous", "00000,00"), ("hot_current", "00000,00")):
                    data[f"{key}_{unit.pk}"] = reading
        if attachment is not None:
            data["attachment"] = SimpleUploadedFile("conta.pdf", attachment)
        response = self.client.post(reverse("expense_create", args=[self.period.pk]), data)
        self.assertRedirects(response, self.detail)

    def close(self):
        page = self.client.get(self.detail)
        data = {"version": page.context["period"].current_version, "preview_key": page.context["preview_key"]}
        self.assertRedirects(self.client.post(self.close_url, data), self.detail)
        return data

    def test_reference_month_preview_payments_privacy_and_reversal(self):
        other = self.units[-1]
        other.responsible = "Responsável privado 401"
        other.save()
        self.expense("Água", "Água de setembro", "R$ 200,00")
        self.expense("Energia", "Iluminação comum", "96,01")
        self.expense("Faxina", "Faxina de 04/09", "120,00")
        self.expense("Faxina", "Faxina extra de 18/09", "60,00", participants=3)
        self.categories["Rateio percentual"] = Category.objects.create(name="Rateio percentual")
        self.expense("Rateio percentual", "Despesa de setembro", "180,00", weights=[10, 20, 30, 40])
        first_post = self.close()
        self.client.post(self.close_url, first_post)
        self.assertEqual(Closing.objects.count(), 1)
        self.assertEqual(list(Statement.objects.values_list("total_cents", flat=True)), [14201, 16000, 17800, 17600])
        for unit, amount in zip(self.units[:2], ("R$ 50,00", "R$ 160,00")):
            url = reverse("charge", args=[self.period.pk, unit.pk])
            receipt = self.client.get(url).context["form"].initial["token"]
            data = {"amount": amount, "paid_on": "2026-10-10", "token": receipt}
            self.assertRedirects(self.client.post(url, data), url)
            self.client.post(url, data)
        dashboard = self.client.get(self.detail)
        self.assertContains(dashboard, "R$ 210,00")
        self.assertContains(dashboard, "R$ 446,01")
        doc = Statement.objects.get(code="101")
        url = reverse("statement", args=[doc.pk])
        html = self.client.get(url)
        self.assertContains(html, "R$ 142,01")
        self.assertNotContains(html, other.responsible)
        self.assertNotContains(html, "Apartamento 401")
        payment = Payment.objects.filter(unit=self.units[0]).get()
        for _ in range(2):
            self.client.post(reverse("payment_reverse", args=[payment.pk]), {"reason": "Registro em duplicidade"})
        self.assertEqual(Payment.objects.count(), 3)
        self.assertContains(self.client.get(self.detail), "R$ 496,01")
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_archived_document_attachment_and_history_after_empty_revision(self):
        with tempfile.TemporaryDirectory() as storage, override_settings(MEDIA_ROOT=Path(storage)):
            self.expense("Água", "Conta de água", "10,00", attachment=b"%PDF-1.7\nconta-original")
            self.close()
            old = Closing.objects.get()
            expense = Expense.objects.get()
            attachment = reverse("archived_attachment", args=[old.pk, expense.pk])
            self.client.post(reverse("period_revise", args=[self.period.pk]), {"version": 1, "reason": "Mês errado"})
            self.client.post(reverse("expense_delete", args=[expense.pk]))
            self.close()
            self.assertEqual(Statement.objects.get(closing__version=2, code="101").total_cents, 0)
            lookup = self.client.get(reverse("history"), {"categoria": self.categories["Água"].pk})
            self.assertContains(lookup, "Substituída")
            self.assertContains(lookup, attachment)
            response = self.client.get(attachment)
            self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.7\nconta-original")
            self.client.logout()
            self.assertEqual(self.client.get(attachment).status_code, 302)

    def test_stale_preview_cannot_close_changed_expenses(self):
        self.expense("Faxina", "Primeira", "12,00")
        key = self.client.get(self.detail).context["preview_key"]
        self.expense("Energia", "Outra", "24,00")
        self.client.post(self.close_url, {"version": 0, "preview_key": key})
        self.assertFalse(Closing.objects.exists())


class FirstAccessTests(TestCase):
    @override_settings(ALLOWED_HOSTS=["127.0.0.1", "localhost", "testserver"], PRODUCTION=False)
    def test_first_access_is_loopback_only_and_single_use(self):
        url = reverse("first_access")
        self.assertEqual(self.client.get(url, HTTP_HOST="localhost", REMOTE_ADDR="192.0.2.9").status_code, 404)
        self.assertEqual(self.client.get(url, HTTP_HOST="testserver").status_code, 404)
        password = "Realistic-" + uuid.uuid4().hex
        self.assertEqual(self.client.post(url, {"username": "primeiro", "password1": password, "password2": password},
                                          HTTP_HOST="localhost").status_code, 302)
        self.assertTrue(get_user_model().objects.get().is_staff)
        self.assertEqual(self.client.get(url, HTTP_HOST="localhost").status_code, 404)
