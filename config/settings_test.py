import os

from config.settings import *

SECRET_KEY = "test-only-not-for-deployment-campkit-key"
DEBUG = True
# The suite uses in-memory SQLite unless a file is requested. A file database
# lets the protocol-level MCP smoke test share committed fixtures with the
# stdio subprocess it launches.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("CAMPKIT_TEST_DB") or ":memory:",
    }
}
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
