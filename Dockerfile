FROM python:3.12-slim

ARG MUSIC_API_BASE_URL=
ARG APP_VERSION=0.5.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    MUSIC_API_BASE_URL=${MUSIC_API_BASE_URL} \
    APP_VERSION=${APP_VERSION}

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=10001:10001 app ./app

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/healthz',timeout=3)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]

LABEL org.opencontainers.image.source="https://github.com/yaobin-kid/nas-music-kit-agent-api"
LABEL org.opencontainers.image.description="NAS Music Kit search/download API wrapper"
