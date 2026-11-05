import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from qualitative_ir_downloader.company_identity import domain_candidates, identify
from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.corporate_domain import (
    IMPLEMENTATION_VERSION,
    candidate_from_url,
    classify_verified,
    deterministic_domain_order,
    generated_candidates,
)
from qualitative_ir_downloader.ir_discovery import IRDiscovery, corporate_identity
from qualitative_ir_downloader.ir_state import candidate_from, load, upsert
from qualitative_ir_downloader.models import IRCandidate, Stock
from qualitative_ir_downloader.network import ResourceResponse


class CorporateDomainResolutionTests(unittest.TestCase):
    def test_foreign_legal_suffixes_are_brand_normalization_noise(self):
        cases = {
            "VODAFONE GROUP PUBLIC LTD CO": "VODAFONE",
            "Example Holdings plc": "Example",
            "Example AG": "Example",
            "Example NV": "Example",
            "Example N.V.": "Example",
            "Example SE": "Example",
            "Example S.p.A.": "Example",
            "Example AB": "Example",
            "Example ASA": "Example",
            "Example OYJ": "Example",
            "Example PTE LTD": "Example",
        }
        for legal_name, expected in cases.items():
            with self.subTest(legal_name=legal_name):
                self.assertEqual(identify(legal_name).normalized_company_name, expected)

    def test_adr_and_brand_aliases_are_candidates_not_proof(self):
        identity = identify("Example Group Limited Sponsored ADR")
        self.assertEqual(identity.normalized_company_name, "Example")
        self.assertIn("example.com", domain_candidates("Example Group Limited Sponsored ADR"))
        self.assertEqual(generated_candidates("International Holdings AG", corporate_brand="Acme")[0].domain, "acme.com")
        self.assertEqual(generated_candidates("International Holdings AG", corporate_brand="Acme")[0].source_type, "corporate_brand")

    def test_name_guess_is_weak_and_verified_brand_is_deterministic(self):
        candidates = generated_candidates("VODAFONE GROUP PUBLIC LTD CO")
        domains = [candidate.domain for candidate in candidates]
        self.assertIn("vodafone.com", domains)
        self.assertIn("vodafonegrouppublicltdco.com", domains)
        self.assertEqual(candidates[0].domain, "vodafone.com")
        self.assertEqual(candidates[-1].source_type, "legal_name_guess")
        verified = classify_verified(candidates[0], identity_match=True, ir_link_found=True)
        self.assertEqual(verified.verification_status, "VERIFIED_IR")
        self.assertEqual(deterministic_domain_order(candidates)[0].domain, "vodafone.com")

    def test_false_positive_domains_are_not_accepted(self):
        self.assertIsNone(candidate_from_url("https://www.google.com/search?q=vodafone", source_type="search"))
        self.assertFalse(corporate_identity(
            "https://vodafone.example.net/", "Vodafone Investor Relations",
            "A parked domain supplied by a registrar", "VODAFONE GROUP PUBLIC LTD CO",
        ))

    def test_cache_requires_current_domain_implementation(self):
        stock = Stock("VOD", "VODAFONE GROUP PUBLIC LTD CO", "manual", 0)
        candidate = IRCandidate(
            "https://www.vodafone.com/investors", 100,
            ["Company identity verified", "Direct Investors page"], True,
            corporate_url="https://www.vodafone.com/", content_validated=True,
        )
        with tempfile.TemporaryDirectory() as temp:
            upsert(Path(temp), stock, candidate)
            stored = load(Path(temp), stock)
            self.assertIsNotNone(stored)
            item = {"company_name": stock.company_name, "official": True,
                    "official_ir_url": candidate.url, "officiality_score": 100,
                    "evidence": candidate.evidence,
                    "domain_resolution_implementation": "old-domain-resolution"}
            self.assertIsNone(candidate_from(item, stock))
            self.assertEqual(load(Path(temp), stock).url, candidate.url)

    def test_incompatible_verified_cache_is_replaced_after_reverification(self):
        stock = Stock("VOD", "VODAFONE GROUP PUBLIC LTD CO", "manual", 0)
        candidate = IRCandidate(
            "https://www.vodafone.com/investors", 100,
            ["Company identity verified", "Direct Investors page"], True,
            corporate_url="https://www.vodafone.com/", content_validated=True,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "verified_ir.json").write_text(json.dumps({
                "VOD": {
                    "ticker": "VOD", "company_name": stock.company_name,
                    "official_ir_url": "https://legacy.example/investors",
                    "official_corporate_url": "https://legacy.example/",
                    "corporate_domain": "legacy.example", "official": True,
                    "officiality_score": 100, "evidence": ["Legacy verification"],
                    "domain_resolution_implementation": "old-domain-resolution",
                }
            }), encoding="utf-8")
            upsert(root, stock, candidate, source="discovery", confirmed=True)
            stored = json.loads((root / "verified_ir.json").read_text(encoding="utf-8"))["VOD"]
            self.assertEqual(stored["official_ir_url"], candidate.url)
            self.assertEqual(stored["corporate_domain"], "vodafone.com")
            self.assertEqual(stored["domain_resolution_implementation"], IMPLEMENTATION_VERSION)


class CorporateDomainDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_legal_guess_does_not_terminate_verified_candidate(self):
        browser = SimpleNamespace(
            config=Config(download_root=Path(tempfile.mkdtemp())),
            current_company=SimpleNamespace(scan_only=True),
            goto=AsyncMock(),
        )
        try:
            discovery = IRDiscovery(browser)
            discovery.ticker = "VOD"
            discovery.domain_candidates = generated_candidates("VODAFONE GROUP PUBLIC LTD CO")
            bad = "https://vodafonegrouppublicltdco.com/"
            good = "https://vodafone.com/"

            async def http(url, *args, **kwargs):
                if "vodafonegrouppublicltdco.com" in url:
                    raise OSError("Name or service not known")
                return ResourceResponse(
                    good, 200, {"content-type": "text/html"},
                    b"<title>Vodafone Investors</title><p>About Vodafone Investors Careers Products. Copyright Vodafone Group Public Ltd Co.</p><a href='https://www.vodafone.com/investors'>Investors</a>",
                    "http",
                )

            with patch("qualitative_ir_downloader.network.ResourceClient.http", new=AsyncMock(side_effect=http)):
                result = await discovery.discover_ir_from_corporate_site(
                    SimpleNamespace(context=AsyncMock()), [bad, good], "VODAFONE GROUP PUBLIC LTD CO"
                )
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0].official)
            self.assertEqual(discovery.ecosystem.official_corporate_domain, "vodafone.com")
            codes = [item["code"] for item in discovery.domain_diagnostics]
            self.assertIn("CORPORATE_DOMAIN_DNS_FAILED", codes)
            self.assertIn("CORPORATE_DOMAIN_VERIFIED", codes)
        finally:
            import shutil
            shutil.rmtree(browser.config.download_root, ignore_errors=True)

