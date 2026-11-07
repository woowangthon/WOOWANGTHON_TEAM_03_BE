"""서비스 권한의 기준은 Django Group 소속이며 별도 role 컬럼은 사용하지 않는다."""

ADMIN_GROUP_NAME = "service_admin"
USER_GROUP_NAME = "service_user"
SERVICE_GROUP_NAMES = (ADMIN_GROUP_NAME, USER_GROUP_NAME)


def get_service_role(user):
    """활성 계정의 단일 서비스 역할을 반환한다. 미지정·중복 소속은 거부한다."""
    if not user or not user.is_authenticated or not user.is_active:
        return None

    group_names = set(
        user.groups.filter(name__in=SERVICE_GROUP_NAMES).values_list("name", flat=True)
    )
    # 잘못된 이중 소속을 관리자 우선으로 해석해 권한을 확대하지 않는다.
    if group_names == {ADMIN_GROUP_NAME}:
        return "admin"
    if group_names == {USER_GROUP_NAME}:
        return "user"
    return None
