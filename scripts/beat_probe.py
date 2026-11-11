"""验证「beat 到点 → broker → worker 执行」这条链路。

真实调度是 00:00 / 12:00，等不起；这里临时把 beat 调度改成 20 秒一次，
复用同一个 Celery app（不动项目里的 settings），跑一会儿看 worker 是否真的被触发。

    python scripts/beat_probe.py          # 前台跑，Ctrl+C 结束

需要先起 worker：celery -A config worker -l info --pool=solo
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 允许直接 `python scripts/beat_probe.py`：把项目根目录放进 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from config.celery import app  # noqa: E402  —— 必须在 django.setup() 之后导入

app.conf.beat_schedule = {
    "probe-every-20s": {
        "task": "inventory.tasks.collect_daily_instance_stats",
        "schedule": 20.0,
    }
}

if __name__ == "__main__":
    print("beat 调度已被临时改写为每 20 秒一次，观察 worker 日志…")
    app.start(["beat", "--loglevel=info", "--schedule", "var/beat-probe-schedule"])
