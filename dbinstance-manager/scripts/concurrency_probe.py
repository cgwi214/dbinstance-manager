"""并发轮换压测：验证乐观锁在真并发下"不丢更新、不重复版本"。

需要真实 broker + 至少两个 worker：

    celery -A config worker -l info --pool=solo -n w1@%h
    celery -A config worker -l info --pool=solo -n w2@%h
    python scripts/concurrency_probe.py --tasks 3

它同时投递 N 个轮换任务，让多个 worker 抢同一批实例，然后逐实例核对：
版本号是否严格等于「初始版本 + 成功轮换次数」、历史条数是否与版本数一致、
最新一条历史里的密码是否就是当前密码。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from inventory.models import DBInstance, InstancePasswordHistory  # noqa: E402
from inventory.tasks import rotate_all_instance_passwords  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=3, help="同时投递几个轮换任务")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    instances = list(DBInstance.objects.filter(is_active=True).order_by("id"))
    if not instances:
        print("没有启用中的实例，先跑 python manage.py seed_demo")
        return 2
    baseline = {item.pk: item.password_version for item in instances}
    print(f"参与实例 {len(instances)} 个，初始版本 {sorted(set(baseline.values()))}，投递 {args.tasks} 个任务\n")

    results = [rotate_all_instance_passwords.delay(reason="scheduled") for _ in range(args.tasks)]
    summaries = []
    for index, async_result in enumerate(results, 1):
        summary = async_result.get(timeout=args.timeout)
        summaries.append(summary)
        print(
            f"任务{index} {async_result.id[:8]} -> total={summary['total']} "
            f"rotated={summary['rotated']} failed={summary['failed']}"
        )

    # 每个成功任务会给**每个**实例 +1 个版本；rotated 是「实例×任务」的总和，
    # 不能直接拿去和单个实例的版本号比。
    increment = len([item for item in summaries if item["failed"] == 0])
    if increment != len(summaries):
        print(f"注意：{len(summaries) - increment} 个任务存在失败项，单实例增量按 {increment} 计算\n")
    failures = []

    for instance in DBInstance.objects.filter(pk__in=baseline).order_by("id"):
        expected_version = baseline[instance.pk] + increment
        history = list(
            InstancePasswordHistory.objects.filter(instance_id=instance.pk).order_by("version")
        )
        versions = [row.version for row in history]

        if instance.password_version != expected_version:
            failures.append(
                f"{instance.name}: 版本 {instance.password_version} != 期望 {expected_version}（丢更新）"
            )
        if len(versions) != len(set(versions)):
            failures.append(f"{instance.name}: 历史里有重复版本 {versions}")
        if versions != list(range(1, instance.password_version + 1)):
            failures.append(f"{instance.name}: 历史版本不连续 {versions}")
        if history and history[-1].password_ciphertext != instance.password:
            failures.append(f"{instance.name}: 最新历史密码与当前密码不一致")

    print()
    if failures:
        print(f"发现 {len(failures)} 个问题：")
        for item in failures:
            print("  -", item)
        return 1

    print(
        f"PASS: {len(instances)} 个实例 × {increment} 个任务，"
        f"版本连续、历史无重复、最新历史密码与当前密码一致"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
