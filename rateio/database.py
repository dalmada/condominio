from urllib.parse import parse_qs, unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured


def postgres_config(url):
    """Connect the Django server to the dedicated Pátio schema."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.username or not parsed.path.strip("/"):
        raise ImproperlyConfigured("RATEIO_DATABASE_URL deve ser uma URL PostgreSQL completa.")
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
        "HOST": parsed.hostname,
        "PORT": str(parsed.port or 5432),
        "CONN_MAX_AGE": 0,
        "DISABLE_SERVER_SIDE_CURSORS": True,
        "OPTIONS": options,
    }
