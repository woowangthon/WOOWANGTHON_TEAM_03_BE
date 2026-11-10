from django.urls import path

from .views import (
    RequestApprovalDecisionView,
    RequestApprovalPreviewView,
    RequestFileView,
    RequestIntakeDetailView,
    RequestIntakeView,
)

app_name = "gateway"

urlpatterns = [
    path("", RequestIntakeView.as_view(), name="request-intake"),
    path("<uuid:request_id>/", RequestIntakeDetailView.as_view(), name="request-intake-detail"),
    path("<uuid:request_id>/file/", RequestFileView.as_view(), name="request-file"),
    path("<uuid:request_id>/approval-preview/", RequestApprovalPreviewView.as_view(), name="request-approval-preview"),
    path("<uuid:request_id>/approval/", RequestApprovalDecisionView.as_view(), name="request-approval-decision"),
]
