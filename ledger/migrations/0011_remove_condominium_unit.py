from django.db import migrations


def remove_condominium(apps, schema_editor):
    database = schema_editor.connection.alias
    units = apps.get_model("ledger", "Unit").objects.using(database)
    categories = apps.get_model("ledger", "Category").objects.using(database)
    unit = units.filter(code="Condomínio").first()
    if unit is None:
        return

    # The foreign keys protect expenses, statements and payments from deletion.
    unit.delete()
    previous_energy_default = dict.fromkeys(("101", "201", "301", "401", "Condomínio"))
    for category in categories.all():
        if "Condomínio" not in category.default_shares:
            continue
        if category.name == "Energia" and not category.default_rule and category.default_shares == previous_energy_default:
            category.default_shares = {}
        else:
            category.default_shares.pop("Condomínio")
        category.save(update_fields=["default_shares"])


class Migration(migrations.Migration):
    dependencies = [("ledger", "0010_condominium_unit")]
    operations = [migrations.RunPython(remove_condominium, migrations.RunPython.noop)]
