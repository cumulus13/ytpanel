"""ytpanel settings. Everything environment specific comes from env vars (see .env.example)."""
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from envdot import load_env
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / '.env'
load_env(ENV_FILE)

def env_bool(name, default=False):
    return os.getenv(name, default)


def env_list(name, default=[], cast_type=list):
    return os.getenv(name, default, cast_type=cast_type)


DEBUG = env_bool("DJANGO_DEBUG", False)
TESTING = "test" in sys.argv

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG or TESTING:
        SECRET_KEY = "insecure-dev-key-do-not-use-in-production"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set (see .env.example)")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS") or ['http://127.0.0.1:8000', 'http://127.0.0.1:7788']

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "logs"
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "ytpanel.urls"
WSGI_APPLICATION = "ytpanel.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]


def _database():
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3",
                "OPTIONS": {"timeout": 30}}
    p = urlparse(url)
    if p.scheme not in ("postgres", "postgresql"):
        raise ImproperlyConfigured("DATABASE_URL must be postgres://user:pass@host:port/db")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(p.path.lstrip("/")),
        "USER": unquote(p.username or ""),
        "PASSWORD": unquote(p.password or ""),
        "HOST": p.hostname or "localhost",
        "PORT": str(p.port or 5432),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }


DATABASES = {"default": _database()}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Redis is used for the live speed/progress numbers (Django cache) and as the Celery broker.
REDIS_URL = os.getenv("TEST_REDIS_URL", "") if TESTING else os.getenv("REDIS_URL", "")
if REDIS_URL:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache",
                          "LOCATION": REDIS_URL, "KEY_PREFIX": "ytpanel"}}
else:  # dev / tests without Redis: single process only
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL or "memory://")
CELERY_TASK_DEFAULT_QUEUE = "celery"
CELERY_TASK_ROUTES = {"core.tasks.run_download_task": {"queue": "downloads"}}
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 86400 * 7}
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TIMEZONE = "UTC"
CELERY_TASK_IGNORE_RESULT = True
CELERY_BEAT_SCHEDULE = {
    "scheduler-tick": {"task": "core.tasks.scheduler_tick", "schedule": 2.0},
    "reaper": {"task": "core.tasks.reaper", "schedule": 10.0},
    "auto-purge": {"task": "core.tasks.auto_purge", "schedule": 600.0},
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# Security
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True  # the token is delivered via a <meta> tag, JS never reads the cookie
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
_secure = env_bool("DJANGO_SECURE_COOKIES", not DEBUG and not TESTING)
SESSION_COOKIE_SECURE = _secure
CSRF_COOKIE_SECURE = _secure
if env_bool("DJANGO_BEHIND_TLS_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024

# ---- ytpanel -------------------------------------------------------------------------------
YTP_DOWNLOAD_ROOT = Path(os.getenv("YTP_DOWNLOAD_ROOT", BASE_DIR / "downloads"))
YTP_HEARTBEAT_GRACE = int(os.getenv("YTP_HEARTBEAT_GRACE", "60"))   # seconds
YTP_CHECKPOINT_INTERVAL = float(os.getenv("YTP_CHECKPOINT_INTERVAL", "5"))
YTP_INNER_RETRIES = int(os.getenv("YTP_INNER_RETRIES", "3"))        # library-level retries
YTP_RUNNER_PYTHON = os.getenv("YTP_RUNNER_PYTHON", sys.executable)
YTP_MAX_LIST = 500

# LOGGING = {
#     "version": 1,
#     "disable_existing_loggers": False,
#     "handlers": {"console": {"class": "logging.StreamHandler"}},
#     "root": {"handlers": ["console"], "level": os.getenv("YTP_LOG_LEVEL", "INFO")},
# }

LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, os.getenv('DJANGO_LOG_FILE_PATH', 'debug.log'))


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'colored_verbose': {
            '()': 'colorlog.ColoredFormatter',
            'format': "%(log_color)s%(levelname)-8s%(red)s%(module)-30s%(reset)s %(blue)s%(message)s"
        },
        'verbose': {
            'format': '%(asctime)s [PID:%(process)d] [TID:%(thread)d] [%(filename)s:%(lineno)d] %(levelname)s %(message)s',
        },
    },
    'handlers': {
        'file': {
            'level': os.getenv("HANDLER_FILE_LOG_LEVEL", 'ERROR'),
            'class': 'logging.FileHandler',
            'filename': LOG_FILE,
        },
        'console': {
            'level': os.getenv("HANDLER_CONSOLE_LOG_LEVEL", 'INFO'),
            'class':'rich.logging.RichHandler',
        },
        'colored_console': {
            'level': os.getenv("HANDLER_COLORED_CONSOLE_LOG_LEVEL", 'INFO'),
            'class': 'logging.StreamHandler',
            'formatter': 'colored_verbose'
        },
        'syslog': {
            #'class': 'logging.handlers.SysLogHandler',
            'class': 'logs.handler.SysLogTextHandler',
            'address': (os.getenv("SYSLOG_PROXY_HOST", '222.222.222.7'), os.getenv("SYSLOG_PROXY_PORT", 514)),
            'formatter': os.getenv('DJANGO_SYSLOG_PROXY_LOG_FORMAT', 'verbose'),
            "env": os.getenv('DJANGO_SYSLOG_PROXY_LOG_ENV', "production"),
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': os.getenv("LOGGER_DJANGO_LOG_LEVEL", 'INFO'),
            'propagate': True,
            },
        'gunicorn.error': {
            'handlers': ['console', 'syslog'],
            'level': os.getenv("LOGGER_GUNICORN_ERROR_LOG_LEVEL", 'ERROR'),
            'propagate': True,
        },
        'gunicorn.access': {
            'handlers': ['console', 'syslog'],
            'level': os.getenv("LOGGER_GUNICORN_ACCESS_LOG_LEVEL", 'WARNING'),
            'propagate': False,
        },
        "root": {
            "handlers": ["console", 'syslog'], 
            "level": os.getenv("DJANGO_LOG_LEVEL", str(os.getenv("LOG_LEVEL", "INFO")).upper())
        },
        'celery': {
            'handlers': ['console', 'file', 'syslog'], 
            'level': 'WARNING', 
            'propagate': False, 
        },        
        'celery.worker': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'celery.task': {
            'handlers': ['console'],
            'level': 'WARNING', 
            'propagate': False,
        },
        'celery.beat': {
            'handlers': ['console'],
            'level': 'WARNING', 
            'propagate': False,
        }
    }
}

