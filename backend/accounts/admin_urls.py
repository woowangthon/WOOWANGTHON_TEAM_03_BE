from django.urls import path

from .account_views import CreateUserView
from .password_views import ResetUserPasswordView

app_name = "accounts_admin"

urlpatterns = [
    path("users/", CreateUserView.as_view(), name="user-create"),
    path("users/<uuid:user_id>/password/reset/", ResetUserPasswordView.as_view(), name="password-reset"),
]
