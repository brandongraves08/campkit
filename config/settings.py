from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
# Every mutable file the app writes (currently the private intake store) lives
# under one directory so a container can mount it as a durable volume. Local
# runs keep the historical ``BASE_DIR`` layout.
DATA_DIR = Path(os.environ.get("CAMPKIT_DATA_DIR", BASE_DIR))
SECRET_KEY = os.environ.get("SECRET_KEY", "")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = [host.strip() for host in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "oauth2_provider",
    "apps.mobile_api.apps.MobileApiConfig",
    "apps.households.apps.HouseholdsConfig",
    "apps.audit.apps.AuditConfig",
    "apps.inventory.apps.InventoryConfig",
    "apps.trips.apps.TripsConfig",
    "apps.meals.apps.MealsConfig",
    "apps.intake.apps.IntakeConfig",
    "apps.enrichment.apps.EnrichmentConfig",
    "apps.api.apps.ApiConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.households.context.household_context",
            ],
        },
    },
]
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "campkit"),
        "USER": os.environ.get("POSTGRES_USER", "campkit"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {"connect_timeout": 5},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "login"

# Secure defaults are deliberately enabled even for the local deployment.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# The deployment never terminates TLS itself. A reverse proxy on the home
# network or VPN terminates TLS and forwards to the loopback-bound container,
# so Django must trust that one proxy header to know the request is secure.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Public origins the proxy serves, for POST/CSRF under that hostname.
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CAMPKIT_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

# Private photo/barcode intake. Uploads live outside static/media roots and are
# served only through household-scoped views.
INTAKE_UPLOAD_ROOT = Path(os.environ.get("CAMPKIT_INTAKE_UPLOAD_ROOT", DATA_DIR / "private" / "intake"))
INTAKE_MAX_IMAGE_BYTES = int(os.environ.get("CAMPKIT_INTAKE_MAX_IMAGE_BYTES", 5 * 1024 * 1024))
INTAKE_MAX_ATTEMPTS = int(os.environ.get("CAMPKIT_INTAKE_MAX_ATTEMPTS", 3))
INTAKE_CONFIDENCE_THRESHOLDS = {
    "identity": float(os.environ.get("CAMPKIT_INTAKE_IDENTITY_THRESHOLD", "0.85")),
    "quantity": float(os.environ.get("CAMPKIT_INTAKE_QUANTITY_THRESHOLD", "0.80")),
    "nutrition": float(os.environ.get("CAMPKIT_INTAKE_NUTRITION_THRESHOLD", "0.90")),
    "allergen": float(os.environ.get("CAMPKIT_INTAKE_ALLERGEN_THRESHOLD", "0.95")),
}

# Provider credentials are separate app environment secrets. Never read or edit
# the Hermes runtime configuration for these values.
INTAKE_VISION_CLIENT = os.environ.get("CAMPKIT_VISION_CLIENT", "")
CAMPKIT_VISION_ENDPOINT = os.environ.get("CAMPKIT_VISION_ENDPOINT", "")
CAMPKIT_VISION_TOKEN = os.environ.get("CAMPKIT_VISION_TOKEN", "")
CAMPKIT_VISION_TIMEOUT = float(os.environ.get("CAMPKIT_VISION_TIMEOUT", "10"))
CAMPKIT_HERMES_NOTIFY_URL = os.environ.get("CAMPKIT_HERMES_NOTIFY_URL", "")
CAMPKIT_HERMES_NOTIFY_TOKEN = os.environ.get("CAMPKIT_HERMES_NOTIFY_TOKEN", "")
CAMPKIT_HERMES_NOTIFY_TRANSPORT = os.environ.get("CAMPKIT_HERMES_NOTIFY_TRANSPORT", "")
CAMPKIT_HERMES_NOTIFY_TIMEOUT = float(os.environ.get("CAMPKIT_HERMES_NOTIFY_TIMEOUT", "5"))

# The API/MCP adapter authenticates as one dedicated service identity so every
# audit row is attributable to Hermes rather than to a household human.
CAMPKIT_SERVICE_USERNAME = os.environ.get("CAMPKIT_SERVICE_USERNAME", "hermes")

# Public native member OAuth client. Access tokens are short-lived; refresh
# tokens rotate and replay revokes the token family. API views must still enforce
# active household Membership independently of OAuth authentication.
OAUTH2_PROVIDER = {
    "OAUTH2_GRANT_TYPES_SUPPORTED": ["authorization_code", "refresh_token"],
    "OAUTH2_RESPONSE_TYPES_SUPPORTED": ["code"],
    "PKCE_REQUIRED": True,
    "COMPLIANT_BCP_RFC9700_PKCE_METHOD": True,
    "ALLOW_URI_WILDCARDS": False,
    "ALLOWED_REDIRECT_URI_SCHEMES": ["com.graveystudios.campkit"],
    "SCOPES": {"mobile:packing": "Mobile packing access"},
    "DEFAULT_SCOPES": ["mobile:packing"],
    "ACCESS_TOKEN_EXPIRE_SECONDS": 900,
    "REFRESH_TOKEN_EXPIRE_SECONDS": 30 * 24 * 60 * 60,
    "ROTATE_REFRESH_TOKEN": True,
    "REFRESH_TOKEN_REUSE_PROTECTION": True,
    "REFRESH_TOKEN_GRACE_PERIOD_SECONDS": 0,
    "COMPLIANT_BCP_RFC9700_IMPLICIT_GRANT": True,
    "COMPLIANT_BCP_RFC9700_PASSWORD_GRANT": True,
    "COMPLIANT_BCP_RFC9700_REFRESH_TOKEN": True,
    "COMPLIANT_BCP_RFC9700_REDIRECT_URI_SCHEME": True,
    "COMPLIANT_BCP_RFC9700_REDIRECT_URI_MATCHING": True,
    "COMPLIANT_BCP_RFC9700_PKCE_REQUIRED": True,
}

# Sourced enrichment providers are selected by dotted path (or an instance in
# tests). Unset means the shipped null provider: no data, no fabrication, and no
# blocked manual entry. Credentials are separate app environment secrets and are
# never read from, or written to, the Hermes runtime configuration.
CAMPKIT_NUTRITION_PROVIDER = os.environ.get(
    "CAMPKIT_NUTRITION_PROVIDER", "apps.enrichment.usda.UsdaNutritionProvider"
)
CAMPKIT_PRODUCT_RESEARCHER = os.environ.get(
    "CAMPKIT_PRODUCT_RESEARCHER", "apps.enrichment.research.HermesResearchProvider"
)
CAMPKIT_USDA_API_KEY = os.environ.get("CAMPKIT_USDA_API_KEY", "")
CAMPKIT_USDA_BASE_URL = os.environ.get("CAMPKIT_USDA_BASE_URL", "https://api.nal.usda.gov/fdc/v1")
CAMPKIT_USDA_TIMEOUT = float(os.environ.get("CAMPKIT_USDA_TIMEOUT", "10"))
CAMPKIT_HERMES_RESEARCH_URL = os.environ.get("CAMPKIT_HERMES_RESEARCH_URL", "")
CAMPKIT_HERMES_RESEARCH_TOKEN = os.environ.get("CAMPKIT_HERMES_RESEARCH_TOKEN", "")
CAMPKIT_HERMES_RESEARCH_TIMEOUT = float(os.environ.get("CAMPKIT_HERMES_RESEARCH_TIMEOUT", "10"))
# Provider responses are cached with their fetch time; stale cached values may
# still be shown, but only labelled stale.
CAMPKIT_ENRICHMENT_CACHE_TTL_SECONDS = int(
    os.environ.get("CAMPKIT_ENRICHMENT_CACHE_TTL_SECONDS", str(30 * 24 * 3600))
)
CAMPKIT_ENRICHMENT_MIN_CONFIDENCE = os.environ.get("CAMPKIT_ENRICHMENT_MIN_CONFIDENCE", "0.300")
# A price observation older than this is presented as stale, never as current.
CAMPKIT_PRICE_STALE_DAYS = int(os.environ.get("CAMPKIT_PRICE_STALE_DAYS", "30"))
