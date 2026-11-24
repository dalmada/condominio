from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from .allocation import allocate
from .models import AuditEvent, Category, Closing, Expense, ExpenseShare, Payment, Period, Rule, Statement, Unit
from .services import balances, begin_revision, close_period, preview_period, record_payment, reverse_payment


class CalculationTests(SimpleTestCase):
    def test_remainders_are_invariant_under_input_order(self):
        shares = {"104": {}, "103": {}, "102": {}, "101": {}}
        self.assertEqual(allocate(10003, Rule.EQUAL, shares), {"101": 2501, "102": 2501, "103": 2501, "104": 2500})
        self.assertEqual(allocate(1, Rule.EQUAL, shares)["101"], 1)

    def test_percentages_are_exact(self):
        shares = {"A": {"percentage": "33.33"}, "B": {"percentage": "33.33"}, "C": {"percentage": "33.34"}}
        self.assertEqual(allocate(100, Rule.PERCENT, shares), {"A": 33, "B": 33, "C": 34})
        for invalid in (None, "-1", "33.340001", 101):
            shares["C"]["percentage"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                allocate(100, Rule.PERCENT, shares)

    def test_metered_and_mixed_fixed_plus_variable(self):
        shares = {"101": {"previous_reading": "12.0", "current_reading": "12.3"},
                  "102": {"previous_reading": "10.9", "current_reading": "11.8"}}
        self.assertEqual(allocate(10001, Rule.MEASURED, shares, invoice_volume="1.2"), {"101": 2500, "102": 7501})
        self.assertEqual(allocate(10001, Rule.MIXED, shares, invoice_volume="1.2", fixed_cents=4000),
                         {"101": 3500, "102": 6501})
        self.assertEqual(allocate(1, Rule.MIXED, shares, invoice_volume="1.2", fixed_cents=1),
                         {"101": 1, "102": 0})

    def test_measurement_errors_and_documented_resolution(self):
        measured = {"101": {"previous_reading": "12", "current_reading": "13"}}
        for reading in (None, "11", "12"):
            shares = {"101": {"previous_reading": "12", "current_reading": reading}}
            with self.subTest(reading=reading), self.assertRaises(ValidationError):
                allocate(100, Rule.MEASURED, shares, invoice_volume=1)
        for bad_args in ({"invoice_volume": None}, {"invoice_volume": 2},
                         {"invoice_volume": 2, "variance_action": "proportional"},
                         {"invoice_volume": 2, "variance_action": "common", "variance_reason": "Confirmado"}):
            with self.subTest(args=bad_args), self.assertRaises(ValidationError):
                allocate(100, Rule.MEASURED, measured, **bad_args)
        self.assertEqual(allocate(100, Rule.MEASURED, measured, invoice_volume=2,
                                  variance_action="proportional", variance_reason="Critério aprovado"), {"101": 100})
        self.assertEqual(allocate(100, Rule.MIXED, measured, invoice_volume=2, fixed_cents=20,
                                  variance_action="common", variance_reason="Área comum aferida"), {"101": 100})
        with self.assertRaises(ValidationError):
            allocate(100, Rule.MIXED, measured, invoice_volume="0.5", fixed_cents=20,
                     variance_action="common", variance_reason="Área comum")

    def test_mixed_reconciles_each_component_and_allows_zero_usage_on_fixed_bill(self):
        shares = {code: {"previous_reading": 0, "current_reading": 1} for code in ("A", "B")}
        self.assertEqual(allocate(2, Rule.MIXED, shares, invoice_volume=2, fixed_cents=1),
                         {"A": 2, "B": 0})
        for share in shares.values():
            share["current_reading"] = 0
        self.assertEqual(allocate(3, Rule.MIXED, shares, invoice_volume=0, fixed_cents=3),
                         {"A": 2, "B": 1})

    def test_no_participants_or_fixed_above_total(self):
        with self.assertRaises(ValidationError):
            allocate(100, Rule.EQUAL, {})
        with self.assertRaises(ValidationError):
            allocate(100, Rule.MIXED, {"101": {}}, fixed_cents=101)


class LedgerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = get_user_model().objects.create_user("admin_test")
        cls.units = list(Unit.objects.order_by("code"))
        cls.category = Category.objects.get(name="Água")
        cls.period = Period.objects.create(month=date(2026, 2, 1), due_on=date(2026, 3, 10))

    def add_expense(self, amount=10000, units=None, category=None, **changes):
        if units is None:
            units = self.units
        defaults = {"rule": Rule.EQUAL, "override_reason": "Critério conferido pelo administrador."}
        defaults.update(changes)
        expense = Expense.objects.create(period=self.period, category=category or self.category, amount_cents=amount,
                                         description="Conta do prédio", due_on=date(2026, 3, 3), **defaults)
        for unit in units:
            ExpenseShare.objects.create(expense=expense, unit=unit)
        return expense

    def close(self, version=0):
        return close_period(self.period, self.actor, expected_version=version)

    def test_initial_register_has_fixed_metered_rules(self):
        self.assertEqual([unit.code for unit in self.units], ["101", "201", "301", "401"])
        self.assertEqual(Category.objects.count(), 4)
        self.assertEqual(set(Category.objects.filter(default_rule=Rule.MEASURED).values_list("name", flat=True)), {"Água", "Gás"})
        self.assertFalse(Category.objects.filter(allows_metering=False).exclude(default_rule="").exists())
        with self.assertRaises(ValidationError):
            self.close()

    def test_full_reference_month_and_partial_receipts(self):
        self.add_expense(amount=20000)
        self.add_expense(amount=9601, category=Category.objects.get(name="Energia"))
        cleaning = Category.objects.get(name="Faxina")
        self.add_expense(amount=12000, category=cleaning, service_on=date(2026, 2, 5))
        self.add_expense(amount=6000, category=cleaning, units=self.units[:3], service_on=date(2026, 2, 18))
        gas = self.add_expense(amount=18000, category=Category.objects.get(name="Gás"), rule=Rule.PERCENT)
        for percentage, unit in zip((10, 20, 30, 40), self.units):
            gas.shares.filter(unit=unit).update(percentage=percentage)
        closing = self.close()
        self.assertEqual(closing.snapshot["expense_total"], 65601)
        self.assertEqual(closing.snapshot["expense_total"], closing.snapshot["allocated_total"])
        self.assertEqual([s.total_cents for s in closing.statements.all()], [14201, 16000, 17800, 17600])
        paid_on = date(2026, 3, 10)
        record_payment(self.period, self.units[0], 5000, paid_on, self.actor)
        record_payment(self.period, self.units[1], 16000, paid_on, self.actor)
        charges = balances(self.period)
        self.assertEqual([b["balance_cents"] for b in charges], [9201, 0, 17800, 17600])
        self.assertEqual(sum(b["received_cents"] for b in charges), 21000)
        self.assertEqual(sum(b["outstanding_cents"] for b in charges), 44601)

    def test_exclusion_conservation_and_statement_privacy(self):
        self.units[0].occupied = False
        self.units[0].save()
        other = self.units[1]
        other.responsible = "Identidade da outra unidade"
        other.save()
        self.add_expense(amount=10001, units=self.units[:3])
        closing = self.close()
        self.assertEqual([s.total_cents for s in closing.statements.all()], [3334, 3334, 3333, 0])
        self.assertEqual(closing.statements.last().items, [])
        statement = closing.statements.first()
        self.assertEqual(statement.display_name, statement.code)
        self.assertNotIn(other.responsible, str(statement.items))
        self.assertNotIn(other.code, str(statement.items))
        self.assertEqual(sum(row["share_cents"] for row in statement.items), statement.total_cents)
        self.assertNotIn("shares", statement.items[0])
        self.assertEqual(self.close().pk, closing.pk)
        self.assertEqual(Closing.objects.count(), 1)

    def test_invalid_percentages_and_missing_readings_block_the_entire_close(self):
        percentages = self.add_expense(rule=Rule.PERCENT)
        percentages.shares.update(percentage=Decimal("24.9999"))
        metered = self.add_expense(rule=Rule.MEASURED, consumed_from=date(2026, 2, 1),
                                   consumed_to=date(2026, 2, 28), invoice_volume=4)
        metered.shares.update(previous_reading=1)
        self.assertEqual(len(preview_period(self.period)["errors"]), 2)
        with self.assertRaises(ValidationError):
            self.close()
        self.assertFalse(Closing.objects.exists())
        self.assertFalse(Statement.objects.exists())
        percentages.shares.update(percentage=25)
        metered.shares.update(current_reading=2)
        self.assertEqual(self.close().snapshot["allocated_total"], 20000)

    def test_metered_rules_are_restricted_to_water_and_gas(self):
        self.assertSetEqual(set(Category.objects.filter(allows_metering=True).values_list("name", flat=True)),
                            {"Água", "Gás"})
        for name, rule in (("Energia", Rule.MEASURED), ("Faxina", Rule.MIXED)):
            expense = self.add_expense(category=Category.objects.get(name=name), rule=rule, invoice_volume=4,
                                       consumed_from=date(2026, 2, 1), consumed_to=date(2026, 2, 28))
            expense.shares.update(previous_reading=0, current_reading=1)
        self.assertEqual(len(preview_period(self.period)["errors"]), 2)
        with self.assertRaisesMessage(ValidationError, "água e gás"):
            self.close()

    def test_empty_correction_zeroes_statements_and_preserves_credit(self):
        unit = self.units[0]
        self.add_expense(amount=4000, units=[unit])
        original = self.close()
        record_payment(self.period, unit, 3000, date(2026, 3, 5), self.actor)
        with self.assertRaises(ValidationError):
            begin_revision(self.period, self.actor, " ", expected_version=1)
        begin_revision(self.period, self.actor, "Única despesa pertencia a outro prédio", expected_version=1)
        self.period.expenses.all().delete()
        self.assertEqual(balances(self.period)[0]["balance_cents"], 1000)
        corrected = self.close(1)
        self.assertEqual(corrected.snapshot["expenses"], [])
        self.assertEqual([s.total_cents for s in corrected.statements.all()], [0, 0, 0, 0])
        self.assertEqual(balances(self.period)[0]["credit_cents"], 3000)
        self.assertEqual(original.statements.first().total_cents, 4000)

    def test_unapproved_override_blocks_close_and_unit_code_is_permanent(self):
        self.category.default_rule = Rule.EQUAL
        self.category.default_shares = {u.code: None for u in self.units}
        self.category.save()
        expense = self.add_expense(override_reason="")
        self.category.default_shares = {}
        self.category.save()
        self.units[0].code = "A"
        with self.assertRaises(ValidationError):
            self.units[0].save()
        self.units[0].refresh_from_db()
        self.assertFalse(preview_period(self.period)["errors"])
        expense.shares.filter(unit=self.units[0]).delete()
        with self.assertRaises(ValidationError):
            self.close()

    def test_copy_revision_preserves_metadata_and_statements_even_after_master_change(self):
        unit = self.units[0]
        unit.display_name = "Apartamento do térreo"
        unit.responsible = "Moradora em fevereiro"
        unit.save()
        expense = self.add_expense(units=[unit], service_on=date(2026, 2, 18))
        old = self.close()
        self.category.name = "Água renomeada"
        self.category.default_rule = Rule.PERCENT
        self.category.save()
        unit.responsible, unit.occupied = "Novo responsável", False
        unit.display_name = "Nome atual"
        unit.save()
        self.assertEqual(preview_period(self.period), old.snapshot)
        begin_revision(self.period, self.actor, "Conta corrigida", expected_version=1)
        draft = preview_period(self.period)
        self.assertEqual(old.snapshot["expenses"], draft["expenses"])
        self.assertEqual(draft["units"][0]["responsible"], "Moradora em fevereiro")
        Expense.objects.filter(pk=expense.pk).update(amount_cents=9000)
        self.assertEqual(balances(self.period)[0]["balance_cents"], 10000)
        new = self.close(1)
        self.assertEqual(new.statements.first().total_cents, 9000)
        self.assertEqual(new.statements.first().code, "101")
        self.assertEqual(new.statements.first().display_name, "Apartamento do térreo")
        self.assertEqual(new.statements.first().responsible, "Moradora em fevereiro")
        self.assertEqual(new.statements.first().items[0]["category"], "Água")
        self.assertEqual(new.statements.first().items[0]["service_on"], "2026-02-18")
        self.assertEqual(old.statements.first().total_cents, 10000)
        self.assertEqual(old.snapshot["units"], new.snapshot["units"])
        self.assertEqual(AuditEvent.objects.filter(action="period.closed").count(), 2)

    def test_payments_partial_overdue_credit_retry_and_reversal(self):
        unit = self.units[0]
        expense = self.add_expense(units=[unit])
        self.close()
        token = uuid4()
        paid_on = date(2026, 3, 5)
        first = record_payment(self.period, unit, 3000, paid_on, self.actor, token=token)
        self.assertEqual(record_payment(self.period, unit, 3000, paid_on, self.actor, token=token), first)
        with self.assertRaises(ValidationError):
            record_payment(self.period, unit, 3001, paid_on, self.actor, token=token)
        record_payment(self.period, unit, 2000, paid_on, self.actor)
        with patch("ledger.services.timezone.localdate", return_value=date(2026, 3, 11)):
            row = balances(self.period)[0]
        self.assertEqual(row["balance_cents"], 5000)
        self.assertTrue(row["overdue"])
        self.assertTrue(row["paid_partial"])
        with self.assertRaises(ValidationError):
            record_payment(self.period, unit, 5001, paid_on, self.actor)
        begin_revision(self.period, self.actor, "A concessionária reduziu a fatura", expected_version=1)
        expense.amount_cents = 4500
        expense.save()
        self.close(1)
        row = balances(self.period)[0]
        self.assertEqual((row["credit_cents"], row["received_cents"], row["outstanding_cents"]), (500, 5000, 0))
        with self.assertRaises(ValidationError):
            reverse_payment(first, self.actor, "")
        reversal = reverse_payment(first, self.actor, "Pagamento lançado por engano")
        self.assertEqual(reverse_payment(first, self.actor, "Pagamento lançado por engano"), reversal)
        self.assertEqual(reversal.amount_cents, -3000)
        self.assertEqual(Payment.objects.count(), 3)
        self.assertEqual(balances(self.period)[0]["balance_cents"], 2500)

    def test_transaction_rolls_back_if_statement_step_fails(self):
        self.add_expense()
        with patch.object(Statement.objects, "bulk_create", side_effect=RuntimeError("falha simulada")):
            with self.assertRaises(RuntimeError):
                self.close()
        self.assertFalse(Closing.objects.exists())
        self.period.refresh_from_db()
        self.assertEqual(self.period.state, Period.State.OPEN)
        self.assertEqual(self.period.current_version, 0)
