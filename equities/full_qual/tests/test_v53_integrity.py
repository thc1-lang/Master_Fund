import json
from types import SimpleNamespace

from management_intelligence.board_composition import infer_independence
from qualitative_ir_downloader.google_sheets_publisher import (
    _v51_economic_object,
    _v51_kpis,
    _v51_relationships,
    _v4_sector_adapter,
)
from research_output.assembler import _ticker_match
from research_output.assembler import assemble_dossier


def test_time_deposit_investment_is_not_customer_deposit():
    assert _v51_economic_object(
        "investments in time deposits classified as held-to-maturity debt securities"
    ) == "TIME_DEPOSIT_INVESTMENT"


def test_customer_deposit_remains_a_valid_bank_object():
    assert _v51_economic_object("average customer deposits increased") == "CUSTOMER_DEPOSIT"


def test_sector_adapter_requires_linked_bank_evidence_not_stale_factor_label():
    payload = {
        "demand_assessment": {
            "industry_adapter": "bank-v1",
            "factors": [{"factor": "FUNDING_QUALITY", "evidence": ["cat-funding"]}],
        }
    }
    context = {"claims": [{"claim_id": "cat-funding", "claim_text": "Pension funding obligations may impair liquidity.", "pit_eligible": True}], "facts": []}
    assert _v4_sector_adapter(payload, context) == "generic"


def test_sector_adapter_preserves_bank_graph_when_linked_evidence_is_banking():
    payload = {
        "demand_assessment": {
            "industry_adapter": "bank-v1",
            "factors": [
                {"factor": "DEPOSIT_FRANCHISE_QUALITY", "evidence": ["deposit"]},
                {"factor": "CREDIT_QUALITY", "evidence": ["loan"]},
            ],
        }
    }
    context = {"claims": [
        {"claim_id": "deposit", "claim_text": "Customer deposits increased and interest-bearing deposits grew.", "pit_eligible": True},
        {"claim_id": "loan", "claim_text": "The loan portfolio and commercial banking balances grew.", "pit_eligible": True},
    ], "facts": []}
    assert _v4_sector_adapter(payload, context) == "bank"


def test_generic_causal_graph_does_not_emit_bank_mechanism_for_stale_hint():
    payload = {
        "demand_assessment": {
            "industry_adapter": "bank-v1",
            "factors": [{"factor": "FUNDING_QUALITY", "evidence": ["funding"]}],
        }
    }
    context = {"claims": [{"claim_id": "funding", "claim_text": "Pension funding obligations may impair liquidity.", "pit_eligible": True}], "facts": []}
    kpi = {"kpi_id": "kpi:revenue", "kpi": "REVENUE", "canonical_metric": "REVENUE", "parent_concept": "REVENUE", "kpi_type": "MODEL_READY_KPI", "evidence_ids": ["k"], "source_status": "VERIFIED_INTERNAL", "evidence_strength": "HIGH"}
    driver = {"driver_id": "driver:pricing", "driver": "PRICING", "driver_type": "PROXY_REQUIRED", "evidence_ids": ["d"], "source_status": "VERIFIED_INTERNAL", "evidence_strength": "HIGH", "mechanism": "higher pricing increases revenue"}
    sector = _v4_sector_adapter(payload, context)
    accepted, _rejected, _diagnostics = _v51_relationships([kpi], [driver], "TST", sector=sector)
    assert accepted and accepted[0]["sector_mechanism_id"] == "generic:pricing_to_revenue"


def test_explicit_issuer_field_blocks_substring_ticker_contamination():
    assert not _ticker_match(
        {"ticker": "ZM", "company_name": "Zoom Communications, Inc.", "source_url": "https://example.test/catalyst"},
        "CAT",
    )
    assert _ticker_match({"ticker": "CAT", "company_name": "Caterpillar Inc."}, "CAT")


def test_governance_independence_is_tri_state():
    row = SimpleNamespace(evidence_text="Director A | Yes | independent")
    person = SimpleNamespace(company_name="Example Inc", full_name="Director A")
    assert infer_independence(row, person)[0] == "INDEPENDENT"
    row.evidence_text = "Director B | No | not independent"
    assert infer_independence(row, person)[0] == "NOT_INDEPENDENT"
    row.evidence_text = "Director C; no classification disclosed"
    assert infer_independence(row, person)[0] == "UNKNOWN"


def test_kpi_sentence_fragment_is_not_primary(monkeypatch):
    import qualitative_ir_downloader.google_sheets_publisher as publisher

    monkeypatch.setattr(
        publisher,
        "_V5_KPIS_BASE",
        lambda payload, context, ticker, cutoff: [
            {
                "kpi_id": "k1",
                "kpi": "LIABILITIES_ASSUMED_AS_OF_THE_ACQUISITION_DATE_WERE_APPROXIMATELY_38_MILLION",
                "display_name": "Liabilities assumed as of the acquisition date were approximately 38 million",
                "definition": "one-off acquisition disclosure",
                "metric_type": "FACT",
                "unit": "USD",
                "frequency": "ONCE",
                "segment": "CONSOLIDATED",
                "scope_type": "CONSOLIDATED",
                "scope_name": "CONSOLIDATED",
                "financial_transmission": "UNKNOWN",
                "latest_value": "VALUE_NOT_CAPTURED",
                "latest_period": "NOT_CAPTURED",
                "source_status": "VERIFIED_INTERNAL",
                "evidence_ids": ["c1"],
            }
        ],
    )
    rows = _v51_kpis({}, {"claims": [{"claim_id": "c1", "claim_text": "liabilities assumed as of the acquisition date were approximately $38 million", "pit_eligible": True}], "facts": []}, "CAT", "2026-09-25")
    assert rows[0]["kpi_type"] in {"REJECTED_KPI", "NON_MODEL_FINANCIAL_FACT"}


def test_sequential_issuer_assembly_does_not_reuse_identity(tmp_path):
    analysis = tmp_path / "qualitative_analysis"
    analysis.mkdir()
    (analysis / "documents.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"ticker": "CAT", "company_name": "CATERPILLAR INC", "issuer_cik": "0000018230", "document_id": "cat-doc"},
                {"ticker": "ZM", "company_name": "Zoom Communications, Inc.", "issuer_cik": "0001585521", "document_id": "zm-doc"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    identity_root = tmp_path / "issuer_identity"
    identity_root.mkdir()
    (identity_root / "CAT.json").write_text(json.dumps({"requested_ticker": "CAT", "resolved_ticker": "CAT", "legal_name": "CATERPILLAR INC", "issuer_cik": "0000018230"}), encoding="utf-8")
    (identity_root / "ZM.json").write_text(json.dumps({"requested_ticker": "ZM", "resolved_ticker": "ZM", "legal_name": "Zoom Communications, Inc.", "issuer_cik": "0001585521"}), encoding="utf-8")

    cat = assemble_dossier("CAT", "2026-09-25", artifacts_root=tmp_path)
    zm = assemble_dossier("ZM", "2026-09-25", artifacts_root=tmp_path)
    assert (cat.company_name, cat.issuer_cik) == ("CATERPILLAR INC", "0000018230")
    assert (zm.company_name, zm.issuer_cik) == ("Zoom Communications, Inc.", "0001585521")

