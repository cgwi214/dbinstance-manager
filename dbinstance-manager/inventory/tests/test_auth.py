"""鉴权与越权测试。

两个维度都要覆盖：

- **认证**（401）：没带 token / token 错误 → 拒绝；
- **授权**（403）：登录了但权限不够（普通用户做写操作、看明文凭据）→ 拒绝。

默认 `API_AUTH_REQUIRED=0`（开箱即用），所以这里用 `override_settings` 显式打开，
既验证"关掉时确实不拦"，也验证"打开时确实拦得住"。
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token

from inventory.tests.factories import make_cluster, make_department, make_instance

AUTH_ON = override_settings(API_AUTH_REQUIRED=True)

# 测试里不需要真实强度的口令哈希，换 MD5 让整套测试快一个数量级
FAST_HASHERS = override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)


def _token_for(user) -> str:
    token, _ = Token.objects.get_or_create(user=user)
    return f"Token {token.key}"


class AuthDisabledTests(TestCase):
    """默认（本地开发）模式：不拦，保证开箱即用。"""

    def test_anonymous_can_read(self):
        response = self.client.get("/api/instances/")
        self.assertEqual(response.status_code, 200)

    def test_anonymous_can_write(self):
        dept = make_department(code="OPEN")
        response = self.client.patch(
            f"/api/departments/{dept.pk}/", {"owner": "匿名"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)


@AUTH_ON
@FAST_HASHERS
class AuthenticationTests(TestCase):
    def setUp(self):
        self.dept = make_department()
        self.cluster = make_cluster(self.dept)
        self.instance = make_instance(self.cluster, password="First#Pass1")
        User = get_user_model()
        self.reader = User.objects.create_user("reader", password="x")
        self.ops = User.objects.create_user("ops", password="x", is_staff=True)

    def test_anonymous_read_returns_401(self):
        for path in ("/api/departments/", "/api/clusters/", "/api/instances/", "/api/stats/daily/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_wrong_token_returns_401(self):
        response = self.client.get(
            "/api/instances/", HTTP_AUTHORIZATION="Token 0000000000000000000000000000000000000000"
        )
        self.assertEqual(response.status_code, 401)

    def test_malformed_header_returns_401(self):
        response = self.client.get("/api/instances/", HTTP_AUTHORIZATION="Bearer abc")
        self.assertEqual(response.status_code, 401)

    def test_valid_token_can_read(self):
        response = self.client.get("/api/instances/", HTTP_AUTHORIZATION=_token_for(self.reader))
        self.assertEqual(response.status_code, 200)

    def test_health_check_stays_public(self):
        """健康检查要给探针用，鉴权开着也不能要求 token。"""
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["database"])


@AUTH_ON
@FAST_HASHERS
class AuthorizationTests(TestCase):
    """越权：登录了但权限不够。"""

    def setUp(self):
        self.dept = make_department()
        self.cluster = make_cluster(self.dept)
        self.instance = make_instance(self.cluster, password="First#Pass1")
        User = get_user_model()
        self.reader = User.objects.create_user("reader", password="x")
        self.ops = User.objects.create_user("ops", password="x", is_staff=True)

    # ---- 普通用户：只读可以，写不行 ----
    def test_reader_cannot_create(self):
        response = self.client.post(
            "/api/departments/",
            {"name": "新部门", "code": "NEW"},
            content_type="application/json",
            HTTP_AUTHORIZATION=_token_for(self.reader),
        )
        self.assertEqual(response.status_code, 403)

    def test_reader_cannot_update(self):
        response = self.client.patch(
            f"/api/instances/{self.instance.pk}/",
            {"account": "hacked"},
            content_type="application/json",
            HTTP_AUTHORIZATION=_token_for(self.reader),
        )
        self.assertEqual(response.status_code, 403)

    def test_reader_cannot_delete(self):
        response = self.client.delete(
            f"/api/instances/{self.instance.pk}/", HTTP_AUTHORIZATION=_token_for(self.reader)
        )
        self.assertEqual(response.status_code, 403)

    def test_reader_cannot_probe(self):
        """探测会改写实例状态，属于写操作。"""
        response = self.client.post(
            f"/api/instances/{self.instance.pk}/probe/",
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION=_token_for(self.reader),
        )
        self.assertEqual(response.status_code, 403)

    def test_reader_cannot_see_plaintext_credential(self):
        """最敏感的一个：明文密码。普通用户即使登录了也不能看。"""
        response = self.client.post(
            f"/api/instances/{self.instance.pk}/credential/",
            {"reason": "越权测试"},
            content_type="application/json",
            HTTP_AUTHORIZATION=_token_for(self.reader),
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("password", response.data)

    def test_reader_cannot_rotate_password(self):
        response = self.client.post(
            f"/api/instances/{self.instance.pk}/rotate-password/",
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION=_token_for(self.reader),
        )
        self.assertEqual(response.status_code, 403)

    def test_reader_can_read_password_history_metadata(self):
        """历史接口只回版本号，不含明文，普通用户可读。"""
        response = self.client.get(
            f"/api/instances/{self.instance.pk}/password-history/",
            HTTP_AUTHORIZATION=_token_for(self.reader),
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("password", str(response.data))

    # ---- staff：全通 ----
    def test_staff_can_write(self):
        response = self.client.post(
            "/api/departments/",
            {"name": "运维部", "code": "OPS"},
            content_type="application/json",
            HTTP_AUTHORIZATION=_token_for(self.ops),
        )
        self.assertEqual(response.status_code, 201)

    def test_staff_can_see_credential(self):
        response = self.client.post(
            f"/api/instances/{self.instance.pk}/credential/",
            {"reason": "运维取用"},
            content_type="application/json",
            HTTP_AUTHORIZATION=_token_for(self.ops),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.instance.check_password(response.data["password"]))

    def test_staff_can_rotate_password(self):
        response = self.client.post(
            f"/api/instances/{self.instance.pk}/rotate-password/",
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION=_token_for(self.ops),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["password_version"], 2)

    def test_reader_cannot_write_but_ops_can_on_same_endpoint(self):
        """同一接口、同一数据，只因为身份不同结果不同——越权的本质。"""
        url = f"/api/departments/{self.dept.pk}/"
        payload = {"owner": "改成这样"}
        reader = self.client.patch(
            url,
            payload,
            content_type="application/json",
            HTTP_AUTHORIZATION=_token_for(self.reader),
        )
        ops = self.client.patch(
            url,
            payload,
            content_type="application/json",
            HTTP_AUTHORIZATION=_token_for(self.ops),
        )
        self.assertEqual(reader.status_code, 403)
        self.assertEqual(ops.status_code, 200)
        self.dept.refresh_from_db()
        self.assertEqual(self.dept.owner, "改成这样")


@FAST_HASHERS
class CreateApiTokenCommandTests(TestCase):
    def test_creates_staff_user_and_token(self):
        call_command("create_api_token", "--username", "ci-bot", stdout=StringIO())

        user = get_user_model().objects.get(username="ci-bot")
        self.assertTrue(user.is_staff)
        self.assertFalse(user.has_usable_password(), "API 用户不该能登录后台")
        self.assertTrue(Token.objects.filter(user=user).exists())

    def test_no_staff_creates_reader(self):
        call_command("create_api_token", "--username", "auditor", "--no-staff", stdout=StringIO())
        user = get_user_model().objects.get(username="auditor")
        self.assertFalse(user.is_staff)

    def test_rotate_replaces_token(self):
        call_command("create_api_token", "--username", "ci-bot", stdout=StringIO())
        old = Token.objects.get(user__username="ci-bot").key

        call_command("create_api_token", "--username", "ci-bot", "--rotate", stdout=StringIO())
        new = Token.objects.get(user__username="ci-bot").key

        self.assertNotEqual(old, new)
        self.assertEqual(Token.objects.filter(user__username="ci-bot").count(), 1)

    @AUTH_ON
    def test_issued_token_actually_works(self):
        call_command("create_api_token", "--username", "ci-bot", stdout=StringIO())
        token = Token.objects.get(user__username="ci-bot")

        response = self.client.get(
            "/api/instances/", HTTP_AUTHORIZATION=f"Token {token.key}"
        )
        self.assertEqual(response.status_code, 200)
