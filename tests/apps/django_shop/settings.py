import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SECRET_KEY = "tests"
DEBUG = False
ALLOWED_HOSTS = ["*"]
USE_TZ = True
ROOT_URLCONF = "apps.django_shop.urls"
INSTALLED_APPS = ["django.contrib.contenttypes", "slowpoke.django", "apps.django_shop"]
MIDDLEWARE = ["slowpoke.django.SlowpokeMiddleware", "django.middleware.common.CommonMiddleware"]
DATABASES = {
    # Shared-cache in-memory SQLite: the async view's thread sees the same database.
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "file:slowpoke_shop?mode=memory&cache=shared"},
}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
LOGGING_CONFIG = None
