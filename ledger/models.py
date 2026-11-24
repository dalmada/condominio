import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class Rule(models.TextChoices):
    EQUAL = "equal", "Divisão igualitária"
    PERCENT = "percent", "Percentuais"
    MEASURED = "measured", "Consumo medido"
    MIXED = "mixed", "Parcela fixa e consumo"


class Unit(models.Model):
    code = models.CharField("identificador", max_length=32, unique=True)
    display_name = models.CharField("nome da unidade", max_length=100, blank=True)
    responsible = models.CharField("responsável", max_length=160, blank=True)
    occupied = models.BooleanField("ocupada", default=True)

    class Meta:
        ordering = ["code"]

    def save(self, *args, **kwargs):
        if not self._state.adding and Unit.objects.filter(pk=self.pk).exclude(code=self.code).exists():
            raise ValidationError({"code": "O identificador da unidade é permanente."})
        super().save(*args, **kwargs)

    def __str__(self):
        return self.code


class Category(models.Model):
    name = models.CharField("categoria", max_length=80, unique=True)
    supplier = models.CharField("fornecedor habitual", max_length=160, blank=True)
    gas_tariff = models.BooleanField(default=False, editable=False)
    allows_metering = models.BooleanField("aceita medição em m³", default=False, editable=False)
    default_rule = models.CharField("regra aprovada", max_length=16, choices=Rule, blank=True)
    default_shares = models.JSONField("participação padrão", default=dict, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Period(models.Model):
    class State(models.TextChoices):
        OPEN = "open", "Aberta"
        CLOSED = "closed", "Fechada"
        REVISING = "revising", "Em retificação"

    month = models.DateField("competência", unique=True)
    due_on = models.DateField("vencimento da cobrança")
    state = models.CharField("situação", max_length=12, choices=State, default=State.OPEN)
    current_version = models.PositiveIntegerField(default=0)
    revision_reason = models.TextField("motivo da retificação", blank=True)

    class Meta:
        ordering = ["-month"]
        constraints = [models.CheckConstraint(condition=Q(month__day=1), name="period_month_start")]

    def __str__(self):
        return self.month.strftime("%m/%Y")


def private_attachment(instance, filename):
    extension = Path(filename).suffix.lower()
    return f"attachments/{uuid.uuid4().hex}{extension}"


class Expense(models.Model):
    period = models.ForeignKey(Period, on_delete=models.PROTECT, related_name="expenses")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="expenses")
    description = models.CharField("descrição", max_length=240)
    supplier = models.CharField("fornecedor ou prestador", max_length=160, blank=True)
    amount_cents = models.PositiveBigIntegerField("valor em centavos", validators=[MinValueValidator(1)])
    due_on = models.DateField("vencimento do fornecedor")
    service_on = models.DateField("data do serviço", null=True, blank=True)
    consumed_from = models.DateField("início do consumo", null=True, blank=True)
    consumed_to = models.DateField("fim do consumo", null=True, blank=True)
    rule = models.CharField("critério aplicado", max_length=16, choices=Rule, blank=True)
    override_reason = models.TextField("motivo da exceção", blank=True)
    fixed_cents = models.PositiveBigIntegerField("parcela fixa em centavos", default=0)
    invoice_volume = models.DecimalField("consumo faturado em m³", max_digits=16, decimal_places=4,
                                         null=True, blank=True)
    variance_action = models.CharField("tratamento da diferença", max_length=16, blank=True,
                                      choices=[("proportional", "Proporcional com justificativa"),
                                               ("common", "Parcela de área comum")])
    variance_reason = models.TextField("justificativa da diferença", blank=True)
    attachment = models.FileField("anexo privado", upload_to=private_attachment, blank=True)
    baseline = models.JSONField("padrão da categoria no lançamento", default=dict, editable=False)

    class Meta:
        ordering = ["pk"]
        constraints = [models.CheckConstraint(condition=Q(amount_cents__gt=0), name="expense_positive")]

    def save(self, *args, **kwargs):
        if self.category_id and self.baseline.get("category_id") != self.category_id:
            category = self.category
            shares_by_id = {str(unit.pk): category.default_shares[unit.code]
                            for unit in Unit.objects.filter(code__in=category.default_shares)}
            self.baseline = {"category_id": category.pk, "rule": category.default_rule,
                             "shares": category.default_shares, "shares_by_id": shares_by_id}
        super().save(*args, **kwargs)

    def __str__(self):
        return self.description


class ExpenseShare(models.Model):
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="shares")
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, related_name="expense_shares")
    percentage = models.DecimalField("percentual", max_digits=9, decimal_places=6, null=True, blank=True)
    previous_reading = models.DecimalField("leitura anterior", max_digits=16, decimal_places=4,
                                          null=True, blank=True)
    current_reading = models.DecimalField("leitura atual", max_digits=16, decimal_places=4,
                                         null=True, blank=True)

    hot_previous_reading = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    hot_current_reading = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["expense", "unit"], name="unique_expense_unit")]


class Closing(models.Model):
    period = models.ForeignKey(Period, on_delete=models.PROTECT, related_name="closings")
    version = models.PositiveIntegerField()
    snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-version"]
        constraints = [models.UniqueConstraint(fields=["period", "version"], name="unique_closing_version")]


class Statement(models.Model):
    closing = models.ForeignKey(Closing, on_delete=models.PROTECT, related_name="statements")
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, related_name="statements")
    code = models.CharField(max_length=32)
    display_name = models.CharField(max_length=100, blank=True)
    responsible = models.CharField(max_length=160, blank=True)
    total_cents = models.PositiveBigIntegerField()
    due_on = models.DateField()
    items = models.JSONField(default=list)

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["closing", "unit"], name="unique_closing_statement")]


class Payment(models.Model):
    period = models.ForeignKey(Period, on_delete=models.PROTECT, related_name="payments")
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, related_name="payments")
    amount_cents = models.BigIntegerField()
    paid_on = models.DateField("data do movimento")
    note = models.TextField("observação", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    token = models.UUIDField(default=uuid.uuid4, unique=True)
    reversal_of = models.OneToOneField("self", on_delete=models.PROTECT, related_name="reversal",
                                      blank=True, null=True)

    class Meta:
        ordering = ["-paid_on", "-pk"]
        constraints = [models.CheckConstraint(
            condition=Q(amount_cents__gt=0, reversal_of__isnull=True) | Q(amount_cents__lt=0, reversal_of__isnull=False),
            name="payment_direction")]


class AuditEvent(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=80)
    subject = models.CharField(max_length=120)
    detail = models.TextField(blank=True)

    class Meta:
        ordering = ["-pk"]
