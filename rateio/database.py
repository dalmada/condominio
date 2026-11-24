from urllib.parse import parse_qs, unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured


def postgres_config(url):
    """Connect the Django server to the dedicated Pátio schema."""
    value = url.strip().removeprefix("export ").removeprefix("RATEIO_DATABASE_URL=")
    value = value.strip().strip("`").strip("'\"").strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise ImproperlyConfigured(
            "RATEIO_DATABASE_URL tem host ou porta inválidos. Confira se os caracteres especiais da senha estão codificados."
        ) from None
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured("RATEIO_DATABASE_URL deve começar com postgresql://.")
    if not host or not parsed.username:
        raise ImproperlyConfigured("RATEIO_DATABASE_URL precisa conter o usuário e o host do PostgreSQL.")
    if not parsed.path.strip("/"):
        raise ImproperlyConfigured("RATEIO_DATABASE_URL precisa incluir o nome do banco, normalmente /postgres.")
    tls = parse_qs(parsed.query).get("sslmode", ["require"])[0]
    if tls not in {"require", "verify-ca", "verify-full"}:
        raise ImproperlyConfigured("A conexão PostgreSQL do Pátio exige TLS.")
    options = {"sslmode": tls, "options": "-c search_path=patio"}
    roots = parse_qs(parsed.query).get("sslrootcert")
    if roots:
        options["sslrootcert"] = roots[0]
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": host,
        "PORT": str(port or 5432),
        "CONN_MAX_AGE": 0,
        "DISABLE_SERVER_SIDE_CURSORS": True,
        "OPTIONS": options,
    }
