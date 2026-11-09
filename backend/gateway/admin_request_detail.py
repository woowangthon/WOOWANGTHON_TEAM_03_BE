"""관리자 감사용 상세 응답. 저장된 정보만 제공하고 AI 답변은 조회하지 않는다."""

from django.db import DatabaseError
from django.urls import reverse
from rest_framework.response import Response

from accounts.views import error_body

from .models import DetectionSpan, MaskingMapping, Request


def get_admin_request_record(request_id):
    # 사용자 비밀번호·AI 답변·저장 경로를 명시적 조회 필드에서 제외한다.
    # 숨김·비활성 계정의 기록도 감사 목적으로 조회한다.
    return Request.objects.values(
        "request_id", "user_id", "user__username", "user__full_name", "user__is_active",
        "instruction", "input_text", "state", "processing_route", "inspection_status",
        "warning_codes", "inspection_versions", "error_stage", "error_code",
        "approval_status", "approval_expires_at", "decided_at", "delivery_status",
        "external_send_started_at", "created_at", "updated_at", "finished_at", "hidden_at",
        "document__original_filename", "document__size_bytes", "document__page_count",
        "document__extraction_status", "document__extracted_text",
    ).get(pk=request_id)


def get_admin_detection_spans(request_id):
    # 위치는 input_text 기준의 저장 좌표 그대로 반환하며 문자 단위나 영역별 좌표를 추정하지 않는다.
    rows = DetectionSpan.objects.filter(request_id=request_id).order_by("start_offset", "end_offset", "pk").values(
        "id", "sensitive_term_id", "sensitive_term__value", "sensitive_term__label",
        "input_source", "start_offset", "end_offset", "mapping_id",
    )
    return [{
        "id": str(row["id"]),
        "sensitive_term_id": str(row["sensitive_term_id"]),
        "value": row["sensitive_term__value"], "label": row["sensitive_term__label"],
        "input_source": row["input_source"],
        "start_offset": row["start_offset"], "end_offset": row["end_offset"],
        "mapping_id": str(row["mapping_id"]) if row["mapping_id"] is not None else None,
    } for row in rows]


def get_admin_masking_mappings(request_id):
    # 같은 기밀 사전 항목을 참조해도 다른 의뢰의 토큰 대응 정보는 섞지 않는다.
    rows = MaskingMapping.objects.filter(request_id=request_id).order_by("pk").values(
        "id", "sensitive_term_id", "sensitive_term__value", "sensitive_term__label", "placeholder",
    )
    return [{
        "id": str(row["id"]), "sensitive_term_id": str(row["sensitive_term_id"]),
        "value": row["sensitive_term__value"], "label": row["sensitive_term__label"],
        "placeholder": row["placeholder"],
    } for row in rows]


def optional_timestamp(value):
    return value.isoformat() if value is not None else None


def admin_document_detail(record):
    if record["document__original_filename"] is None:
        return None
    return {
        "original_filename": record["document__original_filename"],
        "size_bytes": record["document__size_bytes"], "page_count": record["document__page_count"],
        "extraction_status": record["document__extraction_status"],
        "extracted_text": record["document__extracted_text"],
        # 원본 존재 여부와 파일 권한은 기존 파일 API가 열기 시점에 다시 확인한다.
        "file_url": reverse("gateway:request-file", kwargs={"request_id": record["request_id"]}),
    }


def admin_request_detail_body(record, spans, mappings):
    is_final = record["inspection_status"] == Request.InspectionStatus.COMPLETE
    return {
        "request_id": str(record["request_id"]),
        "user": {
            "id": str(record["user_id"]), "username": record["user__username"],
            "full_name": record["user__full_name"], "is_active": record["user__is_active"],
        },
        "instruction": record["instruction"], "input_text": record["input_text"],
        "input_type": "pdf" if record["document__original_filename"] is not None else "text",
        "document": admin_document_detail(record),
        "state": record["state"], "processing_route": record["processing_route"],
        "created_at": record["created_at"].isoformat(), "updated_at": record["updated_at"].isoformat(),
        "finished_at": optional_timestamp(record["finished_at"]),
        "hidden_at": optional_timestamp(record["hidden_at"]),
        "inspection_status": record["inspection_status"],
        # 불완전 검사에서 저장된 구간은 확인할 수 있지만 최종 0건·최종 결과로 표시하지 않는다.
        "detection_is_final": is_final,
        "detection_count": len(spans) if is_final else None,
        "detection_labels": sorted({span["label"] for span in spans}) if is_final else None,
        "detection_spans": spans, "masking_mappings": mappings,
        "warning_codes": record["warning_codes"], "inspection_versions": record["inspection_versions"],
        "approval": {
            "status": record["approval_status"],
            "expires_at": optional_timestamp(record["approval_expires_at"]),
            "decided_at": optional_timestamp(record["decided_at"]),
        },
        # 대응표·승인 존재만으로 마스킹 실행 또는 외부 전송 완료를 추정하지 않는다.
        "delivery": {
            "status": record["delivery_status"],
            "external_send_started_at": optional_timestamp(record["external_send_started_at"]),
        },
        "failure": {"stage": record["error_stage"], "code": record["error_code"]},
    }


def get_admin_request_detail_response(request_id):
    try:
        record = get_admin_request_record(request_id)
        spans = get_admin_detection_spans(request_id)
        mappings = get_admin_masking_mappings(request_id)
        return Response({"request": admin_request_detail_body(record, spans, mappings)})
    except Request.DoesNotExist:
        return Response(error_body("REQUEST_NOT_FOUND", "의뢰를 찾을 수 없습니다."), status=404)
    except DatabaseError:
        return Response(
            error_body("ADMIN_REQUEST_READ_FAILED", "이용 상세를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."),
            status=503,
        )
