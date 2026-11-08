"""관리자용 일반 사용자 검색 조건과 페이지 응답을 구성한다."""

from django.contrib.auth import get_user_model
from django.core.paginator import EmptyPage, Paginator
from django.db import DatabaseError
from django.db.models import Q
from rest_framework import serializers
from rest_framework.response import Response

from .roles import ADMIN_GROUP_NAME, USER_GROUP_NAME
from .views import error_body


class UserListQuerySerializer(serializers.Serializer):
    q = serializers.CharField(max_length=100, allow_blank=True, default="")
    status = serializers.ChoiceField(choices=("active", "inactive", "all"), default="active")
    page = serializers.IntegerField(min_value=1, max_value=2147483647, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=20)

    def to_internal_value(self, data):
        # 오타나 중복 파라미터를 무시하여 의도보다 넓은 목록을 반환하지 않는다.
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
        # QueryDict의 폼 처리에서 빈 status를 미지정으로 바꾸지 않도록 한다.
        # 중복 검사 후 단일 문자열 사전으로 변환하여 생략과 빈 값을 구분한다.
        return super().to_internal_value({key: data[key] for key in data})


def get_regular_users(filters):
    # 재설정 API와 동일하게 관리자·운영 계정·서비스 그룹 중복 소속을 제외한다.
    users = (
        get_user_model().objects.filter(
            groups__name=USER_GROUP_NAME, is_staff=False, is_superuser=False,
        )
        .exclude(groups__name=ADMIN_GROUP_NAME)
    )
    if filters["status"] != "all":
        users = users.filter(is_active=filters["status"] == "active")
    if filters["q"]:
        users = users.filter(
            Q(username__icontains=filters["q"]) | Q(full_name__icontains=filters["q"])
        )
    # 고정된 정렬과 명시한 필드만 조회하여 비밀번호 등 내부 정보를 노출하지 않는다.
    return users.order_by("username").values("id", "username", "full_name", "is_active")


def get_user_list_response(query_params):
    serializer = UserListQuerySerializer(data=query_params)
    serializer.is_valid(raise_exception=True)
    filters = serializer.validated_data
    try:
        paginator = Paginator(get_regular_users(filters), filters["page_size"])
        page = paginator.page(filters["page"])
        results = [
            {
                "id": str(user["id"]),
                "username": user["username"],
                "full_name": user["full_name"],
                "role": "user",
                "is_active": user["is_active"],
            }
            for user in page.object_list
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
        return Response(
            error_body("PAGE_NOT_FOUND", "요청한 페이지가 없습니다."), status=404,
        )
    except DatabaseError:
        return Response(
            error_body("USER_LIST_FAILED", "사용자 목록을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."),
            status=503,
        )
