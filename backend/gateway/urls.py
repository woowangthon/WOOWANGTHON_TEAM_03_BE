from django.urls import path

from .views import RequestFileView, RequestIntakeDetailView, RequestIntakeView

app_name = "gateway"

urlpatterns = [
    path("", RequestIntakeView.as_view(), name="request-intake"),
    path("<uuid:request_id>/", RequestIntakeDetailView.as_view(), name="request-intake-detail"),
    path("<uuid:request_id>/file/", RequestFileView.as_view(), name="request-file"),
]
