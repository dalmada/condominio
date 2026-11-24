import tempfile
from datetime import date

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import AuditEvent, Category, Expense, Period, Rule, Unit
from .services import preview_period


class EntryWorkflowsTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("admin", is_staff=True)
        self.client.force_login(user)
        self.period = Period.objects.create(month=date(2026, 11, 1), due_on=date(2026, 12, 8))
        self.units = list(Unit.objects.order_by("code"))
        self.category = Category.objects.get(name="Faxina")
        self.category.default_rule = Rule.EQUAL
        self.category.default_shares = {unit.code: None for unit in self.units}
        self.category.save()
        self.create_url = reverse("expense_create", args=[self.period.pk])

    def expense_data(self):
        return {"category": self.category.pk, "amount": "103,01",
                "due_on": "2026-11-19", "rule": Rule.EQUAL, **{f"participates_{unit.pk}": "on" for unit in self.units}}

    def test_configure_participants_and_preserve_unit_identifier(self):
        url = reverse("category_edit", args=[self.category.pk])
        unit = self.units[0]
        data = {"name": self.category.name, "rule": "percent", f"participates_{unit.pk}": "on", f"percentage_{unit.pk}": "99,99"}
        self.assertContains(self.client.post(url, data), "A soma precisa ser 100%.")
        self.category.refresh_from_db()
        self.assertEqual(self.category.default_rule, Rule.EQUAL)
        data[f"percentage_{unit.pk}"] = "100"
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.client.post(reverse("unit_edit", args=[unit.pk]), {"code": "A", "display_name": "Térreo", "responsible": "Pessoa", "occupied": "False"})
        self.category.refresh_from_db()
        unit.refresh_from_db()
        self.assertEqual(self.category.default_shares, {"101": "100"})
        self.assertEqual(unit.code, "101")
        self.assertEqual(unit.display_name, "Térreo")
        self.assertContains(self.client.get(reverse("unit_list")), "Térreo")
        self.assertContains(self.client.get(self.create_url), "Térreo")
        self.assertFalse(unit.occupied)

    def test_launch_override_and_duplicate_warning(self):
        data = self.expense_data()
        data.pop(f"participates_{self.units[-1].pk}")
        data["supplier"] = "Érica"
        self.assertEqual(self.client.post(self.create_url, data).status_code, 302)
        created = Expense.objects.get()
        self.assertEqual(created.amount_cents, 10301)
        self.assertEqual(created.description, self.category.name)
        self.assertEqual(created.shares.count(), 3)
        self.assertTrue(created.override_reason)
        data.update(supplier="ÉRICA")
        response = self.client.post(self.create_url, data)
        self.assertContains(response, "Possível duplicidade")
        self.assertEqual(Expense.objects.count(), 1)
        data.update(duplicate_token=response.context["duplicate_token"], confirm_duplicate="1")
        self.assertEqual(self.client.post(self.create_url, data).status_code, 302)
        self.assertEqual(Expense.objects.count(), 2)

    def test_duplicate_confirmation_bound_to_data_matches_and_supplier(self):
        data = self.expense_data()
        data["supplier"] = "Pessoa A"
        self.client.post(self.create_url, data)
        data["supplier"] = "Pessoa B"
        self.assertEqual(self.client.post(self.create_url, data).status_code, 302)
        first_review = self.client.post(self.create_url, data)
        data.update(confirm_duplicate="1", duplicate_token=first_review.context["duplicate_token"],
                    consumed_from="2026-11-03")
        changed = self.client.post(self.create_url, data)
        self.assertContains(changed, "Possível duplicidade")
        self.assertEqual(Expense.objects.count(), 2)
        data["duplicate_token"] = changed.context["duplicate_token"]
        self.assertEqual(self.client.post(self.create_url, data).status_code, 302)
        replay = self.client.post(self.create_url, data)
        self.assertContains(replay, "Possível duplicidade")
        self.assertEqual(Expense.objects.count(), 3)

    def test_duplicate_requires_upload_reselection(self):
        with tempfile.TemporaryDirectory() as attachments, override_settings(MEDIA_ROOT=attachments):
            data = self.expense_data()
            self.client.post(self.create_url, data)
            content = b"%PDF-1.7 anexo particular"
            data["attachment"] = SimpleUploadedFile("conta.pdf", content)
            warning = self.client.post(self.create_url, data)
            self.assertContains(warning, "Selecione novamente")
            del data["attachment"]
            data.update(confirm_duplicate="1", duplicate_token=warning.context["duplicate_token"])
            self.assertContains(self.client.post(self.create_url, data), "Selecione novamente o anexo antes de salvar.")
            self.assertEqual(Expense.objects.count(), 1)
            data["attachment"] = SimpleUploadedFile("conta.pdf", content)
            self.assertEqual(self.client.post(self.create_url, data).status_code, 302)
            with Expense.objects.last().attachment.open() as uploaded:
                self.assertEqual(uploaded.read(), content)

    def test_edit_keeps_original_rule_after_defaults_change(self):
        data = self.expense_data()
        self.client.post(self.create_url, data)
        expense = Expense.objects.get()
        self.category.default_rule = Rule.PERCENT
        self.category.default_shares = {self.units[0].code: "100"}
        self.category.save()
        Expense.objects.filter(pk=expense.pk).update(description="Limpeza do térreo")
        response = self.client.post(reverse("expense_edit", args=[expense.pk]), data)
        self.assertEqual(response.status_code, 302)
        expense.refresh_from_db()
        self.assertEqual(expense.description, "Limpeza do térreo")

    def test_editing_closed_period_is_blocked_for_each_mutation(self):
        data = self.expense_data()
        self.client.post(self.create_url, data)
        expense = Expense.objects.get()
        self.period.state = "closed"
        self.period.save()
        data["amount"] = "999,00"
        for url in (self.create_url, reverse("expense_edit", args=[expense.pk]), reverse("expense_delete", args=[expense.pk])):
            self.assertRedirects(self.client.post(url, data), reverse("period_detail", args=[self.period.pk]), fetch_redirect_response=False)
        self.assertEqual(Expense.objects.count(), 1)
        self.assertEqual(Expense.objects.get().amount_cents, 10301)

    def test_metered_draft_and_variance(self):
        data = self.expense_data()
        data.update(category=Category.objects.get(name="Água").pk, rule=Rule.MEASURED,
                    override_reason="Conforme medidores.", invoice_volume="8,0000",
                    consumed_from="2026-10-01", consumed_to="2026-10-31")
        for unit in self.units:
            data[f"previous_{unit.pk}"] = "0001,10"
        self.assertEqual(self.client.post(self.create_url, data).status_code, 302)
        self.assertTrue(preview_period(self.period)["errors"])
        for unit in self.units:
            data[f"current_{unit.pk}"] = "0002,10"
            data[f"hot_previous_{unit.pk}"] = "00000,25"
            data[f"hot_current_{unit.pk}"] = "00000,75"
        data.update(variance_action="proportional", variance_reason="Responsável aprovou o rateio do consumo residual.")
        response = self.client.post(reverse("expense_edit", args=[Expense.objects.get().pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertIn("diverge do faturado", preview_period(self.period)["errors"][0])
        self.assertEqual(preview_period(self.period)["expenses"][0]["water_price_per_m3"], "12,8762")
        calculation = preview_period(self.period)["expenses"][0]["water_calculations"]["101"]
        self.assertEqual((calculation["consumption"], calculation["cents"]), ("1.5000", 1931))
        data["invoice_volume"] = "6,0000"
        self.assertEqual(self.client.post(reverse("expense_edit", args=[Expense.objects.get().pk]), data).status_code, 302)
        result = preview_period(self.period)
        self.assertFalse(result["errors"])
        self.assertEqual(result["allocated_total"], 10301)
        self.assertEqual(result["expenses"][0]["measured_volume"], "6.0000")
        edit_url = reverse("expense_edit", args=[Expense.objects.get().pk])
        hot_current = f"hot_current_{self.units[0].pk}"
        self.assertContains(self.client.post(edit_url, {**data, hot_current: "00000,750"}), "Use 5 dígitos")
        self.assertEqual(self.client.post(edit_url, {**data, hot_current: "00000,10"}).status_code, 302)
        self.assertIn("leitura atual de água quente", preview_period(self.period)["errors"][0])


    def test_metered_rules_only_available_for_water_and_gas(self):
        for category in Category.objects.all():
            url = reverse("category_edit", args=[category.pk])
            choices = dict(self.client.get(url).context["form"].fields["rule"].choices)
            self.assertEqual("measured" in choices, category.name in {"Água", "Gás"})
            self.assertNotIn("mixed", choices)
            if category.name in {"Energia", "Faxina"}:
                data = self.expense_data()
                data.update(category=category.pk, rule=Rule.MEASURED, override_reason="Teste")
                form = self.client.post(self.create_url, data).context["form"]
                self.assertIn("rule", form.errors)
                self.assertIn("rule", self.client.post(url, {**data, "name": category.name}).context["form"].errors)
        self.assertFalse(Expense.objects.exists())

    def test_create_and_rename_category_with_unique_name(self):
        url = reverse("category_create")
        self.assertContains(self.client.get(reverse("category_list")), url)
        self.assertEqual(self.client.post(url, {"name": "Manutenção", "allows_metering": "on"}).status_code, 302)
        category = Category.objects.get(name="Manutenção")
        self.assertFalse(category.allows_metering)
        self.assertEqual(category.default_rule, "")
        self.assertEqual(category.default_shares, {})
        self.assertContains(self.client.post(url, {"name": "  MANUTENÇÃO "}), "Já existe uma categoria")
        edit = reverse("category_edit", args=[category.pk])
        self.assertContains(self.client.post(edit, {"name": "ÁGUA"}), "Já existe uma categoria")
        self.assertEqual(self.client.post(edit, {"name": "Reparos"}).status_code, 302)
        self.assertContains(self.client.get(self.create_url), "Reparos")
        unit = self.units[0]
        self.assertEqual(self.client.post(url, {"name": "Seguro", "rule": "equal",
                                               f"participates_{unit.pk}": "on"}).status_code, 302)
        approved = Category.objects.get(name="Seguro")
        self.assertEqual(approved.default_shares, {unit.code: None})
        self.assertEqual(approved.default_rule, Rule.EQUAL)

    def test_renaming_metered_category_keeps_capability_and_published_name(self):
        water = Category.objects.get(name="Água")
        data = self.expense_data()
        data.update(category=water.pk, rule=Rule.MEASURED, invoice_volume="4", override_reason="Teste",
                    consumed_from="2026-11-01", consumed_to="2026-11-18")
        for unit in self.units:
            for key, reading in (("previous", "0000,00"), ("current", "0001,00"),
                                 ("hot_previous", "00000,00"), ("hot_current", "00000,00")):
                data[f"{key}_{unit.pk}"] = reading
        self.assertEqual(self.client.post(self.create_url, data).status_code, 302)
        detail_url = reverse("period_detail", args=[self.period.pk])
        token = self.client.get(detail_url).context["preview_key"]
        self.client.post(reverse("period_close", args=[self.period.pk]), {"version": 0, "preview_key": token})
        closing = self.period.closings.get()
        edit = reverse("category_edit", args=[water.pk])
        self.assertEqual(self.client.post(edit, {"name": "Abastecimento", "supplier": "COPASA", **{f"participates_{unit.pk}": "on" for unit in self.units}}).status_code, 302)
        water.refresh_from_db()
        self.assertTrue(water.allows_metering)
        self.assertIn("measured", dict(self.client.get(edit).context["form"].fields["rule"].choices))
        closing.refresh_from_db()
        self.assertEqual(closing.snapshot["expenses"][0]["category"], "Água")
        statement = self.client.get(reverse("statement", args=[closing.statements.first().pk]))
        self.assertContains(statement, "Água")
        self.assertNotContains(statement, "Abastecimento")

    def test_attachments_require_staff_and_valid_format(self):
        with tempfile.TemporaryDirectory() as attachments, override_settings(MEDIA_ROOT=attachments):
            data = self.expense_data()
            data["attachment"] = SimpleUploadedFile("fatura.pdf", b"not a pdf")
            self.assertContains(self.client.post(self.create_url, data), "arquivo precisa ser")
            data["attachment"] = SimpleUploadedFile("fatura.pdf", b"%PDF-1.7\nexample")
            self.assertEqual(self.client.post(self.create_url, data).status_code, 302)
            expense = Expense.objects.get()
            url = reverse("expense_attachment", args=[expense.pk])
            response = self.client.get(url)
            self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.7\nexample")
            self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
            self.client.logout()
            self.assertEqual(self.client.get(url).status_code, 302)
            self.client.force_login(get_user_model().objects.create_user("morador"))
            self.assertEqual(self.client.get(url).status_code, 403)

    def test_period_management(self):
        url = reverse("period_create")
        data = {"month": "2026-12", "due_on": "2027-01-05"}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        period = Period.objects.get(month=date(2026, 12, 1))
        self.assertContains(self.client.post(url, data), "competência", status_code=200)
        self.assertEqual(Period.objects.count(), 2)
        data["due_on"] = "2027-01-10"
        self.assertEqual(self.client.post(reverse("period_edit", args=[period.pk]), data).status_code, 302)
        period.refresh_from_db()
        self.assertEqual(period.due_on, date(2027, 1, 10))
        self.assertTrue(AuditEvent.objects.filter(action="period.updated").exists())

    def test_gas_tariff_sets_value_from_readings(self):
        for rule, expected in ((Rule.MEASURED, 5500),):
            data = self.expense_data()
            data.update(category=Category.objects.get(name="Gás").pk, rule=rule,
                        amount="", fixed_amount="10,00", invoice_volume="2,5",
                        consumed_from="2026-11-01", consumed_to="2026-11-18",
                        override_reason="Cobrança por medidor.")
            for unit, current in zip(self.units, ("00012,500", "00010,000", "00010,000", "00010,000")):
                data[f"previous_{unit.pk}"] = "00010,000"
                data[f"current_{unit.pk}"] = current
            self.assertEqual(self.client.post(self.create_url, data).status_code, 302)
            invalid = {**data, f"current_{self.units[0].pk}": "12,5"}
            self.assertContains(self.client.post(self.create_url, invalid), "Use cinco dígitos")
            self.assertIn("rule", self.client.post(self.create_url, {**data, "rule": Rule.MIXED}).context["form"].errors)
            expense = Expense.objects.get(rule=rule)
            self.assertEqual(expense.amount_cents, expected)
            detail = reverse("period_detail", args=[self.period.pk])
            self.assertContains(self.client.get(detail, {"visao": "unidades"}), "R$ 55,00")
            expense.consumed_from = None
            expense.save(update_fields=["consumed_from"])
            page = self.client.get(detail, {"visao": "unidades"})
            breakdown_url = reverse("unit_breakdown", args=[self.period.pk, self.units[0].pk])
            self.assertContains(page, breakdown_url)
            self.assertContains(page, "Provisório")
            self.assertNotContains(page, "Conferência do mês")
            self.assertNotContains(page, "Por apartamento")
            self.assertContains(page, "Despesas por categoria")
            breakdown = self.client.get(breakdown_url)
            self.assertContains(breakdown, "Consumo medido")
            self.assertContains(breakdown, "R$ 55,00")
            self.assertNotContains(breakdown, "Apartamento 201")
            self.assertContains(page, "R$ 55,00")
            self.assertEqual(self.client.post(self.create_url, self.expense_data()).status_code, 302)
            combined = self.client.get(detail, {"visao": "unidades"}).context["units"][0]
            self.assertEqual(combined["display_total"], 8076)
            self.assertTrue(combined["provisional"])

            expense.consumed_from = date(2026, 11, 1)
            expense.save(update_fields=["consumed_from"])
            self.assertFalse(preview_period(self.period)["errors"])
