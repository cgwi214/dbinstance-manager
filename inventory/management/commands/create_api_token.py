"""创建/更新一个 API 用户并输出 Token。

    python manage.py create_api_token --username ops
    python manage.py create_api_token --username reader --no-staff   # 只读账号
    python manage.py create_api_token --username ops --rotate        # 重新发一个 token（旧的作废）

`API_AUTH_REQUIRED=1` 时用它来发凭据：

    curl -H "Authorization: Token <token>" http://127.0.0.1:8000/api/instances/
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from rest_framework.authtoken.models import Token


class Command(BaseCommand):
    help = "创建/更新 API 用户并输出 Token（--no-staff 生成只读账号）"

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True, help="用户名")
        parser.add_argument(
            "--no-staff",
            action="store_false",
            dest="staff",
            help="创建普通用户（只读权限：GET 可用，写操作 403）",
        )
        parser.add_argument(
            "--rotate", action="store_true", help="作废旧 Token 并重新签发"
        )

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"]

        user, created = User.objects.get_or_create(username=username)
        user.is_staff = options["staff"]
        user.is_active = True
        # API 用户不用于后台登录，密码设为不可用
        user.set_unusable_password()
        user.save()

        if options["rotate"]:
            Token.objects.filter(user=user).delete()
        token, token_created = Token.objects.get_or_create(user=user)

        action = "创建" if created else "更新"
        self.stdout.write(
            self.style.SUCCESS(
                f"用户 {username} 已{action}（staff={user.is_staff}）；"
                f"Token {'新签发' if token_created or options['rotate'] else '已存在'}："
            )
        )
        self.stdout.write(token.key)
