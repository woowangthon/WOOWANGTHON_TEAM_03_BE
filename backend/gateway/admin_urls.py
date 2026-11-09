from django.urls import path

from .admin_views import AdminRequestListView

app_name = "gateway_admin"

urlpatterns = [
    path("requests/", AdminRequestListView.as_view(), name="request-list"),
]
