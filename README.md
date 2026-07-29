# Videoflix Backend

REST API backend for **Videoflix**, a video-streaming platform. It powers user
authentication (with e-mail activation and password reset) and serves videos as
adaptive **HLS** streams in 480p, 720p and 1080p. Built with Django and the
Django REST Framework; uploaded videos are transcoded in the background with
FFmpeg via Django-RQ.

The frontend is the Vanilla-JS app provided by the Developer Akademie
([project.Videoflix](https://github.com/Developer-Akademie-Backendkurs/project.Videoflix)),
maintained as a **separate repository**. For local development it sits next to
this backend as a sibling folder (`../frontend/`). It talks to this backend over
the REST API and authenticates via JWTs stored in HttpOnly cookies.

## Tech stack

- **Django 5 / Django REST Framework**: REST API
- **PostgreSQL**: database
- **Redis**: cache layer and Django-RQ broker
- **Django-RQ**: background jobs on two queues: `default` for e-mail,
  `media` for HLS transcoding, each with its own worker in production so a
  long transcode never delays an activation e-mail
- **FFmpeg**: thumbnail generation and HLS transcoding
- **SimpleJWT**: JWT auth delivered as HttpOnly cookies
- **Gunicorn + WhiteNoise**: production serving
- **Caddy**: production only: automatic HTTPS, and serves HLS playlists/segments
  directly off the media volume, asking Django only for authorization (see
  [HLS delivery](#hls-delivery-dev-vs-production) below)
- **Docker / docker-compose**: fully containerised setup

## Prerequisites

- [Docker](https://docs.docker.com/compose/install/) with `docker-compose`
- [git](https://git-scm.com/downloads)

## Quickstart

```bash
# 1. Clone this backend repository and enter it
git clone <your-backend-repo-url> && cd backend

# 2. Create the environment file and fill in the values
cp .env.template .env
#    Set at least: SECRET_KEY, DB_NAME / DB_USER / DB_PASSWORD,
#    the EMAIL_* SMTP credentials, and (if needed) CORS_ALLOWED_ORIGINS.

# 3. Build and start everything (database, redis, web + RQ worker)
docker-compose up --build
```

The API is then available at <http://127.0.0.1:8000/>. The entrypoint runs the
migrations and creates the admin superuser automatically from the
`DJANGO_SUPERUSER_*` variables in `.env`.

- **Admin:** <http://127.0.0.1:8000/admin/>
- **Background jobs dashboard:** <http://127.0.0.1:8000/django-rq/>

## Contributing

Install the dev tooling and arm the git hooks once per clone (cloning alone
does not do it):

```bash
pip install -r requirements-dev.txt
lefthook install
```

`lefthook.yml` then runs `ruff format` + `ruff check --fix` on staged files
before each commit, checks the commit message is a Conventional Commit, and
runs `mypy` + `pytest` before each push.

## Adding videos

1. Open the admin and add a **Video** (title, description, category) with a
   source file (e.g. an MP4).
2. On save, a background job generates a thumbnail and transcodes the video to
   HLS in all three resolutions. Watch its progress on the RQ dashboard; the
   video's status flips to **Ready** when finished.
3. The video then appears in `GET /api/video/` and can be streamed.

## Running the frontend locally

The frontend (the sibling `../frontend/` repository) expects the API at
`http://127.0.0.1:8000/api/`. Serve it on **`http://127.0.0.1:5500`** (e.g. the
VS Code *Live Server* extension pointed at `../frontend/`), use `127.0.0.1`, not
`localhost`, so the auth cookies set by the API are sent back.
`CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS` already allow that origin.

## API endpoints

| Method | Path | Description |
| ------ | ---- | ----------- |
| POST | `/api/register/` | Register a new (inactive) user; sends an activation e-mail |
| GET  | `/api/activate/<uidb64>/<token>/` | Activate an account |
| POST | `/api/login/` | Log in; sets `access_token` & `refresh_token` cookies |
| POST | `/api/logout/` | Log out; blacklists the refresh token and clears cookies |
| POST | `/api/token/refresh/` | Issue a new access token from the refresh cookie |
| POST | `/api/password_reset/` | Send a password-reset e-mail |
| POST | `/api/password_confirm/<uidb64>/<token>/` | Set a new password |
| GET  | `/api/video/` | List all ready videos (newest first) |
| GET  | `/api/video/<id>/<resolution>/index.m3u8` | HLS playlist for a resolution |
| GET  | `/api/video/<id>/<resolution>/<segment>` | HLS `.ts` segment |
| GET  | `/api/health/` | Health check: database, cache, media-queue worker (no auth) |

All `/api/video/` endpoints require authentication.

## HLS delivery: dev vs. production

The two HLS routes above resolve identically from the client's point of view,
but a different process actually serves the bytes:

- **Development** (`docker-compose.yml`, no Caddy): the Django views above
  serve both the playlist and the segments directly, exactly as before.
- **Production** (`compose.prod.yml`, behind Caddy): Caddy serves playlists and
  segments straight off the shared media volume and never wakes Gunicorn for
  the payload. It still asks Django to authorize each request first, via a
  `forward_auth` subrequest to `/internal/hls-auth/` (see `core/views.py` and
  `caddy/Caddyfile`) that carries the original request's cookies; a 2xx lets
  Caddy serve the file, anything else is returned to the client verbatim. This
  keeps a 6-second segment fetch from ever costing a Gunicorn worker.

## Running the tests

`pytest` is canonical (config lives in `pyproject.toml`); it finds 163 tests.
`python manage.py test` also runs, but Django's unittest loader does not
discover this project's plain-class pytest tests and only finds around 146 of
them, so it is not a substitute for a real run.

Set up a local virtualenv once:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

Then run the suite:

```bash
.venv/bin/pytest
```

A subset of tests is marked `ffmpeg`: real end-to-end tests that shell out to
`ffmpeg`/`ffprobe` to prove the on-disk HLS layout the streaming views and
Caddy depend on. Install ffmpeg (e.g. `brew install ffmpeg`) to run them; if
it is not on `PATH`, deselect them instead of getting silent gaps:

```bash
.venv/bin/pytest -m "not ffmpeg"
```

The suite uses an in-memory SQLite database, a local-memory cache and
synchronous jobs, so it needs no external services (Postgres, Redis) beyond
what pip installs.

## Deployment

Production deploys go through CI (image build + push to GHCR) and the
umbrella repo's dispatch, never a manual `docker compose up` against
`compose.prod.yml`. See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the
full flow, how to deploy a specific commit, what `deploy.sh`'s exit codes
mean, and how to roll back.
