FROM python:3.12-alpine

LABEL maintainer="namquoctran24@gmail.com"
LABEL version="1.0"
LABEL description="Videoflix backend (Django) on Python 3.12 / Alpine 3.21"
LABEL org.opencontainers.image.source=https://github.com/tranqn/videoflix-backend

WORKDIR /app

# Dependencies first: this layer only invalidates (and rebuilds psycopg2 from
# source / re-runs apk add ffmpeg) when requirements.txt itself changes, not
# on every source edit.
COPY requirements.txt .

RUN apk update && \
    apk add --no-cache --upgrade bash && \
    apk add --no-cache postgresql-client ffmpeg && \
    apk add --no-cache --virtual .build-deps gcc musl-dev postgresql-dev && \
    pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    apk del .build-deps

COPY . .
RUN chmod +x backend.entrypoint.sh backend.entrypoint.prod.sh

EXPOSE 8000

ENTRYPOINT [ "./backend.entrypoint.sh" ]
