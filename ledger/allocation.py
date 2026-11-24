"""Exact apportionment in cents. Keys are the unit identifiers at closing time."""
from fractions import Fraction

from django.core.exceptions import ValidationError


def _number(value, label):
    if value is None or value == "":
        raise ValidationError(f"Informe {label}.")
    try:
        number = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        raise ValidationError(f"Valor inválido para {label}.") from None
    if number < 0:
        raise ValidationError(f"{label.capitalize()} deve ser maior ou igual a zero.")
    return number


def _reconcile(quotas, amount):
    floors = {unit: quota.numerator // quota.denominator for unit, quota in quotas.items()}
    left = amount - sum(floors.values())
    ranking = sorted(quotas, key=lambda unit: (-(quotas[unit] - floors[unit]), unit))
    for unit in ranking[:left]:
        floors[unit] += 1
    return floors


def allocate(amount_cents, rule, shares, *, fixed_cents=0, invoice_volume=None,
             variance_action="", variance_reason=""):
    """Return {unit code: cents}; shares maps codes to percent and reading fields."""
    if type(amount_cents) is not int or amount_cents <= 0:
        raise ValidationError("O valor da despesa deve ser positivo e expresso em centavos.")
    if not shares:
        raise ValidationError("Selecione ao menos uma unidade participante.")
    if rule == "equal":
        quotas = dict.fromkeys(shares, Fraction(amount_cents, len(shares)))
    elif rule == "percent":
        percents = {code: _number(share.get("percentage"), f"o percentual de {code}")
                    for code, share in shares.items()}
        if sum(percents.values()) != 100:
            raise ValidationError("Os percentuais das unidades participantes devem somar 100%.")
        quotas = {code: amount_cents * pct / 100 for code, pct in percents.items()}
    elif rule in ("measured", "mixed"):
        if type(fixed_cents) is not int or not 0 <= fixed_cents <= amount_cents:
            raise ValidationError("A parcela fixa deve estar entre zero e o valor da despesa.")
        if rule == "measured" and fixed_cents:
            raise ValidationError("Escolha a regra mista para atribuir uma parcela fixa.")
        billed = _number(invoice_volume, "o volume faturado em m³")
        weights = {}
        for code, share in shares.items():
            old = _number(share.get("previous_reading"), f"a leitura anterior de {code}")
            new = _number(share.get("current_reading"), f"a leitura atual de {code}")
            if new < old:
                raise ValidationError(f"A leitura atual de {code} é inferior à anterior.")
            weights[code] = new - old
        measured = sum(weights.values())
        variable_cents = amount_cents - fixed_cents
        if measured == 0 and variable_cents:
            raise ValidationError("Consumo total medido igual a zero. Revise as leituras e o critério.")
        if billed != measured:
            if not variance_reason.strip() or variance_action not in ("proportional", "common"):
                raise ValidationError("O consumo medido diverge do faturado. Confira as leituras e o volume faturado.")
            if variance_action == "common" and (rule != "mixed" or fixed_cents <= 0 or billed < measured):
                raise ValidationError("A área comum exige diferença positiva de volume e valor fixo explícito na regra mista.")
        fixed_quotas = dict.fromkeys(shares, Fraction(fixed_cents, len(shares)))
        fixed = _reconcile(fixed_quotas, fixed_cents)
        if not variable_cents:
            return fixed
        variable_quotas = {code: variable_cents * usage / measured for code, usage in weights.items()}
        variable = _reconcile(variable_quotas, variable_cents)
        return {code: fixed[code] + variable[code] for code in shares}
    else:
        raise ValidationError("Selecione uma regra de divisão aprovada.")
    return _reconcile(quotas, amount_cents)
