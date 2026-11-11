"""演示数据：两个部门 / 三个集群 / 若干实例。

    python manage.py seed_demo
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from inventory import crypto
from inventory.models import Cluster, DBInstance, Department


class Command(BaseCommand):
    help = "生成演示数据（幂等，可重复执行）"

    @transaction.atomic
    def handle(self, *args, **options):
        dept_a, _ = Department.objects.get_or_create(
            code="TECH", defaults={"name": "技术部", "owner": "张三"}
        )
        dept_b, _ = Department.objects.get_or_create(
            code="FIN", defaults={"name": "财务部", "owner": "李四"}
        )

        cluster_a, _ = Cluster.objects.get_or_create(
            code="tech-mysql-prod",
            defaults={
                "name": "技术部 MySQL 生产集群",
                "department": dept_a,
                "engine": Cluster.Engine.MYSQL,
                "environment": Cluster.Environment.PROD,
                "vip": "10.0.1.10",
                "port": 3306,
            },
        )
        cluster_b, _ = Cluster.objects.get_or_create(
            code="tech-redis-prod",
            defaults={
                "name": "技术部 Redis 生产集群",
                "department": dept_a,
                "engine": Cluster.Engine.REDIS,
                "environment": Cluster.Environment.PROD,
                "vip": "10.0.1.20",
                "port": 6379,
            },
        )
        cluster_c, _ = Cluster.objects.get_or_create(
            code="fin-pg-prod",
            defaults={
                "name": "财务部 PostgreSQL 集群",
                "department": dept_b,
                "engine": Cluster.Engine.POSTGRESQL,
                "environment": Cluster.Environment.PROD,
                "vip": "10.0.2.10",
                "port": 5432,
            },
        )

        seeds = [
            (cluster_a, "tech-mysql-01", "10.0.1.11", 3306, DBInstance.Role.PRIMARY),
            (cluster_a, "tech-mysql-02", "10.0.1.12", 3306, DBInstance.Role.REPLICA),
            (cluster_b, "tech-redis-01", "10.0.1.21", 6379, DBInstance.Role.PRIMARY),
            (cluster_c, "fin-pg-01", "10.0.2.11", 5432, DBInstance.Role.PRIMARY),
            (cluster_c, "fin-pg-02", "10.0.2.12", 5432, DBInstance.Role.REPLICA),
        ]

        created = 0
        for cluster, name, host, port, role in seeds:
            if DBInstance.objects.filter(cluster=cluster, name=name).exists():
                continue
            instance = DBInstance(
                name=name, cluster=cluster, host=host, port=port, role=role, engine=cluster.engine
            )
            instance.set_password(crypto.generate_password())
            instance.save()
            created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"演示数据就绪：部门 {Department.objects.count()} 个，集群 {Cluster.objects.count()} 个，"
                f"实例 {DBInstance.objects.count()} 个（本次新建 {created} 个）"
            )
        )
