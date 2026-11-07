"""계정 생성과 분리하여 서비스 권한 그룹만 초기 등록한다."""

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.roles import SERVICE_GROUP_NAMES


class Command(BaseCommand):
    help = "관리자·일반 이용자 그룹을 등록합니다. 기존 계정과 권한은 변경하지 않습니다."
    requires_migrations_checks = True

    def handle(self, *args, **options):
        results = []
        with transaction.atomic():
            for name in SERVICE_GROUP_NAMES:
                group, created = Group.objects.get_or_create(name=name)
                results.append((group.name, created))

        for name, created in results:
            result = "생성 완료" if created else "기존 그룹 유지"
            self.stdout.write(self.style.SUCCESS(f"{name}: {result}"))
