"""依赖一致性守卫。

目的：防止"偷偷引入一个未声明的包"或"声明了却没人用"这类供应链问题。
做法是把项目里所有 `import` 抽出来，和 `requirements.txt` 对账：

1. 每个第三方顶层模块必须在 `MODULE_TO_DIST` 里有明确映射（新包必须显式登记）；
2. 映射到的发行包必须出现在 `requirements.txt` 里；
3. `requirements.txt` 里声明的包必须都有人用（避免拉进无用依赖）；
4. 不允许出现 `importlib.import_module` / `__import__` 这类动态导入——
   它们会绕过本测试的静态审计。

这是纯静态检查，不联网、不依赖第三方工具。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from django.test import SimpleTestCase

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 顶层模块名 -> PyPI 发行包名（两者经常不一致，必须显式登记）
MODULE_TO_DIST = {
    "django": "Django",
    "rest_framework": "djangorestframework",
    "celery": "celery",
    "cryptography": "cryptography",
    # 下面两个不是被 import 的，而是 Django / Celery 运行时的驱动
    "psycopg": "psycopg[binary]",
    "redis": "redis",
}

# 本项目自己的包，不算第三方
LOCAL_MODULES = {"config", "inventory", "scripts", "manage", "var"}

REQUIREMENTS_FILE = BASE_DIR / "requirements.txt"


def _python_files() -> list[Path]:
    return [
        path
        for path in BASE_DIR.rglob("*.py")
        if "__pycache__" not in path.parts and "migrations" not in path.parts
    ]


def _declared_requirements() -> dict[str, str]:
    """{'django': 'Django==5.2.*', ...} —— key 是小写发行包名。"""
    declared = {}
    for line in REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~\[]", line, maxsplit=1)[0].strip()
        declared[name.lower()] = line
    return declared


class DependencyConsistencyTests(SimpleTestCase):
    def test_no_dynamic_imports(self):
        """动态导入会绕过依赖审计，一律不允许。

        本文件自己例外：它需要 `importlib.import_module` 去验证模块真能加载，
        属于审计工具本身，不是业务代码。
        """
        offenders = []
        for path in _python_files():
            if path.resolve() == Path(__file__).resolve():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = getattr(func, "id", None) or getattr(func, "attr", None)
                    if name in {"__import__", "import_module"}:
                        offenders.append(f"{path.relative_to(BASE_DIR)}:{node.lineno} {name}()")
        self.assertEqual(offenders, [], f"发现动态导入，依赖审计会被绕过：{offenders}")

    def test_every_third_party_import_is_declared(self):
        stdlib = set(sys.stdlib_module_names)
        imported: dict[str, set[str]] = {}

        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    top = name.split(".")[0]
                    if top in stdlib or top in LOCAL_MODULES:
                        continue
                    imported.setdefault(top, set()).add(str(path.relative_to(BASE_DIR)))

        undeclared = sorted(set(imported) - set(MODULE_TO_DIST))
        self.assertEqual(
            undeclared,
            [],
            f"这些第三方模块没有登记到 MODULE_TO_DIST / requirements.txt："
            f"{ {k: sorted(imported[k]) for k in undeclared} }",
        )

        declared = _declared_requirements()
        missing = [
            f"{module} -> {dist}"
            for module, dist in MODULE_TO_DIST.items()
            if module in imported and dist.split("[")[0].lower() not in declared
        ]
        self.assertEqual(missing, [], f"代码用到了但 requirements.txt 没声明：{missing}")

    def test_no_unused_requirements(self):
        """声明了却没人用的包，等于凭空扩大攻击面。"""
        declared = _declared_requirements()
        mapped = {dist.split("[")[0].lower() for dist in MODULE_TO_DIST.values()}

        # 运行时驱动（不 import，但必须存在）单独列出，避免误判为"没用"
        runtime_drivers = {"psycopg", "redis"}

        unused = [
            line
            for key, line in declared.items()
            if key not in mapped and key not in runtime_drivers
        ]
        self.assertEqual(unused, [], f"requirements.txt 里有未被使用的依赖：{unused}")

    def test_requirements_file_exists_and_pins_major_versions(self):
        self.assertTrue(REQUIREMENTS_FILE.exists(), "requirements.txt 不存在")
        declared = _declared_requirements()
        for key in ("django", "djangorestframework", "celery", "cryptography"):
            self.assertIn(key, declared, f"requirements.txt 缺少 {key}")
            self.assertRegex(
                declared[key], r"[=><~]", f"{key} 没有版本约束，升级可能静默破坏兼容性"
            )

    def test_imported_modules_are_real_and_loadable(self):
        """每个第三方模块必须真的能导入，且指向真实文件（防止装了个空壳包）。"""
        import importlib

        for module in ("django", "rest_framework", "celery", "cryptography"):
            mod = importlib.import_module(module)
            origin = getattr(mod, "__file__", None)
            self.assertIsNotNone(origin, f"{module} 没有 __file__，可能是伪造的命名空间包")
            self.assertTrue(Path(origin).exists(), f"{module} 的来源文件不存在：{origin}")
            self.assertGreater(
                Path(origin).stat().st_size, 0, f"{module} 的来源文件是空文件：{origin}"
            )
