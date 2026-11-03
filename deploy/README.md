# CampKit deployment (single host, private network)

CampKit is a self-hosted household app with self-registration and invitation-based household sharing. This deployment is one
Compose stack on one host:

| Service | Image | Purpose | Reaches the network |
| --- | --- | --- | --- |
| `web` | built from `../Dockerfile` | Django app under gunicorn, running as uid/gid **10001** (`campkit`) | `127.0.0.1:${CAMPKIT_HTTP_PORT:-8000}` only |
| `db` | `postgres:16-alpine` | PostgreSQL 16 with a `pg_isready` healthcheck | `127.0.0.1:${POSTGRES_PORT:-5432}` only |

`web` starts only after `db` reports healthy (`depends_on: condition:
service_healthy`), and on every start it applies migrations and collects static
files before gunicorn binds the port.

**Ingress model.** Nothing is published beyond the host's loopback interface.
A TLS-terminating reverse proxy on the home network or VPN is the only way in
(see [Reverse proxy](#reverse-proxy)). Django is configured with
`SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`, which is why the
proxy must set that header.

## Files

```
Dockerfile          non-root image; pins Django/psycopg from pyproject.toml + gunicorn
compose.yaml        db + web, healthchecks, loopback ports, named volumes
.env.example        every setting, no real secret; copy to .env
deploy/backup.sh    PostgreSQL dump + media archive, checksummed, pruned
deploy/README.md    this file
```

## Prerequisites

- Docker Engine with the Compose v2 plugin (`docker compose version`).
- A host on the trusted network or VPN, with a TLS-terminating reverse proxy.
- Roughly 2 GB of free disk for images, the database, photos, and backups.

## First run

1. Create the environment file and keep it private:

   ```bash
   cp .env.example .env
   chmod 600 .env
   python3 -c "import secrets; print(secrets.token_urlsafe(64))"   # SECRET_KEY
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # POSTGRES_PASSWORD
   ```

   Fill in `SECRET_KEY`, `POSTGRES_PASSWORD`, and `ALLOWED_HOSTS` (the hostname
   the proxy serves, for example `campkit.lan`). Add
   `CAMPKIT_CSRF_TRUSTED_ORIGINS=https://campkit.lan` when the proxy serves TLS
   under that name.

2. Build and start:

   ```bash
   docker compose up -d --build
   docker compose ps
   docker compose logs -f web
   ```

3. Create the first user and household at `/accounts/signup/` through the internal
   HTTPS hostname. The form creates a new private household and gives that user
   the owner role; there is no seed command. A household member joins an existing
   household only by following its owner's invitation link.

   Owners issue invitations from the authenticated app with
   `POST /accounts/invitations/` (form field `email`). The recipient uses
   `https://<host>/accounts/signup/<token>/` and must use the invited email
   address. New recipients create an account there; existing account holders sign
   in, explicitly accept the invitation, and can switch between their households
   from the app header. Data remains scoped to the selected household.

## Secrets

- Secrets exist only in `.env`, which is git-ignored and must be `chmod 600`.
  The image and `compose.yaml` contain no credential; `compose.yaml` only
  references variable names and fails fast (`${VAR:?message}`) when one is unset.
- `SECRET_KEY`, `POSTGRES_PASSWORD`, and every provider token are separate
  values. Rotating `POSTGRES_PASSWORD` means changing it in `.env` and in the
  database (`ALTER USER ... PASSWORD ...`), then `docker compose up -d`.
- Optional provider credentials (`CAMPKIT_USDA_API_KEY`, `CAMPKIT_VISION_*`)
  are app environment secrets. Unset means the shipped null provider: no data
  is invented and manual entry still works.

## Data and durability

Three named volumes:

| Volume | Mount | Contents | Backed up |
| --- | --- | --- | --- |
| `<project>_postgres_data` | `db:/var/lib/postgresql/data` | database cluster | yes, `deploy/backup.sh` |
| `<project>_campkit_data` | `web:/data` | private intake photos (`/data/private/intake`) | yes, `deploy/backup.sh` |
| `<project>_campkit_static` | `web:/app/staticfiles` | collected static files | no (rebuilt on start) |

`CAMPKIT_DATA_DIR=/data` is set for `web`, so every mutable app file lives on
`campkit_data`. Photos are never served by Django's static or media handlers;
they are streamed only by the authenticated, household-scoped intake view.

## Reverse proxy

The proxy terminates TLS, forwards to `127.0.0.1:8000`, and sets
`X-Forwarded-Proto`. Minimal nginx location blocks:

```nginx
server {
    listen 443 ssl;
    server_name campkit.lan;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 8m;   # above CAMPKIT_INTAKE_MAX_IMAGE_BYTES (5 MB)
    }

    # Collected static files live on the campkit_static volume. Either run the
    # proxy as a container that mounts that volume, or copy them to the host:
    #   docker compose cp web:/app/staticfiles/. /srv/campkit/static/
    location /static/ {
        alias /srv/campkit/static/;
        access_log off;
    }
}
```

Django does not serve static files when `DEBUG=false`; the proxy must serve
`/static/` from the collected volume. That proxy configuration is operator
territory and is not shipped here.

## Backup

`deploy/backup.sh` writes one PostgreSQL custom-format dump and one gzip archive
of `/data/private`, records SHA-256 checksums and a manifest, and prunes files
older than `CAMPKIT_BACKUP_RETENTION_DAYS` (default 14).

```bash
# A consistent backup: stops web if it was running and always restores its prior state
CAMPKIT_BACKUP_DIR=/srv/campkit-backups deploy/backup-consistent.sh

# verify the newest backup's checksums and dump signature (no downtime)
CAMPKIT_BACKUP_DIR=/srv/campkit-backups deploy/backup-consistent.sh --verify
```

The script takes no password: `pg_dump` runs inside the `db` container, where
Compose already supplied `POSTGRES_USER`/`POSTGRES_DB`. Copy backups off the
host as well; a backup on the same disk is not a backup.

### Restore

Restore into a stack that is running the same image and `.env`. Stop the app so
no worker writes during the restore; the database stays up so it can be
restored. `pg_restore --clean --if-exists` replaces the current schema in the
existing database.

```bash
STAMP=20261101T120000Z
BACKUP=/srv/campkit-backups

docker compose stop web

# 1. database
docker compose exec -T db sh -c \
  'exec pg_restore --clean --if-exists --no-owner --no-privileges \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"' \
  < "$BACKUP/campkit-db-$STAMP.dump"

# 2. media (paths inside the archive are relative to /data)
docker compose run --rm --no-deps -T web sh -c 'exec tar --extract --gzip --file - --directory /data' \
  < "$BACKUP/campkit-media-$STAMP.tar.gz"

# 3. checksums before you trust either file
( cd "$BACKUP" && sha256sum -c "campkit-$STAMP.sha256" )

docker compose start web
docker compose logs --tail=50 web
```

Then sign in and spot-check the pantry, the verification queue, and one intake
photo. Restore into a throwaway stack first when rehearsing
(`docker compose -p campkit-rehearsal ...` with separate volumes) — never
rehearse against the household's only copy.

## Upgrades

```bash
git pull
docker compose build web
docker compose up -d --remove-orphans   # migrations run on start
docker compose ps
```

`campkit_data` and `postgres_data` are untouched by a rebuild. Take a backup
before an upgrade that includes migrations.

## Verification status of this deployment

Deployment checks were performed on 2026-11-03; the isolated restore rehearsal used a 2026-11-02 backup. Evidence and remaining gaps:

| Check | Status | Notes |
| --- | --- | --- |
| Local automated checks | **passed** | Full pytest suite: 236 passed; Django system check under test settings and `git diff --check` passed |
| HTTPS login/signup pages | **passed** | The live internal route returned HTTP 200 |
| Disposable self-registration/login, invitation acceptance, and trip/gear/pantry/meal creation | **passed** | Exercised over internal HTTPS; synthetic users, households, and records were readback-verified removed |
| Case-insensitive login ambiguity and household-selection regressions | **passed** | Covered by automated tests; not claimed as a live mixed-case login check |
| Isolated backup-restore rehearsal | **passed** | Backup passed SHA-256 and gzip checks; PostgreSQL restore in an isolated stack matched table row counts. The media archive restored without files; no production records were modified |
| Deployment-backed MCP smoke call and optional external-provider integrations | **not run** | These integrations were outside the live app-flow check and need separate verification before use |

Local tests and checks do not substitute for separate live verification of optional integrations or workflows outside the exercised paths.

## Security notes

- `web` runs as the unprivileged `campkit` user (uid 10001) with
  `no-new-privileges`; both services bind to loopback only.
- Self-registration creates a new private household with the registering user
  as OWNER; an invitation is required to join an existing household. Household-
  scoped records and the audit trail are enforced in the application; the
  container adds no bypass. The signup flow has no email verification or request
  throttling, so keep the reverse proxy restricted to a trusted network/VPN and
  never expose the route publicly without a separate security review.
- Only item photos are sent to a configured vision provider, and only because
  the household authorizes it. No other household data leaves the host.
- Nothing here deploys publicly, purchases anything, or automates destructive
  bulk changes; those remain human actions.
