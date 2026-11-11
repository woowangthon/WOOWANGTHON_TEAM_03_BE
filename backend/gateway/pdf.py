"""비공개 저장소의 PDF 원본에서 검사용 글자를 뽑는다. 접수(TK-62)의 pypdf 구조 검증과는 별개다.

추출 규칙은 AI 저장소 src/extract.py 와 같다: 쪽마다 get_text("text", sort=True), 쪽 사이 빈 줄 하나, 글자를 고치지 않는다.
검출 구간 위치가 이 글자열 기준이므로 규칙을 바꾸면 AI 저장소와 함께 바꿔야 한다.
한도는 settings.PDF_MAX_BYTES·PDF_MAX_PAGES(접수 API 와 같은 값). 넘거나 못 읽으면 PdfError(code) 로 알린다."""
from dataclasses import dataclass

from django.conf import settings

PAGE_SEP = "\n\n"


class PdfError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class ExtractedPdf:
    text: str
    page_count: int
    size_bytes: int


def extract_pdf(data: bytes) -> ExtractedPdf:
    if len(data) > settings.PDF_MAX_BYTES:
        raise PdfError("PDF_TOO_LARGE", "PDF는 10MB 이내여야 합니다.")
    if not data.startswith(b"%PDF"):
        raise PdfError("INVALID_PDF", "PDF 파일을 읽을 수 없습니다.")
    try:
        import pymupdf
    except ImportError as e:  # pragma: no cover - 설치 누락은 배포 문제
        raise PdfError("PDF_SUPPORT_MISSING", "서버에 PDF 처리 모듈이 없습니다.") from e
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001 - pymupdf 는 손상 파일에 다양한 예외를 낸다
        raise PdfError("INVALID_PDF", "PDF를 열 수 없습니다. 파일이 손상되었을 수 있습니다.") from e
    try:
        if doc.needs_pass:
            raise PdfError("PDF_ENCRYPTED", "암호화되지 않은 PDF를 첨부해 주세요.")
        if doc.page_count > settings.PDF_MAX_PAGES:
            raise PdfError("PDF_PAGE_LIMIT", "PDF는 20쪽 이내여야 합니다.")
        text = PAGE_SEP.join(page.get_text("text", sort=True) for page in doc)
        page_count = doc.page_count
    finally:
        doc.close()
    if not text.strip():
        raise PdfError("PDF_NO_TEXT", "글자를 뽑을 수 없는 PDF입니다. 스캔 이미지일 수 있습니다.")
    return ExtractedPdf(text=text, page_count=page_count, size_bytes=len(data))
