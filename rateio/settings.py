import os
import secrets
import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .database import postgres_config

BASE_DIR = Path(__file__).resolve().parent.parent
ON_VERCEL = os.environ.get("VERCEL") == "1"
DEFAULT_DATA_DIR = Path("/tmp/patio") if ON_VERCEL else BASE_DIR / "var"
DATA_DIR = Path(os.environ.get("RATEIO_DATA_DIR", DEFAULT_DATA_DIR)).resolve()
DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
PRODUCTION = ON_VERCEL or os.environ.get("RATEIO_PRODUCTION", "") == "1"
DEBUG = not PRODUCTION
SECRET_KEY = os.environ.get("RATEIO_SECRET_KEY", "")
if not SECRET_KEY:
    if PRODUCTION:
        raise ImproperlyConfigured("Defina RATEIO_SECRET_KEY para produção.")
    key_file = DATA_DIR / ".local-key"
    if not key_file.exists():
        with tempfile.NamedTemporaryFile(mode="w", dir=DATA_DIR) as temporary:
            temporary.write(secrets.token_urlsafe(64))
            temporary.flush()
            try:
                os.link(temporary.name, key_file)
            except FileExistsError:
                pass
    SECRET_KEY = key_file.read_text().strip()
ALLOWED_HOSTS = [value.strip() for value in os.environ.get(
    "RATEIO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]" if DEBUG else ""
).split(",") if value.strip()]
if ON_VERCEL:
    ALLOWED_HOSTS += [host for name in ("VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL")
                      if (host := os.environ.get(name))]
if PRODUCTION and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("Defina RATEIO_ALLOWED_HOSTS para produção.")
CSRF_TRUSTED_ORIGINS = [f"https://{host}" for host in ALLOWED_HOSTS if host != "*"]
ROOT_URLCONF = "rateio.urls"
WSGI_APPLICATION = "rateio.wsgi.application"
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "django.contrib.staticfiles", "ledger",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *(["whitenoise.middleware.WhiteNoiseMiddleware"] if PRODUCTION else []),
    "django.contrib.sessions.middleware.SessionMiddleware", "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware", "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware", "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3", "NAME": DATA_DIR / "db.sqlite3",
    "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
}}
database_url = os.environ.get("RATEIO_DATABASE_URL")
if database_url:
    DATABASES["default"] = postgres_config(database_url)
elif ON_VERCEL:
    raise ImproperlyConfigured("Configure RATEIO_DATABASE_URL na Vercel.")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
]
LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_TZ = True
USE_I18N = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = DATA_DIR / "static"
MEDIA_ROOT = DATA_DIR / "private"
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
SECURE_SSL_REDIRECT = PRODUCTION
SESSION_COOKIE_SECURE = PRODUCTION
CSRF_COOKIE_SECURE = PRODUCTION
SECURE_HSTS_SECONDS = 31536000 if PRODUCTION else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = PRODUCTION
SECURE_HSTS_PRELOAD = PRODUCTION
if os.environ.get("RATEIO_TRUST_PROXY") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

