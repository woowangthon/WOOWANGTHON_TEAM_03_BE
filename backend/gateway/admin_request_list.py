"""관리자용 의뢰 목록의 검색·페이지 처리와 최종 탐지 요약을 구성한다."""

import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.core.paginator import EmptyPage, Paginator
from django.db import DatabaseError
from django.db.models import Count, Exists, OuterRef, Q
from rest_framework import serializers
from rest_framework.response import Response

from accounts.views import error_body

from .models import DetectionSpan, Request, SensitiveTerm


class AdminRequestFilterSerializer(serializers.Serializer):
    q = serializers.CharField(max_length=100, allow_blank=True, default="")
    request_id = serializers.UUIDField(required=False)
    user_id = serializers.UUIDField(required=False)
    date_from = serializers.DateField(required=False, input_formats=["%Y-%m-%d"])
    date_to = serializers.DateField(required=False, input_formats=["%Y-%m-%d"])
    state = serializers.ChoiceField(choices=Request.State.values, required=False)
    processing_route = serializers.ChoiceField(choices=Request.ProcessingRoute.values, required=False)
    detection_label = serializers.ChoiceField(choices=SensitiveTerm.Label.values, required=False)

    def to_internal_value(self, data):
        if set(data) - set(self.fields):
            raise serializers.ValidationError({
                "non_field_errors": ["허용되지 않는 검색 항목이 있습니다."]
            })
        for key in data:
            if len(data.getlist(key)) != 1:
                raise serializers.ValidationError({key: ["각 검색 항목은 한 번만 지정해 주세요."]})
        for key in ("page", "page_size"):
            if key in data and not (data[key].isascii() and data[key].isdecimal()):
                raise serializers.ValidationError({key: ["양의 정수로 입력해 주세요."]})
        for key in ("date_from", "date_to"):
            if key in data and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", data[key]):
                raise serializers.ValidationError({key: ["YYYY-MM-DD 형식으로 입력해 주세요."]})
        # 빈 필터를 생략으로 취급하여 검색 범위를 넓히지 않는다.
        return super().to_internal_value({key: data[key] for key in data})

    def validate(self, attrs):
        if "date_from" in attrs and "date_to" in attrs and attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError({"date_to": ["종료일은 시작일보다 빠를 수 없습니다."]})
        return attrs


class AdminRequestListQuerySerializer(AdminRequestFilterSerializer):
    page = serializers.IntegerField(min_value=1, max_value=2147483647, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=20)


def get_filtered_admin_requests(filters):
    # 감사 조회는 사용자의 숨김 처리·계정 비활성화·현재 역할에 따라 과거 기록을 제외하지 않는다.
    records = Request.objects.all()
    for field in ("request_id", "user_id", "state", "processing_route"):
        if field in filters:
            records = records.filter(**{field: filters[field]})
    seoul = ZoneInfo("Asia/Seoul")
    if "date_from" in filters:
        records = records.filter(created_at__gte=datetime.combine(filters["date_from"], time.min, seoul))
    if "date_to" in filters:
        records = records.filter(created_at__lte=datetime.combine(filters["date_to"], time.max, seoul))
    if filters["q"]:
        records = records.filter(
            Q(user__username__icontains=filters["q"])
            | Q(user__full_name__icontains=filters["q"])
            | Q(document__original_filename__icontains=filters["q"])
        )
    if "detection_label" in filters:
        # 여러 탐지 구간이 검색에 일치해도 의뢰와 페이지 건수는 한 번만 센다.
        matching_spans = DetectionSpan.objects.filter(
            request_id=OuterRef("pk"), sensitive_term__label=filters["detection_label"],
        )
        records = records.filter(inspection_status=Request.InspectionStatus.COMPLETE).filter(Exists(matching_spans))
    return records


def get_admin_request_history(filters):
    records = get_filtered_admin_requests(filters)
    # 대용량 원문·AI 답변·마스킹 원값은 목록 쿼리에서 읽지 않는다.
    return records.order_by("-created_at", "-request_id").values(
        "request_id", "user_id", "user__username", "user__full_name", "created_at", "finished_at",
        "document__original_filename", "state", "processing_route", "inspection_status",
        "warning_codes", "error_code",
    )


def get_final_detection_summaries(records):
    # complete는 최종 구간 저장 완료를 의미한다. 그 외 상태는 0건으로 단정하지 않는다.
    completed_ids = [record["request_id"] for record in records
                     if record["inspection_status"] == Request.InspectionStatus.COMPLETE]
    summaries = {request_id: {"count": 0, "labels": []} for request_id in completed_ids}
    if completed_ids:
        groups = (
            DetectionSpan.objects.filter(request_id__in=completed_ids)
            .values("request_id", "sensitive_term__label")
            .annotate(count=Count("pk"))
            .order_by("request_id", "sensitive_term__label")
        )
        for group in groups:
            summary = summaries[group["request_id"]]
            summary["count"] += group["count"]
            summary["labels"].append(group["sensitive_term__label"])
    return summaries


def admin_request_list_item(record, summary):
    filename = record["document__original_filename"]
    return {
        "request_id": str(record["request_id"]),
        "user": {
            "id": str(record["user_id"]), "username": record["user__username"],
            "full_name": record["user__full_name"],
        },
        "created_at": record["created_at"].isoformat(),
        "finished_at": record["finished_at"].isoformat() if record["finished_at"] else None,
        "input_type": "pdf" if filename is not None else "text",
        "original_filename": filename,
        "state": record["state"],
        "processing_route": record["processing_route"],
        "inspection_status": record["inspection_status"],
        "detection_count": summary["count"] if summary is not None else None,
        "detection_labels": summary["labels"] if summary is not None else None,
        "warning_codes": record["warning_codes"],
        "error_code": record["error_code"],
    }


def get_admin_request_list_response(query_params):
    serializer = AdminRequestListQuerySerializer(data=query_params)
    serializer.is_valid(raise_exception=True)
    filters = serializer.validated_data
    try:
        paginator = Paginator(get_admin_request_history(filters), filters["page_size"])
        page = paginator.page(filters["page"])
        records = list(page.object_list)
        summaries = get_final_detection_summaries(records)
        return Response({
            "count": paginator.count, "page": page.number, "page_size": filters["page_size"],
            "total_pages": paginator.num_pages,
            "next_page": page.next_page_number() if page.has_next() else None,
            "previous_page": page.previous_page_number() if page.has_previous() else None,
            "results": [admin_request_list_item(record, summaries.get(record["request_id"]))
                        for record in records],
        })
    except EmptyPage:
        return Response(error_body("PAGE_NOT_FOUND", "요청한 페이지가 없습니다."), status=404)
    except DatabaseError:
        return Response(
            error_body("ADMIN_REQUEST_LIST_FAILED", "이용 이력을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."),
            status=503,
        )
