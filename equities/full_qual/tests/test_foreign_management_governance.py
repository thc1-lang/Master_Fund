from __future__ import annotations

from pathlib import Path

from management_intelligence.board_committees import extract_committees
from management_intelligence.board_governance_models import GovernanceBoardMembership
from management_intelligence.career_history import ManagementParseResult, parse_foreign_source
from management_intelligence.main import _apply_annual_board_precedence, _departure_role_matches, _person_name_match
from management_intelligence.governance_sources import CachedGovernanceSource
from management_intelligence.models import BoardMembership, ManagementPerson, OfficialSource, RoleAssertion
from management_intelligence.source_roles import (
    AGM_PRIMARY,
    ANNUAL_REPORT_PRIMARY,
    GOVERNANCE_SUPPLEMENTAL,
    MANAGEMENT_PRIMARY,
    source_role,
    source_roles,
)
from management_intelligence.board_composition import infer_independence
from insider_intelligence.models import SECFiling
from qualitative_analysis.normalization import classify_document


def _filing(form: str, accession: str = "000000000000000001", date: str = "2026-05-20") -> SECFiling:
    return SECFiling(
        ticker="GBX",
        issuer_cik="0000000001",
        form_type=form,
        accession_number=accession,
        filing_date=date,
        report_date="2026-03-31",
        primary_document="source.htm",
        source_url=f"https://www.sec.gov/Archives/{accession}/source.htm",
        local_source_path=f"artifacts/GBX/{accession}/source.htm",
        available_date=date,
    )


def test_foreign_annual_report_provides_management_board_and_roles() -> None:
    text = """
    Our Board
    Alice Smith
    Chair - Independent
    Bob Jones
    Non-Executive Director
    Carol Brown
    Independent Non-Executive Director
    Our Executive Committee
    Biographical details of the Executive Committee
    David White
    Chief Executive Officer
    Erin Green
    Chief Financial Officer
    Culture and the Board
    """
    result = parse_foreign_source(text, _filing("20-F"), "Example International plc", as_of_date="2026-09-25")
    names = {person.full_name for person in result.people}
    assert {"Alice Smith", "Bob Jones", "Carol Brown", "David White", "Erin Green"} <= names
    assert any("Chief Executive Officer" in role.role for role in result.roles)
    assert any(row.board_role == "Independent Non-Executive Director" for row in result.boards)
    assert result.source is not None and result.source.source_type == "20-F"


def test_foreign_annual_report_preserves_titles_before_biography_boundary() -> None:
    text = """
    Our Executive Committee
    Biographical details of the Executive Committee
    Ahmed Essam
    Executive Chairman Vodafone Germany and CEO European Markets
    Ahmed was appointed Executive Chairman Vodafone Germany and CEO European markets on 1 April 2024.
    Ruth McGill
    Chief Human Resources Officer
    Ruth McGill was appointed Chief Human Resources Officer on 1 January 2026.
    Guillaume Boutin
    CEO Vodafone Investments & Strategy
    Guillaume was appointed CEO Vodafone Investments & Strategy in May 2025.
    Culture and the Board
    """
    result = parse_foreign_source(text, _filing("20-F"), "Example International plc", as_of_date="2026-09-25")
    roles = {person.full_name: " | ".join(person.current_roles) for person in result.people}
    assert "CEO European Markets" in roles["Ahmed Essam"]
    assert "Chief Human Resources Officer" in roles["Ruth McGill"]
    assert "CEO Vodafone Investments & Strategy" in roles["Guillaume Boutin"]


def test_foreign_6k_role_changes_respect_point_in_time() -> None:
    text = """
    Management update
    Alice Smith stepped down as Chief Financial Officer effective 2026-07-01.
    Eve Green was appointed Chief Financial Officer effective 2026-07-02.
    Future Person was appointed Chief Operating Officer effective 2026-12-01.
    """
    result = parse_foreign_source(text, _filing("6-K", "000000000000000002", "2026-07-03"), "Example International plc", as_of_date="2026-09-25")
    by_name = {person.full_name: person for person in result.people}
    assert "Eve Green" in by_name
    assert "Chief Financial Officer" in " ".join(by_name["Eve Green"].current_roles)
    # A future-dated appointment may remain in the source event audit, but it
    # must not materialize as a current management person before its effective
    # date.
    assert "Future Person" not in by_name
    assert {event.event_type for event in result.role_changes} >= {"appointment", "departure"}
    assert all((event.effective_date or "") <= "2026-09-25" or event.event_type == "appointment" for event in result.role_changes)


def test_foreign_agm_wrapped_resolutions_update_seated_board_without_withdrawn_nominee() -> None:
    text = """
    Results of the Annual General Meeting
    To elect Olaf
    Koch as a
    Director.
    To elect Pilar
    López as a
    Director.
    Resolution 7 to re-elect Hatem Dowidar as a Director was withdrawn
    following his resignation from the Board.
    Amparo Moraleda did not stand for re-election as a Director and retired from the Board.
    """
    result = parse_foreign_source(text, _filing("6-K", "000000000000000004", "2026-07-27"), "Example International plc", as_of_date="2026-09-25")
    board_names = {row.person_id for row in result.boards if row.is_current}
    people = {person.normalized_name: person.person_id for person in result.people}
    assert people["olaf koch"] in board_names
    assert people["pilar lopez"] in board_names
    assert "hatem dowidar" not in people
    assert "amparo moraleda" not in people
    assert any(event.event_type == "departure" and "Amparo" in event.person_name for event in result.role_changes)


def test_foreign_role_change_reconciles_abbreviated_name_to_full_roster_name() -> None:
    assert _person_name_match("Amparo Moraleda", "Maria Amparo Moraleda Martinez")
    assert not _person_name_match("Amparo", "Maria Amparo Moraleda Martinez")
    assert _departure_role_matches("Independent Non-Executive Director", None, "Amparo Moraleda did not stand for re-election")
    assert not _departure_role_matches("Chief Financial Officer", None, "Amparo Moraleda did not stand for re-election")
    assert _departure_role_matches("Group Chief Financial Officer", None, "Luka Mucic stepped down as Group Chief Financial Officer and a member of the Executive Committee")
    assert not _departure_role_matches("Director", None, "Luka Mucic stepped down as Group Chief Financial Officer and a member of the Executive Committee")


def test_latest_annual_roster_retires_omitted_older_board_rows() -> None:
    old_source = OfficialSource(source_id="old", source_authority="sec_official_filing", source_type="20-F", url="", accession_number="old", filing_date="2025-06-01")
    latest_source = OfficialSource(source_id="latest", source_authority="sec_official_filing", source_type="20-F", url="", accession_number="latest", filing_date="2026-05-22")
    old_person = ManagementPerson(person_id="p:old", ticker="GBX", company_name="Example International plc", full_name="Former Director", normalized_name="former director", current_roles=["Director"], issuer_cik="0000000001")
    current_person = ManagementPerson(person_id="p:current", ticker="GBX", company_name="Example International plc", full_name="Current Director", normalized_name="current director", current_roles=["Director"], issuer_cik="0000000001")
    old_board = BoardMembership(board_id="b:old", person_id="p:old", ticker="GBX", board_company="Example International plc", board_ticker="GBX", board_role="Director", accession_number="old")
    current_board = BoardMembership(board_id="b:current", person_id="p:current", ticker="GBX", board_company="Example International plc", board_ticker="GBX", board_role="Director", accession_number="latest")
    old_role = RoleAssertion(assertion_id="r:old", person_id="p:old", ticker="GBX", role="Director", role_category="director", is_current=True)
    result = ManagementParseResult(people=[old_person, current_person], boards=[old_board, current_board], roles=[old_role])
    parsed = [
        ManagementParseResult(source=old_source, people=[old_person], boards=[old_board]),
        ManagementParseResult(source=latest_source, people=[current_person], boards=[current_board]),
    ]
    _apply_annual_board_precedence(result, parsed)
    assert old_board.is_current is False
    assert old_person.current_roles == []
    assert old_role.is_current is False
    assert current_board.is_current is True


def test_foreign_committee_prose_extracts_members_and_chair() -> None:
    people = [
        ManagementPerson(person_id="p:alice", ticker="GBX", company_name="Example International plc", full_name="Alice Smith", normalized_name="alice smith", issuer_cik="0000000001"),
        ManagementPerson(person_id="p:bob", ticker="GBX", company_name="Example International plc", full_name="Bob Jones", normalized_name="bob jones", issuer_cik="0000000001"),
    ]
    source = CachedGovernanceSource(
        ticker="GBX", accession_number="000000000000000003", form_type="20-F", filing_date="2026-05-20", report_date="2026-03-31",
        source_url="https://example.test/20f", local_path="", text=(
            "Audit and Risk Committee\nChair: Alice Smith\nMembers: Alice Smith, Bob Jones.\n"
            "Remuneration Committee\nMembers: Bob Jones.\n"
            "Nominations and Governance Committee\nMembers: Alice Smith."
        ),
    )
    memberships = [GovernanceBoardMembership(board_membership_id="b:alice", person_id="p:alice", ticker="GBX"), GovernanceBoardMembership(board_membership_id="b:bob", person_id="p:bob", ticker="GBX")]
    committees = extract_committees("GBX", source, people, memberships)
    by_type = {item.committee_type: item for item in committees}
    assert {"audit", "remuneration", "nominating_governance"} <= set(by_type)
    assert "p:alice" in by_type["audit"].members
    assert by_type["audit"].chair_person_id == "p:alice"


def test_independence_is_tri_state_and_never_defaults_unknown_to_negative() -> None:
    person = ManagementPerson(person_id="p:unknown", ticker="GBX", company_name="Example International plc", full_name="Casey Brown", normalized_name="casey brown", issuer_cik="0000000001")
    row = BoardMembership(board_id="b:unknown", person_id=person.person_id, ticker="GBX", board_company="Example International plc", issuer_cik="0000000001", board_role="Director", evidence_text="Director Casey Brown")
    assert infer_independence(row, person, "") [0] == "UNKNOWN"
    independent = BoardMembership(**{**row.__dict__, "board_id": "b:ind", "evidence_text": "Independent Non-Executive Director"})
    assert infer_independence(independent, person, "")[0] == "INDEPENDENT"
    executive = BoardMembership(**{**row.__dict__, "board_id": "b:exec", "evidence_text": "Chief Executive Officer and Director"})
    assert infer_independence(executive, person, "")[0] == "NOT_INDEPENDENT"


def test_source_roles_are_jurisdiction_neutral_and_6k_boilerplate_is_supplemental() -> None:
    assert ANNUAL_REPORT_PRIMARY in source_roles(form_type="20-F", text="Board of Directors and Executive Committee")
    assert MANAGEMENT_PRIMARY in source_roles(form_type="20-F", text="Board of Directors and Executive Committee")
    assert AGM_PRIMARY in source_roles(form_type="6-K", text="Results of the Annual General Meeting and election of directors")
    assert GOVERNANCE_SUPPLEMENTAL in source_roles(form_type="6-K", text="PDMR shareholding notification")


def test_foreign_filing_containers_classify_by_form_and_content() -> None:
    assert classify_document({"sec_form": "20-F"}, "Vodafone 20-F annual report.pdf") == ("annual_report", "20-F")
    assert classify_document({"sec_form": "6-K", "description": "Half-year results"}, "Vodafone 6-K results.pdf") == ("quarterly_report", "RESULTS_6K")
    assert classify_document({"sec_form": "6-K", "description": "Annual General Meeting results"}, "Vodafone 6-K AGM.pdf") == ("other_report", "OTHER_6K")

