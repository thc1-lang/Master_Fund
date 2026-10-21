"""Small, auditable deterministic rules used by the initial provider."""

from __future__ import annotations

import re
from datetime import datetime
from dataclasses import dataclass
from typing import Any

from .extraction_models import ClaimCandidate, CERTAINTY_VALUES, DIMENSIONS, DIRECTION_VALUES


@dataclass(frozen=True)
class ExtractionRule:
    dimension: str
    topic: str
    patterns: tuple[str, ...]


RULES: tuple[ExtractionRule, ...] = (
    # Retail metrics come before broad revenue/volume/margin rules.  A single
    # sentence still becomes one persisted claim; the additional dimensions
    # are retained as structured secondary evidence below.
    ExtractionRule("international_comparable_sales", "international comparable sales", (r"\binternational\b.{0,45}\b(?:comp(?:arable)?|same[- ]store)\s+sales\b", r"\b(?:comp(?:arable)?|same[- ]store)\s+sales\b.{0,45}\binternational\b")),
    ExtractionRule("comparable_sales", "comparable sales", (r"\b(?:comparable|comp|same[- ]store)\s+sales\b", r"\bsame[- ]store\s+(?:sales|growth)\b")),
    ExtractionRule("same_store_sales", "same-store sales", (r"\bsame[- ]store\s+(?:sales|growth)\b", r"\bSSS\b")),
    ExtractionRule("traffic_transactions", "traffic and transactions", (r"\b(?:customer\s+)?traffic\b", r"\btransactions?\b.{0,35}\b(?:grew|growth|increased|declined|decreased|sales)\b")),
    ExtractionRule("average_ticket", "average ticket", (r"\baverage\s+(?:ticket|basket)\b", r"\b(?:ticket|basket)\s+size\b")),
    ExtractionRule("unit_volume", "unit volume", (r"\bunit\s+(?:volume|sales)\b", r"\bunits?\b.{0,30}\b(?:sold|volume|grew|growth)\b")),
    ExtractionRule("ecommerce_growth", "e-commerce growth", (r"\b(?:e[- ]?commerce|digital|online)\b.{0,40}\b(?:grew|growth|increased|declined|decreased|sales|penetration)\b", r"\be[- ]?commerce\s+growth\b")),
    ExtractionRule("ecommerce_sales", "e-commerce sales", (r"\b(?:e[- ]?commerce|digital|online)\s+(?:sales|business|orders?)\b", r"\bonline\s+sales\b")),
    ExtractionRule("digital_penetration", "digital penetration", (r"\bdigital\s+penetration\b", r"\bonline\s+penetration\b")),
    ExtractionRule("membership_growth", "membership growth", (r"\bmembership\b.{0,40}\b(?:grew|growth|increased|declined|decreased|members?)\b", r"\bmembers?\b.{0,35}\b(?:grew|growth|increased|declined|decreased)\b")),
    ExtractionRule("membership_revenue", "membership revenue", (r"\bmembership\s+(?:revenue|income|fees?)\b", r"\bmembership\b.{0,30}\b(?:revenue|income|fees?)\b")),
    ExtractionRule("advertising_revenue", "advertising revenue", (r"\b(?:advertising|ads?|retail\s+media)\b.{0,35}\b(?:revenue|sales|income|grew|growth)\b", r"\b(?:advertising|retail\s+media)\s+revenue\b")),
    ExtractionRule("retail_media", "retail media", (r"\bretail\s+media\b", r"\badvertising\s+business\b")),
    ExtractionRule("store_count", "store count", (r"\b(?:store|club|location)s?\b.{0,30}\b(?:count|number|total)\b", r"\b(?:operat(?:e|ing)|have|with)\s+\d[\d,]*\s+(?:stores?|clubs?|locations?)\b")),
    ExtractionRule("unit_expansion", "unit expansion", (r"\b(?:new|additional)\s+(?:stores?|clubs?|locations?)\b", r"\b(?:store|unit|club)\s+(?:expansion|openings?|growth)\b")),
    ExtractionRule("store_remodels", "store remodels", (r"\b(?:store|club)\s+(?:remodel|renovation)s?\b", r"\bremodel(?:ed|ing)?\b")),
    ExtractionRule("gross_margin", "gross margin", (r"\bgross\s+(?:margin|profit\s+rate)\b", r"\bgross\s+profit\s+(?:rate|margin)\b")),
    ExtractionRule("operating_margin", "operating margin", (r"\boperating\s+margin\b", r"\boperating\s+income\s+margin\b")),
    ExtractionRule("operating_expense_rate", "operating expense rate", (r"\boperating\s+(?:expense|cost)\s+(?:rate|ratio)\b", r"\boperating\s+expenses?\b.{0,45}\b(?:percent|percentage|%|rate|basis\s+points?|bps)\b")),
    ExtractionRule("inventory_growth", "inventory growth", (r"\binventor(?:y|ies)\b.{0,35}\b(?:grew|growth|increased|declined|decreased|higher|lower)\b", r"\b(?:grew|growth|increased|declined|decreased)\b.{0,35}\binventor(?:y|ies)\b")),
    ExtractionRule("inventory_efficiency", "inventory efficiency", (r"\binventory\s+(?:turns?|efficiency|productivity)\b", r"\b(?:turns?|productivity)\b.{0,30}\binventory\b")),
    ExtractionRule("markdowns", "markdowns", (r"\bmarkdown(?:s|ing)?\b", r"\bpromotional\s+markdown\b")),
    ExtractionRule("shrink", "shrink", (r"\bshrink(?:age)?\b", r"\bshrink\s+rate\b")),
    ExtractionRule("labor_costs", "labor costs", (r"\blabor\s+(?:cost|costs|expense|expenses)\b", r"\bwage\s+expense\b")),
    ExtractionRule("wage_pressure", "wage pressure", (r"\bwage\s+(?:pressure|inflation|growth|increases?)\b", r"\blabor\s+inflation\b")),
    ExtractionRule("sales_mix", "sales mix", (r"\bsales\s+mix\b", r"\bproduct\s+mix\b")),
    ExtractionRule("price_investment", "price investment", (r"\bprice\s+investment\b", r"\binvest(?:ed|ing)\s+in\s+(?:price|pricing)\b")),
    ExtractionRule("private_label_mix", "private-label mix", (r"\bprivate[- ]label\b.{0,30}\b(?:mix|sales|growth)\b", r"\bstore[- ]brand\b.{0,30}\b(?:mix|sales)\b")),
    ExtractionRule("supply_chain_cost", "supply-chain cost", (r"\bsupply[- ]chain\s+(?:cost|costs|expense|inflation)\b", r"\bfreight\s+(?:cost|costs)\b")),
    ExtractionRule("fulfillment_cost", "fulfillment cost", (r"\bfulfillment\s+(?:cost|costs|expense|expenses)\b", r"\blast[- ]mile\s+(?:cost|costs)\b")),
    ExtractionRule("automation", "automation", (r"\bautomation\b", r"\bautomated\s+(?:fulfillment|distribution|stores?)\b")),
    # Risk-table footnotes often mention the accounting line where a risk
    # measure is reported.  The risk measure is the economic proposition;
    # the nearby revenue header is only table context.
    ExtractionRule("market_risk", "market risk", (r"\b(?:market[- ]making|market\s+risk|trading)\b.{0,35}\bVaR\b", r"\bVaR\b.{0,35}\b(?:market|trading)\b")),
    ExtractionRule("credit_risk", "credit risk", (r"\bcredit\s+(?:portfolio|risk|protection|spread)\b", r"\b(?:CVA|credit\s+valuation\s+adjustment)\b")),
    ExtractionRule("var", "value at risk", (r"\bVaR\b", r"\bvalue[- ]at[- ]risk\b")),
    ExtractionRule("cva", "credit valuation adjustment", (r"\bCVA\b", r"\bcredit\s+valuation\s+adjustment\b")),
    ExtractionRule("currency_effect", "currency effect", (r"\b(?:currency|foreign exchange|FX)\b.{0,35}\b(?:impact|effect|headwind|tailwind|translation)\b", r"\b(?:impact|effect|headwind|tailwind)\b.{0,35}\b(?:currency|foreign exchange|FX)\b")),
    ExtractionRule("customer_frequency", "customer frequency", (r"\bcustomer\s+frequency\b", r"\bshopping\s+frequency\b")),
    ExtractionRule("net_interest_income", "net interest income", (r"\bnet\s+interest\s+income\b", r"\bNII\b")),
    ExtractionRule("net_interest_margin", "net interest margin", (r"\bnet\s+interest\s+margin\b", r"\bNIM\b")),
    ExtractionRule("deposits", "deposits", (r"\bdeposits?\b", r"\bdeposit franchise\b")),
    ExtractionRule("deposit_growth", "deposit growth", (r"\bdeposit(?:s)?\b.{0,40}\b(?:grew|growth|increased|declined|decreased)\b", r"\b(?:grew|growth|increased|declined|decreased)\b.{0,40}\bdeposit(?:s)?\b")),
    ExtractionRule("deposit_mix", "deposit mix", (r"\bdeposit mix\b", r"\bchecking|savings|term deposits?\b")),
    ExtractionRule("deposit_beta", "deposit beta", (r"\bdeposit beta\b", r"\bbeta of deposits?\b")),
    ExtractionRule("loan_growth", "loan growth", (r"\bloans?\b.{0,40}\b(?:grew|growth|increased|declined|decreased)\b", r"\bloan growth\b")),
    ExtractionRule("loan_mix", "loan mix", (r"\bloan mix\b", r"\bcommercial|consumer|mortgage\b.{0,20}\bloans?\b")),
    ExtractionRule("credit_quality", "credit quality", (r"\bcredit quality\b", r"\bcredit performance\b")),
    ExtractionRule("net_charge_offs", "net charge-offs", (r"\bnet\s+charge[- ]offs?\b", r"\bcharge[- ]offs?\b")),
    ExtractionRule("delinquencies", "delinquencies", (r"\bdelinquenc(?:y|ies)\b", r"\bpast due\b")),
    ExtractionRule("provisions", "provisions", (r"\bprovision(?:s)?\s+for\s+credit\s+loss(?:es)?\b", r"\bcredit loss provision\b")),
    ExtractionRule("allowance_reserves", "allowance and reserves", (r"\ballowance for credit losses\b", r"\bloan loss reserves?\b")),
    ExtractionRule("cet1", "CET1", (r"\bCET1\b", r"\bcommon equity tier 1\b")),
    ExtractionRule("risk_weighted_assets", "risk-weighted assets", (r"\brisk[- ]weighted assets?\b", r"\bRWA\b")),
    ExtractionRule("capital_ratios", "capital ratios", (r"\bcapital ratios?\b", r"\bTier 1 ratio\b", r"\bleverage ratio\b")),
    ExtractionRule("liquidity", "liquidity", (r"\bliquidity\b", r"\bliquidity coverage ratio\b")),
    ExtractionRule("rotce_roe", "ROTCE / ROE", (r"\bROTCE\b", r"\breturn on tangible common equity\b", r"\breturn on equity\b", r"\bROE\b")),
    ExtractionRule("investment_banking_fees", "investment-banking fees", (r"\binvestment banking fees?\b", r"\bunderwriting fees?\b")),
    ExtractionRule("markets_trading_revenue", "markets and trading revenue", (r"\bmarkets? revenue\b", r"\btrading revenue\b")),
    ExtractionRule("asset_management_flows", "asset-management flows", (r"\basset management flows?\b", r"\bnet flows?\b.{0,30}\basset management\b")),
    ExtractionRule("aum", "assets under management", (r"\bassets? under management\b", r"\bAUM\b")),
    ExtractionRule("auc", "assets under custody", (r"\bassets? under custody\b", r"\bAUC\b")),
    ExtractionRule("card_payment_volume", "card and payment volume", (r"\bcard (?:spending|sales|volume)\b", r"\bpayment volumes?\b")),
    ExtractionRule("efficiency_ratio", "efficiency ratio", (r"\befficiency ratio\b", r"\bcost[- ]to[- ]income ratio\b")),
    ExtractionRule("operating_leverage", "operating leverage", (r"\boperating leverage\b", r"\bpositive operating leverage\b")),
    ExtractionRule("capital_return", "capital return", (r"\bcapital returns?\b", r"\breturn(?:ed|ing) capital\b")),
    ExtractionRule("regulatory_capital", "regulatory capital", (r"\bregulatory capital\b", r"\bcapital requirement\b")),
    ExtractionRule("stress_test", "stress test and CCAR", (r"\bstress tests?\b", r"\bCCAR\b", r"\bDodd[- ]Frank\b")),
    ExtractionRule("balance_sheet_optimization", "balance-sheet optimization", (r"\bbalance[- ]sheet optim(?:i|)z(?:ation|e|ed|ing)\b", r"\bbalance[- ]sheet repositioning\b")),
    ExtractionRule("guidance", "outlook", (r"\bguidance\b", r"\boutlook\b", r"\bforecast\b", r"\bguide(?:d|s|line)?\b", r"\bexpect(?:ed|s|ing)?\b")),
    ExtractionRule("demand", "demand", (r"\bdemand\b", r"\binterest\b", r"\bbookings?\b", r"\bwin rate\b")),
    ExtractionRule("revenue", "revenue", (r"\brevenue\b", r"\bnet sales\b", r"\bsales (?:grew|increased|were|of|to)\b", r"\bARR\b", r"\bannual recurring revenue\b")),
    ExtractionRule("pricing", "pricing", (r"\bpric(?:e|ing|ed)\b", r"\bprice increase\b", r"\bupsell\b")),
    ExtractionRule("volume", "volume", (r"\bvolume\b", r"\bunits?\b", r"\bshipments?\b")),
    ExtractionRule("margins", "margin", (r"\b(?:gross|operating|EBITDA|adjusted) margin\b", r"\bmargin(?:s)?\b")),
    ExtractionRule("costs", "cost", (r"\bcost(?:s)?\b", r"\bexpenses?\b", r"\bspend(?:ing)?\b")),
    ExtractionRule("operating_expenses", "operating expenses", (r"\boperating expenses?\b", r"\bOpEx\b")),
    ExtractionRule("capex", "capital expenditure", (r"\bcapex\b", r"\bcapital expenditures?\b")),
    ExtractionRule("cash_flow", "cash flow", (r"\bcash flow\b", r"\bfree cash flow\b")),
    ExtractionRule("backlog", "backlog", (r"\bbacklog\b", r"\bbook(?:ed|ings?)\b")),
    ExtractionRule("customer_growth", "customer growth", (r"\bcustomer(?:s)?\b.*\b(?:grew|growth|increase|added|expanded)\b", r"\bnew customers?\b")),
    ExtractionRule("customer_retention", "customer retention", (r"\bretention\b", r"\bchurn\b", r"\brenewal\b")),
    ExtractionRule("product", "product", (r"\bproduct(?:s)?\b", r"\bplatform\b", r"\bsolution\b", r"\blaunch(?:ed|ing)?\b", r"\bfeature(?:s)?\b")),
    ExtractionRule("geography", "geography", (r"\b(?:international|global|domestic|Europe|Asia|Americas)\b", r"\bgeograph(?:y|ic)\b")),
    ExtractionRule("competition", "competition", (r"\bcompet(?:e|es|ed|ing|ition)\b", r"\bcompetitive\b")),
    ExtractionRule("market_share", "market share", (r"\bmarket share\b", r"\bshare of the market\b")),
    ExtractionRule("capacity", "capacity", (r"\bcapacity\b", r"\bmanufacturing footprint\b")),
    ExtractionRule("supply_chain", "supply chain", (r"\bsupply chain\b", r"\bsupplier(?:s)?\b", r"\bshortage\b")),
    ExtractionRule("inventory", "inventory", (r"\binventor(?:y|ies)\b", r"\bstock levels?\b")),
    ExtractionRule("management", "management", (r"\bCEO\b", r"\bCFO\b", r"\bmanagement\b", r"\bleadership\b")),
    ExtractionRule("capital_allocation", "capital allocation", (r"\bshare repurchas(?:e|es|ed|ing)\b", r"\bbuyback\b", r"\bdividend\b", r"\bacquisition\b", r"\bdebt repayment\b")),
    ExtractionRule("balance_sheet", "balance sheet", (r"\bbalance sheet\b", r"\bliquidity\b", r"\bcash balance\b", r"\bdebt\b")),
    ExtractionRule("regulation", "regulation", (r"\bregulat(?:ion|ory|or)\b", r"\bapproval\b", r"\bFDA\b")),
    ExtractionRule("risks", "risk", (r"(?<!de-)\brisk(?:s|y)?\b", r"\bchallenge(?:s)?\b", r"\buncertain(?:ty|ties)?\b", r"\bheadwind(?:s)?\b")),
    ExtractionRule("catalysts", "catalyst", (r"\bcatalyst(?:s)?\b", r"\bopportunit(?:y|ies)\b", r"\btailwind(?:s)?\b")),
    ExtractionRule("strategy", "strategy", (r"\bstrateg(?:y|ic)\b", r"\bpriorit(?:y|ies|ize)\b", r"\bfocus(?:ed|ing)?\b")),
)

TEMPORAL_STATUSES = ("CURRENT", "HISTORICAL", "FUTURE", "RESOLVED", "UNKNOWN_TEMPORAL_STATUS")
_OWNERSHIP_GOVERNANCE_CONTEXT = re.compile(
    r"\b(?:acquir(?:e|ed|es|ing)|majority\s+interest|minority\s+interest|shareholder|beneficial\s+ownership|director|board|nominee|committee|governance|voting\s+power|stake|equity\s+interest)\b",
    re.I,
)
_RETAIL_CONTEXT = re.compile(
    r"\b(?:retail(?:er|ing)?|consumer\s+retail|stores?|clubs?|merchandise|same[- ]store|comparable\s+sales|"
    r"shopping|basket|customer\s+traffic|omnichannel|grocery|department\s+store|warehouse\s+club|e[- ]commerce|"
    r"online\s+sales|store\s+base|private[- ]label|promotional\s+markdown)\b", re.I,
)
_RETAIL_CONTEXT_REQUIRED = {
    "comparable_sales", "same_store_sales", "average_ticket", "unit_volume",
    "ecommerce_sales", "ecommerce_growth", "digital_penetration",
    "membership_revenue", "membership_growth", "retail_media", "store_count",
    "unit_expansion", "store_remodels", "markdowns", "shrink", "private_label_mix",
}


def semantic_dimension_exclusion(dimension: str, text: str) -> str | None:
    """Return an explicit exclusion reason for keyword-only false positives."""
    lowered = str(text or "").lower()
    if dimension == "demand" and _OWNERSHIP_GOVERNANCE_CONTEXT.search(lowered):
        # ``interest`` and ``acquired`` are demand keywords only when tied to
        # customers, orders, usage, bookings, or a commercial pipeline.
        if not re.search(r"\b(?:customer|end[- ]market|orders?|bookings?|pipeline|usage|adoption|consumption|sales|volume|win\s+rate|demand)\b", lowered):
            return "OWNERSHIP_OR_GOVERNANCE_CONTEXT"
    if dimension == "catalysts":
        # A historical narrative can contain opportunity/momentum language,
        # but it is not a current catalyst.  Catalyst evidence must describe
        # a future or unresolved event capable of changing expectations.
        if temporal_status(text) in {"HISTORICAL", "RESOLVED"}:
            return "HISTORICAL_OR_RESOLVED_EVENT"
        if not re.search(r"\b(?:will|expect(?:ed|s)?|plan(?:ned|s|ning)?|target(?:ed|s)?|upcoming|next|launch(?:ed|ing)?|approval|milestone|catalyst|complete(?:d|ion)?|acqui(?:re|red|sition)|submit(?:ted|s)?|authorize(?:d|s)?)\b", lowered):
            return "NO_FORWARD_OR_UNRESOLVED_EVENT"
        # Aspirational nouns alone do not identify an event.
        if re.fullmatch(r"[\w\s,;:'\-]+(?:opportunity|momentum|potential)[\w\s,;:'\-]*[.!]?", lowered):
            return "ASPIRATIONAL_LANGUAGE_ONLY"
    if dimension == "pricing" and re.search(r"\b(?:share price|stock price|purchase price)\b", lowered) and not re.search(r"\b(?:price increase|pricing power|raise(?:d)? prices?|customer pricing|average selling price)\b", lowered):
        return "SECURITY_OR_TRANSACTION_PRICE"
    if dimension in _RETAIL_CONTEXT_REQUIRED and not _RETAIL_CONTEXT.search(lowered):
        return "RETAIL_CONTEXT_REQUIRED"
    if dimension in {"revenue", "markets_trading_revenue"} and re.search(r"\b(?:credit\s+portfolio\s+VaR|\bVaR\b.{0,50}\bCVA\b|\bCVA\b.{0,50}\bVaR\b)", text, re.I):
        return "RISK_TABLE_CONTEXT"
    return None


def temporal_status(text: str) -> str:
    """Classify temporal eligibility without inventing a reporting period."""
    lowered = str(text or "").lower()
    if re.search(r"\b(?:resolved|no longer|eliminated|withdrawn|cancelled|completed|closed|ended|stopped)\b", lowered):
        return "RESOLVED"
    if re.search(r"\b(?:will|expects?|expecting|plans?|planned|target(?:s|ed)?|next year|next quarter|future|upcoming|by\s+20\d{2})\b", lowered):
        return "FUTURE"
    year_match = re.search(r"\b(?:fiscal\s+year|fy|quarter)\s*((?:19|20)\d{2})\b", lowered) or re.search(r"\b(?:in|during|for)\s+((?:19|20)\d{2})\b", lowered)
    if year_match and int(year_match.group(1)) < datetime.now().year:
        return "HISTORICAL"
    if re.search(r"\b(?:began|was|were|had|reported|delivered|achieved|increased|decreased|grew|declined)\b", lowered):
        return "CURRENT"
    return "UNKNOWN_TEMPORAL_STATUS"

_PRIMARY_ORDER = (
    "guidance", "net_interest_income", "net_interest_margin", "deposits", "deposit_growth", "deposit_mix", "deposit_beta",
    "loan_growth", "loan_mix", "credit_quality", "net_charge_offs", "delinquencies", "provisions", "allowance_reserves",
    "cet1", "risk_weighted_assets", "capital_ratios", "liquidity", "rotce_roe", "investment_banking_fees",
    "markets_trading_revenue", "asset_management_flows", "aum", "auc", "card_payment_volume", "efficiency_ratio",
    "operating_leverage", "capital_return", "regulatory_capital", "stress_test", "balance_sheet_optimization",
    "market_risk", "credit_risk", "var", "cva",
    "international_comparable_sales", "comparable_sales", "same_store_sales", "traffic_transactions", "average_ticket",
    "unit_volume", "ecommerce_growth", "ecommerce_sales", "digital_penetration", "membership_growth", "membership_revenue",
    "advertising_revenue", "retail_media", "store_count", "unit_expansion", "store_remodels", "gross_margin", "operating_margin",
    "operating_expense_rate", "inventory_growth", "inventory_efficiency", "markdowns", "shrink", "labor_costs", "wage_pressure",
    "sales_mix", "price_investment", "private_label_mix", "supply_chain_cost", "fulfillment_cost", "automation", "currency_effect", "customer_frequency",
    "revenue", "demand", "margins", "capex", "capital_allocation", "costs", "operating_expenses",
    "cash_flow", "pricing", "volume", "backlog", "bookings",
    "customer_growth", "customer_retention", "product", "geography", "competition",
    "market_share", "capacity", "supply_chain", "inventory",
    "balance_sheet", "regulation", "risks", "catalysts", "strategy", "management",
)
_BANK_ONLY_DIMENSIONS = {
    "net_interest_income", "net_interest_margin", "deposits", "deposit_growth", "deposit_mix", "deposit_beta",
    "loan_growth", "loan_mix", "credit_quality", "net_charge_offs", "delinquencies", "provisions", "allowance_reserves",
    "cet1", "risk_weighted_assets", "capital_ratios", "liquidity", "rotce_roe", "investment_banking_fees",
    "markets_trading_revenue", "asset_management_flows", "aum", "auc", "card_payment_volume", "efficiency_ratio",
    "regulatory_capital", "stress_test", "balance_sheet_optimization",
}
_BANK_CONTEXT = re.compile(r"\b(?:bank|banking|deposit(?:s|ary)?|loan(?:s)?|credit quality|net interest|CET1|capital ratio|risk[- ]weighted|charge[- ]offs?|allowance|branch(?:es)?)\b", re.I)
_NOISE_PATTERNS = (
    r"\bforward[- ]looking statements?\b", r"\bactual results? may differ\b",
    r"\bsubject to (?:certain )?risks? and uncertainties\b", r"\bconsult the risk factors\b",
    r"\bdiscuss(?:ed|ed) in (?:our|the) (?:annual|quarterly) report\b",
    r"\bform 10-?[kq]\b", r"\bsafe harbor\b", r"\bfor additional detail\b",
    r"\b(?:i'm|we are) joined (?:today )?by\b", r"\bthank you for (?:the )?(?:question|joining|your time)\b",
    r"\b(?:good morning|good afternoon|good evening|welcome to)\b", r"\bplease go ahead\b",
    r"\b(?:our )?next question\b", r"\bthis concludes\b", r"\boperator instructions?\b",
    r"\bmaybe a higher level strategic question\b", r"\bi will begin with comments\b", r"\bto close,?\b", r"\bturn it over to\b",
    r"\b(?:i was|we were) (?:just |maybe )?hoping\b", r"\bjust how much\b", r"\bhoping you could\b",
    r"\b(?:first|second|third) question\b", r"\bjust to give .* guidance\b", r"\bwhat do you need to do\b",
    r"\b(?:what are some|how are you looking|understanding you guys)\b",
    r"\bnow i would like to turn\b", r"\bnow turning to (?:our )?outlook\b",
    r"\bsuggests to me that maybe\b", r"\bmaybe net new customers\b",
    r"\bfor the sake of re-repeating myself\b", r"\bfirst of all, we look at everything\b",
    r"\bmore in large,? we work discounting\b", r"\bmoving to our non-gaap results\b",
    r"\bwhat we talked to with investors\b", r"\bsmart calculation\b",
    r"^and then online,? maybe\b", r"^it's product diversification,?\b",
    r"\btrying to square\b", r"\bi(?:'d| would) like to follow up\b",
    r"\bworking on churn, churn\b",
    r"\bthe big picture is to continue\b", r"\bhow to accelerate the .* portfolio\b",
    r"\bframed buyback\b", r"^turning to (?:the )?(?:guidance|outlook)\.?$",
)
_MATERIAL_PATTERNS = (
    r"\b(?:increase|increased|increasing|decrease|decreased|decreasing|grew|growth|declin|higher|lower|stronger|weaker|improv|accelerat|decelerat|stable|unchanged|flat|positive|negative)\b",
    r"\b(?:expect|expected|expects|guidance|outlook|forecast|target|plan(?:ned|s|ning)?|goal|rais(?:e|ed|ing)|lower(?:ed|ing)|cut)\b",
    r"\b(?:reported|generated|delivered|achieved|initiated|submitted|approved|launched|built|expanded|acquired|repurchased)\b",
    r"\b(?:revenue|sales|margin|expenses?|cash flow|customers?|bookings?|ARR)\b.{0,45}\b(?:generated|reported|grew|increased|decreased|rose|declined)\b",
    r"\b(?:revenue|sales|margin|expenses?|cash flow|customers?|bookings?|ARR)\b.{0,45}\b(?:was|were|has|have)\b.{0,20}(?:\$|\d|strong|stable|up|down|flat)\b",
    r"\b(?:driven by|due to|because of|reflects|as a result|resulting in)\b",
    r"\b(?:risk|headwind|challenge|uncertain|opportunit|tailwind|potential)\b",
    r"\b(?:launch|launched|approval|approved|acqui(?:re|red|sition)|repurchas|buyback|dividend|invest|build|expand|advance|priorit|capacity|demand|churn|retention|submit|milestone)\b",
    r"\b(?:platform|product|solution|feature)\b.{0,45}\b(?:launch|launched|extend|expanded|introduced|added|deliver|offer|scale|monetiz|adopt)\b",
    r"\b(?:platform|product|solution)\b.{0,50}\b(?:power|extend|serve|enable|connect|system)\b",
    r"\b(?:market share|competitive advantage|differentiated|displac(?:e|ed|ing))\b",
    r"\b(?:customer|client|enterprise)\b.{0,45}\b(?:grew|growth|added|expanded|landed|adopt|churn|retention|demand|purchas|deal|contract)\b",
    r"(?:\$\d|\d+(?:\.\d+)?\s*(?:%|bps|million|billion|thousand)\b)",
)

_DIRECTION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("raised", (r"\brais(?:e|ed|ing)\b.{0,40}\b(?:guidance|outlook|forecast|target)\b", r"\b(?:guidance|outlook|forecast|target)\b.{0,40}\b(?:rais(?:e|ed|ing)|higher|increase)\b")),
    ("lowered", (r"\blower(?:ed|ing)\b.{0,40}\b(?:guidance|outlook|forecast|target)\b", r"\b(?:guidance|outlook|forecast|target)\b.{0,40}\b(?:lower|cut|reduc)")),
    ("improving", (r"improv(?:e|ed|ement|ing)", r"better", r"stronger", r"recovery")),
    ("deteriorating", (r"deteriorat", r"weaker", r"pressure", r"headwind", r"declin", r"challeng")),
    ("increasing", (r"increas", r"grew", r"growth", r"higher", r"up \d", r"expanded", r"added")),
    ("decreasing", (r"decreas", r"declin", r"lower", r"down \d", r"fell", r"reduced")),
    ("accelerating", (r"accelerat",)),
    ("decelerating", (r"decelerat", r"slower")),
    ("positive", (r"positive", r"optimistic", r"encouraging", r"strength")),
    ("negative", (r"negative", r"pessimistic", r"unfavorable")),
    ("stable", (r"stable", r"unchanged", r"consistent", r"flat")),
)


def split_sentences(text: str) -> list[tuple[str, int, int]]:
    """Split text while preserving exact source offsets."""
    results: list[tuple[str, int, int]] = []
    for match in re.finditer(r"[^.!?\n]+(?:[.!?]+(?=\s|$)|$)", text, flags=re.S):
        value = match.group(0).strip()
        if not value:
            continue
        left = match.start() + (len(match.group(0)) - len(match.group(0).lstrip()))
        right = left + len(value)
        results.append((value, left, right))
    return results


def matching_rules(text: str) -> list[ExtractionRule]:
    if not is_material_sentence(text):
        return []
    matched = [rule for rule in RULES if any(re.search(pattern, text, re.I) for pattern in rule.patterns)]
    if not _BANK_CONTEXT.search(text):
        matched = [rule for rule in matched if rule.dimension not in _BANK_ONLY_DIMENSIONS]
    matched = [rule for rule in matched if semantic_dimension_exclusion(rule.dimension, text) is None]
    # The word "expected" is common in governance, legal and risk prose.  It
    # is only a guidance claim when the sentence is about a company financial
    # or operating metric (or an explicit operating target).
    if any(rule.dimension == "guidance" for rule in matched) and guidance_subtype(text) is None:
        matched = [rule for rule in matched if rule.dimension != "guidance"]
    # A realized result compared with guidance is an actual performance
    # observation, even when the sentence does not name the metric.
    if not matched and re.search(r"\b(?:result|revenue|sales|income|earnings|EPS)\b.{0,55}\b(?:above|below|versus|in line with)\b.{0,35}\b(?:guidance|outlook|forecast)\b", text, re.I):
        matched = [ExtractionRule("revenue", "reported result", (r"\b(?:result|revenue|sales|income|earnings|EPS)\b",))]
    return matched


_GUIDANCE_EXCLUSION_PATTERNS = (
    r"\b(?:director|directors|board|shareholder|nominee|nomination|committee|governance|employee|employment|officer election)\b",
    r"\b(?:legal|litigation|regulatory|compliance|accounting|tax|audit|risk|uncertainty|uncertain|safe harbor)\b",
    r"\b(?:analyst|consensus|third[- ]party|market expects?|investors? expect)\b",
    r"\b(?:expected|expect)\s+to\s+(?:be|remain|serve|continue|vote|approve|attend|participate)\b",
)
_GUIDANCE_METRIC_PATTERNS = (
    r"\b(?:revenue|sales|net sales|eps|earnings per share|profit|income|ebitda|gross margin|operating margin|margin|operating expenses?|opex|capex|capital expenditures?|cash flow|free cash flow|volume|units?|production|shipments?|customers?|customer growth|retention|churn|bookings?|backlog|arr|annual recurring revenue|market share|price|pricing)\b",
    r"\b(?:capacity|utilization|headcount|cost savings|cost reduction|installed base)\b",
)
_GUIDANCE_LANGUAGE = r"\b(?:guidance|outlook|forecast|guide(?:d|s|line)?|target(?:s|ed)?|plan(?:s|ned|ning)?|goal(?:s)?|expect(?:ed|s|ing)?)\b"


def guidance_subtype(text: str) -> str | None:
    """Return an eligible guidance subtype, or ``None`` for non-guidance prose.

    This deliberately requires both forward-looking language and a business
    metric/operating target.  Governance, legal, risk and third-party
    expectations remain ordinary evidence for their own dimensions, never
    management guidance.
    """
    lowered = str(text or "").lower()
    if not re.search(_GUIDANCE_LANGUAGE, lowered):
        return None
    if any(re.search(pattern, lowered, re.I) for pattern in _GUIDANCE_EXCLUSION_PATTERNS):
        return None
    if not any(re.search(pattern, lowered, re.I) for pattern in _GUIDANCE_METRIC_PATTERNS):
        # An explicit management-guidance statement is still guidance even
        # when the source does not state a numeric metric.  It will remain
        # non-comparable downstream; generic ``expected`` prose will not.
        if re.search(r"\b(?:management|company)\s+guidance\b", lowered):
            return "FINANCIAL_GUIDANCE"
        return None
    if re.search(r"\b(?:target|goal|plan|priorit(?:y|ize)|strategy|strategic)\b", lowered) and re.search(r"\b(?:capacity|production|customers?|launch|product|platform|market share|cost|margin|revenue|sales|cash flow|capex|volume|bookings?|backlog)\b", lowered):
        return "STRATEGIC_TARGET"
    if re.search(r"\b(?:revenue|sales|eps|earnings|profit|income|ebitda|margin|opex|capex|cash flow|free cash flow|cost|pricing)\b", lowered):
        return "FINANCIAL_GUIDANCE"
    return "OPERATING_GUIDANCE"


def classify_regulatory_semantics(text: str) -> str:
    """Separate routine regulatory description from economically material change."""
    lowered = str(text or "").lower()
    if re.search(r"stress test|ccar|dodd[- ]frank", lowered):
        return "STRESS_TEST_OUTCOME" if re.search(r"result|passed|failed|requirement|capital action", lowered) else "ROUTINE_REGULATORY_DESCRIPTION"
    if re.search(r"enforcement|fine|penalty|consent order|legal matter", lowered):
        return "ENFORCEMENT_OR_LEGAL_MATTER"
    if re.search(r"pending|proposed|will require|expected rule|rule change", lowered):
        return "PENDING_RULE_CHANGE"
    if re.search(r"capital requirement|minimum capital|capital ratio|regulatory capital", lowered):
        return "CAPITAL_REQUIREMENT"
    if re.search(r"binding constraint|constrain|constraint", lowered):
        return "BINDING_CONSTRAINT"
    if re.search(r"risk|uncertain|adverse impact|could affect", lowered):
        return "REGULATORY_RISK"
    if re.search(r"historical|previously|prior rule|under the .* rule", lowered):
        return "HISTORICAL_RULE"
    return "ROUTINE_REGULATORY_DESCRIPTION"


def is_transcript_noise(text: str) -> bool:
    """Identify boilerplate, logistics, and question-only transcript spans."""
    stripped = text.strip()
    lowered = stripped.lower()
    if not stripped or stripped[0].islower() or stripped[0] in ",;:" or re.match(r"^\d", stripped):
        return True
    if "?" in stripped:
        return True
    if any(re.search(pattern, lowered, re.I) for pattern in _NOISE_PATTERNS):
        return True
    if re.match(r"^(?:can|could|would|what|how|why|where|when|who|i(?:'m| am) just wondering|i wonder)\b", lowered):
        return True
    if re.match(r"^(?:maybe|so maybe|to me|if helpful|and kind of|and then i think|i was .*hoping|i'm just wondering)\b", lowered):
        return True
    question_framing = re.search(r"\b(?:you guys|you mentioned|could you|can you|would you|how do you|how much|what are|what do you|why did|where do|kind of|sort of)\b", lowered)
    substantive_answer = re.search(r"\b(?:reported|generated|grew|increased|decreased|expect|guidance|approved|launched|acquired|authorized|raised|lowered|driven|resulted|revenue|sales|margin|demand|bookings|churn|repurchase|buyback|dividend|capacity|FDA|data)\b", lowered)
    if question_framing and not substantive_answer:
        return True
    return False


def is_material_sentence(text: str) -> bool:
    """Require a research-relevant proposition in addition to a keyword."""
    if is_transcript_noise(text):
        return False
    lowered = text.lower()
    if len(re.findall(r"\b[\w'-]+\b", text)) < 4:
        # Compact investor language such as "positive comparable sales" is
        # still a complete retail metric proposition, even though it has
        # fewer than the general sentence minimum.
        if not re.search(r"\b(?:positive|negative|strong|weak|higher|lower|stable)\s+(?:comparable|comp|same[- ]store)\s+sales\b", lowered):
            return False
    if re.search(r"\b(?:continue to )?(?:focus|work|look) (?:on|at) (?:our )?(?:product|platform|strategy)\b", lowered) and not re.search(r"\b(?:launch|growth|revenue|sales|customer|demand|margin|capacity|invest|build|expand)\b", lowered):
        return False
    if re.search(r"\b(?:because of|due to)\s+(?:a|the)\s+(?:product|feature|thing)\b", lowered) and not re.search(r"\b(?:growth|revenue|sales|customer|demand|margin|launch|adopt|scale)\b", lowered):
        return False
    return any(re.search(pattern, lowered, re.I) for pattern in _MATERIAL_PATTERNS)


def primary_rule(text: str, rules: list[ExtractionRule]) -> ExtractionRule:
    """Collapse one source sentence to one primary proposition."""
    names = {rule.dimension for rule in rules}
    if "guidance" in names:
        actual_result = re.search(r"\b(?:result|revenue|sales|income|expenses?|margin)\b.{0,45}\b(?:was|were|grew|increased|decreased|above|below|versus|year over year|in line)\b", text, re.I)
        metric = next((rule for rule in rules if rule.dimension in {"revenue", "margins", "costs", "operating_expenses", "cash_flow", "capex", "demand", "pricing", "volume"}), None)
        if actual_result and metric is not None and not re.search(r"\b(?:we expect|we are (?:updating|raising|lowering)|new guidance|guidance range|outlook assumes)\b", text, re.I):
            return metric
        return ExtractionRule("guidance", metric.topic if metric else "outlook", ())
    for dimension in _PRIMARY_ORDER:
        for rule in rules:
            if rule.dimension == dimension:
                return rule
    return rules[0]


def infer_direction(text: str) -> str:
    lowered = text.lower()
    # An actual result above/below guidance is a performance comparison, not
    # a change to the guidance itself.
    actual_vs_guidance = re.search(r"\b(?:result|income|revenue|sales|EPS|earnings)\b.{0,50}\b(?:above|below)\b.{0,30}\b(?:guidance|outlook|forecast)\b", lowered)
    if actual_vs_guidance:
        return "increasing" if re.search(r"\babove\b", actual_vs_guidance.group(0)) else "decreasing"
    matches: list[str] = []
    for direction, patterns in _DIRECTION_PATTERNS:
        if any(re.search(pattern, lowered) for pattern in patterns):
            matches.append(direction)
    if len(matches) > 1 and {"increasing", "decreasing"}.issubset(matches):
        return "mixed"
    return matches[0] if matches else "unknown"


def infer_certainty(text: str, dimension: str) -> str:
    lowered = text.lower()
    if dimension == "risks" or re.search(r"\b(risk|may|might|could|uncertain|uncertainty|subject to)\b", lowered):
        return "risk" if dimension == "risks" or re.search(r"\b(risk|uncertain|uncertainty)\b", lowered) else "possibility"
    if re.search(r"\b(?:result|revenue|sales|income|expenses?|margin)\b.{0,45}\b(?:was|were|grew|increased|decreased|above|below|versus|year over year|in line)\b", lowered) and not re.search(r"\b(?:we expect|we are (?:updating|raising|lowering)|new guidance|outlook assumes)\b", lowered):
        return "actual"
    if re.search(r"\bif approved\b|\bwould\b|\bpotential to\b|\bmay\b|\bmight\b|\bcould\b", lowered):
        return "possibility"
    if re.search(r"\b(?:approved|launched|acquired|initiated|submitted|authorized|completed|closed)\b", lowered):
        return "confirmed"
    if re.search(r"\b(guidance|outlook|forecast|expect(?:ed|s|ing)?|plan(?:s|ned|ning)?|target)\b", lowered):
        return "guidance" if dimension == "guidance" or "guidance" in lowered or "outlook" in lowered else "expectation"
    if re.search(r"\b(estimate|approximately|roughly|about)\b", lowered):
        return "estimate"
    if re.search(r"\b(will|intend|goal|target)\b", lowered):
        return "target"
    if re.search(r"\b(reported|delivered|increased|decreased|grew|declined)\b", lowered):
        return "actual"
    return "unknown"


def infer_magnitude(text: str) -> dict[str, Any] | None:
    """Capture explicit numeric quantities without interpreting them."""
    # Require a currency marker or an explicit unit.  This avoids treating
    # fiscal years and dates as business magnitudes.
    matches = list(re.finditer(r"(?P<value>\$\d+(?:\.\d+)?|\d+(?:\.\d+)?)(?P<unit>\s*%|\s*(?:bps|basis points|million|billion|thousand|M|B|K)\b)?", text, re.I))
    matches = [m for m in matches if m.group("value").startswith("$") or m.group("unit")]
    if not matches:
        return None
    values: list[dict[str, Any]] = []
    for match in matches[:3]:
        raw = match.group(0).strip()
        value = match.group("value").replace("$", "")
        try:
            numeric: int | float = int(float(value)) if float(value).is_integer() else float(value)
        except ValueError:
            continue
        unit = (match.group("unit") or "").strip().lower()
        if raw.startswith("$"):
            unit = "USD" if not unit else f"USD_{unit.replace(' ', '_')}"
        elif unit == "%":
            unit = "percent"
        values.append({"raw": raw, "value": numeric, "unit": unit or None})
    if not values:
        return None
    return values[0] if len(values) == 1 else {"values": values}


def candidate_for_sentence(sentence: str, rule: ExtractionRule, matched_rules: list[ExtractionRule] | None = None) -> ClaimCandidate:
    direction = infer_direction(sentence)
    certainty = infer_certainty(sentence, rule.dimension)
    return ClaimCandidate(
        dimension=rule.dimension,
        topic=rule.topic,
        subtopic=classify_regulatory_semantics(sentence) if rule.dimension == "regulation" else None,
        claim_text=sentence,
        direction=direction if direction in DIRECTION_VALUES else "unknown",
        magnitude=infer_magnitude(sentence),
        certainty=certainty if certainty in CERTAINTY_VALUES else "unknown",
        evidence_text=sentence,
        extraction_method="deterministic_rules_v8",
        extraction_confidence=0.84 if direction != "unknown" else 0.72,
        guidance_subtype=guidance_subtype(sentence) if rule.dimension == "guidance" else None,
        temporal_status=temporal_status(sentence),
        primary_dimension=rule.dimension,
        secondary_dimensions=[item.dimension for item in (matched_rules or []) if item.dimension != rule.dimension],
    )



