"""가상 계정과 테스트 전용 뷰로 그룹 설정 및 API 접근 제한을 검사한다."""

from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import TestCase
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from .permissions import IsServiceAdmin
from .roles import ADMIN_GROUP_NAME, USER_GROUP_NAME, get_service_role


class MemberOnlyTestView(APIView):
    # 기본 권한 설정이 적용되는지 확인하기 위해 permission_classes를 재정의하지 않는다.
    def get(self, request):
        return Response({"allowed": True})


class AdminOnlyTestView(MemberOnlyTestView):
    permission_classes = [IsServiceAdmin]


class ServicePermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_service_groups", stdout=StringIO())
        cls.admin_group = Group.objects.get(name=ADMIN_GROUP_NAME)
        cls.user_group = Group.objects.get(name=USER_GROUP_NAME)
        cls.user = get_user_model().objects.create_user(
            username="ROLE0001", full_name="권한 검증용 가상 사용자", password="SecurePass1!"
        )

    def response_for(self, view, user=None):
        request = APIRequestFactory().get("/test-only/")
        if user is not None:
            force_authenticate(request, user=user)
        return view.as_view()(request)

    def test_group_setup_is_idempotent_and_preserves_existing_data(self):
        self.user.groups.add(self.user_group)
        permission = Permission.objects.get(
            content_type__app_label="auth", content_type__model="group", codename="view_group"
        )
        self.user_group.permissions.add(permission)
        initial_ids = (self.admin_group.pk, self.user_group.pk)
        output = StringIO()
        call_command("setup_service_groups", stdout=output)
        call_command("setup_service_groups", stdout=output)
        self.assertEqual(Group.objects.get(name=ADMIN_GROUP_NAME).pk, initial_ids[0])
        self.assertEqual(Group.objects.get(name=USER_GROUP_NAME).pk, initial_ids[1])
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertTrue(self.user.groups.filter(pk=self.user_group.pk).exists())
        self.assertTrue(self.user_group.permissions.filter(pk=permission.pk).exists())
        self.assertIn("기존 그룹 유지", output.getvalue())

    def test_anonymous_user_cannot_access_protected_views(self):
        for view in (MemberOnlyTestView, AdminOnlyTestView):
            with self.subTest(view=view.__name__):
                self.assertEqual(self.response_for(view).status_code, 403)

    def test_user_without_service_group_is_denied(self):
        for view in (MemberOnlyTestView, AdminOnlyTestView):
            with self.subTest(view=view.__name__):
                self.assertEqual(self.response_for(view, self.user).status_code, 403)

    def test_general_user_can_access_member_view_but_not_admin_view(self):
        self.user.groups.add(self.user_group)
        self.assertEqual(get_service_role(self.user), "user")
        self.assertEqual(self.response_for(MemberOnlyTestView, self.user).status_code, 200)
        self.assertEqual(self.response_for(AdminOnlyTestView, self.user).status_code, 403)

    def test_service_admin_does_not_need_staff_or_superuser_flags(self):
        self.user.groups.add(self.admin_group)
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.assertEqual(get_service_role(self.user), "admin")
        self.assertEqual(self.response_for(MemberOnlyTestView, self.user).status_code, 200)
        self.assertEqual(self.response_for(AdminOnlyTestView, self.user).status_code, 200)

    def test_staff_and_superuser_flags_do_not_bypass_service_groups(self):
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save(update_fields=["is_staff", "is_superuser"])
        self.assertEqual(self.response_for(AdminOnlyTestView, self.user).status_code, 403)
        self.user.groups.add(self.user_group)
        self.assertEqual(self.response_for(AdminOnlyTestView, self.user).status_code, 403)

    def test_inactive_account_is_denied_even_with_admin_group(self):
        self.user.groups.add(self.admin_group)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        for view in (MemberOnlyTestView, AdminOnlyTestView):
            with self.subTest(view=view.__name__):
                self.assertEqual(self.response_for(view, self.user).status_code, 403)

    def test_conflicting_service_groups_are_denied(self):
        self.user.groups.add(self.admin_group, self.user_group)
        self.assertIsNone(get_service_role(self.user))
        for view in (MemberOnlyTestView, AdminOnlyTestView):
            with self.subTest(view=view.__name__):
                self.assertEqual(self.response_for(view, self.user).status_code, 403)

    def test_removing_admin_membership_revokes_access(self):
        self.user.groups.add(self.admin_group)
        self.assertEqual(self.response_for(AdminOnlyTestView, self.user).status_code, 200)
        self.user.groups.remove(self.admin_group)
        self.assertEqual(self.response_for(AdminOnlyTestView, self.user).status_code, 403)
