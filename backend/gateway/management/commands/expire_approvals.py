"""기한이 지난 승인 대기 의뢰만 종료한다. 정기 실행 설정은 배포 시 별도로 적용한다."""

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from gateway.approval_lifecycle import expire_due_approvals


class Command(BaseCommand):
    help = "기한이 지난 승인 대기 의뢰를 만료 처리합니다. --dry-run은 예상 대상 건수만 확인합니다."
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="DB를 변경하지 않고 대상 건수만 확인")
        parser.add_argument("--batch-size", type=int, default=500, help="트랜잭션별 처리 건수: 1~1000, 기본 500")

    def handle(self, *args, **options):
        if not 1 <= options["batch_size"] <= 1000:
            raise CommandError("--batch-size는 1~1000이어야 합니다.")
        try:
            count = expire_due_approvals(batch_size=options["batch_size"], dry_run=options["dry_run"])
        except DatabaseError:
            # 일부 묶음은 커밋되었을 수 있다. 재실행해도 결정·만료된 의뢰를 다시 처리하지 않는다.
            raise CommandError("DB 오류로 만료 처리를 중단했습니다. 상태를 확인한 뒤 다시 실행해 주세요.") from None
        message = f"만료 대상: {count}건 (변경 없음)" if options["dry_run"] else f"만료 처리 완료: {count}건"
        self.stdout.write(self.style.SUCCESS(message))
