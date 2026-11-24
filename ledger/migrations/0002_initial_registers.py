from django.db import migrations


def seed(apps, schema_editor):
    units = apps.get_model("ledger", "Unit")
    categories = apps.get_model("ledger", "Category")
    db = schema_editor.connection.alias
    for code in ("101", "102", "103", "104"):
        units.objects.using(db).get_or_create(code=code)
    for name, supplier in (("Água", "COPASA"), ("Energia", "CEMIG"), ("Faxina", ""), ("Gás", "")):
        categories.objects.using(db).get_or_create(name=name, defaults={"supplier": supplier})


class Migration(migrations.Migration):
    dependencies = [("ledger", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
