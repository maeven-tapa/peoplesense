import os
import platform
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv('DJANGO_DEBUG', '1') == '1'
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'local-development-only-peoplesense-change-before-deployment')
if not DEBUG and SECRET_KEY.startswith('local-development'):
    raise RuntimeError('Set DJANGO_SECRET_KEY when DJANGO_DEBUG=0.')
ALLOWED_HOSTS = os.getenv('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1],testserver,10.0.0.1').split(',')
DEMO_MODE = False
if not DEBUG and DEMO_MODE:
    raise RuntimeError('Set PEOPLESENSE_DEMO=0 when DJANGO_DEBUG=0.')
INSTALLED_APPS = ['django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.humanize',
                  'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles', 'portal']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'portal.reset_lock.ResetGuardMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware',
              'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
              'django.contrib.auth.middleware.AuthenticationMiddleware', 'django.contrib.messages.middleware.MessageMiddleware', 'portal.middleware.ApplianceAccessMiddleware',
              'django.middleware.clickjacking.XFrameOptionsMiddleware']
ROOT_URLCONF = 'peoplesense.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [BASE_DIR / 'templates'],
              'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.request',
              'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages', 'portal.context.portal_context']}}]
WSGI_APPLICATION = 'peoplesense.wsgi.application'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': os.getenv('DJANGO_DB_PATH', str(BASE_DIR / 'db.sqlite3')), 'OPTIONS': {'timeout': 20}}}
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Manila'
USE_I18N = True
USE_TZ = True
STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
VISION_MODEL_DIR = BASE_DIR / 'models'
CAMERA_RUNTIME_DIR = BASE_DIR / 'runtime' / 'cameras'
CAMERA_LOG_DIR = BASE_DIR / 'logs'
SURVEILLANCE_CAPTURE_DIR = BASE_DIR / 'runtime' / 'captures'
AP_SECRET_KEY = os.getenv('PEOPLESENSE_AP_SECRET_KEY')
AP_KEY_FILE = BASE_DIR / 'runtime' / 'secrets' / 'wifi.key'
HARDWARE_ENABLED = os.getenv('PEOPLESENSE_HARDWARE', '0') == '1'
HOTSPOT_ENABLED = os.getenv('PEOPLESENSE_HOTSPOT', '0') == '1'
HARDWARE_PAN_PIN = 18  # BCM GPIO18 / physical pin 12
HARDWARE_TILT_PIN = 13  # BCM GPIO13 / physical pin 33
HARDWARE_DHT_PIN = 4  # BCM GPIO4 / physical pin 7
SERVO_MIN_ANGLE = int(os.getenv('PEOPLESENSE_SERVO_MIN', '10'))
SERVO_MAX_ANGLE = int(os.getenv('PEOPLESENSE_SERVO_MAX', '170'))
SERVO_MIN_PULSE = float(os.getenv('PEOPLESENSE_SERVO_MIN_PULSE', '0.001'))
SERVO_MAX_PULSE = float(os.getenv('PEOPLESENSE_SERVO_MAX_PULSE', '0.002'))
SERVO_PAN_REVERSED = os.getenv('PEOPLESENSE_PAN_REVERSED', '0') == '1'
SERVO_TILT_REVERSED = os.getenv('PEOPLESENSE_TILT_REVERSED', '0') == '1'
VISION_BACKEND = os.getenv('PEOPLESENSE_VISION_BACKEND', 'litert' if platform.system() == 'Linux' else 'mediapipe')
LOGIN_URL = '/'
LOGIN_REDIRECT_URL = '/setup/'
LOGOUT_REDIRECT_URL = '/'
if os.getenv('PEOPLESENSE_PROXY_HTTPS', '0') == '1':
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = os.getenv('DJANGO_SSL_REDIRECT', '1') == '1'
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
