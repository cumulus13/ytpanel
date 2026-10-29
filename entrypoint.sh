#!/bin/sh
# Usage: entrypoint.sh web | worker | ctl | beat
set -e
case "$1" in
  web)
    python manage.py migrate --noinput
    python manage.py bootstrap
    # gthread: every open /api/v1/events stream (mobile app) occupies one thread for its lifetime
    exec gunicorn ytpanel.wsgi -b 0.0.0.0:8000 -k gthread -w "${GUNICORN_WORKERS:-3}" --threads "${GUNICORN_THREADS:-8}" --access-logfile - --timeout 60 --graceful-timeout 20
    ;;
  worker)  # runs the wget processes, one celery slot per concurrent download
    mkdir -p "$YTP_DOWNLOAD_ROOT" "$YTP_LOG_DIR"
    exec celery -A ytpanel worker -Q "${YTP_QUEUE_NAME:-downloads}" -n "dl@%h" -c "${YTP_WORKER_CONCURRENCY:-8}" --loglevel "${YTP_WORKER_LOG_LEVEL:-INFO}"
    ;;
  ctl)     # scheduler ticks, reaper, maintenance
    exec celery -A ytpanel worker -Q "${YTP_QUEUE_NAME:-celery}" -n "ctl@%h" -c 2 --loglevel "${YTP_SCHEDULER_LOG_LEVEL:-INFO}"
    ;;
  beat)
    exec celery -A ytpanel beat --loglevel "${YTP_BEAT_LOG_LEVEL:-INFO}" -s /tmp/celerybeat-schedule
    ;;
  *) exec "$@" ;;
esac
