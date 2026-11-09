"""의뢰 생성일을 기준으로 관리자용 통계와 한국 시간의 일별 추이를 집계한다."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import DatabaseError
from django.db.models import BigIntegerField, Case, Count, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response

from accounts.views import error_body

from .admin_request_list import AdminRequestFilterSerializer, get_filtered_admin_requests
from .models import DetectionSpan, Request


class AdminStatisticsQuerySerializer(AdminRequestFilterSerializer):
    date_from = serializers.DateField(input_formats=["%Y-%m-%d"])
    date_to = serializers.DateField(input_formats=["%Y-%m-%d"])

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if (attrs["date_to"] - attrs["date_from"]).days >= 366:
            raise serializers.ValidationError({"date_to": ["조회 기간은 시작일을 포함하여 최대 366일입니다."]})
        return attrs


COUNT_FIELDS = (
    "total", "inspection_complete", "inspection_pending", "inspection_degraded", "inspection_failed",
    "detections", "warnings", "warnings_unknown", "blocked", "external_confirmed", "external_unknown",
)


def get_daily_statistics(filters):
    # 탐지 구간을 직접 JOIN하면 의뢰 건수가 증가하므로 의뢰별 구간 수만 서브쿼리로 가져온다.
    span_counts = (
        DetectionSpan.objects.filter(request_id=OuterRef("pk"))
        .order_by().values("request_id").annotate(total=Count("pk")).values("total")
    )
    complete = Q(inspection_status=Request.InspectionStatus.COMPLETE)
    pending = Q(inspection_status=Request.InspectionStatus.PENDING)
    # warning_codes에는 AI 처리에서 분류된 경고 코드만 저장한다. 탐지 구간 자체는 경고가 아니다.
    warnings = ~Q(warning_codes=[])
    unknown_delivery = Q(delivery_status=Request.DeliveryStatus.UNKNOWN) | Q(
        delivery_status__isnull=True, external_send_started_at__isnull=False,
    )
    # 기간 합계와 일별 값은 같은 SQL 집계 결과에서 만들어 동시 갱신에 따른 차이를 피한다.
    return list(
        get_filtered_admin_requests(filters).order_by()
        .annotate(day=TruncDate("created_at", tzinfo=ZoneInfo("Asia/Seoul")))
        .values("day")
        .annotate(
            total=Count("pk"),
            inspection_complete=Count("pk", filter=complete),
            inspection_pending=Count("pk", filter=pending),
            inspection_degraded=Count("pk", filter=Q(inspection_status=Request.InspectionStatus.DEGRADED)),
            inspection_failed=Count("pk", filter=Q(inspection_status=Request.InspectionStatus.FAILED)),
            detections=Sum(Case(
                When(complete, then=Coalesce(
                    Subquery(span_counts, output_field=BigIntegerField()),
                    Value(0, output_field=BigIntegerField()),
                )),
                default=Value(0), output_field=BigIntegerField(),
            )),
            warnings=Count("pk", filter=warnings),
            warnings_unknown=Count("pk", filter=~warnings & ~complete),
            blocked=Count("pk", filter=Q(state=Request.State.BLOCKED)),
            external_confirmed=Count("pk", filter=Q(delivery_status=Request.DeliveryStatus.CONFIRMED_SENT)),
            external_unknown=Count("pk", filter=unknown_delivery),
        ).order_by("day")
    )


def metric(known_value, unknown_count, total):
    # 미수집 값은 0으로 확정하지 않는다. known_value는 확인된 부분만을 나타낸다.
    return {
        "value": known_value if unknown_count == 0 else None,
        "known_value": known_value,
        "unknown_request_count": unknown_count,
        "status": ("complete" if unknown_count == 0 else
                   "unavailable" if unknown_count == total else "partial"),
    }


def statistics_item(counts):
    total = counts["total"]
    anomalies = counts["inspection_degraded"] + counts["inspection_failed"]
    return {
        "metrics": {
            "total_requests": metric(total, 0, total),
            "detection_count": metric(counts["detections"], total - counts["inspection_complete"], total),
            "warning_requests": metric(counts["warnings"], counts["warnings_unknown"], total),
            "inspection_anomalies": metric(anomalies, counts["inspection_pending"], total),
            "policy_blocked_requests": metric(counts["blocked"], 0, total),
            # 확인된 송신 건수이며, 송신 여부 불명 건수는 coverage로 별도 제공한다.
            "external_sent_confirmed_requests": metric(counts["external_confirmed"], 0, total),
        },
        "coverage": {
            "inspection_complete_requests": counts["inspection_complete"],
            "inspection_pending_requests": counts["inspection_pending"],
            "inspection_degraded_requests": counts["inspection_degraded"],
            "inspection_failed_requests": counts["inspection_failed"],
            "external_delivery_unknown_requests": counts["external_unknown"],
        },
    }


def get_admin_statistics_response(query_params):
    serializer = AdminStatisticsQuerySerializer(data=query_params)
    serializer.is_valid(raise_exception=True)
    filters = serializer.validated_data
    calculated_at = timezone.now()
    try:
        groups = get_daily_statistics(filters)
    except DatabaseError:
        return Response(
            error_body("ADMIN_STATISTICS_FAILED", "이용 통계를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."),
            status=503,
        )
    totals = {field: sum(group[field] for group in groups) for field in COUNT_FIELDS}
    by_day = {group["day"]: group for group in groups}
    daily = []
    for index in range((filters["date_to"] - filters["date_from"]).days + 1):
        day = filters["date_from"] + timedelta(days=index)
        counts = by_day.get(day, {field: 0 for field in COUNT_FIELDS})
        daily.append({"date": day.isoformat(), **statistics_item(counts)})
    return Response({
        "timezone": "Asia/Seoul", "date_basis": "created_at",
        "calculated_at": calculated_at.isoformat(),
        "filters": {key: str(value) for key, value in filters.items()},
        "summary": statistics_item(totals), "daily": daily,
    })
