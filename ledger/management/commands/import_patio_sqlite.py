from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.management import BaseCommand, CommandError, call_command
from django.db import connections, transaction
from django.db.models import Sum

from ledger.models import AuditEvent, Category, Closing, Expense, Payment, Period, Unit


class Command(BaseCommand):
    help = "Importa um SQLite do Pátio em um schema PostgreSQL recém-migrado."

    def add_arguments(self, parser):
        parser.add_argument("source", type=Path, help="Cópia consistente do db.sqlite3.")

    def handle(self, source, **options):
        target = connections["default"]
        if target.vendor != "postgresql":
            raise CommandError("Configure RATEIO_DATABASE_URL para o PostgreSQL de destino.")
        source = source.resolve(strict=True)
        source_db = {
            **deepcopy(connections.databases["default"]),
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": source.as_uri() + "?mode=ro",
            "OPTIONS": {"uri": True},
            "USER": "", "PASSWORD": "", "HOST": "", "PORT": "",
        }
        connections.databases["patio_source"] = source_db
        with TemporaryDirectory(prefix="patio-import-") as temp:
            path = Path(temp) / "patio.json"
            call_command(
                "dumpdata", "auth.user", "auth.group", "ledger", database="patio_source",
                use_natural_foreign_keys=True, output=str(path), verbosity=0,
            )
            with transaction.atomic():
                with target.cursor() as cursor:
                    cursor.execute("SELECT current_schema()")
                    if cursor.fetchone()[0] != "patio":
                        raise CommandError("O destino precisa estar no schema patio.")
                user = get_user_model()
                models = (user, Period, Expense, Closing, Payment, AuditEvent)
                if any(model.objects.exists() for model in models):
                    raise CommandError("O destino já tem dados de uso. A importação foi interrompida.")
                if set(Unit.objects.values_list("code", flat=True)) != {"101", "201", "301", "401"}:
                    raise CommandError("As unidades do destino diferem do cadastro inicial.")
                if set(Category.objects.values_list("name", flat=True)) != {"Água", "Gás", "Energia", "Faxina"}:
                    raise CommandError("As categorias do destino diferem do cadastro inicial.")
                Unit.objects.all().delete()
                Category.objects.all().delete()
                call_command("loaddata", str(path), database="default", verbosity=0)
                counts = (user, Unit, Category, Period, Expense, Closing, Payment, AuditEvent)
                for model in counts:
                    if model.objects.count() != model.objects.using("patio_source").count():
                        raise CommandError(f"Contagem inconsistente em {model._meta.label}.")
                expected = Expense.objects.using("patio_source").aggregate(total=Sum("amount_cents"))
                if Expense.objects.aggregate(total=Sum("amount_cents")) != expected:
                    raise CommandError("O total de despesas difere da origem.")
        self.stdout.write(self.style.SUCCESS("Importação concluída e conferida."))
