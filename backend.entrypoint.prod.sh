#!/bin/sh

# Production entrypoint for the `web` service (Gunicorn, no auto-reload).
# The RQ worker runs as its own service in compose.prod.yml, so this script
# does NOT start `rqworker`. The dev entrypoint (backend.entrypoint.sh) stays
# untouched for local `docker-compose up`.

set -e

echo "Warte auf PostgreSQL auf $DB_HOST:$DB_PORT..."
while ! pg_isready -h "$DB_HOST" -p "$DB_PORT" -q; do
  echo "PostgreSQL ist nicht erreichbar - schlafe 1 Sekunde"
  sleep 1
done
echo "PostgreSQL ist bereit - fahre fort..."

# Static assets + schema. No `makemigrations` in production: migrations are
# committed to the repo, we only apply them.
python manage.py collectstatic --noinput
python manage.py migrate --noinput

# /admin/ is publicly reachable through Caddy in production, so a superuser with
# a password that is readable in this repository is a public admin login. Refuse
# to boot instead of creating one. Keep the markers: a test extracts this block
# and runs it. (Guard is here, not in the heredoc, so the failure is loud and
# the container never reaches gunicorn.)
# --- superuser-guard-start ---
if [ -z "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
  echo "FATAL: DJANGO_SUPERUSER_PASSWORD is not set." >&2
  echo "Refusing to create a superuser with a default password: /admin/ is" >&2
  echo "publicly exposed in production. Set DJANGO_SUPERUSER_PASSWORD (and" >&2
  echo "optionally DJANGO_SUPERUSER_USERNAME / _EMAIL) in the deploy env." >&2
  exit 1
fi
# --- superuser-guard-end ---

# Idempotent superuser creation from DJANGO_SUPERUSER_* env vars.
python manage.py shell <<'EOF'
import os
from django.contrib.auth import get_user_model

User = get_user_model()
username = os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin')
email = os.environ.get('DJANGO_SUPERUSER_EMAIL', 'admin@example.com')
password = os.environ['DJANGO_SUPERUSER_PASSWORD']

if not User.objects.filter(username=username).exists():
    print(f"Creating superuser '{username}'...")
    User.objects.create_superuser(username=username, email=email, password=password)
    print(f"Superuser '{username}' created.")
else:
    print(f"Superuser '{username}' already exists.")
EOF

# No --reload in production. Worker count via GUNICORN_WORKERS (default 3;
# lower to 2 on a small VM). Threaded (gthread) workers keep the arbiter
# heartbeat firing while a request runs, so a large upload streamed through
# Caddy (unbuffered) is not aborted mid-read — with the default sync worker,
# --timeout is a hard ceiling on the whole request, including the slow body
# read, and large uploads 500 at exactly that many seconds. --timeout=120 now
# only reaps a genuinely stuck worker. Transcoding runs async in the RQ worker.
exec gunicorn core.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-3}" \
  --worker-class gthread \
  --threads 4 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -
