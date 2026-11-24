"""Transactional operations for drafts, published assessments and cash receipts."""
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .allocation import allocate
from .models import AuditEvent, Closing, Expense, ExpenseShare, Payment, Period, Rule, Statement, Unit


def log_event(actor, action, subject, detail=""):
    return AuditEvent.objects.create(actor=actor, action=action,
                                     subject=f"{subject._meta.label_lower}:{subject.pk}", detail=detail)


def _plain(value):
    return str(value) if value is not None else None


def _br(value):
    return format(Decimal(str(value)).normalize(), "f").replace(".", ",")


def _matches_default(expense, rows):
    baseline = expense.baseline
    if not expense.rule or baseline.get("rule") != expense.rule:
        return False
    weights = baseline.get("shares_by_id")
    if weights is None:
        weights = {str(row.unit_id): baseline["shares"][row.unit.code]
                   for row in rows if row.unit.code in baseline.get("shares", {})}
    if {str(row.unit_id) for row in rows} != weights.keys():
        return False
    if expense.rule != Rule.PERCENT:
        return True
    try:
        return all(row.percentage == Decimal(str(weights[str(row.unit_id)])) for row in rows)
    except (InvalidOperation, ValueError):
        return False


def meter_usage(share, prefix, code):
    previous = share[prefix + "previous_reading"]
    current = share[prefix + "current_reading"]
    meter = "água quente" if prefix else "água fria"
    if previous is None or current is None:
        raise ValidationError(f"Informe as leituras de {meter} da unidade {code}.")
    usage = Decimal(current) - Decimal(previous)
    if usage < 0:
        raise ValidationError(f"A leitura atual de {meter} da unidade {code} é inferior à anterior.")
    return usage


def _expense_row(expense, units, original):
    rows = list(expense.shares.all())
    shares = {}
    for row in rows:
        code = units[row.unit_id]["code"]
        shares[code] = {"unit_id": row.unit_id, "percentage": _plain(row.percentage),
                        "previous_reading": _plain(row.previous_reading),
                        "current_reading": _plain(row.current_reading),
                        "hot_previous_reading": _plain(row.hot_previous_reading),
                        "hot_current_reading": _plain(row.hot_current_reading)}
    category_name = expense.category.name
    if original and original["category_id"] == expense.category_id:
        category_name = original["category"]
    entry = {
        "id": expense.pk, "category_id": expense.category_id, "category": category_name,
        "description": expense.description, "supplier": expense.supplier, "amount_cents": expense.amount_cents,
        "due_on": _plain(expense.due_on), "service_on": _plain(expense.service_on),
        "consumed_from": _plain(expense.consumed_from),
        "consumed_to": _plain(expense.consumed_to), "rule": expense.rule,
        "rule_label": expense.get_rule_display(), "override_reason": expense.override_reason,
        "participants": sorted(shares), "shares": shares, "allocations": {}, "error": "",
        "baseline": expense.baseline, "fixed_cents": expense.fixed_cents,
        "invoice_volume": _plain(expense.invoice_volume), "measured_volume": None, "variance_volume": None,
        "variance_action": expense.variance_action, "variance_reason": expense.variance_reason,
        "attachment": expense.attachment.name or "",
    }
    metered = expense.rule in (Rule.MEASURED, Rule.MIXED)
    if expense.baseline.get("dual_water") and expense.invoice_volume and expense.invoice_volume > 0:
        price = Decimal(expense.amount_cents) / (100 * expense.invoice_volume)
        entry["water_price_per_m3"] = format(price, ".4f").replace(".", ",")
        entry["water_calculations"] = {}
    if expense.category.gas_tariff and expense.rule == Rule.MEASURED:
        entry["is_gas"] = True
    allocation_shares = shares
    try:
        if metered and expense.baseline.get("dual_water"):
            allocation_shares = {}
            for code, share in shares.items():
                cold = meter_usage(share, "", code)
                hot = meter_usage(share, "hot_", code)
                allocation_shares[code] = {"previous_reading": "0", "current_reading": str(cold + hot)}
                if "water_calculations" in entry:
                    consumption = cold + hot
                    quota = Fraction(consumption) * expense.amount_cents / Fraction(expense.invoice_volume)
                    entry["water_calculations"][code] = {
                        "cold": str(cold), "hot": str(hot), "consumption": str(consumption),
                        "cents": int(quota + Fraction(1, 2)),
                    }
        if metered and allocation_shares and all(
            share["previous_reading"] is not None and share["current_reading"] is not None
            for share in allocation_shares.values()
        ):
            usage = sum(Decimal(share["current_reading"]) - Decimal(share["previous_reading"])
                        for share in allocation_shares.values())
            entry["measured_volume"] = str(usage)
            if entry.get("is_gas") and usage > 0:
                try:
                    entry["gas_estimate"] = allocate(
                        expense.amount_cents, expense.rule, allocation_shares, invoice_volume=usage,
                    )
                except ValidationError:
                    pass

            if expense.invoice_volume is not None:
                entry["variance_volume"] = str(expense.invoice_volume - usage)
        if metered and not expense.category.allows_metering:
            raise ValidationError("A divisão por medição em m³ está disponível para água e gás.")
        if expense.consumed_from and expense.consumed_to and expense.consumed_from > expense.consumed_to:
            raise ValidationError("O período de consumo é inválido.")
        if metered and not (expense.consumed_from and expense.consumed_to):
            raise ValidationError("Informe o período de consumo correspondente às leituras e à fatura.")
        if not expense.override_reason.strip() and not _matches_default(expense, rows):
            raise ValidationError("Aprove e justifique a regra ou a participação específica deste lançamento.")
        entry["allocations"] = allocate(expense.amount_cents, expense.rule, allocation_shares,
                                        fixed_cents=expense.fixed_cents, invoice_volume=expense.invoice_volume,
                                        variance_action=expense.variance_action, variance_reason=expense.variance_reason)
    except ValidationError as exc:
        entry["error"] = " ".join(exc.messages)
    return entry


def _draft(period):
    base = {}
    if period.current_version:
        base = period.closings.get(version=period.current_version).snapshot
    unit_list = base.get("units") or list(Unit.objects.values("id", "code", "display_name", "responsible", "occupied"))
    unit_list = [{**unit, "display_name": unit.get("display_name") or unit["code"]} for unit in unit_list]
    units = {item["id"]: item for item in unit_list}
    previous_expenses = {item["id"]: item for item in base.get("expenses", [])}
    totals = {unit["code"]: 0 for unit in unit_list}
    entries, errors = [], []
    expenses = period.expenses.select_related("category").prefetch_related("shares__unit")
    for expense in expenses:
        row = _expense_row(expense, units, previous_expenses.get(expense.pk))
        entries.append(row)
        if row["error"]:
            errors.append(f"{expense.description} — {row['error']}")
        for code, value in row["allocations"].items():
            totals[code] += value
    if not entries and not (period.state == Period.State.REVISING and period.current_version and period.revision_reason.strip()):
        errors.append("Inclua ao menos uma despesa antes de fechar a competência.")
    return {"month": str(period.month), "due_on": str(period.due_on), "units": unit_list,
            "expenses": entries, "totals": totals, "expense_total": sum(e["amount_cents"] for e in entries),
            "allocated_total": sum(totals.values()), "errors": errors}


def preview_period(period):
    period = Period.objects.get(pk=period.pk)
    if period.state == Period.State.CLOSED:
        return period.closings.get(version=period.current_version).snapshot
    return _draft(period)


def _criterion(entry, code):
    rule = entry["rule"]
    share = entry["shares"][code]
    count = len(entry["participants"])
    if rule == Rule.EQUAL:
        return f"1 de {count} unidades participantes"
    if rule == Rule.PERCENT:
        return f"{_br(share['percentage'])}% do valor da despesa"
    usage = Decimal(share["current_reading"]) - Decimal(share["previous_reading"])
    proportion = f"{_br(usage)} de {_br(entry['measured_volume'])} m³"
    if entry["baseline"].get("dual_water"):
        hot = meter_usage(share, "hot_", code)
        proportion = f"{_br(usage + hot)} de {_br(entry['measured_volume'])} m³ ({_br(usage)} fria + {_br(hot)} quente)"
    if rule == Rule.MIXED:
        fixed = entry["fixed_cents"]
        if fixed == entry["amount_cents"]:
            return f"Parcela integralmente fixa entre {count} unidades"
        return f"R$ {fixed // 100},{fixed % 100:02d} entre {count} unidades e parte variável por {proportion}"
    return proportion


def _statement_items(snapshot, code):
    items = []
    for entry in snapshot["expenses"]:
        if code not in entry["allocations"]:
            continue
        item = {field: entry.get(field) for field in ("id", "category_id", "category", "description", "supplier", "service_on",
                                                   "rule", "rule_label", "amount_cents")}
        item.update(share_cents=entry["allocations"][code], criterion=_criterion(entry, code))
        items.append(item)
    return items


@transaction.atomic
def close_period(period, actor, *, expected_version):
    period = Period.objects.select_for_update().get(pk=period.pk)
    if period.state == Period.State.CLOSED and period.current_version == expected_version + 1:
        return period.closings.get(version=period.current_version)
    if period.state == Period.State.CLOSED or period.current_version != expected_version:
        raise ValidationError("A competência foi alterada. Atualize a prévia antes de fechar.")
    snapshot = _draft(period)
    if snapshot["errors"]:
        raise ValidationError(snapshot["errors"])
    if snapshot["expense_total"] != snapshot["allocated_total"]:
        raise ValidationError("O total das parcelas difere do total lançado.")
    version = period.current_version + 1
    closing = Closing.objects.create(period=period, version=version, snapshot=snapshot,
                                     created_by=actor, reason=period.revision_reason)
    Statement.objects.bulk_create([
        Statement(closing=closing, unit_id=unit["id"], code=unit["code"], display_name=unit["display_name"],
                  responsible=unit["responsible"],
                  total_cents=snapshot["totals"][unit["code"]], due_on=period.due_on,
                  items=_statement_items(snapshot, unit["code"]))
        for unit in snapshot["units"]
    ])
    period.state, period.current_version, period.revision_reason = Period.State.CLOSED, version, ""
    period.save(update_fields=["state", "current_version", "revision_reason"])
    log_event(actor, "period.closed", period, f"Versão {version}. {closing.reason}".strip())
    return closing


@transaction.atomic
def begin_revision(period, actor, reason, *, expected_version):
    period = Period.objects.select_for_update().get(pk=period.pk)
    if not reason.strip():
        raise ValidationError("Informe o motivo da retificação.")
    if period.state != Period.State.CLOSED or period.current_version != expected_version:
        raise ValidationError("Atualize a página e confira a versão vigente da competência.")
    snapshot = period.closings.get(version=expected_version).snapshot
    # Restore editable source rows from the published snapshot. Read-only statements keep their version.
    for row in snapshot["expenses"]:
        fields = {k: row[k] for k in ("category_id", "description", "supplier", "amount_cents", "due_on",
                                       "consumed_from", "consumed_to", "rule", "override_reason", "baseline",
                                       "fixed_cents", "invoice_volume", "variance_action", "variance_reason",
                                       "attachment")}
        fields["service_on"] = row.get("service_on")
        expense, _ = Expense.objects.update_or_create(pk=row["id"], period=period, defaults=fields)
        expense.shares.all().delete()
        ExpenseShare.objects.bulk_create([ExpenseShare(expense=expense, **share) for share in row["shares"].values()])
    period.due_on = snapshot["due_on"]
    period.state, period.revision_reason = Period.State.REVISING, reason.strip()
    period.save(update_fields=["state", "revision_reason", "due_on"])
    log_event(actor, "period.revision", period, period.revision_reason)
    period.refresh_from_db()
    return period


def balances(period):
    period = Period.objects.get(pk=period.pk)
    statements = Statement.objects.filter(closing__period=period, closing__version=period.current_version)
    receipts = dict(period.payments.values("unit_id").annotate(net=Sum("amount_cents"))
                    .values_list("unit_id", "net"))
    result = []
    today = timezone.localdate()
    for statement in statements.select_related("unit"):
        received = receipts.get(statement.unit_id, 0)
        balance = statement.total_cents - received
        result.append({"unit": statement.unit, "code": statement.code, "statement": statement,
                       "total_cents": statement.total_cents, "received_cents": received,
                       "balance_cents": balance, "outstanding_cents": max(0, balance),
                       "credit_cents": max(0, -balance), "overdue": balance > 0 and today > statement.due_on,
                       "paid_partial": received > 0 and balance > 0})
    return result


@transaction.atomic
def record_payment(period, unit, amount_cents, paid_on, actor, note="", *, token=None):
    period = Period.objects.select_for_update().get(pk=period.pk)
    if type(amount_cents) is not int or amount_cents <= 0:
        raise ValidationError("Informe um valor positivo para o pagamento.")
    if not paid_on:
        raise ValidationError("Informe a data do pagamento.")
    if token is not None:
        prior = Payment.objects.filter(token=token).first()
        if prior is not None:
            if (prior.period_id, prior.unit_id, prior.amount_cents, prior.paid_on, prior.note, prior.created_by_id) != (
                period.pk, unit.pk, amount_cents, paid_on, note, actor.pk
            ):
                raise ValidationError("O identificador de envio já foi usado para outro pagamento.")
            return prior
    statement = Statement.objects.filter(closing__period=period, closing__version=period.current_version,
                                         unit=unit).first()
    if statement is None:
        raise ValidationError("Feche a competência antes de registrar um pagamento.")
    received = period.payments.filter(unit=unit).aggregate(net=Sum("amount_cents"))["net"] or 0
    if amount_cents > statement.total_cents - received:
        raise ValidationError("O valor informado supera o saldo a receber da unidade.")
    token_fields = {"token": token} if token is not None else {}
    payment = Payment.objects.create(period=period, unit=unit, amount_cents=amount_cents, paid_on=paid_on,
                                     created_by=actor, note=note, **token_fields)
    log_event(actor, "payment.recorded", payment, note)
    return payment


@transaction.atomic
def reverse_payment(payment, actor, reason):
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if not reason.strip():
        raise ValidationError("Informe o motivo do estorno.")
    if payment.reversal_of_id is not None:
        raise ValidationError("Este movimento já é um estorno.")
    prior = Payment.objects.filter(reversal_of=payment).first()
    if prior:
        return prior
    reversal = Payment.objects.create(period_id=payment.period_id, unit_id=payment.unit_id,
                                      amount_cents=-payment.amount_cents, paid_on=timezone.localdate(),
                                      note=reason.strip(), created_by=actor, reversal_of=payment)
    log_event(actor, "payment.reversed", reversal, reason.strip())
    return reversal
