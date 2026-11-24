from decimal import Decimal

from django import template
from django.utils.formats import date_format
from django.utils.html import format_html

from ledger.icons import PATHS

register = template.Library()


@register.filter
def money(cents):
    if cents is None or cents == "":
        return "—"
    cents = int(cents)
    whole, fraction = divmod(abs(cents), 100)
    reais = format(whole, ",").replace(",", ".")
    return f"{'−' if cents < 0 else ''}R$ {reais},{fraction:02}"


@register.filter
def month_label(value):
    return date_format(value, "F \\d\\e Y").capitalize() if value else ""


@register.filter
def volume(value, signed=False):
    if value is None or value == "":
        return "—"
    number = Decimal(str(value))
    text = format(number.normalize(), "f").replace(".", ",")
    return f"+{text}" if signed and number > 0 else text


@register.simple_tag
def icon(name):
    return format_html(
        '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" '
        'aria-hidden="true"><path d="{}"/></svg>', PATHS[name],
    )
