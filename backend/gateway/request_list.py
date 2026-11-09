"""본인 의뢰 이력의 조회 조건과 최소 목록 응답을 구성한다."""

from django.core.paginator import EmptyPage, Paginator
from django.db import DatabaseError
from django.db.models.functions import Substr
from rest_framework import serializers
from rest_framework.response import Response

from accounts.views import error_body

from .models import Request


class RequestListQuerySerializer(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, max_value=2147483647, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=20)

    def to_internal_value(self, data):
        # 소유자·숨김 여부·정렬을 클라이언트 입력으로 바꾸지 않는다.
        if set(data) - set(self.fields):
            raise serializers.ValidationError({
                "non_field_errors": ["허용되지 않는 조회 항목이 있습니다."]
            })
        for key in data:
            if len(data.getlist(key)) != 1:
                raise serializers.ValidationError({key: ["각 조회 항목은 한 번만 지정해 주세요."]})
            if not (data[key].isascii() and data[key].isdecimal()):
                raise serializers.ValidationError({key: ["양의 정수로 입력해 주세요."]})
        # 빈 문자열을 생략으로 취급하지 않도록 단일 값 사전으로 변환한다.
        return super().to_internal_value({key: data[key] for key in data})


def get_request_history(user):
    # 큰 검사 원문·답변·복원 정보는 목록 조회 시 DB에서 읽지 않는다.
    # 동일 시각의 요청도 페이지별 순서가 고정되도록 요청 ID로 다시 정렬한다.
    return (
        Request.objects.filter(user=user, hidden_at__isnull=True)
        .order_by("-created_at", "-request_id")
        .annotate(instruction_preview=Substr("instruction", 1, 200))
        .values(
            "request_id", "instruction_preview", "state", "created_at", "updated_at",
            "finished_at", "document__original_filename",
        )
    )


def get_request_list_response(user, query_params):
    serializer = RequestListQuerySerializer(data=query_params)
    serializer.is_valid(raise_exception=True)
    filters = serializer.validated_data
    try:
        paginator = Paginator(get_request_history(user), filters["page_size"])
        page = paginator.page(filters["page"])
        results = [
            {
                "request_id": str(record["request_id"]),
                "instruction_preview": record["instruction_preview"],
                "state": record["state"],
                "created_at": record["created_at"].isoformat(),
                "updated_at": record["updated_at"].isoformat(),
                "finished_at": (
                    record["finished_at"].isoformat() if record["finished_at"] else None
                ),
                "document": (
                    {"original_filename": record["document__original_filename"]}
                    if record["document__original_filename"] is not None else None
                ),
            }
            for record in page.object_list
        ]
        return Response({
            "count": paginator.count,
            "page": page.number,
            "page_size": filters["page_size"],
            "total_pages": paginator.num_pages,
            "next_page": page.next_page_number() if page.has_next() else None,
            "previous_page": page.previous_page_number() if page.has_previous() else None,
            "results": results,
        })
    except EmptyPage:
        return Response(error_body("PAGE_NOT_FOUND", "요청한 페이지가 없습니다."), status=404)
    except DatabaseError:
        return Response(
            error_body("REQUEST_LIST_FAILED", "의뢰 목록을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."),
            status=503,
        )
