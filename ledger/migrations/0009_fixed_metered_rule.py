from django.db import migrations


def set_measured_rule(apps, schema_editor):
    database = schema_editor.connection.alias
    units = apps.get_model("ledger", "Unit").objects.using(database)
    categories = apps.get_model("ledger", "Category").objects.using(database)
    codes = list(units.values_list("code", flat=True))
    for category in categories.filter(allows_metering=True):
        category.default_rule = "measured"
        category.default_shares = dict.fromkeys(category.default_shares or codes)
        category.save(update_fields=["default_rule", "default_shares"])


class Migration(migrations.Migration):
    dependencies = [("ledger", "0008_water_meter_readings")]
    operations = [migrations.RunPython(set_measured_rule, migrations.RunPython.noop)]
