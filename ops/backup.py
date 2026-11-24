"""Cópia diária do SQLite e dos anexos privados para um volume externo montado."""

import argparse
import fcntl
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.db.backends.sqlite3._functions import register as register_django_sql


MAGIC = b"RATEIO1\x00"
NONCE_SIZE = 12
TAG_SIZE = 16
BLOCK = 1024 * 1024
DEFAULT_DATA = Path(__file__).resolve().parents[1] / "var"


def separated(a, b):
    a, b = a.resolve(), b.resolve()
    return not (a.is_relative_to(b) or b.is_relative_to(a))


def generate_key(path):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as output:
        output.write(secrets.token_bytes(32))


def load_key(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("A chave precisa ser um arquivo regular acessível apenas ao proprietário (0600 ou 0400).")
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("Chave inválida. Use a chave gerada para este conjunto de cópias.")
    return key


def encrypt(source, output, key):
    nonce = secrets.token_bytes(NONCE_SIZE)
    header = MAGIC + nonce
    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(header)
    output.write(header)
    for part in iter(lambda: source.read(BLOCK), b""):
        output.write(cipher.update(part))
    output.write(cipher.finalize())
    output.write(cipher.tag)


def decrypt(source, output, key):
    header = source.read(len(MAGIC) + NONCE_SIZE)
    length = source.seek(0, os.SEEK_END) - len(header) - TAG_SIZE
    if not header.startswith(MAGIC) or len(header) != len(MAGIC) + NONCE_SIZE or length < 1:
        raise ValueError("Formato de cópia inválido ou arquivo incompleto.")
    source.seek(-TAG_SIZE, os.SEEK_END)
    cipher = Cipher(algorithms.AES(key), modes.GCM(header[len(MAGIC):], source.read(TAG_SIZE))).decryptor()
    cipher.authenticate_additional_data(header)
    source.seek(len(header))
    while length:
        part = source.read(min(BLOCK, length))
        if not part:
            raise ValueError("Arquivo de cópia incompleto.")
        output.write(cipher.update(part))
        length -= len(part)
    try:
        output.write(cipher.finalize())
    except InvalidTag as exc:
        raise ValueError("Cópia não autenticada. Confira a integridade do arquivo e a chave.") from exc


def database(path, mode):
    return closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode={mode}", uri=True, timeout=30))


def verify_database(connection, private):
    # A restrição do primeiro dia da competência usa funções SQLite do backend Django.
    register_django_sql(connection)
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("O SQLite informou falha de integridade.")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("O banco contém vínculos inválidos.")
    rows = connection.execute("""
        SELECT attachment FROM ledger_expense WHERE attachment <> ''
        UNION
        SELECT json_extract(item.value, '$.attachment') FROM ledger_closing
        JOIN json_each(ledger_closing.snapshot, '$.expenses') AS item
        WHERE json_extract(item.value, '$.attachment') <> ''
    """)
    for (filename,) in rows:
        file = private / filename
        if not file.resolve().is_relative_to(private.resolve()) or not file.is_file():
            raise ValueError(f"Anexo referenciado ausente ou fora da pasta privada: {filename}")


def snapshot(data, work):
    db_path = data / "db.sqlite3"
    private = data / "private"
    db_copy = work / "db.sqlite3"
    bundle = work / "instantaneo.tar.gz"
    if not db_path.is_file() or db_path.is_symlink():
        raise ValueError("Banco de origem ausente ou inválido.")
    with database(db_path, "rw") as writer, database(db_path, "ro") as reader:
        writer.execute("BEGIN IMMEDIATE")
        with database(db_copy, "rwc") as target:
            reader.backup(target)
            verify_database(target, private)
        with tarfile.open(bundle, "w:gz") as archive:
            archive.add(db_copy, arcname="db.sqlite3")
            if private.is_symlink() or (private.exists() and not private.is_dir()):
                raise ValueError("Diretório privado inválido.")
            if private.exists():
                def regular_only(info):
                    if not (info.isfile() or info.isdir()):
                        raise ValueError("O diretório privado contém um vínculo ou arquivo especial.")
                    return info
                archive.add(private, arcname="private", filter=regular_only)
    return bundle


def prune(folder, today):
    cutoff = today - timedelta(days=29)
    for item in folder.iterdir():
        match = re.fullmatch(r"rateio-(\d{4}-\d{2}-\d{2})\.rbackup", item.name)
        if match and item.is_file() and match[1] < cutoff.isoformat():
            item.unlink()


def save(data, mount, key_file):
    mount = mount.resolve()
    data = data.resolve()
    if mount == Path(mount.anchor) or not mount.is_mount():
        raise ValueError("O destino precisa ser a raiz de um volume externo montado.")
    if not separated(data, mount) or not separated(key_file, mount) or not separated(key_file, data):
        raise ValueError("A chave, a origem e o volume de cópias precisam estar em locais separados.")
    key = load_key(key_file)
    with (data / ".backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        folder = mount / "rateio"
        if folder.is_symlink():
            raise ValueError("O diretório de destino não pode ser um vínculo simbólico.")
        folder.mkdir(mode=0o700, exist_ok=True)
        today = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
        destination = folder / f"rateio-{today.isoformat()}.rbackup"
        with tempfile.TemporaryDirectory(prefix="rateio-copia-") as work:
            bundle = snapshot(data, Path(work))
            with tempfile.NamedTemporaryFile(dir=folder, prefix=".incompleto-", delete=False) as temp:
                temp_path = Path(temp.name)
                try:
                    with bundle.open("rb") as source:
                        encrypt(source, temp, key)
                    temp.flush()
                    os.fsync(temp.fileno())
                    os.replace(temp_path, destination)
                finally:
                    temp_path.unlink(missing_ok=True)
        prune(folder, today)
        return destination


def unpack(bundle, target):
    with tarfile.open(bundle, "r:gz") as archive:
        for item in archive:
            name = PurePosixPath(item.name)
            valid_root = name == PurePosixPath("db.sqlite3") or name.parts[:1] == ("private",)
            if not valid_root or ".." in name.parts or not (item.isfile() or item.isdir()):
                raise ValueError("Conteúdo inesperado no arquivo de cópia.")
            dest = target / name
            if item.isdir():
                dest.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.extractfile(item) as source, dest.open("xb") as output:
                    shutil.copyfileobj(source, output)
    (target / "private").mkdir(mode=0o700, exist_ok=True)
    with database(target / "db.sqlite3", "ro") as reader:
        verify_database(reader, target / "private")


def restore(archive_path, destination, key_file, live_data):
    destination = destination.absolute()
    if not separated(destination, live_data):
        raise ValueError("A restauração exige um diretório separado dos dados em uso.")
    if destination.exists() or destination.is_symlink():
        raise ValueError("O diretório de restauração já existe. Escolha um caminho novo.")
    key = load_key(key_file)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".rateio-restauracao-") as work:
        bundle = Path(work) / "instantaneo.tar.gz"
        with archive_path.open("rb") as source, bundle.open("xb") as output:
            decrypt(source, output, key)
        stage = Path(work) / "dados"
        stage.mkdir(mode=0o700)
        unpack(bundle, stage)
        # Reserva exclusiva depois de verificar o conteúdo. Uma segunda execução nunca sobrescreve dados.
        destination.mkdir(mode=0o700)
        try:
            stage.replace(destination)
        except OSError:
            destination.rmdir()
            raise
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    key_cmd = sub.add_parser("chave", help="Cria uma chave privada em arquivo novo.")
    key_cmd.add_argument("arquivo", type=Path)
    save_cmd = sub.add_parser("salvar", help="Publica uma cópia diária no volume montado.")
    save_cmd.add_argument("--montagem", required=True, type=Path)
    restore_cmd = sub.add_parser("restaurar", help="Restaura e verifica uma cópia em diretório novo.")
    restore_cmd.add_argument("arquivo", type=Path)
    restore_cmd.add_argument("--destino", required=True, type=Path)
    for command in (save_cmd, restore_cmd):
        command.add_argument("--chave", required=True, type=Path)
        command.add_argument("--dados", type=Path, default=os.environ.get("RATEIO_DATA_DIR", DEFAULT_DATA))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "chave":
            generate_key(args.arquivo)
            print(f"Chave criada em {args.arquivo}")
        elif args.command == "salvar":
            print(f"Cópia salva em {save(args.dados, args.montagem, args.chave)}")
        else:
            print(f"Cópia restaurada e verificada em {restore(args.arquivo, args.destino, args.chave, args.dados)}")
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError) as exc:
        print(f"Falha: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
