#!/bin/bash
# bash (not sh/dash): `wait -n` below needs it to supervise gunicorn + the
# rqworker without pulling in supervisord. bash is already installed by
# backend.Dockerfile.

set -e

echo "Warte auf PostgreSQL auf $DB_HOST:$DB_PORT..."

# -q für "quiet" (keine Ausgabe außer Fehlern)
# Die Schleife läuft, solange pg_isready *nicht* erfolgreich ist (Exit-Code != 0)
while ! pg_isready -h "$DB_HOST" -p "$DB_PORT" -q; do
  echo "PostgreSQL ist nicht erreichbar - schlafe 1 Sekunde"
  sleep 1
done

echo "PostgreSQL ist bereit - fahre fort..."

# Deine originalen Befehle (ohne wait_for_db)
#
# No `makemigrations` here: migrations are committed to the repo and
# reviewed like any other code change. Generating them inside a running
# container lets two developers' local containers invent divergent,
# unreviewed migrations for the same model change.
python manage.py collectstatic --noinput
python manage.py migrate

# Create a superuser using environment variables
# (Dein Superuser-Erstellungs-Code bleibt gleich)
python manage.py shell <<EOF
import os
from django.contrib.auth import get_user_model

User = get_user_model()
username = os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin')
email = os.environ.get('DJANGO_SUPERUSER_EMAIL', 'admin@example.com')
password = os.environ.get('DJANGO_SUPERUSER_PASSWORD', 'adminpassword')

if not User.objects.filter(username=username).exists():
    print(f"Creating superuser '{username}'...")
    # Korrekter Aufruf: username hier übergeben
    User.objects.create_superuser(username=username, email=email, password=password)
    print(f"Superuser '{username}' created.")
else:
    print(f"Superuser '{username}' already exists.")
EOF

# One worker for both queues is fine locally (production gives 'media' its own
# worker service, see compose.prod.yml). Queue order = priority: e-mails are
# drained before the next transcode is picked up. This list MUST cover every
# queue in settings.RQ_QUEUES — an enqueue to a queue nobody consumes never
# errors, the job just sits there forever.
python manage.py rqworker default media &
WORKER_PID=$!

# gunicorn also runs backgrounded (not `exec`) so `wait -n` below can watch
# both of them: an unsupervised worker that dies would otherwise leave the
# container running and healthy while jobs pile up on the queue forever.
gunicorn core.wsgi:application --bind 0.0.0.0:8000 --reload &
GUNICORN_PID=$!

# Whichever of the two exits first ends the container with its exit status,
# so a crashed worker actually stops the container instead of going unnoticed.
wait -n "$WORKER_PID" "$GUNICORN_PID"
exit $?
