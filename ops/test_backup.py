import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

from ops import backup


def prepare_closed_period():
    from django.contrib.auth import get_user_model
    from django.core.files.base import ContentFile
    from ledger.models import Category, Expense, ExpenseShare, Period, Rule, Unit
    from ledger.services import close_period, record_payment

    actor = get_user_model().objects.create_user(username="ensaio", is_staff=True)
    period = Period.objects.create(month=date(2026, 10, 1), due_on=date(2026, 11, 10))
    expense = Expense.objects.create(
        period=period, category=Category.objects.get(name="Água"), description="Fatura do ensaio",
        amount_cents=10001, due_on=date(2026, 11, 2), rule=Rule.EQUAL,
        override_reason="Critério fictício do ensaio de restauração.",
        attachment=ContentFile(b"%PDF-1.4 anexo de ensaio", name="teste.pdf"),
    )
    units = list(Unit.objects.order_by("code"))
    ExpenseShare.objects.bulk_create([ExpenseShare(expense=expense, unit=unit) for unit in units])
    close_period(period, actor, expected_version=0)
    record_payment(period, units[0], 1000, date(2026, 11, 5), actor)


def check_restored_period():
    from django.contrib.auth import get_user_model
    from django.test import Client
    from django.urls import reverse
    from ledger.models import Expense, Period, Statement
    from ledger.services import balances

    period = Period.objects.get(state=Period.State.CLOSED)
    balance_by_unit = {row["code"]: row["balance_cents"] for row in balances(period)}
    check = unittest.TestCase()
    check.assertEqual(balance_by_unit, {"101": 1501, "201": 2500, "301": 2500, "401": 2500})
    client = Client(HTTP_HOST="localhost")
    client.force_login(get_user_model().objects.get(username="ensaio"))
    statements = list(Statement.objects.filter(closing__period=period))
    check.assertEqual(len(statements), 4)
    for statement in statements:
        response = client.get(reverse("statement", args=[statement.pk]))
        check.assertEqual(response.status_code, 200)
        check.assertIn(statement.code, response.content.decode())
        check.assertIn("Fatura do ensaio", response.content.decode())
    response = client.get(reverse("expense_attachment", args=[Expense.objects.get().pk]))
    check.assertEqual(response.status_code, 200)
    check.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 anexo de ensaio")
    response.close()


class BackupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "live"
        self.private = self.data / "private" / "attachments"
        self.private.mkdir(parents=True)
        self.bill = self.private / "conta.pdf"
        self.contents = b"fatura privada de teste"
        self.bill.write_bytes(self.contents)
        self.db = sqlite3.connect(self.data / "db.sqlite3")
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA wal_autocheckpoint=0")
        self.db.execute("CREATE TABLE ledger_expense (id INTEGER PRIMARY KEY, attachment TEXT, amount INTEGER)")
        self.db.execute("CREATE TABLE ledger_closing (snapshot TEXT)")
        self.db.execute("INSERT INTO ledger_expense VALUES (1, 'attachments/conta.pdf', 10001)")
        self.db.commit()
        self.key = self.root / "chave"
        backup.generate_key(self.key)
        self.mount = self.root / "volume"
        self.mount.mkdir()
        self.destination = self.root / "restaurado"

    def save(self):
        # O ambiente de teste não monta volumes. A barreira real é exercitada em teste separado.
        with patch.object(Path, "is_mount", return_value=True):
            return backup.save(self.data, self.mount, self.key)

    def test_wal_and_attachment_round_trip(self):
        archive = self.save()
        self.assertFalse(archive.read_bytes().startswith(b"SQLite"))
        self.assertNotIn(self.contents, archive.read_bytes())
        backup.restore(archive, self.destination, self.key, self.data)
        with closing(sqlite3.connect(self.destination / "db.sqlite3")) as restored:
            self.assertEqual(restored.execute("SELECT amount FROM ledger_expense").fetchone(), (10001,))
        self.assertEqual((self.destination / "private/attachments/conta.pdf").read_bytes(), self.contents)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

    def test_migrated_database_and_real_restore_cli(self):
        data = self.root / "django"
        env = dict(os.environ, RATEIO_DATA_DIR=str(data), RATEIO_PRODUCTION="0", RATEIO_ALLOWED_HOSTS="localhost")
        project = Path(__file__).resolve().parents[1]
        subprocess.run([sys.executable, "manage.py", "migrate", "--skip-checks", "--verbosity", "0"],
                       cwd=project, check=True, env=env)
        subprocess.run([sys.executable, "manage.py", "shell", "-c",
                        "from ops.test_backup import prepare_closed_period; prepare_closed_period()"],
                       cwd=project, check=True, env=env, stdout=subprocess.DEVNULL)
        with patch.object(Path, "is_mount", return_value=True):
            archive = backup.save(data, self.mount, self.key)
        subprocess.run([sys.executable, "ops/backup.py", "restaurar", str(archive), "--chave",
                        str(self.key), "--destino", str(self.destination)], cwd=project,
                       check=True, env=env, capture_output=True)
        restored_env = dict(env, RATEIO_DATA_DIR=str(self.destination))
        subprocess.run([sys.executable, "manage.py", "shell", "-c",
                        "from ops.test_backup import check_restored_period; check_restored_period()"],
                       cwd=project, check=True, env=restored_env, stdout=subprocess.DEVNULL)

    def test_failed_snapshot_preserves_daily_copy(self):
        archive = self.save()
        previous = archive.read_bytes()
        self.bill.unlink()
        with self.assertRaisesRegex(ValueError, "Anexo referenciado"):
            self.save()
        self.assertEqual(archive.read_bytes(), previous)
        self.bill.write_bytes(self.contents)
        self.db.execute("UPDATE ledger_expense SET amount=2")
        self.db.commit()
        self.assertEqual(self.save(), archive)
        backup.restore(archive, self.destination, self.key, self.data)
        with closing(sqlite3.connect(self.destination / "db.sqlite3")) as restored:
            self.assertEqual(restored.execute("SELECT amount FROM ledger_expense").fetchone(), (2,))

    def test_missing_historical_attachment_rejects_snapshot(self):
        self.db.execute("INSERT INTO ledger_closing VALUES (?)",
                        ('{"expenses":[{"attachment":"attachments/anterior.pdf"}]}',))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "anterior.pdf"):
            self.save()

    def test_mounted_destination_required(self):
        with self.assertRaisesRegex(ValueError, "montado"):
            backup.save(self.data, self.mount, self.key)
        self.assertEqual(list(self.mount.iterdir()), [])

    def test_refuse_restore_over_live_or_existing(self):
        archive = self.save()
        self.destination.mkdir()
        for dest in (self.data, self.data / "another", self.destination):
            with self.subTest(dest=dest), self.assertRaises(ValueError):
                backup.restore(archive, dest, self.key, self.data)

    def test_wrong_key_and_tampering_leave_no_destination(self):
        archive = self.save()
        wrong_key = self.root / "outra"
        backup.generate_key(wrong_key)
        with self.assertRaisesRegex(ValueError, "não autenticada"):
            backup.restore(archive, self.destination, wrong_key, self.data)
        with archive.open("r+b") as output:
            output.seek(-17, os.SEEK_END)
            byte = output.read(1)[0]
            output.seek(-17, os.SEEK_END)
            output.write(bytes([byte ^ 1]))
        with self.assertRaisesRegex(ValueError, "não autenticada"):
            backup.restore(archive, self.destination, self.key, self.data)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".rateio-restauracao-*")), [])

    def test_prune_exact_window_only(self):
        for name in ("rateio-2026-10-25.rbackup", "rateio-2026-10-26.rbackup", "inventario.txt", "rateio.csv"):
            (self.mount / name).touch()
        backup.prune(self.mount, date(2026, 11, 24))
        self.assertEqual(sorted(x.name for x in self.mount.iterdir()),
                         ["inventario.txt", "rateio-2026-10-26.rbackup", "rateio.csv"])

    def test_key_is_private_and_never_replaced(self):
        original = self.key.read_bytes()
        with self.assertRaises(FileExistsError):
            backup.generate_key(self.key)
        self.assertEqual(self.key.read_bytes(), original)
        self.key.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            backup.load_key(self.key)
