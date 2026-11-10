"""검사 대기(queued) 의뢰를 모델 서비스로 처리하고 승인 기한 만료·장시간 진행 의뢰를 정리한다.

GATEWAY_WORKER=thread(기본)에서는 요청 처리 프로세스의 배경 스레드가 같은 일을 하므로, 이 명령은
서버 재시작으로 스레드가 사라진 의뢰의 복구와 기한 만료 정리에 쓴다. GATEWAY_WORKER 를 다른 값으로 두면
이 명령(--loop)이 유일한 작업자다. 정기 실행 설정은 배포 시 별도로 적용한다."""

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from gateway.approval_lifecycle import expire_due_approvals
from gateway.processing import dispatch_pending, fail_stuck


class Command(BaseCommand):
    help = "queued 의뢰를 처리하고(검사 또는 승인 뒤 외부 전송) 기한 만료·장시간 진행 의뢰를 정리합니다."
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="종료하지 않고 --interval 초마다 반복")
        parser.add_argument("--interval", type=float, default=5.0, help="반복 간격(초), 기본 5")
        parser.add_argument("--limit", type=int, default=20, help="한 번에 가져갈 queued 의뢰 수(1~100), 기본 20")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 100:
            raise CommandError("--limit은 1~100이어야 합니다.")
        if options["interval"] < 1:
            raise CommandError("--interval은 1초 이상이어야 합니다.")
        while True:
            try:
                expired = expire_due_approvals()
                stuck = fail_stuck()
                ran = dispatch_pending(options["limit"])
            except DatabaseError:
                if not options["loop"]:
                    raise CommandError("DB 오류로 처리를 중단했습니다. 상태를 확인한 뒤 다시 실행해 주세요.") from None
                self.stderr.write("DB 오류: 다음 주기에 다시 시도합니다.")
            else:
                if ran or expired or stuck or not options["loop"]:
                    self.stdout.write(f"처리 {ran}건, 기한 만료 {expired}건, 장시간 진행 실패 처리 {stuck}건")
            if not options["loop"]:
                break
            time.sleep(options["interval"])
