from django.urls import path

from .account_views import CreateUserView
from .password_views import ResetUserPasswordView
from .profile_views import ChangeUserNameView

app_name = "accounts_admin"

urlpatterns = [
    path("users/", CreateUserView.as_view(), name="user-create"),
    path("users/<uuid:user_id>/name/", ChangeUserNameView.as_view(), name="user-name-change"),
    path("users/<uuid:user_id>/password/reset/", ResetUserPasswordView.as_view(), name="password-reset"),
]
