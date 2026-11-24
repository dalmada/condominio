from django.db import migrations


def register_condominium(apps, schema_editor):
    database = schema_editor.connection.alias
    units = apps.get_model("ledger", "Unit").objects.using(database)
    categories = apps.get_model("ledger", "Category").objects.using(database)
    unit, _ = units.get_or_create(code="Condomínio", defaults={"display_name": "Condomínio"})
    for category in categories.all():
        if not category.allows_metering and category.name != "Energia":
            continue
        if category.default_rule == "percent":
            continue
        shares = category.default_shares or dict.fromkeys(units.values_list("code", flat=True))
        category.default_shares = {**shares, unit.code: None}
        category.save(update_fields=["default_shares"])


class Migration(migrations.Migration):
    dependencies = [("ledger", "0009_fixed_metered_rule")]
    operations = [migrations.RunPython(register_condominium, migrations.RunPython.noop)]
