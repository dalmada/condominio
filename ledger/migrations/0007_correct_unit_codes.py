from django.db import migrations


CODES = {"102": "201", "103": "301", "104": "401"}


def rename_units(apps, schema_editor, codes):
    database = schema_editor.connection.alias
    units = apps.get_model("ledger", "Unit").objects.using(database)
    categories = apps.get_model("ledger", "Category").objects.using(database)
    expenses = apps.get_model("ledger", "Expense").objects.using(database)

    def rename_keys(shares):
        return {codes.get(code, code): share for code, share in shares.items()}

    for previous, current in codes.items():
        units.filter(code=previous).update(code=current)
    for category in categories.all():
        shares = rename_keys(category.default_shares)
        if shares != category.default_shares:
            category.default_shares = shares
            category.save(update_fields=["default_shares"])
    for expense in expenses.exclude(period__state="closed"):
        if "shares" in expense.baseline:
            shares = rename_keys(expense.baseline["shares"])
            if shares != expense.baseline["shares"]:
                expense.baseline["shares"] = shares
                expenses.filter(pk=expense.pk).update(baseline=expense.baseline)


def forwards(apps, schema_editor):
    rename_units(apps, schema_editor, CODES)


def backwards(apps, schema_editor):
    rename_units(apps, schema_editor, {current: previous for previous, current in CODES.items()})


class Migration(migrations.Migration):
    dependencies = [("ledger", "0006_gas_tariff")]
    operations = [migrations.RunPython(forwards, backwards)]
