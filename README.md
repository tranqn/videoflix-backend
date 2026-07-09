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

- **Django 5 / Django REST Framework** — REST API
- **PostgreSQL** — database
- **Redis** — cache layer and Django-RQ broker
- **Django-RQ** — background jobs (HLS transcoding, e-mail delivery)
- **FFmpeg** — thumbnail generation and HLS transcoding
- **SimpleJWT** — JWT auth delivered as HttpOnly cookies
- **Gunicorn + WhiteNoise** — production serving
- **Docker / docker-compose** — fully containerised setup

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
VS Code *Live Server* extension pointed at `../frontend/`) — use `127.0.0.1`, not
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

All `/api/video/` endpoints require authentication.

## Running the tests

```bash
docker-compose exec web python manage.py test
```

The test suite uses an in-memory SQLite database, a local-memory cache and
synchronous jobs, so it needs no external services.
