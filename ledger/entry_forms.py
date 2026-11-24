import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from unicodedata import normalize

from django import forms
from django.core.exceptions import ValidationError

from .models import Category, Period, Rule, Unit


GAS_PRICE_CENTS = 2200


class BRDecimalField(forms.DecimalField):
    """Accept a monetary or measured value typed with a decimal comma on mobile."""
    widget = forms.TextInput

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.widget.attrs["inputmode"] = "decimal"

    def to_python(self, value):
        if isinstance(value, str):
            value = value.strip()
            if "," in value:
                integer, _, fraction = value.partition(",")
                if "." in integer and _grouped(integer):
                    integer = integer.replace(".", "")
                value = integer + "." + fraction
        return super().to_python(value)


class BRMoneyField(BRDecimalField):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.widget.attrs.update({"data-money": "", "inputmode": "numeric", "placeholder": "R$ 0,00"})

    def prepare_value(self, value):
        if isinstance(value, Decimal):
            formatted = format(value, ",.2f").translate(str.maketrans(",.", ".,"))
            return f"R$ {formatted}"
        return super().prepare_value(value)

    def to_python(self, value):
        if isinstance(value, str):
            value = value.strip().removeprefix("R$").strip()
        return super().to_python(value)


def _grouped(integer):
    groups = integer.split(".")
    return 1 <= len(groups[0]) <= 3 and all(len(item) == 3 and item.isdigit() for item in groups[1:])


def date_field(label, required=True):
    return forms.DateField(label=label, required=required, input_formats=("%Y-%m-%d", "%d/%m/%Y"), widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))


def rule_choices(category):
    if category and category.allows_metering:
        return [(Rule.MEASURED, Rule.MEASURED.label)]
    return [(value, label) for value, label in Rule.choices if value in (Rule.EQUAL, Rule.PERCENT)]


class UnitForm(forms.ModelForm):
    occupied = forms.TypedChoiceField(label="Situação de ocupação", coerce=lambda value: value == "True",
                                      choices=(("True", "Ocupado"), ("False", "Vazio")))

    class Meta:
        model = Unit
        fields = ("code", "display_name", "responsible", "occupied")
        labels = {"code": "Identificador da unidade", "display_name": "Nome da unidade (opcional)",
                  "responsible": "Responsável (opcional)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].disabled = True


class PeriodForm(forms.ModelForm):
    month = forms.DateField(label="Competência", input_formats=("%Y-%m",), widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}))
    due_on = date_field("Vencimento da cobrança dos apartamentos")

    class Meta:
        model = Period
        fields = ("month", "due_on")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.current_version:
            self.fields["month"].disabled = True

    def validate_unique(self):
        super().validate_unique()
        month = self.cleaned_data.get("month")
        if month and Period.objects.filter(month=month).exclude(pk=self.instance.pk).exists():
            self.add_error("month", "Esta competência já existe.")


class ShareFields:
    def add_unit_fields(self, units, selected, readings=None):
        self.units = list(units)
        self.unit_rows = []
        for unit in self.units:
            suffix = str(unit.pk)
            enabled, percentage = "participates_" + suffix, "percentage_" + suffix
            self.fields[enabled] = forms.BooleanField(required=False, initial=unit.code in selected, label="Participa")
            self.fields[percentage] = BRDecimalField(required=False, label="Percentual (%)", decimal_places=6, max_digits=9, min_value=0, max_value=100, initial=selected.get(unit.code))
            row = {"unit": unit, "enabled": self[enabled], "percentage": self[percentage]}
            if readings is not None:
                previous, current, hot_previous, hot_current = readings.get(unit.code, (None,) * 4)
                for key, label, raw in (("previous", "Leitura anterior (m³)", previous), ("current", "Leitura atual (m³)", current),
                                         ("hot_previous", "Água quente · anterior (m³)", hot_previous),
                                         ("hot_current", "Água quente · atual (m³)", hot_current)):
                    name = key + "_" + suffix
                    self.fields[name] = BRDecimalField(required=False, label=label, initial=raw, min_value=0, decimal_places=4, max_digits=16)
                    row[key] = self[name]
            if self.is_bound:
                enabled_now = self[enabled].value()
                active_rule = self.data.get("rule")
                self.fields[percentage].disabled = not enabled_now or active_rule != Rule.PERCENT
                if readings is not None:
                    for key in ("previous", "current", "hot_previous", "hot_current"):
                        self.fields[f"{key}_{unit.pk}"].disabled = not enabled_now or active_rule not in (Rule.MIXED, Rule.MEASURED)
            self.unit_rows.append(row)

    def selected_shares(self, data):
        result = {}
        for unit in self.units:
            if data.get(f"participates_{unit.pk}"):
                result[unit.code] = data.get(f"percentage_{unit.pk}") if data.get("rule") == Rule.PERCENT else None
        return result


class CategoryForm(ShareFields, forms.Form):
    name = forms.CharField(label="Nome da categoria", max_length=80)
    supplier = forms.CharField(label="Fornecedor ou prestador padrão", required=False, max_length=160)
    rule = forms.ChoiceField(label="Regra padrão", choices=[("", "Sem padrão aprovado"), *Rule.choices], required=False)

    def __init__(self, *args, category, units, **kwargs):
        super().__init__(*args, initial={"name": category.name, "supplier": category.supplier, "rule": category.default_rule}, **kwargs)
        self.category = category
        self.fields["rule"].choices = [("", "Sem padrão aprovado"), *rule_choices(category)]
        if category.allows_metering:
            self.fields["rule"].choices = rule_choices(category)
            self.fields["rule"].disabled = True
            self.initial["rule"] = Rule.MEASURED
        self.add_unit_fields(units, category.default_shares or {})

    def clean_name(self):
        name = normalize("NFC", self.cleaned_data["name"])
        others = Category.objects.exclude(pk=self.category.pk).values_list("name", flat=True)
        if any(normalize("NFC", other).casefold() == name.casefold() for other in others):
            raise ValidationError("Já existe uma categoria com esse nome.")
        return name

    def clean(self):
        data = super().clean()
        shares = self.selected_shares(data)
        rule = data.get("rule")
        if rule and not shares:
            raise ValidationError("Selecione pelo menos uma unidade participante.")
        if rule == Rule.PERCENT and (None in shares.values() or sum(shares.values()) != 100):
            raise ValidationError("Preencha os percentuais das unidades participantes. A soma precisa ser 100%.")
        data["shares"] = {code: str(value) if value is not None else None for code, value in shares.items()} if rule else {}
        return data


class ExpenseForm(ShareFields, forms.Form):
    category = forms.ModelChoiceField(label="Categoria", queryset=Category.objects.none(), empty_label="Escolha uma categoria")
    supplier = forms.CharField(label="Fornecedor ou prestador", max_length=160, required=False)
    amount = BRMoneyField(label="Valor da despesa (R$)", required=False, decimal_places=2, max_digits=11, min_value=Decimal("0.01"))
    due_on = date_field("Vencimento da conta do fornecedor")
    consumed_from = date_field("Início do período de consumo", required=False)
    consumed_to = date_field("Fim do período de consumo", required=False)
    rule = forms.ChoiceField(label="Critério de divisão", choices=[("", "Escolha o critério"), *Rule.choices])
    fixed_amount = BRMoneyField(label="Parcela fixa ou comum (R$)", decimal_places=2, max_digits=11, min_value=0, required=False)
    invoice_volume = BRDecimalField(label="Volume faturado (m³)", decimal_places=4, max_digits=16, min_value=0, required=False)
    attachment = forms.FileField(label="Foto ou PDF (opcional)", required=False, help_text="PDF, JPEG ou PNG de até 10 MB.",
                                 widget=forms.FileInput(attrs={"accept": ".pdf,.png,.jpg,.jpeg"}))
    remove_attachment = forms.BooleanField(label="Remover o anexo atual", required=False)

    def __init__(self, *args, units, categories, expense=None, preset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.expense = expense
        self.fields["category"].queryset = categories
        self.fields["category"].label_from_instance = lambda obj: obj.name
        selected_category = expense.category if expense else preset
        if self.is_bound:
            selected_category = next((category for category in categories if str(category.pk) == self.data.get("category")), None)
        self.fields["rule"].choices = [("", "Escolha o critério"), *rule_choices(selected_category)]
        if expense is None:
            shares, readings = {}, {}
            if preset:
                self.initial.update(category=preset.pk, supplier=preset.supplier, rule=preset.default_rule)
                shares = preset.default_shares or {}
        else:
            for field in ("supplier", "due_on", "consumed_from", "consumed_to", "rule",
                          "invoice_volume"):
                self.initial[field] = getattr(expense, field)
            self.initial.update(category=expense.category_id, amount=Decimal(expense.amount_cents) / 100,
                                fixed_amount=Decimal(expense.fixed_cents) / 100)
            entries = list(expense.shares.select_related("unit"))
            shares = {row.unit.code: row.percentage for row in entries}
            readings = {row.unit.code: (row.previous_reading, row.current_reading, row.hot_previous_reading, row.hot_current_reading) for row in entries}
        if selected_category and selected_category.allows_metering:
            self.fields["rule"].choices = rule_choices(selected_category)
            self.initial["rule"] = Rule.MEASURED
        self.add_unit_fields(units, shares, readings)
        if selected_category and selected_category.allows_metering:
            places = 3 if selected_category.gas_tariff else 2
            for row in self.unit_rows:
                for key in ("previous", "current", "hot_previous", "hot_current"):
                    whole = 5 if selected_category.gas_tariff or key.startswith("hot_") else 4
                    name = row[key].name
                    value = self.fields[name].initial
                    if value is not None and value == round(value, places):
                        self.fields[name].initial = f"{value:0{whole + 1 + places}.{places}f}".replace(".", ",")
        if self.is_bound:
            active_rule = self.data.get("rule")
            self.fields["fixed_amount"].disabled = active_rule != Rule.MIXED
            self.fields["invoice_volume"].disabled = active_rule not in (Rule.MIXED, Rule.MEASURED)

    def clean_attachment(self):
        upload = self.cleaned_data.get("attachment")
        if not upload:
            return upload
        if upload.size > 10 * 1024 * 1024:
            raise ValidationError("Escolha um arquivo de até 10 MB.")
        signatures = {".pdf": b"%PDF-", ".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff"}
        signature = signatures.get(Path(upload.name).suffix.lower())
        prefix = upload.read(8)
        upload.seek(0)
        if not signature or not prefix.startswith(signature):
            raise ValidationError("O arquivo precisa ser um PDF, JPEG ou PNG válido.")
        return upload

    def clean(self):
        data = super().clean()
        start, end = data.get("consumed_from"), data.get("consumed_to")
        if start and end and start > end:
            self.add_error("consumed_to", "O fim do período deve ser igual ou posterior ao início.")
        if data.get("attachment") and data.get("remove_attachment"):
            self.add_error("remove_attachment", "Escolha entre substituir e remover o anexo.")
        shares = self.selected_shares(data)
        if not shares:
            self.add_error(None, "Selecione pelo menos uma unidade participante.")
        rule, category = data.get("rule"), data.get("category")
        if rule == Rule.MIXED and data.get("fixed_amount") is None:
            self.add_error("fixed_amount", "Informe a parcela fixa da fatura.")
        tariff_applies = category and category.gas_tariff and rule in (Rule.MEASURED, Rule.MIXED)
        if category and category.allows_metering and not category.gas_tariff and rule in (Rule.MEASURED, Rule.MIXED):
            for unit in self.units:
                if unit.code not in shares:
                    continue
                for key in ("previous", "current", "hot_previous", "hot_current"):
                    name = f"{key}_{unit.pk}"
                    raw = self.data.get(name, "")
                    whole = 5 if key.startswith("hot_") else 4
                    if raw and not re.fullmatch(rf"[0-9]{{{whole}}},[0-9]{{2}}", raw):
                        example = f"{14:0{whole}d},77"
                        self.add_error(name, f"Use {whole} dígitos, vírgula e dois decimais. Ex.: {example}.")
        if tariff_applies:
            consumption = Decimal(0)
            for unit in self.units:
                if unit.code not in shares:
                    continue
                for key in ("previous", "current"):
                    name = f"{key}_{unit.pk}"
                    if not re.fullmatch(r"[0-9]{5},[0-9]{3}", self.data.get(name, "")):
                        self.add_error(name, "Use cinco dígitos, vírgula e três decimais. Ex.: 00001,764.")
                previous = data.get(f"previous_{unit.pk}")
                current = data.get(f"current_{unit.pk}")
                if previous is None or current is None or current < previous:
                    self.add_error(f"current_{unit.pk}", "Informe as duas leituras. A atual deve ser igual ou superior à anterior.")
                else:
                    consumption += current - previous
            fixed = (data.get("fixed_amount") or Decimal(0)) if rule == Rule.MIXED else Decimal(0)
            total = (consumption * GAS_PRICE_CENTS / 100 + fixed).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if total <= 0:
                self.add_error(None, "O valor calculado para o gás deve ser positivo.")
            data["amount"] = total
            if consumption > 0 and total == fixed:
                self.add_error(None, "O consumo informado gera menos de um centavo de cobrança.")
        elif not data.get("amount") and "amount" not in self.errors:
            self.add_error("amount", "Informe o valor da despesa.")
        if rule and category:
            baseline = self.expense.baseline if self.expense and category.pk == self.expense.category_id else {}
            default_rule = baseline.get("rule", category.default_rule)
            if "shares_by_id" in baseline:
                stored_shares = baseline["shares_by_id"]
                defaults = {unit.code: stored_shares[str(unit.pk)] for unit in self.units if str(unit.pk) in stored_shares}
            else:
                defaults = baseline.get("shares", category.default_shares) or {}
            changed = rule != default_rule or shares.keys() != defaults.keys()
            if not changed and rule == Rule.PERCENT:
                changed = any(defaults[code] is None or weight != Decimal(str(defaults[code])) for code, weight in shares.items())
            if changed:
                same_selection = self.expense and self.expense.category_id == category.pk and self.expense.rule == rule
                original = self.expense.override_reason if same_selection else ""
                data["override_reason"] = original or "Critério ou participação selecionados no lançamento."
            else:
                data["override_reason"] = ""
        return data
