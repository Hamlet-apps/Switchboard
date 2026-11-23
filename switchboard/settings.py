"""Switchboard settings.

Development by default (SQLite, DEBUG on). `ENVIRONMENT=production` turns DEBUG off and
requires SECRET_KEY; see .env.example for everything that can be set.
"""
import os
import sys
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env(path):
    """Read KEY=value lines from .env without overriding what the process already has."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(BASE_DIR / '.env')

PRODUCTION = os.environ.get('ENVIRONMENT', '').lower() == 'production'
TESTING = len(sys.argv) > 1 and sys.argv[1] == 'test'
DEBUG = not PRODUCTION

SECRET_KEY = os.environ.get('SECRET_KEY', '')
if not SECRET_KEY:
    if PRODUCTION:
        raise RuntimeError('Set SECRET_KEY: Switchboard also encrypts stored keys with it.')
    SECRET_KEY = 'dev-only-switchboard-key-not-for-production'

ALLOWED_HOSTS = [h.strip() for h in os.environ.get('ALLOWED_HOSTS', '').split(',') if h.strip()] or ['*']
CSRF_TRUSTED_ORIGINS = [f'https://{h}' for h in ALLOWED_HOSTS if h != '*']
# Behind the host's proxy the callback and webhook addresses we hand out must say https.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'board',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'board.middleware.SetupMiddleware',
    'board.middleware.ZoneMiddleware',
    'board.middleware.WorkerMiddleware',
]

ROOT_URLCONF = 'switchboard.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'board.context.shell',
            ],
        },
    },
]

WSGI_APPLICATION = 'switchboard.wsgi.application'

DATABASES = {
    'default': dj_database_url.config(
        default=f'sqlite:///{BASE_DIR / "db.sqlite3"}', conn_max_age=60,
    ),
}
if DATABASES['default']['ENGINE'].endswith('sqlite3'):
    # Runs execute on a worker thread next to the web requests; wait for the lock instead of failing.
    DATABASES['default'].setdefault('OPTIONS', {})['timeout'] = 20

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
]

# Own cookie names: buildings on the same host (another port, in development) would otherwise
# overwrite our session during the connect handshake.
SESSION_COOKIE_NAME = 'switchboard_session'
CSRF_COOKIE_NAME = 'switchboard_csrf'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'workflows'

LANGUAGE_CODE = 'en'
TIME_ZONE = 'UTC'  # the organization's zone is a Switchboard setting (board.models.Settings)
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage' if (DEBUG or TESTING)
        else 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

if PRODUCTION:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# --- Switchboard -------------------------------------------------------------------------
# Seconds to wait before the 2nd, 3rd… try of a step that hit a network error or a 5xx.
STEP_RETRY_WAITS = [2, 8]
# The most items a "For each" step goes through, and the most steps one run may do in all.
LOOP_LIMIT = 500
RUN_STEP_LIMIT = 5000
# A workflow that starts this many times within the window is taken to be looping and is paused.
RUNAWAY_LIMIT = 120
RUNAWAY_WINDOW = 60
# Seconds a building or outside URL gets to answer.
HTTP_TIMEOUT = 15
# Tests run workflows on the calling thread; the server hands them to the worker thread.
RUN_INLINE = TESTING
WORKER_ENABLED = os.environ.get('SWITCHBOARD_WORKER', 'on').lower() != 'off' and not TESTING
if TESTING:
    STEP_RETRY_WAITS = [0, 0]
    PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'loggers': {'board': {'handlers': ['console'], 'level': 'INFO'}},
}
