import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pypdf import PdfWriter

from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.browser import Browser
from qualitative_ir_downloader.document_targets import resolve_document_target
from qualitative_ir_downloader.downloader import Downloader
from qualitative_ir_downloader.models import Document
from qualitative_ir_downloader.crawler import control_kind


def _pdf() -> bytes:
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.write(stream)
    return stream.getvalue()


class DocumentTargetTests(unittest.TestCase):
    def test_query_parameter_wrapper_resolves_relative_asset(self):
        wrapper = "https://www.example.com/pdf-viewer.aspx?gid=1&src=%2Fdocuments%2Fannual-report.pdf"
        target = resolve_document_target(wrapper)
        self.assertTrue(target.wrapper_detected)
        self.assertEqual(target.resolution_method, "QUERY_PARAM_SRC")
        self.assertEqual(target.canonical_document_url, "https://www.example.com/documents/annual-report.pdf")
        self.assertEqual(target.document_type_hint, "annual_report")

    def test_vodafone_style_viewer_resolves_without_issuer_specific_logic(self):
        wrapper = (
            "https://www.vodafone.com/pdf-viewer.aspx?gid=514358169&"
            "src=%2F~%2Fmedia%2FFiles%2FV%2Fvodafone%2Fcorp%2Fresults-and-presentations%2F"
            "vodafone-fy27-q1-trading-update.pdf"
        )
        target = resolve_document_target(wrapper)
        self.assertTrue(target.wrapper_detected)
        self.assertEqual(target.resolution_method, "QUERY_PARAM_SRC")
        self.assertEqual(
            target.canonical_document_url,
            "https://www.vodafone.com/~/media/Files/V/vodafone/corp/results-and-presentations/vodafone-fy27-q1-trading-update.pdf",
        )
        self.assertEqual(target.document_type_hint, "trading_update")

    def test_html_embedded_targets_are_resolved_in_priority_order(self):
        body = b"<iframe src='/docs/report.pdf'></iframe><a href='/docs/other.pdf'>Download PDF</a>"
        target = resolve_document_target("https://ir.example.com/viewer", {"body": body, "headers": {"content-type": "text/html"}})
        self.assertEqual(target.resolution_method, "DOWNLOAD_LINK")
        self.assertEqual(target.canonical_document_url, "https://ir.example.com/docs/other.pdf")
        for html, method in (
            (b"<iframe src='/docs/report.pdf'></iframe>", "IFRAME_SRC"),
            (b"<embed src='/docs/report.pdf'>", "EMBED_SRC"),
            (b"<object data='/docs/report.pdf'></object>", "OBJECT_DATA"),
        ):
            with self.subTest(method=method):
                result = resolve_document_target("https://ir.example.com/viewer", html)
                self.assertEqual(result.resolution_method, method)

    def test_bad_embedded_targets_are_rejected(self):
        for value in ("javascript:alert(1)", "file:///etc/passwd", "http://localhost/report.pdf", "http://127.0.0.1/report.pdf", "https://unrelated-malicious-domain.example/report.pdf"):
            with self.subTest(value=value):
                result = resolve_document_target("https://ir.example.com/viewer?src=" + value)
                self.assertNotEqual(result.canonical_document_url, value)
                self.assertFalse(result.identity_eligible)

    def test_verified_foreign_issuer_cdn_relationship_is_allowed(self):
        target = resolve_document_target(
            "https://ir.example.co.uk/viewer?file=https%3A%2F%2Fcdn.example.net%2Fhalf-year-report.pdf",
            issuer_context={
                "official_corporate_domain": "example.co.uk",
                "verified_related_domains": ["cdn.example.net"],
            },
        )
        self.assertTrue(target.identity_eligible)
        self.assertEqual(target.resolution_method, "QUERY_PARAM_FILE")
        self.assertEqual(target.final_domain, "example.net")

    def test_control_filter_avoids_irrelevant_widgets(self):
        self.assertEqual(control_kind("Load More Reports"), "LOAD_MORE")
        self.assertEqual(control_kind("Page 2"), "NEXT_PAGE")
        self.assertEqual(control_kind("1"), "REJECTED_UNRELATED")
        self.assertEqual(control_kind("Annual Reports", role="tab"), "REPORT_CATEGORY")
        for label in ("LSE", "NASDAQ", "Share Price", "Footer", "Cookie settings"):
            with self.subTest(label=label):
                self.assertEqual(control_kind(label, role="tab"), "REJECTED_UNRELATED")


class DownloaderWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapper_download_and_resolved_url_deduplication(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "Reports").mkdir()
            downloader = Downloader(Browser(Config(request_delay=0)), AsyncMock(), folder, "VOD")
            downloader.fetch_pdf = AsyncMock(return_value=(_pdf(), "https://www.example.com/docs/report.pdf"))
            first = await downloader.save(Document(
                "Annual Report", "2026-01-01",
                "https://www.example.com/viewer?src=%2Fdocs%2Freport.pdf", "reports",
                "https://www.example.com/viewer?src=%2Fdocs%2Freport.pdf",
            ))
            second = await downloader.save(Document(
                "Annual Report duplicate", "2026-01-01",
                "https://www.example.com/viewer?src=%2Fdocs%2Freport.pdf&gid=2", "reports",
                "https://www.example.com/viewer?src=%2Fdocs%2Freport.pdf&gid=2",
            ))
            self.assertTrue(first.wrapper_detected)
            self.assertEqual(first.wrapper_resolution_method, "QUERY_PARAM_SRC")
            self.assertEqual(first.resolved_document_url, "https://www.example.com/docs/report.pdf")
            self.assertIsNotNone(second.duplicate_of)
            downloader.fetch_pdf.assert_awaited_once()

    async def test_wrong_content_type_is_accepted_when_magic_bytes_are_pdf(self):
        downloader = Downloader(Browser(Config(request_delay=0)), AsyncMock(), Path(tempfile.mkdtemp()), "ACME")
        response = type("Response", (), {
            "body": _pdf(), "url": "https://ir.example.com/report.pdf", "status": 200,
            "headers": {"content-type": "text/html; charset=utf-8"},
        })()
        client = AsyncMock()
        client.get = AsyncMock(return_value=response)
        with patch("qualitative_ir_downloader.network.ResourceClient", return_value=client):
            data, url = await downloader.fetch_pdf("https://ir.example.com/report.pdf", "https://ir.example.com/")
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertEqual(url, response.url)


class ConsentHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_onetrust_consent_is_handled_once_per_context(self):
        class Control:
            def __init__(self, visible):
                self.visible = visible
                self.clicks = 0

            @property
            def first(self):
                return self

            async def count(self):
                return 1 if self.visible else 0

            async def is_visible(self):
                return self.visible

            async def click(self, **kwargs):
                self.clicks += 1
                self.visible = False

        class Page:
            context = object()

            def __init__(self):
                self.reject = Control(True)

            def locator(self, selector):
                return self.reject if selector == "#onetrust-reject-all-handler" else Control(False)

            def get_by_role(self, *args, **kwargs):
                return Control(False)

        browser = Browser(Config())
        page = Page()
        await browser.dismiss_cookies(page)
        await browser.dismiss_cookies(page)
        self.assertEqual(page.reject.clicks, 1)


if __name__ == "__main__":
    unittest.main()
