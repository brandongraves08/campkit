# syntax=docker/dockerfile:1

# CampKit runs as one unprivileged process in front of PostgreSQL. The image
# contains no credential of any kind: every secret is injected at run time from
# the untracked ``.env`` file that ``compose.yaml`` reads.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings \
    CAMPKIT_DATA_DIR=/data

# Keep these ARG pins aligned with pyproject.toml. The web image installs
# runtime dependencies directly and runs the copied source tree; explicit
# package discovery supports editable developer/test installs only.
ARG DJANGO_VERSION=5.2.18
ARG PSYCOPG_VERSION=3.3.6
ARG GUNICORN_VERSION=23.0.0

# Fixed, unprivileged identity. Nothing in the image is ever run as root.
RUN groupadd --system --gid 10001 campkit \
    && useradd --system --uid 10001 --gid campkit --home-dir /app --shell /usr/sbin/nologin campkit

WORKDIR /app

COPY manage.py pyproject.toml /app/
COPY apps /app/apps
COPY config /app/config
COPY templates /app/templates
COPY static /app/static
COPY deploy /app/deploy

RUN pip install --no-cache-dir \
        "Django==${DJANGO_VERSION}" \
        "psycopg[binary]==${PSYCOPG_VERSION}" \
        "gunicorn==${GUNICORN_VERSION}" \
    && mkdir -p /app/staticfiles /data/private/intake \
    && chown -R campkit:campkit /app /data

USER campkit

EXPOSE 8000

# Liveness only: the process must accept a TCP connection on its own port.
# Readiness is enforced by compose's ``depends_on: db: service_healthy``.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import socket,sys; s=socket.socket(); s.settimeout(4); sys.exit(0 if s.connect_ex(('127.0.0.1', 8000)) == 0 else 1)"]

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "--access-logfile", "-", "--error-logfile", "-", "config.wsgi:application"]
