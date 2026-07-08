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

# Idempotent superuser creation from DJANGO_SUPERUSER_* env vars.
python manage.py shell <<'EOF'
import os
from django.contrib.auth import get_user_model

User = get_user_model()
username = os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin')
email = os.environ.get('DJANGO_SUPERUSER_EMAIL', 'admin@example.com')
password = os.environ.get('DJANGO_SUPERUSER_PASSWORD', 'adminpassword')

if not User.objects.filter(username=username).exists():
    print(f"Creating superuser '{username}'...")
    User.objects.create_superuser(username=username, email=email, password=password)
    print(f"Superuser '{username}' created.")
else:
    print(f"Superuser '{username}' already exists.")
EOF

# No --reload in production. Worker count via GUNICORN_WORKERS (default 3;
# lower to 2 on a 1 GB VM). --timeout raised so long HLS responses don't get cut.
exec gunicorn core.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-3}" \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -
