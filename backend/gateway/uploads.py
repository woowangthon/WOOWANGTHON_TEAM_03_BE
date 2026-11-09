"""큰 업로드를 메모리나 임시 파일에 전부 저장하기 전에 제한한다."""

from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload


class LimitedPDFUploadHandler(FileUploadHandler):
    def __init__(self, request=None):
        super().__init__(request)
        self.received = 0
        self.file_count = 0

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.file_count += 1
        if self.file_count > 1:
            self.request.intake_upload_error = "MULTIPLE_FILES"
            raise StopUpload(connection_reset=False)

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > settings.PDF_MAX_BYTES:
            self.request.intake_upload_error = "PDF_TOO_LARGE"
            raise StopUpload(connection_reset=False)
        return raw_data

    def file_complete(self, file_size):
        return None
