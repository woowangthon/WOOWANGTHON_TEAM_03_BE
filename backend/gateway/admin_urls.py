from django.urls import path

from .admin_views import AdminRequestDetailView, AdminRequestListView

app_name = "gateway_admin"

urlpatterns = [
    path("requests/", AdminRequestListView.as_view(), name="request-list"),
    path("requests/<uuid:request_id>/", AdminRequestDetailView.as_view(), name="request-detail"),
]
