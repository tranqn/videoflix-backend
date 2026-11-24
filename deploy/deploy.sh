#!/usr/bin/env bash
#
# deploy.sh — put a CI-built image live on the server, gate it on health, and
# roll back when it is SAFE to roll back.
#
#   deploy.sh ghcr.io/tranqn/videoflix-backend:<full-commit-sha>
#
# This script never builds anything. compose.prod.yml has no `build:` key, so
# the only thing that can happen here is "pull the exact artifact CI tested and
# run it". The image reference must be the immutable SHA tag; `:latest` is for
# humans reading the package page, not for deployments.
#
# EXPECTED LAYOUT (the compose file only works from it — it mounts ../frontend)
#
#   ~/videoflix/                     <- umbrella repo
#     backend/                       <- this repo, a submodule
#       compose.prod.yml
#       .env                         <- the ONLY copy of production config
#       deploy/deploy.sh             <- you are here
#     frontend/                      <- sibling submodule, mounted into Caddy
#     .videoflix-deploy-state        <- last KNOWN-GOOD (image, commit)
#     .videoflix-rollback-target     <- what was running when this run started
#
# The two state files live OUTSIDE the backend repo on purpose: the umbrella's
# deploy moves the submodule around (checkout/clean), and the one thing that
# must survive that is the record of what was last known good.
#
# CREDENTIALS: none. The GHCR package is public, so the server pulls
# anonymously and there is no registry secret on this box to leak or rotate.
# (If the package is ever made private, a read-only token has to be installed
# here and `docker login` added before the pull — weigh that deliberately.)
# The cross-repo PAT `UMBRELLA_DISPATCH_TOKEN` lives in GitHub Actions, not
# here; see .github/workflows/release.yml.
#
# EXIT CODES (the umbrella's workflow keys off these)
#   0  deployed and healthy
#   1  deploy failed, rolled back automatically, stack serving the old version
#   2  refused to start (bad arguments / missing production config)
#   3  deploy failed and rollback was REFUSED — a human must act now
#
set -Eeuo pipefail

# --- ALWAYS run from the repository root. DO NOT "TIDY" THIS AWAY. -----------
# Compose derives its project name from the basename of the working directory
# ("backend"). Run this from anywhere else and every volume gets a different
# project prefix: `videoflix_media` resolves to a NEW, EMPTY volume and the
# entire transcoded library appears to have vanished. The data is still there,
# attached to the old project name, but the stack serving traffic can no longer
# see it — and the first instinct in that moment is to re-upload everything.
cd "$(dirname "$0")/.."
REPO_DIR="$PWD"
STATE_DIR="$(dirname "$REPO_DIR")"

COMPOSE_FILE="compose.prod.yml"
WEB_CONTAINER="videoflix_backend"
DB_CONTAINER="videoflix_database"
STATE_FILE="${DEPLOY_STATE_FILE:-$STATE_DIR/.videoflix-deploy-state}"
ROLLBACK_FILE="${DEPLOY_ROLLBACK_FILE:-$STATE_DIR/.videoflix-rollback-target}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-240}"   # covers migrate + collectstatic on boot
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"

log()  { printf '[deploy] %s\n' "$*"; }
warn() { printf '[deploy] WARNING: %s\n' "$*" >&2; }
die()  { printf '[deploy] FATAL: %s\n' "$1" >&2; exit "${2:-2}"; }
# Only ever used to frame failures, so it goes to stderr with them.
rule() { printf '%s\n' '--------------------------------------------------------------------------' >&2; }

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
if [ "$#" -ne 1 ]; then
  printf 'usage: %s <full-image-ref>\n' "$0" >&2
  printf '  e.g. %s ghcr.io/tranqn/videoflix-backend:0123abc...\n' "$0" >&2
  exit 2
fi
IMAGE_REF="$1"

# A deploy pinned to a moving tag is not reproducible and cannot be rolled back
# to with any confidence: the same string means different bytes tomorrow.
if [ "${IMAGE_REF##*:}" = "latest" ] && [ "${ALLOW_MUTABLE_TAG:-0}" != "1" ]; then
  die "refusing to deploy ':latest'. Use the immutable :<full-commit-sha> tag (set ALLOW_MUTABLE_TAG=1 to override)."
fi

# The image reference arrives from outside this box — ultimately from a
# repository_dispatch payload. Whoever can send that dispatch would otherwise
# choose ANY image, and this script would pull it and run it under a user that
# is in the docker group, which is root-equivalent on this host. So a leaked
# dispatch token would be arbitrary code execution rather than "a bad version of
# our own app". Pin the registry path: the only thing deployable here is an
# image this project's own pipeline built and pushed.
EXPECTED_IMAGE_PREFIX="${EXPECTED_IMAGE_PREFIX:-ghcr.io/tranqn/videoflix-backend:}"
case "$IMAGE_REF" in
  "$EXPECTED_IMAGE_PREFIX"?*) : ;;
  *) die "refusing to deploy '$IMAGE_REF': only ${EXPECTED_IMAGE_PREFIX}<tag> may be deployed here." ;;
esac

# ---------------------------------------------------------------------------
# Preconditions — checked BEFORE the running stack is touched
# ---------------------------------------------------------------------------
# .env on this box is the only copy of production config. Nothing in CI or CD
# creates it, restores it, or would notice it being empty: compose would simply
# start Django with an empty SECRET_KEY / no DB password and the failure would
# surface later, as a half-broken stack, with the old one already gone.

# Read a value from .env WITHOUT sourcing it. Sourcing production config would
# execute whatever is in it, and one stray backtick would run as root's shell.
env_value() {
  local key="$1" line
  line="$(grep -E "^[[:space:]]*${key}=" .env | tail -n 1 || true)"
  [ -n "$line" ] || return 0
  line="${line#*=}"
  case "$line" in
    \"*\") line="${line#\"}"; line="${line%\"}" ;;
    \'*\') line="${line#\'}"; line="${line%\'}" ;;
  esac
  printf '%s' "$line"
}

[ -f "$COMPOSE_FILE" ] || die "$COMPOSE_FILE not found in $REPO_DIR — wrong directory?"
[ -f .env ] || die ".env not found in $REPO_DIR. Production config is NOT in git and NOT created by CD; restore it before deploying."

for key in SECRET_KEY SITE_ADDRESS DB_PASSWORD; do
  [ -n "$(env_value "$key")" ] || die "$key is missing or empty in $REPO_DIR/.env. Refusing to deploy against incomplete production config."
done
SITE_ADDRESS="$(env_value SITE_ADDRESS)"
DB_NAME="$(env_value DB_NAME)"
DB_USER="$(env_value DB_USER)"
DB_PASSWORD="$(env_value DB_PASSWORD)"

command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is not available"

# ---------------------------------------------------------------------------
# Migration bookkeeping
# ---------------------------------------------------------------------------
# backend.entrypoint.prod.sh runs `migrate` on EVERY container start. So by the
# time a health check fails, the new schema is already applied — and putting
# the old image back would run OLD code against a NEWER schema. That is the one
# failure mode this script must never cause on its own, so it measures whether
# this deploy actually applied anything.
#
# The count is read from the DATABASE, not from `manage.py showmigrations`,
# because it has to mean the same thing before and after: showmigrations only
# lists the migration files the *running code* knows about, so old code and new
# code answer different questions. An empty result means "unknown", and unknown
# is treated exactly like "migrations were applied" — refuse to roll back.
applied_migration_count() {
  local out
  [ -n "$DB_NAME" ] && [ -n "$DB_USER" ] || return 0
  out="$(docker exec -e PGPASSWORD="$DB_PASSWORD" "$DB_CONTAINER" \
        psql -U "$DB_USER" -d "$DB_NAME" -tAc \
        'SELECT count(*) FROM django_migrations' 2>/dev/null | tr -d '[:space:]' || true)"
  case "$out" in
    ''|*[!0-9]*) return 0 ;;
    *) printf '%s' "$out" ;;
  esac
}

# ---------------------------------------------------------------------------
# Health gate
# ---------------------------------------------------------------------------
# Probes the app from INSIDE its own container: port 8000 is `expose`d, not
# published, so the host cannot reach it, and going in via the public HTTPS
# name would depend on DNS hairpinning from the VM back to itself.
#
#   Host:              ALLOWED_HOSTS is domain-only in production, so an
#                      unqualified request gets a DisallowedHost 400.
#   X-Forwarded-Proto: what Caddy sets. Without it SECURE_SSL_REDIRECT answers
#                      301 for every path except the health endpoint (which is
#                      in SECURE_REDIRECT_EXEMPT), so /api/video/ would report
#                      301 instead of the 401 we are asserting.
#   no redirects:      a followed 301 would leave the container and hit the
#                      real public site — the probe must stay local.
probe() {
  docker exec -i -e PROBE_HOST="$SITE_ADDRESS" -e PROBE_PATH="$1" "$WEB_CONTAINER" \
    python - <<'PY' 2>/dev/null || true
import os
import sys
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


request = urllib.request.Request(
    'http://127.0.0.1:8000' + os.environ['PROBE_PATH'],
    headers={
        'Host': os.environ['PROBE_HOST'],
        'X-Forwarded-Proto': 'https',
    },
)
opener = urllib.request.build_opener(NoRedirect)
try:
    with opener.open(request, timeout=10) as response:
        print(response.status)
except urllib.error.HTTPError as exc:
    print(exc.code)
except Exception:
    sys.exit(1)
PY
}

expect_status() {  # expect_status <path> <expected-code> <what it proves>
  local path="$1" want="$2" why="$3" got
  got="$(probe "$path")"
  if [ "$got" != "$want" ]; then
    warn "$path returned '${got:-no response}', expected $want ($why)"
    return 1
  fi
  log "ok: $path -> $want ($why)"
}

# --- assertions ------------------------------------------------------------
# Each assertion is a function returning 0/1 and printing its own diagnosis.
# To add one, write the function and append its name to POST_HEALTH_ASSERTIONS.
# Nothing else changes.

assert_health_endpoint() {
  expect_status /api/health/ 200 'database, cache and media-queue worker all reachable'
}

assert_running_image() {
  # Compare image IDs, not name strings: the ID is what the container was
  # actually created from, so this cannot produce a false alarm (and a false
  # alarm here would roll back a perfectly healthy deploy).
  local running wanted
  running="$(docker inspect -f '{{.Image}}' "$WEB_CONTAINER" 2>/dev/null || true)"
  wanted="$(docker image inspect -f '{{.Id}}' "$IMAGE_REF" 2>/dev/null || true)"
  if [ -z "$running" ] || [ -z "$wanted" ] || [ "$running" != "$wanted" ]; then
    warn "web is not running $IMAGE_REF (container image ${running:-unknown}, expected ${wanted:-unknown}) — it was not recreated"
    return 1
  fi
  log "ok: web is running $IMAGE_REF"
}

assert_video_requires_auth() {
  # 401 and not 200, 500 or 301: proves the URL conf, DRF authentication and
  # the database are all up, i.e. that the process is serving the application
  # and not merely alive.
  expect_status /api/video/ 401 'auth and routing are up, not just the process'
}

POST_HEALTH_ASSERTIONS=(assert_running_image assert_video_requires_auth)

wait_for_health() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  log "waiting up to ${HEALTH_TIMEOUT}s for /api/health/ (migrate + collectstatic run on boot)..."
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ "$(probe /api/health/)" = "200" ]; then
      log "healthy after $((HEALTH_TIMEOUT - (deadline - SECONDS)))s"
      return 0
    fi
    sleep "$HEALTH_INTERVAL"
  done
  warn "/api/health/ did not return 200 within ${HEALTH_TIMEOUT}s"
  dump_diagnostics
  return 1
}

# Printed straight into the CD log, because that is the only place anyone will
# look first — and on a health-gate failure the reason is almost always in the
# last few lines of the app's own output.
dump_diagnostics() {
  warn "--- last 60 log lines per service ---"
  docker compose -f "$COMPOSE_FILE" logs --tail=60 web worker media_worker >&2 || true
  warn "--- container status ---"
  docker compose -f "$COMPOSE_FILE" ps >&2 || true
}

run_post_health_assertions() {
  local assertion failed=0
  # Every assertion runs even after one fails: two symptoms diagnose a lot
  # faster than the first one alone.
  for assertion in "${POST_HEALTH_ASSERTIONS[@]}"; do
    "$assertion" || failed=1
  done
  [ "$failed" -eq 0 ] || dump_diagnostics
  return "$failed"
}

# ---------------------------------------------------------------------------
# Capture the rollback target BEFORE anything is changed
# ---------------------------------------------------------------------------
# The image comes from `docker inspect`, not from a file and not from git:
# it is literally what is serving traffic right now, which is the only
# definition of "the thing that worked" that cannot be out of date.
PREV_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$WEB_CONTAINER" 2>/dev/null || true)"

# The commit CANNOT come from `git rev-parse HEAD`: the umbrella's deploy
# checks this submodule out to the NEW sha before calling this script, so HEAD
# is already the new code. The previous commit is only knowable from what the
# previous successful run recorded.
#
# It matters because compose.prod.yml, the Caddyfile and the entrypoints ship
# in this repo. Rolling back the image alone would run the OLD application
# against the NEW config — a state nobody has ever tested and which looks like
# a successful rollback right up until the first request that touches whatever
# changed.
state_get() {
  [ -f "$STATE_FILE" ] || return 0
  local line
  line="$(grep -E "^$1=" "$STATE_FILE" | tail -n 1 || true)"
  printf '%s' "${line#*=}"
}
PREV_COMMIT="$(state_get commit)"
NEW_COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"

write_kv() {  # write_kv <file> <image> <commit>
  local file="$1" tmp
  tmp="$(mktemp "${file}.XXXXXX")" || { warn "cannot write $file"; return 0; }
  {
    printf 'image=%s\n' "$2"
    printf 'commit=%s\n' "$3"
    printf 'recorded_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$tmp"
  mv -f "$tmp" "$file" || warn "cannot update $file"
}

log "deploying $IMAGE_REF"
log "  previous image:  ${PREV_IMAGE:-<none>}"
log "  previous commit: ${PREV_COMMIT:-<unknown>}"
log "  new commit:      ${NEW_COMMIT:-<unknown>}"

# Mirrored to disk so a human can recover by hand even if this script is killed
# mid-deploy (SSH drop, OOM, CI timeout).
write_kv "$ROLLBACK_FILE" "${PREV_IMAGE:-}" "${PREV_COMMIT:-}"

MIGRATIONS_BEFORE="$(applied_migration_count)"
log "  applied migrations before: ${MIGRATIONS_BEFORE:-<unknown>}"

# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------
print_runbook() {  # print_runbook <sentence describing the current state>
  {
    printf '%s\n\n' "$1"
    printf '  new image:       %s\n' "$IMAGE_REF"
    printf '  new commit:      %s\n' "${NEW_COMMIT:-<unknown>}"
    printf '  previous image:  %s\n' "${PREV_IMAGE:-<none recorded>}"
    printf '  previous commit: %s\n' "${PREV_COMMIT:-<none recorded>}"
    printf '  rollback record: %s\n\n' "$ROLLBACK_FILE"
    printf 'Look first:\n'
    printf '  cd %s\n' "$REPO_DIR"
    printf '  docker compose -f %s logs --tail=200 web worker media_worker\n' "$COMPOSE_FILE"
    printf '  docker compose -f %s ps\n\n' "$COMPOSE_FILE"
    printf 'FIXING FORWARD IS THE PREFERRED OPTION. Rolling the image back on its\n'
    printf 'own would run the old code against the schema this deploy already\n'
    printf 'migrated, and against the new compose/Caddy config in the working tree.\n\n'
    printf 'If you must roll back, do BOTH halves, in this order, by hand:\n'
    printf '  1. reverse the migrations this deploy applied:\n'
    printf '       docker compose -f %s exec web python manage.py showmigrations\n' "$COMPOSE_FILE"
    printf '       docker compose -f %s exec web python manage.py migrate <app> <previous_migration>\n' "$COMPOSE_FILE"
    printf '     (check first that they are reversible AND that no data was lost;\n'
    printf '      take a database dump before you touch anything)\n'
    printf '  2. restore code and image together:\n'
    printf '       git -C %s checkout --detach %s\n' "$REPO_DIR" "${PREV_COMMIT:-<previous_commit>}"
    printf '       WEB_IMAGE=%s docker compose -f %s up -d\n' "${PREV_IMAGE:-<previous_image>}" "$COMPOSE_FILE"
    printf '     (this leaves the submodule detached at the old commit, so the\n'
    printf '      umbrella repo will report it as modified until you re-point it)\n'
  } >&2
}

refuse_rollback() {  # refuse_rollback <reason>
  rule
  printf '[deploy] ROLLBACK REFUSED - MANUAL ACTION REQUIRED\n' >&2
  printf 'Reason: %s\n' "$1" >&2
  rule
  print_runbook 'The stack has been LEFT AS IS, running the new image, so nothing is destroyed while you look at it. It is very likely serving errors.'
  rule
  exit 3
}

do_rollback() {
  log "rolling back to image ${PREV_IMAGE} at commit ${PREV_COMMIT}"

  # Code and image are restored as a PAIR. A half-rollback (old image, new
  # compose/Caddyfile/entrypoint) is a configuration nobody has tested, so if
  # the code half cannot be restored the rollback does not happen at all.
  if ! git -c advice.detachedHead=false checkout --detach "$PREV_COMMIT" >/dev/null 2>&1; then
    refuse_rollback "cannot check out the previous commit ${PREV_COMMIT} (dirty working tree, or the object is missing). Restoring the image alone would leave the old application running against the new compose/Caddy config."
  fi

  if ! WEB_IMAGE="$PREV_IMAGE" docker compose -f "$COMPOSE_FILE" up -d; then
    rule
    printf '[deploy] THE ROLLBACK ITSELF FAILED - MANUAL ACTION REQUIRED\n' >&2
    rule
    dump_diagnostics
    print_runbook 'The working tree is back at the previous commit but the stack did NOT come up. The site is down. No migrations were applied by this deploy, so restoring the previous image by hand is safe.'
    rule
    exit 3
  fi

  if wait_for_health; then
    log "rollback complete: ${PREV_IMAGE} at ${PREV_COMMIT} is healthy again"
  else
    warn "rolled back, but the previous version is not reporting healthy either — investigate the database/redis, not the image"
  fi
  log "the failed image ${IMAGE_REF} was NOT deleted; it is still available for debugging"
  exit 1
}

handle_failure() {  # handle_failure <reason>
  warn "$1"
  local after
  after="$(applied_migration_count)"
  log "applied migrations: before=${MIGRATIONS_BEFORE:-<unknown>} after=${after:-<unknown>}"

  if [ -z "$PREV_IMAGE" ]; then
    refuse_rollback "there is no previous deployment to roll back to (nothing was running before this deploy)."
  fi
  if [ -z "$PREV_COMMIT" ]; then
    refuse_rollback "no previous commit was recorded in $STATE_FILE, so the code half of the rollback cannot be restored. This is expected on the FIRST deploy through this script; every later one records it."
  fi
  if [ -z "$MIGRATIONS_BEFORE" ] || [ -z "$after" ]; then
    refuse_rollback "could not determine whether this deploy applied migrations (the database was unreachable for the check). Unknown is treated as 'yes' on purpose."
  fi
  if [ "$after" -ne "$MIGRATIONS_BEFORE" ]; then
    refuse_rollback "this deploy applied $((after - MIGRATIONS_BEFORE)) migration(s). Rolling the image back would run the old code against the new schema; silent data corruption is worse than a loud outage."
  fi

  log "no migrations were applied by this deploy — automatic rollback is safe"
  do_rollback
}

# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------
# Pull FIRST and on its own: if the image does not exist or the registry is
# unreachable, nothing has been touched yet and the running stack is untouched.
log "pulling $IMAGE_REF"
docker pull "$IMAGE_REF" || die "could not pull $IMAGE_REF — the running stack was not touched" 2

export WEB_IMAGE="$IMAGE_REF"
log "starting the new stack"
if ! docker compose -f "$COMPOSE_FILE" up -d --remove-orphans; then
  handle_failure "docker compose up -d failed"
fi

if wait_for_health && run_post_health_assertions; then
  :
else
  handle_failure "the health gate failed for $IMAGE_REF"
fi

# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------
# Only now, and only now. `docker image prune -f` (no -a) removes DANGLING
# images only — untagged and unreferenced — so it cannot delete the rollback
# target, which is tagged with its own commit SHA. Pruning before the gate
# would be the one command able to destroy the way back.
log "health gate passed; pruning dangling images"
docker image prune -f >/dev/null || warn "prune failed (harmless)"

write_kv "$STATE_FILE" "$IMAGE_REF" "${NEW_COMMIT:-}"
log "deployed $IMAGE_REF (commit ${NEW_COMMIT:-<unknown>}) — healthy"
