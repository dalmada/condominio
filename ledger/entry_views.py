import hashlib
import json
from pathlib import Path

from django.contrib import messages
from django.core import signing
from django.db import IntegrityError, transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from .access import admin_required
from .entry_forms import GAS_PRICE_CENTS, CategoryForm, ExpenseForm, PeriodForm, UnitForm, rule_choices
from .models import Category, Expense, ExpenseShare, Period, Rule, Unit
from .services import log_event


def _form_page(request, form, heading, section, return_url, save_label=None):
    return render(request, "ledger/entry/form.html", {
        "heading": heading, "section": section, "form": form, "return_url": return_url, "save_label": save_label,
    })


@admin_required
@require_GET
def unit_list(request):
    return render(request, "ledger/entry/units.html", {"units": Unit.objects.order_by("code")})


@admin_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def unit_edit(request, pk):
    unit = get_object_or_404(Unit.objects.select_for_update(), pk=pk)
    form = UnitForm(request.POST if request.method == "POST" else None, instance=unit)
    if request.method == "POST" and form.is_valid():
        unit = form.save()
        log_event(request.user, "unit.updated", unit, f"Unidade {unit.code}; ocupação: {unit.occupied}.")
        messages.success(request, "Cadastro da unidade atualizado.")
        return redirect("unit_list")
    return _form_page(request, form, f"Editar unidade {unit.code}", "Cadastros", reverse("unit_list"))


@admin_required
@require_GET
def category_list(request):
    return render(request, "ledger/entry/categories.html", {"categories": Category.objects.order_by("pk")})


@admin_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def category_edit(request, pk=None):
    category = get_object_or_404(Category.objects.select_for_update(), pk=pk) if pk else Category()
    form = CategoryForm(request.POST if request.method == "POST" else None,
                        category=category, units=Unit.objects.order_by("code"))
    if request.method == "POST" and form.is_valid():
        old_name = category.name
        category.name = form.cleaned_data["name"]
        category.supplier = form.cleaned_data["supplier"]
        category.default_rule = form.cleaned_data["rule"]
        category.default_shares = form.cleaned_data["shares"]
        category.save()
        detail = f"Nome: {category.name}; regra: {category.default_rule or 'pendente'}; participação: {category.default_shares}."
        if pk and old_name != category.name:
            detail = f"Nome anterior: {old_name}. {detail}"
        log_event(request.user, "category.updated" if pk else "category.created", category, detail)
        messages.success(request, "Categoria atualizada." if pk else "Categoria criada.")
        return redirect("category_list")
    return render(request, "ledger/entry/category.html", {"category": category, "form": form})


@admin_required
@require_http_methods(["GET", "POST"])
def period_create(request):
    today = timezone.localdate()
    form = PeriodForm(request.POST if request.method == "POST" else None, initial={"month": today.replace(day=1)})
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                period = form.save()
                log_event(request.user, "period.created", period)
        except IntegrityError:
            if not Period.objects.filter(month=form.cleaned_data["month"]).exists():
                raise
            form.add_error("month", "Esta competência já existe.")
        else:
            messages.success(request, "Competência aberta.")
            return redirect("period_detail", pk=period.pk)
    return _form_page(request, form, "Abrir competência", "Competências", reverse("dashboard"), "Abrir competência")


def _closed_redirect(request, period):
    messages.error(request, "Competência fechada. Inicie uma revisão para editar os lançamentos.")
    return redirect("period_detail", pk=period.pk)


@admin_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def period_edit(request, period_id):
    period = get_object_or_404(Period.objects.select_for_update(), pk=period_id)
    if period.state == "closed":
        return _closed_redirect(request, period)
    form = PeriodForm(request.POST if request.method == "POST" else None, instance=period)
    if request.method == "POST" and form.is_valid():
        period = form.save()
        log_event(request.user, "period.updated", period, f"Competência {period.month:%m/%Y}; vencimento {period.due_on:%d/%m/%Y}.")
        messages.success(request, "Competência atualizada.")
        return redirect("period_detail", pk=period.pk)
    return _form_page(request, form, "Editar competência", "Competências", reverse("period_detail", args=[period.pk]))


def _save_expense(expense, form):
    data = form.cleaned_data
    if not expense.description or expense.category_id != data["category"].pk:
        expense.description = data["category"].name
    for field in ("category", "supplier", "due_on", "consumed_from", "consumed_to", "rule", "override_reason"):
        setattr(expense, field, data[field])
    expense.amount_cents = int(data["amount"] * 100)
    expense.fixed_cents = int((data["fixed_amount"] or 0) * 100) if expense.rule == Rule.MIXED else 0
    metered = expense.rule in (Rule.MEASURED, Rule.MIXED)
    expense.invoice_volume = data["invoice_volume"] if metered else None
    expense.variance_action = ""
    expense.variance_reason = ""
    if data["remove_attachment"]:
        expense.attachment = ""
    elif data["attachment"]:
        expense.attachment = data["attachment"]
    expense.save()
    water = metered and expense.category.allows_metering and not expense.category.gas_tariff
    if water != expense.baseline.get("dual_water", False):
        expense.baseline["dual_water"] = water
        expense.save(update_fields=["baseline"])
    expense.shares.all().delete()
    rows = []
    for unit in form.units:
        if data.get(f"participates_{unit.pk}"):
            rows.append(ExpenseShare(
                expense=expense, unit=unit,
                percentage=data.get(f"percentage_{unit.pk}") if expense.rule == Rule.PERCENT else None,
                previous_reading=data.get(f"previous_{unit.pk}") if metered else None,
                current_reading=data.get(f"current_{unit.pk}") if metered else None,
                hot_previous_reading=data.get(f"hot_previous_{unit.pk}") if water else None,
                hot_current_reading=data.get(f"hot_current_{unit.pk}") if water else None,
            ))
    ExpenseShare.objects.bulk_create(rows)


def _duplicate_review(request, period, expense, form):
    """Require confirmation for the same submitted data and observed matches."""
    if not form.is_bound:
        return {}
    salt = f"ledger.duplicate:{request.user.pk}:{period.pk}:{expense.pk if expense else 'new'}"
    token = request.POST.get("duplicate_token", "")
    try:
        previous = signing.loads(token, salt=salt) if token else {}
    except signing.BadSignature:
        previous = {}
    valid = form.is_valid()
    data = form.cleaned_data
    upload = data.get("attachment")
    if previous.get("filename") and not upload:
        if "attachment" not in form.errors:
            form.add_error("attachment", "Selecione novamente o anexo antes de salvar.")
        return {"duplicate_token": token, "pending_attachment": previous["filename"]}
    if not valid:
        return {"duplicate_token": token, "pending_attachment": previous.get("filename", "")}

    candidates = period.expenses.filter(category=data["category"], amount_cents=int(data["amount"] * 100),
                                        due_on=data["due_on"]).exclude(pk=expense.pk if expense else None)
    supplier = data["supplier"].strip().casefold()
    matches = [other for other in candidates if other.supplier.strip().casefold() == supplier]
    if not matches:
        return {}

    fields = {name: value for name, value in data.items() if name not in ("category", "attachment")}
    fields["category"] = data["category"].pk
    fields["matches"] = [(other.pk, other.description, other.supplier, other.service_on) for other in matches]
    if upload:
        file_hash = hashlib.sha256()
        for chunk in upload.chunks():
            file_hash.update(chunk)
        upload.seek(0)
        fields["attachment"] = file_hash.hexdigest()
    serialized = json.dumps(fields, sort_keys=True, default=str).encode()
    state = {"fingerprint": hashlib.sha256(serialized).hexdigest(), "filename": upload.name if upload else ""}
    if request.POST.get("confirm_duplicate") == "1" and previous == state:
        return {}
    return {"duplicates": matches, "duplicate_token": signing.dumps(state, salt=salt),
            "pending_attachment": state["filename"]}


def _expense_form(request, period, expense):
    if period.state == "closed":
        return _closed_redirect(request, period)
    units = list(Unit.objects.order_by("code"))
    categories = Category.objects.order_by("pk")
    preset = next((category for category in categories if str(category.pk) == request.GET.get("categoria")), None)
    form = ExpenseForm(request.POST if request.method == "POST" else None, request.FILES or None,
                       units=units, categories=categories, expense=expense, preset=preset)
    review = _duplicate_review(request, period, expense, form)
    if request.method == "POST" and form.is_valid() and not review:
        creating = expense is None
        if creating:
            expense = Expense(period=period)
        _save_expense(expense, form)
        log_event(request.user, "expense.created" if creating else "expense.updated", expense,
                  f"{expense.description}; {expense.amount_cents} centavos; regra {expense.rule}. {expense.override_reason}")
        messages.success(request, "Lançamento salvo.")
        return redirect("period_detail", pk=period.pk)
    defaults = {cat.pk: {"supplier": cat.supplier, "rule": cat.default_rule,
                         "gas_tariff": cat.gas_tariff, "water": cat.allows_metering and not cat.gas_tariff, "choices": rule_choices(cat), "shares": cat.default_shares or {}} for cat in categories}
    return render(request, "ledger/entry/expense.html", {"period": period, "expense": expense,
                                                         "form": form, "defaults": defaults, "gas_price_cents": GAS_PRICE_CENTS, **review})


@admin_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def expense_create(request, period_id):
    period = get_object_or_404(Period.objects.select_for_update(), pk=period_id)
    return _expense_form(request, period, None)


@admin_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def expense_edit(request, pk):
    period = get_object_or_404(Period.objects.select_for_update(), expenses__pk=pk)
    return _expense_form(request, period, get_object_or_404(Expense, pk=pk, period=period))


@admin_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def expense_delete(request, pk):
    period = get_object_or_404(Period.objects.select_for_update(), expenses__pk=pk)
    if period.state == "closed":
        return _closed_redirect(request, period)
    expense = get_object_or_404(Expense, pk=pk, period=period)
    if request.method == "POST":
        log_event(request.user, "expense.deleted", expense, f"Competência {period.month:%m/%Y}; {expense.description}; {expense.amount_cents} centavos.")
        expense.delete()
        messages.success(request, "Lançamento removido.")
        return redirect("period_detail", pk=period.pk)
    return render(request, "ledger/entry/delete.html", {"expense": expense})


@admin_required
@require_GET
def expense_attachment(request, pk):
    expense = get_object_or_404(Expense, pk=pk)
    if not expense.attachment:
        raise Http404("Anexo indisponível.")
    try:
        stream = expense.attachment.open("rb")
    except FileNotFoundError as error:
        raise Http404("Anexo indisponível.") from error
    response = FileResponse(stream, as_attachment=True, filename=Path(expense.attachment.name).name)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
