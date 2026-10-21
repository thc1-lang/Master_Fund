from __future__ import annotations

from qualitative_analysis.extraction_rules import matching_rules, primary_rule


def _dimensions(text: str) -> list[str]:
    return [rule.dimension for rule in matching_rules(text)]


def test_retail_context_activates_retail_dimensions():
    dimensions = _dimensions("Retail stores delivered positive comparable sales and e-commerce growth.")
    assert "comparable_sales" in dimensions
    assert "ecommerce_growth" in dimensions


def test_bank_words_without_retail_context_do_not_activate_retail_ontology():
    dimensions = _dimensions(
        "The bank discussed markdown inventory, unit shrink, digital commerce and wage pressure, but no store or retail operation."
    )
    assert not set(dimensions) & {
        "comparable_sales", "same_store_sales", "average_ticket", "ecommerce_sales",
        "ecommerce_growth", "digital_penetration", "membership_revenue",
        "membership_growth", "retail_media", "store_count", "unit_expansion",
        "store_remodels", "markdowns", "shrink", "private_label_mix",
    }
    assert "wage_pressure" not in dimensions  # no wage-pressure proposition


def test_genuine_cross_sector_inventory_and_wage_claims_remain_available():
    dimensions = _dimensions("Inventory increased 4% while wage pressure remained elevated.")
    assert "inventory_growth" in dimensions
    assert "wage_pressure" in dimensions


def test_credit_portfolio_var_is_not_revenue():
    text = (
        "Credit Portfolio VaR includes the derivative CVA, hedges of the CVA and credit protection "
        "purchased against certain retained loans and lending-related commitments, which are reported "
        "in principal transactions revenue."
    )
    dimensions = _dimensions(text)
    assert "revenue" not in dimensions
    assert {"credit_risk", "var", "cva"}.issubset(dimensions)
    assert primary_rule(text, matching_rules(text)).dimension == "credit_risk"

