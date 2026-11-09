"""권한 확인 API를 통해서만 제공하는 PDF 원본 저장소."""

from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateDocumentStorage(FileSystemStorage):
    def url(self, name):
        raise NotImplementedError("원본은 권한 확인 API로만 조회합니다.")


def document_storage():
    return PrivateDocumentStorage(
        location=settings.PRIVATE_DOCUMENT_ROOT,
        file_permissions_mode=0o600,
        directory_permissions_mode=0o700,
    )
