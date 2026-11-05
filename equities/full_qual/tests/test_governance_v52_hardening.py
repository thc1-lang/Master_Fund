from __future__ import annotations

from types import SimpleNamespace

from insider_intelligence.models import SECFiling
from management_intelligence.board_committees import extract_committees
from management_intelligence.board_composition import build_board_memberships
from management_intelligence.career_history import parse_proxy_people
from management_intelligence.governance_sources import CachedGovernanceSource
from management_intelligence.models import BoardMembership
from management_intelligence.normalized_sources import NormalizedSECSourceCatalog


def _filing(accession: str = "0000000001-26-000001", filing_date: str = "2026-05-01") -> SECFiling:
    return SECFiling(
        ticker="TST", issuer_cik="0000000001", form_type="DEF 14A",
        accession_number=accession, filing_date=filing_date, report_date=None,
        primary_document="proxy.htm", source_url="https://www.sec.gov/proxy.htm",
    )


def _store_like(people, boards):
    return SimpleNamespace(list_people=lambda: people, list_boards=lambda: boards)


def test_table_roster_splits_bold_name_and_rejects_header():
    html = """
    <table><tr><th>Name</th><th>Age</th><th>Director</th><th>Since</th><th>Current Committees</th></tr>
    <tr><td><b>ALICE EXAMPLE</b><br>Independent investor</td><td>55</td><td>2020</td><td>2020</td><td>Audit</td></tr>
    <tr><td><b>BOB SMITH</b><br>Technology executive</td><td>58</td><td>2019</td><td>2019</td><td>Compensation</td></tr></table>
    """
    result = parse_proxy_people(html, _filing(), "Test Co")
    assert {person.full_name for person in result.people} >= {"ALICE EXAMPLE", "BOB SMITH"}
    assert "Name" not in {person.full_name for person in result.people}
    assert len([row for row in result.boards if row.board_company == "Test Co"]) == 2


def test_biography_format_extracts_real_director():
    text = """Board of Directors
Alice Example
Alice Example has served as a director since 2020 and is independent.
Bob Smith
Bob Smith is the chair of the board and joined the board in 2019.
"""
    result = parse_proxy_people(text, _filing(), "Test Co")
    assert {person.full_name for person in result.people} == {"Alice Example", "Bob Smith"}
    assert len(result.boards) == 2
    assert any("Chair" in row.board_role for row in result.boards)


def test_committee_grid_links_only_validated_people():
    html = """
    <table><tr><th>Name</th><th>Director Since</th><th>Position</th></tr>
    <tr><td>Alice Example</td><td>2020</td><td>Independent Director</td></tr>
    <tr><td>Bob Smith</td><td>2019</td><td>Director</td></tr></table>
    <table><tr><th>Directors</th><th>Audit Committee</th><th>Compensation Committee</th></tr>
    <tr><td>Alice Example</td><td>X</td><td></td></tr>
    <tr><td>Bob Smith</td><td></td><td>Chair</td></tr></table>
    """
    result = parse_proxy_people(html, _filing(), "Test Co")
    boards = build_board_memberships(_store_like(result.people, result.boards), "TST", as_of_date="2026-09-25")
    proxy = CachedGovernanceSource("TST", _filing().accession_number, "DEF 14A", _filing().filing_date, None, _filing().source_url, "proxy.htm", text="")
    proxy = CachedGovernanceSource(**{**proxy.__dict__, "html": html})
    committees = extract_committees("TST", proxy, result.people, boards)
    by_type = {item.committee_type: item for item in committees}
    assert any(person_id == boards[0].person_id for person_id in by_type["audit"].members)
    assert any(person_id == boards[1].person_id for person_id in by_type["compensation"].members)


def test_nominee_list_is_explicitly_not_current():
    text = "Nominees as directors: Alex Example and Jordan Smith. FOR each nominee."
    result = parse_proxy_people(text, _filing(), "Test Co")
    assert {person.full_name for person in result.people} == {"Alex Example", "Jordan Smith"}
    assert all(row.board_status == "NOMINEE" and not row.is_current for row in result.boards)


def test_latest_proxy_source_establishes_current_board():
    old = parse_proxy_people(
        "<table><tr><th>Name</th><th>Director Since</th></tr><tr><td>Alice Brown</td><td>2018</td></tr></table>",
        _filing("0000000001-25-000001", "2025-05-01"), "Test Co",
    )
    new = parse_proxy_people(
        "<table><tr><th>Name</th><th>Director Since</th></tr><tr><td>Bob Smith</td><td>2025</td></tr></table>",
        _filing("0000000001-26-000001", "2026-05-01"), "Test Co",
    )
    boards = old.boards + new.boards
    current = build_board_memberships(
        _store_like(old.people + new.people, boards), "TST", as_of_date="2026-09-25",
        proxy=CachedGovernanceSource("TST", "0000000001-26-000001", "DEF 14A", "2026-05-01", None, "https://www.sec.gov/new.htm", "new.htm", text=""),
    )
    assert {row.person_id for row in current} == {new.boards[0].person_id}


def test_governance_board_membership_retains_normalized_proxy_identity():
    result = parse_proxy_people(
        "<table><tr><th>Name</th><th>Director Since</th></tr>"
        "<tr><td>Alice Brown</td><td>2018</td></tr></table>",
        _filing(), "Test Co",
    )
    proxy = CachedGovernanceSource(
        "TST", _filing().accession_number, "DEF 14A", _filing().filing_date,
        None, _filing().source_url, "proxy.htm", text="", html="Board of Directors",
        normalized_document_id="doc-proxy-tst",
    )
    current = build_board_memberships(
        _store_like(result.people, result.boards), "TST", as_of_date="2026-09-25", proxy=proxy,
    )
    assert current[0].normalized_document_id == "doc-proxy-tst"
    assert current[0].source_filed_at == "2026-05-01"
    assert current[0].as_of_eligibility == "ELIGIBLE"


def test_future_proxy_is_excluded_by_normalized_catalog(tmp_path):
    row = {
        "document_id": "future", "ticker": "TST", "title": "Future DEF 14A",
        "source_url": "https://www.sec.gov/Archives/edgar/data/1/future.htm",
        "filing_date": "2026-10-01", "available_date": "2026-10-01", "text": "Board of Directors",
        "metadata": {"source_family": "SEC_EDGAR", "sec_form": "DEF 14A", "issuer_cik": "0000000001", "accession_number": "0000000001-26-000099"},
    }
    (tmp_path / "documents.jsonl").write_text(__import__("json").dumps(row) + "\n", encoding="utf-8")
    assert NormalizedSECSourceCatalog(tmp_path).latest_proxy("TST", as_of_date="2026-09-25") is None


def test_non_person_headings_are_rejected():
    html = "<table><tr><th>Director Nominees</th><th>Director Since</th></tr><tr><td>Board of Directors</td><td>2020</td></tr><tr><td>Audit Committee</td><td>2020</td></tr></table>"
    result = parse_proxy_people(html, _filing(), "Test Co")
    assert not result.people
    assert not result.boards


def test_committee_source_gap_is_diagnostic():
    proxy = CachedGovernanceSource("TST", _filing().accession_number, "DEF 14A", _filing().filing_date, None, _filing().source_url, "proxy.htm", text="Current board committee membership Audit Committee")
    committees = extract_committees("TST", proxy, [], [])
    assert committees
    assert any(item.membership_status == "EXTRACTION_FAILED" for item in committees)


def test_optional_board_fields_missing_does_not_drop_valid_member():
    result = parse_proxy_people(
        "Board of Directors\nAlice Example\nAlice Example is a director.",
        _filing(), "Test Co",
    )
    assert len(result.boards) == 1
    assert result.boards[0].is_current is True
    assert result.boards[0].start_date is None


def test_committee_headings_without_people_are_insufficient_evidence():
    result = parse_proxy_people(
        "Board Committees\nAudit Committee\nCompensation Committee\nDirector Qualifications",
        _filing(), "Test Co",
    )
    assert result.people == []
    assert result.boards == []


def test_split_name_sec_nominee_table_is_structured_and_current_when_proxy_says_so():
    text = """Election of Directors (Proposal 1)
Each nominee is currently a director.
Name Age Director Since Independent
Tench
Coxe
68 1993 Independent
John O.
Dabiri
46 2020 Independent
Director Qualifications and Nomination of Directors
"""
    result = parse_proxy_people(text, _filing(), "Test Co")
    assert {person.full_name for person in result.people} == {"Tench Coxe", "John O. Dabiri"}
    assert len(result.boards) == 2
    assert all(row.is_current and row.board_status == "CURRENT" for row in result.boards)


def test_flattened_sec_position_and_executive_rosters_are_extracted():
    text = """PROPOSAL NO. 1 ELECTION OF DIRECTORS
Name Age(1) Position(s) Class Director Since
Alice Example 55 Independent Director I 2020
Bob Smith 58 President, Chief Executive Officer and Director II 2019
Board Skills and Experience
EXECUTIVE OFFICERS OF THE COMPANY
The following table shows information about our executive officers as of the Record Date:
Name Title
Bob Smith President, Chief Executive Officer and Director
Carol Jones Chief Financial Officer
Biographical information for each of the above-named officers is set forth below.
"""
    result = parse_proxy_people(text, _filing(), "Test Co")
    assert {person.full_name for person in result.people} >= {"Alice Example", "Bob Smith", "Carol Jones"}
    assert len([row for row in result.boards if row.is_current]) >= 2
    assert any("Chief Executive Officer" in role for person in result.people for role in person.current_roles)
    assert result.diagnostics["SOURCE_ACCESSION"] == _filing().accession_number


def test_flattened_committee_matrix_links_members_and_chairs():
    text = """Committee membership as of the Record Date was as follows:
Name Audit Compensation NCG
Alice Example — Chair Member
Bob Smith Chair Member —
Carol Jones Member — Member
Our Board has adopted written charters for each of these committees.
"""
    result = parse_proxy_people(
        "Board of Directors\nAlice Example\nAlice Example is an independent director.\nBob Smith\nBob Smith is a director.\nCarol Jones\nCarol Jones is an independent director.",
        _filing(), "Test Co",
    )
    proxy = CachedGovernanceSource("TST", _filing().accession_number, "DEF 14A", _filing().filing_date, None, _filing().source_url, "proxy.txt", text=text)
    boards = build_board_memberships(_store_like(result.people, result.boards), "TST", as_of_date="2026-09-25")
    committees = extract_committees("TST", proxy, result.people, boards)
    by_type = {item.committee_type: item for item in committees}
    assert len(by_type["audit"].members) == 2
    assert len(by_type["compensation"].members) == 2
    assert len(by_type["nominating_governance"].members) == 2
    assert by_type["audit"].chair_person_id == next(person.person_id for person in result.people if person.full_name == "Bob Smith")

