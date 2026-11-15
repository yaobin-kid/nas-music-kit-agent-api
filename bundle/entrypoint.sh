#!/usr/bin/env bash
set -uo pipefail

MUSIC_KIT_PORT="${PORT:-8848}"
AGENT_PORT="${AGENT_PORT:-8080}"

export MUSIC_API_BASE_URL="${MUSIC_API_BASE_URL:-http://127.0.0.1:${MUSIC_KIT_PORT}}"

echo "starting music kit on ${MUSIC_KIT_PORT}, agent api on ${AGENT_PORT}, upstream ${MUSIC_API_BASE_URL}"

gunicorn -w "${GUNICORN_WORKERS:-4}" \
  -b "0.0.0.0:${MUSIC_KIT_PORT}" \
  --chdir /app \
  --access-logfile - \
  --error-logfile - \
  app:app &
GUNICORN_PID=$!

uvicorn agent_app.main:app \
  --app-dir /agent \
  --host 0.0.0.0 \
  --port "${AGENT_PORT}" &
AGENT_PID=$!

cleanup() {
  kill -TERM "${GUNICORN_PID}" "${AGENT_PID}" 2>/dev/null || true
  wait "${GUNICORN_PID}" "${AGENT_PID}" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM

wait -n "${GUNICORN_PID}" "${AGENT_PID}"
status=$?

if [[ ${status} -ne 0 ]]; then
  echo "one of the services exited with status ${status}" >&2
fi

exit "${status}"
