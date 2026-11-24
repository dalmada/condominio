import hashlib
import json
import uuid
from decimal import Decimal
from pathlib import Path

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse as route_url
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .access import admin_required
from .entry_forms import BRMoneyField
from .models import Category, Closing, Payment, Period, Statement, Unit
from .services import balances, begin_revision, close_period, preview_period, record_payment, reverse_payment


def preview_key(preview):
    data = json.dumps(preview, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode()).hexdigest()


def category_style(name):
    name = name.casefold()
    if "água" in name:
        return "water", "blue"
    if "energia" in name:
        return "bolt", "amber"
    if "gás" in name:
        return "gas", "clay"
    if "faxina" in name:
        return "clean", "violet"
    return "layers", "sage"


EXPENSE_COLORS = ("#17604f", "#438397", "#c78839", "#a55c54", "#765d91", "#758447")


def expense_chart(rows):
    totals = {}
    for row in rows:
        key = row["category_id"]
        if key not in totals:
            totals[key] = {"name": row["category"], "amount": 0}
        totals[key]["amount"] += row["amount_cents"]

    categories = sorted(totals.values(), key=lambda category: -category["amount"])
    total = sum(category["amount"] for category in categories)
    stops = []
    end = 0
    for index, category in enumerate(categories):
        start = end
        end += category["amount"]
        color = EXPENSE_COLORS[index % len(EXPENSE_COLORS)]
        category["color"] = color
        stops.append(f"{color} {100 * start / total:.6f}% {100 * end / total:.6f}%")
    return categories, ", ".join(stops)


def overview(request, period, unit_id=None):
    context = {"period": period, "periods": Period.objects.order_by("-month")}
    if period is None:
        context.update(units=Unit.objects.order_by("code"), categories=Category.objects.order_by("pk"))
        return render(request, "ledger/overview.html", context)

    closing = period.closings.filter(version=period.current_version).first()
    preview = closing.snapshot if period.state == "closed" and closing else preview_period(period)
    rows = []
    for expense in preview["expenses"]:
        icon_name, color = category_style(expense["category"])
        rows.append({**expense, "icon": icon_name, "color": color})
    charges = balances(period) if closing else []
    by_code = {row["code"]: row for row in charges}
    published = {s.code: s for s in closing.statements.all()} if closing else {}
    units = []
    for unit in preview["units"]:
        code = unit["code"]
        draft = preview["totals"].get(code, 0)
        previous = published.get(code)
        items = []
        estimated = 0
        incomplete = False
        provisional = False
        for row in rows:
            if code not in row["participants"]:
                continue
            amount = row["allocations"].get(code)
            water = row.get("water_calculations", {}).get(code)
            gas_estimate = row.get("gas_estimate", {}).get(code)
            if amount is None and row["error"]:
                amount = water["cents"] if water else gas_estimate
                if amount is not None:
                    estimated += amount
                    provisional = True
                else:
                    incomplete = True
            gas_volume = None
            if row.get("is_gas") or row["icon"] == "gas":
                share = row["shares"][code]
                if share["current_reading"] is not None and share["previous_reading"] is not None:
                    gas_volume = Decimal(share["current_reading"]) - Decimal(share["previous_reading"])
            items.append({"category": row["category"], "rule": row["rule_label"],
                          "amount": amount, "gas_volume": gas_volume, "pending": bool(row["error"])})
        units.append({
            **unit, "draft": draft, "statement": previous, "charge": by_code.get(code),
            "items": items, "display_total": draft + estimated,
            "incomplete": incomplete, "provisional": provisional,
            "difference": draft - (previous.total_cents if previous else 0),
        })
    if unit_id is not None:
        unit = next((unit for unit in units if unit["id"] == unit_id), None)
        if unit is None:
            raise Http404
        return render(request, "ledger/unit_breakdown.html", {"period": period, "unit": unit})
    categories, gradient = expense_chart(rows)
    overview_data = {
        "units": [
            {"code": unit["code"], "name": unit["display_name"], "cents": unit["display_total"],
             "href": route_url("unit_breakdown", args=[period.pk, unit["id"]]),
             "status": "Parcial" if unit["incomplete"] else "Provisório" if unit["provisional"] else ""}
            for unit in units
        ],
        "categories": categories, "gradient": gradient,
    }
    context.update(
        overview_data=overview_data,
        expense_categories=categories, expense_gradient=gradient, preview=preview, rows=rows, units=units, closing=closing, preview_key=preview_key(preview),
        received=sum(row["received_cents"] for row in charges),
        outstanding=sum(row["outstanding_cents"] for row in charges),
        credits=sum(row["credit_cents"] for row in charges),
    )
    return render(request, "ledger/overview.html", context)


@admin_required
def dashboard(request):
    return overview(request, Period.objects.order_by("-month").first())


@admin_required
def period_detail(request, pk):
    return overview(request, get_object_or_404(Period, pk=pk))


@admin_required
def unit_breakdown(request, period_id, unit_id):
    return overview(request, get_object_or_404(Period, pk=period_id), unit_id)


def flash_error(request, error):
    messages.error(request, " ".join(error.messages))


def expected_version(request):
    try:
        return int(request.POST["version"])
    except (KeyError, ValueError):
        raise ValidationError("Atualize a página para conferir a versão da competência.")


@admin_required
@require_POST
def close(request, pk):
    try:
        with transaction.atomic():
            period = get_object_or_404(Period, pk=pk)
            version = expected_version(request)
            if period.state != "closed" and request.POST.get("preview_key") != preview_key(preview_period(period)):
                raise ValidationError("A prévia mudou. Confira os valores atualizados antes de fechar.")
            closed = close_period(period, request.user, expected_version=version)
        messages.success(request, f"Competência fechada. Demonstrativos disponíveis na versão {closed.version}.")
    except ValidationError as error:
        flash_error(request, error)
    return redirect("period_detail", pk=pk)


@admin_required
@require_POST
def revise(request, pk):
    period = get_object_or_404(Period, pk=pk)
    try:
        begin_revision(period, request.user, request.POST.get("reason", "").strip(),
                       expected_version=expected_version(request))
        messages.success(request, "Retificação aberta. O demonstrativo publicado continua disponível.")
    except ValidationError as error:
        flash_error(request, error)
    return redirect("period_detail", pk=pk)


@admin_required
def history(request):
    periods = Period.objects.order_by("-month").prefetch_related("closings__statements")
    categories = Category.objects.order_by("name")
    units = Unit.objects.order_by("code")
    code = request.GET.get("unidade", "")
    unit_id = units.filter(code=code).values_list("pk", flat=True).first() if code else None
    category = request.GET.get("categoria", "")
    month = request.GET.get("mes", "")
    records = []

    def snapshot_code(data):
        return next((unit["code"] for unit in data["units"] if unit["id"] == unit_id), code)

    def matching_items(data):
        historical_code = snapshot_code(data)
        return [
            item for item in data["expenses"]
            if (not category or str(item["category_id"]) == category)
            and (not code or historical_code in item["participants"])
        ]

    def amount(items, data):
        historical_code = snapshot_code(data)
        return sum(item["allocations"].get(historical_code, 0) if code else item["amount_cents"] for item in items)

    for period in periods:
        if month and period.month.strftime("%Y-%m") != month:
            continue
        versions = sorted(period.closings.all(), key=lambda c: c.version, reverse=True)
        snapshot = versions[0].snapshot if versions else preview_period(period)
        expenses = matching_items(snapshot)
        history_rows = []
        for closing in versions:
            items = matching_items(closing.snapshot)
            if not items and (category or code):
                continue
            history_rows.append({
                "version": closing, "amount": amount(items, closing.snapshot), "expenses": items,
                "statements": [s for s in closing.statements.all() if not code or s.unit_id == unit_id],
            })
        if not expenses and not history_rows and (category or code):
            continue
        records.append({
            "period": period, "total": amount(expenses, snapshot), "count": len(expenses), "versions": history_rows,
        })
    return render(request, "ledger/history.html", {
        "records": records, "units": units, "categories": categories, "month": month,
        "unit_filter": code, "category_filter": category, "filtered": bool(category or code or month),
    })


@admin_required
def statement(request, pk):
    doc = get_object_or_404(Statement.objects.select_related("closing__period"), pk=pk)
    return render(request, "ledger/statement.html", {"doc": doc})


@admin_required
def archived_attachment(request, pk, expense_id):
    closing = get_object_or_404(Closing, pk=pk)
    item = next((entry for entry in closing.snapshot["expenses"] if entry["id"] == expense_id), None)
    if not item or not item.get("attachment"):
        raise Http404
    try:
        stream = default_storage.open(item["attachment"], "rb")
    except FileNotFoundError as error:
        raise Http404 from error
    suffix = Path(item["attachment"]).suffix
    filename = f"despesa-{expense_id}-v{closing.version}{suffix}"
    return FileResponse(stream, as_attachment=True, filename=filename)


class PaymentForm(forms.Form):
    amount = BRMoneyField(label="Valor recebido (R$)", min_value=Decimal("0.01"), max_digits=14,
                                decimal_places=2)
    paid_on = forms.DateField(label="Data do pagamento", input_formats=["%Y-%m-%d"],
                              widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))
    note = forms.CharField(label="Observação (opcional)", required=False, max_length=500)
    token = forms.UUIDField(widget=forms.HiddenInput)


@admin_required
@require_http_methods(["GET", "POST"])
def charge(request, period_id, unit_id):
    period = get_object_or_404(Period, pk=period_id)
    unit = get_object_or_404(Unit, pk=unit_id)
    doc = get_object_or_404(Statement, closing__period=period, closing__version=period.current_version, unit=unit)
    balance = next(row for row in balances(period) if row["unit"].pk == unit.pk)
    form = PaymentForm(request.POST if request.method == "POST" else None, initial={
        "paid_on": timezone.localdate(), "token": uuid.uuid4(),
    })
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            record_payment(period, unit, int(data["amount"] * 100), data["paid_on"], request.user,
                           data["note"], token=data["token"])
            messages.success(request, "Pagamento registrado.")
            return redirect("charge", period_id=period.pk, unit_id=unit.pk)
        except ValidationError as error:
            form.add_error(None, error)
    movements = period.payments.filter(unit=unit).select_related("reversal").order_by("-created_at", "-pk")
    return render(request, "ledger/charge.html", {
        "period": period, "unit": unit, "doc": doc, "balance": balance, "form": form, "movements": movements,
    })


@admin_required
@require_POST
def reverse(request, pk):
    movement = get_object_or_404(Payment, pk=pk)
    try:
        reverse_payment(movement, request.user, request.POST.get("reason", "").strip())
        messages.success(request, "Pagamento estornado. O movimento original foi preservado.")
    except ValidationError as error:
        flash_error(request, error)
    return redirect("charge", period_id=movement.period_id, unit_id=movement.unit_id)
