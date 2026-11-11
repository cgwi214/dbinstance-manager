"""仅用于验证「beat 到点会派发」的临时配置。

真实调度是 00:00 / 12:00，等不起。用这个 settings 把调度改成 20 秒一次，
跑一会儿就能看到 worker 被反复触发，从而证明 beat → broker → worker 链路是通的。

    DJANGO_SETTINGS_MODULE=config.settings_beat_probe \
        celery -A config beat -l info -s var/beat-schedule-probe

它只覆盖 beat 调度，其余配置（数据库、broker、加密密钥）与正式环境完全一致。
"""
from .settings import *  # noqa: F401,F403  (settings shim，pyflakes 对 star import 无法判断)

CELERY_BEAT_SCHEDULE = {
    "probe-every-20s": {
        "task": "inventory.tasks.collect_daily_instance_stats",
        "schedule": 20.0,
    }
}
