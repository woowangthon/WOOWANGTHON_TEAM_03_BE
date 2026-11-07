from django.urls import path

from .account_views import CreateUserView

app_name = "accounts_admin"

urlpatterns = [
    path("users/", CreateUserView.as_view(), name="user-create"),
]
