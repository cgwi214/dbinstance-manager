"""Django settings —— 数据库实例管理系统。

设计原则：所有环境相关配置（数据库、broker、加密密钥）都从环境变量读取，
同一份代码既能在本机用 sqlite + eager Celery 直接跑，也能在生产用
PostgreSQL + Redis + 独立 worker/beat。
"""
from __future__ import annotations

import os
from pathlib import Path

from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# .env 支持（不引入 python-dotenv，20 行够用）
# --------------------------------------------------------------------------- #
def load_env_file(path: Path) -> None:
    """把 KEY=VALUE 读进 os.environ（不覆盖已存在的环境变量）。

    优先级：真实环境变量 > .env 文件。这样容器里用 `-e` 注入的配置不会被文件顶掉。
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(BASE_DIR / ".env")


# --------------------------------------------------------------------------- #
# 环境变量小工具
# --------------------------------------------------------------------------- #
def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = env(name)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    raw = env(name)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me-in-production")
DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "inventory",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # 放在最前面（仅次于安全中间件），才能覆盖到后续所有中间件与视图的耗时
    "inventory.middleware.RequestTimingMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #
if env("DB_ENGINE", "sqlite") in {"postgres", "postgresql"}:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("DB_NAME", "dbinstance"),
            "USER": env("DB_USER", "postgres"),
            "PASSWORD": env("DB_PASSWORD", ""),
            "HOST": env("DB_HOST", "127.0.0.1"),
            "PORT": env("DB_PORT", "5432"),
            "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 60),
            # 不加 connect_timeout 的话，库不可达时请求会一直挂着（psycopg 默认无限等），
            # 表现为"服务没报错但也不响应"，排查成本很高。这里给个短超时快速失败。
            "OPTIONS": {"connect_timeout": env_int("DB_CONNECT_TIMEOUT", 5)},
        }
    }
else:
    DB_PATH = Path(env("DB_PATH", str(BASE_DIR / "var" / "db.sqlite3")))
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": DB_PATH,
            "OPTIONS": {"timeout": 20},
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------- #
# 密码字段加密
# --------------------------------------------------------------------------- #
# 支持多个密钥（逗号分隔）：第一个用于加密，其余仅用于解密历史数据，
# 便于做密钥轮换。留空时 DEBUG 下从 SECRET_KEY 派生，生产环境必须显式配置。
INSTANCE_ENCRYPTION_KEYS = [
    k.strip() for k in env("INSTANCE_ENCRYPTION_KEYS", "").split(",") if k.strip()
]
INSTANCE_PASSWORD_LENGTH = env_int("INSTANCE_PASSWORD_LENGTH", 24)
INSTANCE_PASSWORD_ROTATION_HOURS = env_int("INSTANCE_PASSWORD_ROTATION_HOURS", 12)


# --------------------------------------------------------------------------- #
# DRF
# --------------------------------------------------------------------------- #
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "DEFAULT_PAGINATION_CLASS": "inventory.pagination.StandardPagination",
    "PAGE_SIZE": env_int("API_PAGE_SIZE", 20),
    # Token 认证给脚本/CI 用；Session 认证方便浏览器里的可浏览 API 调试
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["inventory.permissions.ApiAccessPermission"],
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

# 鉴权总开关：默认关闭，保证「装好就能跑」；生产必须置 1
API_AUTH_REQUIRED = env_bool("API_AUTH_REQUIRED", False)


# --------------------------------------------------------------------------- #
# Celery
# --------------------------------------------------------------------------- #
CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")
# 本地/测试无 Redis 时置 CELERY_TASK_ALWAYS_EAGER=1，任务同步执行
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = env("CELERY_TIMEZONE", "Asia/Shanghai")
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 30
CELERY_TASK_SOFT_TIME_LIMIT = 60 * 25
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200

CELERY_BEAT_SCHEDULE = {
    # 需求 3：每 12 小时轮换一次（00:00 / 12:00）
    "rotate-instance-passwords": {
        "task": "inventory.tasks.rotate_all_instance_passwords",
        "schedule": crontab(minute=0, hour="*/12"),
        "options": {"expires": 60 * 60 * 6},
    },
    # 需求 4：每天 00:00 统计
    "collect-daily-instance-stats": {
        "task": "inventory.tasks.collect_daily_instance_stats",
        "schedule": crontab(minute=0, hour=0),
        "options": {"expires": 60 * 60 * 6},
    },
}


# --------------------------------------------------------------------------- #
# 请求耗时中间件
# --------------------------------------------------------------------------- #
REQUEST_TIMING = {
    "header": "X-Request-Duration-Ms",
    # 是否把每次请求落库（高 QPS 场景可关掉或降低采样率）
    "persist": env_bool("REQUEST_TIMING_PERSIST", True),
    "sample_rate": env_float("REQUEST_TIMING_SAMPLE_RATE", 1.0),
    "slow_threshold_ms": env_float("REQUEST_TIMING_SLOW_MS", 500.0),
    "skip_prefixes": ("/static/", "/media/", "/favicon.ico", "/admin/jsi18n/"),
}


# --------------------------------------------------------------------------- #
# i18n / 静态文件 / 日志
# --------------------------------------------------------------------------- #
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = env("DJANGO_TIME_ZONE", "Asia/Shanghai")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "var" / "static"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "inventory": {"handlers": ["console"], "level": env("APP_LOG_LEVEL", "INFO"), "propagate": False},
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
