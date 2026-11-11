"""测试用的数据构造工具。"""
from __future__ import annotations

from inventory.models import Cluster, DBInstance, Department


def make_department(code: str = "TECH", name: str | None = None, **kwargs) -> Department:
    return Department.objects.create(code=code, name=name or f"部门{code}", **kwargs)


def make_cluster(department: Department | None = None, code: str = "tech-mysql", **kwargs) -> Cluster:
    department = department or make_department()
    return Cluster.objects.create(
        code=code,
        name=kwargs.pop("name", f"集群{code}"),
        department=department,
        **kwargs,
    )


def make_instance(
    cluster: Cluster | None = None,
    name: str = "inst-01",
    host: str = "10.0.0.1",
    port: int = 3306,
    password: str = "Init#Passw0rd",
    **kwargs,
) -> DBInstance:
    cluster = cluster or make_cluster()
    instance = DBInstance(name=name, cluster=cluster, host=host, port=port, **kwargs)
    if password:
        instance.set_password(password)
    instance.save()
    return instance
