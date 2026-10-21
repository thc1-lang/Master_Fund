"""Extract factual people, roles and career evidence from official proxies."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from insider_intelligence.models import SECFiling

from .models import BoardMembership, CareerEntry, EducationEntry, ManagementPerson, OfficialSource, PersonCompanyRelationship, RoleAssertion, RoleChangeEvent
from .people import normalize_name, person_key
from .education import extract_education
from .organization_resolution import resolve_organization
from .roles import classify_role, is_key_role, responsibilities_from_title


_HEADER_WORDS = {
    "name", "age", "position", "office", "director", "directors", "nominees",
    "executive", "officers", "committee", "information", "business", "board",
    "company", "stockholder", "beneficial", "ownership", "experience",
}


def _clean(value: str) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _stable(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _looks_like_name(value: str) -> bool:
    value = _clean(value)
    if not value or len(value) > 100 or re.search(r"\d", value):
        return False
    # Strip footnote markers, degrees and common suffixes before checking.
    value = re.sub(r"\([^)]*\)|\b(?:Ph\.D\.?|M\.D\.?|J\.D\.?)\b", "", value, flags=re.I)
    tokens = re.findall(r"[A-Za-z][A-Za-z'’-]*", value)
    if not 2 <= len(tokens) <= 7:
        return False
    lowered = {token.lower() for token in tokens}
    if lowered & _HEADER_WORDS:
        return False
    return bool(re.search(r"[A-Z][a-z]+", value))


def _date_from_text(text: str, *, marker: str = "since") -> str | None:
    patterns = (
        rf"\b{marker}\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{{4}})",
        rf"\b{marker}\s+(\d{{4}})",
        r"\bappointed\s+(?:in\s+)?(\d{4})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        if len(match.groups()) == 2 and match.group(1).isalpha():
            return f"{match.group(2)}-{datetime.strptime(match.group(1), '%B').month:02d}"
        return match.group(1)
    return None


def _year_cell(cells: list[str]) -> str | None:
    years = [match.group(0) for cell in cells for match in re.finditer(r"\b(?:19|20)\d{2}\b", cell)]
    return years[0] if years else None


def _bio_for_name(soup: BeautifulSoup, name: str) -> str:
    wanted = set(normalize_name(name).split())
    candidates: list[str] = []
    for node in soup.find_all(["p", "div", "td", "li"]):
        text = _clean(node.get_text(" ", strip=True))
        if len(text) < 80 or len(text) > 3000:
            continue
        low = text.lower()
        if any(marker in low for marker in ("shares beneficially owned", "salary ($)", "stock awards", "compensation table", "table of contents")):
            continue
        tokens = set(normalize_name(text).split())
        if len(wanted) >= 2 and len(wanted.intersection(tokens)) >= max(2, len(wanted) - 1):
            candidates.append(text)
    if not candidates:
        return ""
    narrative = [item for item in candidates if re.search(r"\b(?:has served|has been|joined|previously|prior to|experience|founded|co-founder|director since)\b", item, re.I)]
    surname = normalize_name(name).split()[-1] if normalize_name(name) else ""
    named = [item for item in narrative if surname in normalize_name(item[:220]).split()]
    narrative = named or narrative
    # Nested SEC HTML nodes repeat the same biography at several sizes.  The
    # shortest narrative node is normally the person's own biography; large
    # parent nodes are often an entire section or ownership table.
    dated = [item for item in narrative if re.search(r"\b(?:since|from|until)\b.*\b(?:19|20)\d{2}\b", item, re.I)]
    return min(dated or narrative or candidates, key=len)


def _career_entries(person: ManagementPerson, biography: str, source: OfficialSource) -> list[CareerEntry]:
    entries: list[CareerEntry] = []
    source_ids = [source.source_id]
    for title in person.current_roles or [None]:
        start_date = person.role_start_dates.get(title or "", person.current_since)
        entries.append(CareerEntry(
            career_id=f"career:{_stable(person.person_id, person.company_name, title or '')}",
            person_id=person.person_id,
            ticker=person.ticker,
            employer=person.company_name,
            issuer_cik=person.issuer_cik,
            employer_ticker=person.ticker.upper(),
            employer_cik=person.issuer_cik,
            employer_classification="PUBLIC_COMPANY_CONFIRMED",
            title=title,
            start_date=start_date,
            date_precision=_date_precision(start_date),
            is_current=True,
            responsibilities=person.relevant_responsibilities,
            functional_areas=_functional_areas(title, biography),
            industry_categories=_industries(biography),
            evidence_text=biography or f"Current role reported in {source.source_type}.",
            source_ids=source_ids,
        ))
    if not biography:
        return entries
    # Capture explicit employer/title statements without pretending that
    # ambiguous prose is a dated employment record.
    patterns = (
        r"(?:served|worked|was|acted)\s+as\s+(?P<title>[^.;,]{3,100}?)\s+(?:at|of|for)\s+(?P<employer>[A-Z][^.;,]{2,100})",
        r"(?:held|had)\s+(?:the\s+)?(?P<title>[^.;,]{3,100}?)\s+(?:at|with)\s+(?P<employer>[A-Z][^.;,]{2,100})",
        r"(?:spent|worked)\s+(?:\d+\s+years?\s+)?(?:at|with)\s+(?P<employer>[A-Z][^.;,]{2,100})",
    )
    seen: set[tuple[str, str]] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, biography, flags=re.I):
            employer = _clean(match.groupdict().get("employer") or "").strip(" ,")
            title = _clean(match.groupdict().get("title") or "").strip(" ,") or None
            if not employer or len(employer) < 3:
                continue
            issuer_token = normalize_name(person.company_name).split()[0] if normalize_name(person.company_name) else ""
            if employer.lower() in {person.company_name.lower(), "the company", "the issuer"} or (issuer_token and normalize_name(employer).startswith(issuer_token)):
                continue
            if any(token in employer.lower() for token in ("the board", "board of", "directors of", "the company")):
                continue
            if title and re.search(r"\b(?:board|director|member of)\b", title, re.I):
                continue
            marker = (employer.lower(), (title or "").lower())
            if marker in seen:
                continue
            seen.add(marker)
            years = re.search(r"\b((?:19|20)\d{2})\s+to\s+((?:19|20)\d{2})\b", match.group(0))
            entries.append(CareerEntry(
                career_id=f"career:{_stable(person.person_id, employer, title or '')}",
                person_id=person.person_id,
                ticker=person.ticker,
                employer=employer,
                issuer_cik=person.issuer_cik,
                employer_ticker=resolve_organization(employer).ticker,
                employer_cik=resolve_organization(employer).cik,
                employer_classification=resolve_organization(employer).classification,
                title=title,
                start_date=years.group(1) if years else None,
                end_date=years.group(2) if years else None,
                date_precision="year" if years else None,
                functional_areas=_functional_areas(title, match.group(0)),
                industry_categories=_industries(match.group(0)),
                event_type="employment",
                evidence_text=match.group(0),
                source_ids=source_ids,
            ))
    return entries


def _industries(text: str) -> list[str]:
    labels = {
        "technology": r"technology|software|cloud|cybersecurity|engineering",
        "financial_services": r"financial|banking|payments|investment",
        "healthcare": r"healthcare|health care|medical|biotech|pharmaceutical|drug development",
        "life_sciences": r"life sciences|therapeutic|clinical",
        "commercial": r"sales|marketing|commercial|go-to-market|business development",
        "legal_regulatory": r"legal|regulatory|compliance",
    }
    return sorted(label for label, pattern in labels.items() if re.search(pattern, text, re.I))


def _responsibility_phrases(text: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"\b(?:responsible|accountable)\s+for\s+([^.;]{3,180})", text, re.I):
        phrase = _clean(match.group(1)).strip(" ,")
        if phrase and phrase not in values:
            values.append(phrase)
    return values[:10]


def _functional_areas(title: str | None, text: str = "") -> list[str]:
    value = f"{title or ''} {text}"
    patterns = {
        "finance": r"chief financial|finance|financial reporting|accounting|treasury|controllership",
        "operations": r"chief operating|operations|operational",
        "product": r"product",
        "engineering": r"engineering|technology|software|technical",
        "sales": r"sales|revenue|commercial",
        "marketing": r"marketing|brand",
        "legal": r"general counsel|legal|law",
        "regulatory": r"regulatory|compliance",
        "strategy": r"strategy|strategic",
        "capital_markets": r"capital markets|investment banking",
        "M&A": r"merger|acquisition|M&A|corporate development",
        "research": r"research|discovery",
        "clinical": r"clinical|therapeutic",
        "manufacturing": r"manufacturing|supply chain",
        "government": r"government|public policy|military",
        "academia": r"university|professor|faculty|academic",
    }
    return sorted(label for label, pattern in patterns.items() if re.search(pattern, value, re.I))


def _date_precision(value: str | None) -> str | None:
    if not value:
        return None
    if re.fullmatch(r"\d{4}", value):
        return "year"
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return "month"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return "day"
    return "reported"


def _role_start_from_biography(title: str, biography: str) -> str | None:
    significant = [token for token in re.findall(r"[a-z]+", title.lower()) if len(token) > 3 and token not in {"principal", "officer"}]
    if not significant:
        return None
    pattern = r"(?=[^.;]{0,240}\bsince\s+(?:(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+)?(?:19|20)\d{2})"
    for match in re.finditer(pattern, biography, re.I):
        window = biography[max(0, match.start() - 240):match.end() + 40]
        if sum(1 for token in significant if token in window.lower()) >= max(1, min(2, len(significant))):
            return _date_from_text(window)
    return None


def _founder_scope(company_name: str, biography: str, *, structured: bool = False) -> str:
    if structured:
        return "issuer"
    lower = biography.lower()
    issuer_tokens = [token for token in normalize_name(company_name).split() if len(token) > 3]
    issuer_named = any(token in normalize_name(lower).split() for token in issuer_tokens)
    if re.search(r"(?:our|the company(?:'s)?)\s+(?:co[- ]?founder|founder)|co[- ]?founded\s+(?:the\s+)?(?:company|issuer)", lower) or issuer_named and re.search(r"founder|founded", lower):
        return "issuer"
    return "other_company"


@dataclass
class ManagementParseResult:
    people: list[ManagementPerson] = field(default_factory=list)
    roles: list[RoleAssertion] = field(default_factory=list)
    career: list[CareerEntry] = field(default_factory=list)
    boards: list[BoardMembership] = field(default_factory=list)
    relationships: list[PersonCompanyRelationship] = field(default_factory=list)
    education: list[EducationEntry] = field(default_factory=list)
    role_changes: list[RoleChangeEvent] = field(default_factory=list)
    source: OfficialSource | None = None
    warnings: list[str] = field(default_factory=list)
    status: str = "AVAILABLE"


_TEXT_PROXY_EXECUTIVE_ROLES = re.compile(
    r"Chairman\s+of\s+the\s+Board\s+and\s+Chief\s+Executive\s+Officer|"
    r"Chief\s+Executive\s+Officer|Chief\s+Financial\s+Officer|"
    r"Chief\s+Commercial\s+Officer(?:\s*-\s*Residential\s+Care\s+Software)?|"
    r"Chief\s+Product\s+Officer|Global\s+General\s+Counsel\s+and\s+Secretary",
    re.I,
)


def _plain_text_proxy_roster(
    text: str,
    filing: SECFiling,
    company_name: str,
    source: OfficialSource,
    by_key: dict[str, ManagementPerson],
    result: ManagementParseResult,
) -> None:
    """Recover the explicit roster when a cached proxy is PDF text.

    SEC HTML proxies continue through the structured-table path above.  Some
    official IR/SEC handoffs are cached as PDFs, however, and PDF extraction
    removes table tags while retaining the explicit nominee and officer
    sections.  This adapter consumes only those explicit sections; it does not
    infer people from arbitrary narrative text.
    """
    # Keep line boundaries: flattened PDF tables use one row/cell per line,
    # while the older prose adapter only needs ordinary whitespace cleanup.
    raw_text = text.decode("utf-8", errors="replace") if isinstance(text, bytes) else str(text or "")
    raw_lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    # Keep a parallel copy with column spacing intact.  PDF layout extraction
    # uses those spaces to align multi-column officer tables.
    layout_text = "\n".join(line.strip() for line in raw_lines)
    text = "\n".join(_clean(line) for line in raw_lines)
    if not text:
        return

    # PDF extraction can split a person's name across cells and place the
    # ``Director since`` value on its own line.  Keep the date discovered by
    # this adapter so ``add`` does not have to match a space-joined name
    # against the original line breaks.
    flat_board_dates: dict[str, str] = {}

    def _flat_nominees() -> None:
        """Parse the common SEC PDF text form of the nominee table.

        PDF text extraction removes the table cells, so the old parser saw
        only a handful of accidental ``Name | Position`` fragments.  The
        table still has a strong, issuer-neutral structure: a name line,
        occupation lines, and a terminal ``age / director-since /
        independent / outside-boards`` row.  We retain that evidence as a
        row rather than guessing from biographies or compensation tables.
        """
        section_match = None
        for start_match in re.finditer(r"Our\s+Director\s+Nominees", text, re.I):
            candidate = re.search(
                r"Our\s+Director\s+Nominees(?P<section>.*?)(?:Committee\s+Memberships|The\s+table\s+below\s+provides\s+current\s+membership)",
                text[start_match.start():], re.I | re.S,
            )
            if candidate:
                candidate_section = candidate.group("section")
                header_tokens = ("Name", "Occupation", "Age", "Director", "Independent", "Boards")
                if all(re.search(rf"\b{token}\b", candidate_section[:5000], re.I) for token in header_tokens):
                    # Rebase the match to the full text so subsequent offsets
                    # and evidence remain source-accurate.
                    section_match = candidate
                    break
        if not section_match:
            return
        section = section_match.group("section")
        header = re.search(
            r"\bName\s+Occupation\s+Age\s+Director\s+Since\s+Independent\s+Other\s+Public\s+Boards\b",
            section, re.I,
        )
        # Layout extraction may place the column labels on two lines and the
        # row cells on one line (``Name Age Director Independent Other Public``
        # / ``Occupation Since Boards``).  This is still an explicit proxy
        # table; parse its row shape without relying on issuer names.
        if header is None:
            compact_header = re.search(r"\bName\s+Age\s+Director\s+Independent\s+Other\s+Public\b", section, re.I)
            if compact_header:
                lines = [_clean(line) for line in section[compact_header.end():].splitlines() if _clean(line)]
                row_pattern = re.compile(r"^(?P<name>.+?)\s+(?P<age>\d{2})\s+(?P<since>(?:19|20)\d{2}|New)\s+(?P<independent>Yes|No)\s+(?P<boards>\d+)\s*$", re.I)
                row_positions = [(idx, match) for idx, line in enumerate(lines) if (match := row_pattern.fullmatch(line))]
                if row_positions:
                    for position, (line_index, match) in enumerate(row_positions):
                        name = _clean(match.group("name"))
                        if not _looks_like_name(name):
                            continue
                        end = row_positions[position + 1][0] if position + 1 < len(row_positions) else len(lines)
                        occupation = _clean(" ".join(lines[line_index + 1:end]))
                        independent = match.group("independent")
                        role_title = "Independent Director" if independent.lower() == "yes" else "Director"
                        if re.search(r"lead\s+independent", occupation, re.I):
                            role_title = "Lead Independent Director"
                        elif re.search(r"chair(?:man|person)?", occupation, re.I) and re.search(r"microsoft|company|board", occupation, re.I):
                            role_title = "Chairman and Director" if independent.lower() == "yes" else "Chairman and Chief Executive Officer"
                        evidence = f"{name} | {occupation} | {match.group('age')} | {match.group('since')} | {independent} | {match.group('boards')}"
                        add(name, role_title, board=True, evidence=evidence)
                    return
        if not header:
            return
        body = section[header.end():]
        lines = [_clean(line) for line in body.splitlines() if _clean(line)]
        tail_indices: list[tuple[int, str, str, str, str]] = []
        index = 0
        while index < len(lines):
            match = re.fullmatch(r"(\d{2})\s+((?:19|20)\d{2})\s+(Yes|No)\s+(\d+)", lines[index], re.I)
            since = None
            if not match and re.fullmatch(r"(\d{2})\s+New", lines[index], re.I) and index + 2 < len(lines):
                nominee = re.match(r"Nominee\s+(Yes|No)\s+(\d+)", lines[index + 1] + " " + lines[index + 2], re.I)
                if nominee:
                    match = re.match(r"(\d{2})\s+New", lines[index], re.I)
                    since = "New Nominee"
                    tail_indices.append((index, match.group(1), since, nominee.group(1), nominee.group(2)))
                    index += 3
                    continue
            if match:
                tail_indices.append((index, match.group(1), match.group(2), match.group(3), match.group(4)))
            index += 1
        previous = 0
        blocked_name_terms = re.compile(r"\b(?:former|executive|chief|president|partner|group|founder|chairman|chair|director|cfo|ceo|inc|corporation|company|llc)\b", re.I)
        for tail_index, age, since, independent, outside_boards in tail_indices:
            segment = lines[previous:tail_index]
            previous = tail_index + (2 if since == "New Nominee" else 1)
            candidates = []
            for line_no, line in enumerate(segment):
                if "," in line or not _looks_like_name(line):
                    continue
                if blocked_name_terms.search(line):
                    continue
                # Names have no prose punctuation and are normally 2--5
                # tokens; occupations such as ``Partner Greylock Partners``
                # are excluded by the role/company vocabulary above.
                candidates.append((line_no, line))
            if not candidates:
                continue
            name_line, name = candidates[-1]
            occupation = _clean(" ".join(segment[name_line + 1:]))
            independent_label = "Independent Director" if independent.lower() == "yes" else "Director"
            role_title = independent_label
            if re.search(r"lead\s+independent", occupation, re.I):
                role_title = "Lead Independent Director"
            elif re.search(r"chair(?:man|person)?", occupation, re.I) and re.search(r"microsoft|company|board", occupation, re.I):
                role_title = "Chairman and Director" if independent.lower() == "yes" else "Chairman and Chief Executive Officer"
            evidence = f"{name} | {occupation} | {age} | {since} | {independent} | {outside_boards}"
            add(name, role_title, board=True, evidence=evidence)

    def _flat_director_since_roster() -> None:
        """Recover flattened director rows without issuer-specific names.

        A number of official proxy PDFs expose the nominee table as a stream
        of cells rather than a structured HTML table.  The durable boundary
        in that representation is the explicit ``Director since`` cell.  We
        only use rows immediately preceding that marker and retain the row as
        evidence; arbitrary names elsewhere in the proxy are never treated as
        directors.
        """
        lines = [_clean(line) for line in text.splitlines()]
        raw_marker_indexes = [idx for idx, line in enumerate(lines) if re.search(r"^Director\s+since\b", line, re.I)]
        marker_indexes: list[int] = []
        if raw_marker_indexes:
            marker_indexes = [raw_marker_indexes[0]]
            for marker in raw_marker_indexes[1:]:
                # The nominee grid is a contiguous run.  Later biographies
                # and qualification pages repeat ``Director since`` hundreds
                # of lines away and must not be mistaken for new rows.
                if marker - marker_indexes[-1] > 100:
                    break
                marker_indexes.append(marker)
        if not marker_indexes:
            return
        # This statement is an issuer-neutral way to resolve the common
        # proxy convention where the CEO is the sole non-independent nominee.
        all_independent_except_ceo = bool(re.search(
            r"(?:all\s+(?:are|of\s+(?:the\s+)?(?:director\s+)?nominees)|"
            r"all\s+nominees)\s+(?:are\s+)?independent[^.]{0,100}"
            r"(?:other\s+than|except)\s+(?:our\s+)?(?:ceo|chief\s+executive)",
            text, re.I | re.S,
        ))
        blocked = re.compile(
            r"\b(?:director|directors|independent|nominee|nominees|occupation|retired|"
            r"chairman|chair|chief|executive|officer|president|partner|company|corporation|"
            r"llc|public|board|committee|since|age|lead|management|membership|bank|council|"
            r"foundation|hospital|health|news|world|forum|progress|"
            r"nominating|audit|compensation|development|responsibility|committee|risk|"
            r"membership\d*|"
            r"of|and|for|the|to|in|on|with|from|co-founder)\b|[&,;�]", re.I,
        )
        issuer_tokens = [token for token in normalize_name(company_name).split() if len(token) > 3]
        previous_end = 0
        for marker_index in marker_indexes:
            # Do not consume the entire preceding page.  A nominee row is
            # short even when the occupation and committee cells are long.
            start = max(previous_end, marker_index - 12)
            segment = lines[start:marker_index]
            previous_end = marker_index + 1
            candidates: list[tuple[int, int, str]] = []
            # Layout-preserving extraction keeps the nominee columns on one
            # line and may split a surname onto the next line.  Recover the
            # leading name cell before falling back to ordinary line windows.
            for line_no in range(max(0, len(segment) - 6), len(segment)):
                age_match = re.search(r"\b\d{2}\b", segment[line_no])
                if not age_match:
                    continue
                prefix = _clean(segment[line_no][:age_match.start()])
                continuation: list[str] = []
                for tail in segment[line_no + 1:]:
                    if re.search(r"^Director\s+since\b", tail, re.I) or re.match(r"^(?:committee|risk|audit|chair)\b", tail, re.I):
                        break
                    if (_looks_like_name(tail) and not blocked.search(tail)) or (re.fullmatch(r"[A-Z][A-Za-z'’.-]+", tail) and not blocked.search(tail)):
                        continuation.append(tail)
                    else:
                        surname = re.match(r"(?P<token>[A-Z][A-Za-z'’.-]+)\b", tail)
                        if surname and not blocked.search(surname.group("token")):
                            continuation.append(surname.group("token"))
                        else:
                            break
                value = _clean(" ".join([prefix, *continuation]))
                if _looks_like_name(value) and not blocked.search(value):
                    candidates.append((len(value.split()), line_no, value))
            for width in range(1, min(4, len(segment)) + 1):
                for begin in range(max(0, len(segment) - 10), len(segment) - width + 1):
                    value = _clean(" ".join(segment[begin:begin + width]))
                    if not _looks_like_name(value) or blocked.search(value):
                        continue
                    # Prefer the longest valid contiguous name and, for ties,
                    # the candidate nearest to the explicit marker.
                    candidates.append((width, begin, value))
            if not candidates:
                continue
            width, begin, name = max(candidates, key=lambda item: (item[0], item[1]))
            next_marker = next((item for item in marker_indexes if item > marker_index), len(lines))
            row_text = _clean(" ".join(segment[begin:] + lines[marker_index:min(next_marker, marker_index + 12)]))
            marker = lines[marker_index]
            since_match = re.search(r"\b((?:19|20)\d{2})\b", marker)
            since = since_match.group(1) if since_match else None
            if since:
                flat_board_dates[normalize_name(name)] = since
            lower_row = row_text.lower()
            if re.search(r"lead\s+independent", lower_row):
                role = "Lead Independent Director"
            elif re.search(r"chairman|chief\s+executive\s+officer", lower_row) and (
                any(
                    re.search(r"chief\s+executive\s+officer\s+of[^.;]{0,80}" + re.escape(token), lower_row)
                    for token in issuer_tokens
                )
            ):
                role = "Chairman and Chief Executive Officer"
            elif all_independent_except_ceo:
                role = "Independent Director"
            else:
                role = "Director"
            independent = "independent" if role.lower().startswith(("independent", "lead independent")) else "not independently classified"
            evidence = f"Flattened proxy director row: {name}; {row_text}; {marker}; {independent}"
            add(name, role, board=True, evidence=evidence)

    nominees = re.search(
        r"nominees?\s+as\s+directors?:\s*(?P<names>.+?)(?:\.\s*(?:FOR|Your\s+board)\b)",
        text, re.I,
    )

    # The proxy's flattened PDF text contains a compact nominee overview
    # with an explicit Position column.  Read that bounded table first so a
    # nearby leadership paragraph cannot incorrectly assign every nominee the
    # same chair/lead role.
    overview_roles: dict[str, str] = {}
    overview_match = re.search(
        r"Director\s+Age\s+as\s+of\s+[^:]{0,100}?\s+Position\s+"
        r"(?P<table>.*?)(?:Information\s+about\s+the\s+\w+\s+nominees?\s+for\s+director)",
        text,
        re.I,
    )
    if overview_match:
        table = overview_match.group("table")
        # Name tokens are discovered from the actual nominee roster below;
        # no issuer-specific names are embedded in the parser.
        nominee_names = []
        for raw in re.split(r",|\band\b", (nominees.group("names") if nominees else ""), flags=re.I):
            value = _clean(raw).strip(" .")
            if _looks_like_name(value):
                nominee_names.append(value)
        occurrences: list[tuple[int, str, re.Match[str]]] = []
        for name in nominee_names:
            match = re.search(re.escape(name), table, re.I)
            if match:
                occurrences.append((match.start(), name, match))
        occurrences.sort(key=lambda item: item[0])
        for index, (_start, name, match) in enumerate(occurrences):
            end = occurrences[index + 1][0] if index + 1 < len(occurrences) else len(table)
            tail = table[match.end():end]
            tail = re.sub(r"^\s*\d+\s+", "", tail)
            tail = re.split(r"\b(?:Independent\s+Board|Committee\s+Independence|Our\s+)", tail, maxsplit=1, flags=re.I)[0]
            role = _clean(tail).strip(" ,;.")
            if role:
                overview_roles[normalize_name(name)] = role

    # Leadership structure is a separate compact statement in many flattened
    # proxies.  Prefer its explicit lead/chair labels over the generic
    # "Director" position in the nominee overview.
    leadership_roles: dict[str, str] = {}
    leadership_match = re.search(
        r"Board\s+Leadership\s+Structure\s+(?P<section>.*?)(?:Committee\s+Evolution|Board\s+Committee|The\s+Board\s+believes)",
        text,
        re.I,
    )
    if leadership_match and nominees:
        section = leadership_match.group("section")
        nominee_names = []
        for raw in re.split(r",|\band\b", nominees.group("names"), flags=re.I):
            value = _clean(raw).strip(" .")
            if _looks_like_name(value):
                nominee_names.append(value)
        occurrences = []
        for name in nominee_names:
            match = re.search(re.escape(name), section, re.I)
            if match:
                occurrences.append((match.start(), name, match))
        occurrences.sort(key=lambda item: item[0])
        for index, (_start, name, match) in enumerate(occurrences):
            end = occurrences[index + 1][0] if index + 1 < len(occurrences) else len(section)
            tail = _clean(section[match.end():end]).strip(" ,;.")
            tail = re.split(r"\b(?:Independent\s+Board|Committee\s+Independence|Our\s+)", tail, maxsplit=1, flags=re.I)[0].strip(" ,;.")
            leadership_roles[normalize_name(name)] = tail

    def board_role(name: str) -> str:
        role = leadership_roles.get(normalize_name(name), "") or overview_roles.get(normalize_name(name), "")
        if re.search(r"\blead(?:\s+independent)?\s+director\b", role, re.I):
            return "lead independent director"
        if re.search(r"\bchair(?:man|person)?\s+of\s+the\s+board\b", role, re.I):
            return "chair"
        return "director"

    def board_start_date(name: str) -> str | None:
        if normalize_name(name) in flat_board_dates:
            return flat_board_dates[normalize_name(name)]
        match = re.search(
            re.escape(name) + r".{0,900}?\bDirector\s+since:\s*((?:19|20)\d{2})",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        return match.group(1) if match else None

    def _narrative_nominee_roster() -> None:
        """Parse an explicit nominee overview in flattened proxy text.

        Some SEC proxy PDFs have no table cells after text extraction.  The
        nominee overview still has a durable shape: a person name, an
        explicit independence/role line, and an ``Age ... Director Since``
        marker.  Requiring that marker keeps ordinary biography prose out of
        the board roster.
        """
        matches = []
        for item in re.finditer(
            r"Overview\s+of\s+Director\s+Nominees(?:\s+and\s+Committee\s+Assignments)?",
            text, re.I,
        ):
            tail = text[item.end():item.end() + 20000]
            if re.search(r"\bAge\s*:?\s*\d{2}[^\n]{0,80}\b(?:Director\s+Since|Joined\s+the\s+Board)\b", tail, re.I) and re.search(r"\bBoard\s+Experience\s+and\s+Composition\b", tail, re.I):
                matches.append(item)
        if not matches:
            return
        # The first matching heading is normally the table of contents.  A
        # real overview has a meaningful body before the next section.
        chosen = next((item for item in matches if (re.search(r"\bBoard\s+Experience\s+and\s+Composition\b", text[item.end():], re.I) and re.search(r"\bBoard\s+Experience\s+and\s+Composition\b", text[item.end():], re.I).start() > 1000)), matches[0])
        start = chosen.end()
        end_match = re.search(r"\b(?:Board\s+Experience\s+and\s+Composition|Director\s+Nominees\s+for\s+20\d{2}|Corporate\s+Governance)\b", text[start:], re.I)
        section = text[start:start + end_match.start()] if end_match else text[start:start + 25000]
        lines = [_clean(line).strip() for line in section.splitlines() if _clean(line)]
        blocked = re.compile(r"\b(?:board|committee|assignments?|overview|independence|tenure|age|director|nominees?|public|company|other|current|chair|chairman|president|ceo|cfo|chief|executive|officer|founder|co-founder|owner|advisor|partner|retired|former|skills|experience|proposal|shareholders?|inc|labs|corporation|group|news)\b", re.I)
        for idx, line in enumerate(lines):
            candidate = _clean(line).rstrip("*:")
            if not _looks_like_name(candidate) or blocked.search(candidate):
                continue
            marker_idx = next((j for j in range(idx + 1, min(len(lines), idx + 7)) if re.search(r"\bAge\s*:?\s*\d{2}.*\b(?:Director\s+Since|Joined\s+the\s+Board)\b", lines[j], re.I)), None)
            if marker_idx is None:
                continue
            role_lines = lines[idx + 1:marker_idx]
            if not role_lines:
                continue
            role_text = _clean(" ".join(role_lines))
            if not re.search(r"\b(?:independent|chair|director|president|ceo|chief|founder|retired|advisor|partner|owner)\b", role_text, re.I):
                continue
            marker = lines[marker_idx]
            since_match = re.search(r"\b(?:Director\s+Since|Joined\s+the\s+Board)\s*:?[\s\u00b7-]*((?:19|20)\d{2})\b", marker, re.I)
            age_match = re.search(r"\bAge\s*:?\s*(\d{2})", marker, re.I)
            role = "Independent Director" if re.search(r"\bindependent\b", role_text, re.I) else "Director"
            if re.search(r"lead\s+independent", role_text, re.I):
                role = "Lead Independent Director"
            elif role == "Director" and re.search(r"non[- ]executive\s+chair", role_text, re.I):
                role = "Non-Executive Chairman and Director"
            evidence = f"{candidate} | {role_text} | {marker}"
            add(candidate, role, board=True, evidence=evidence)
            if since_match:
                flat_board_dates[normalize_name(candidate)] = since_match.group(1)

    def _narrative_neo_roster() -> None:
        """Parse the explicit ``For fiscal ... our NEOs were`` roster."""
        match = re.search(r"(?:For\s+fiscal\s+20\d{2},\s+)?(?:our\s+)?(?:NEOs|Named\s+Executive\s+Officers)\s+(?:were|are)\s*:", text, re.I)
        if not match:
            return
        section = text[match.end():match.end() + 5000]
        lines = [_clean(line).strip() for line in section.splitlines() if _clean(line)]
        former = {m.group(1).lower() for m in re.finditer(r"\b([A-Z][A-Za-z'’-]+)\s+retired\b", section, re.I)}
        role_re = re.compile(r"\b(?:chief|ceo|cfo|president|executive\s+vice\s+president|vice\s+president|secretary|treasurer|officer)\b", re.I)
        for idx, line in enumerate(lines):
            name = _clean(line).rstrip("*")
            if not _looks_like_name(name) or role_re.search(name):
                continue
            role_lines: list[str] = []
            for role_line in lines[idx + 1:idx + 6]:
                if role_lines and _looks_like_name(role_line) and not role_re.search(role_line):
                    break
                if re.match(r"^\*\s*Mr\.?", role_line, re.I):
                    break
                role_lines.append(role_line)
                if role_re.search(" ".join(role_lines)) and len(role_lines) >= 1:
                    # A wrapped title may have a final location/segment line,
                    # so keep consuming until the next person-shaped line.
                    continue
            role = _clean(" ".join(role_lines))
            if not role_re.search(role):
                continue
            surname = normalize_name(name).split()[-1].lower() if normalize_name(name) else ""
            current = surname not in former and not re.search(r"\bretired\b", role, re.I)
            add(name, role, board=False, is_current=current, evidence=f"Explicit NEO roster: {name} — {role}")

    def add(name: str, role_title: str, *, board: bool, is_current: bool = True, evidence: str = "") -> None:
        name = re.sub(r"\s*\([a-z]\)", "", _clean(name), flags=re.I).strip(" ,")
        if not _looks_like_name(name):
            return
        key = normalize_name(name)
        categories = classify_role(role_title)
        if board and "director" not in categories and "chair" not in categories and "lead_independent_director" not in categories:
            categories.append("director")
        categories = sorted(set(categories or (["director"] if board else ["officer"])))
        start_date = board_start_date(name) if board and is_current else None
        person = by_key.get(key)
        if person is None:
            person = ManagementPerson(
                person_id=person_key(filing.ticker, name), ticker=filing.ticker.upper(), company_name=company_name,
                full_name=name, normalized_name=key, issuer_cik=filing.issuer_cik,
                current_roles=[role_title] if is_current else [], role_categories=categories,
                current_since=start_date,
                is_founder="founder" in categories, founder_scope="issuer" if "founder" in categories else "unknown",
                relevant_industries=_industries(role_title), relevant_responsibilities=responsibilities_from_title(role_title),
                source_ids=[source.source_id], created_at=filing.filing_date or "", updated_at=filing.filing_date or "",
            )
            by_key[key] = person
        else:
            if is_current and role_title not in person.current_roles:
                person.current_roles.append(role_title)
            if is_current and start_date and person.current_since is None:
                person.current_since = start_date
            person.role_categories = sorted(set(person.role_categories + categories))
            person.relevant_industries = sorted(set(person.relevant_industries + _industries(role_title)))
            person.relevant_responsibilities = sorted(set(person.relevant_responsibilities + responsibilities_from_title(role_title)))
            person.source_ids = sorted(set(person.source_ids + [source.source_id]))
        result.roles.extend(
            RoleAssertion(
                assertion_id=f"role:{_stable(person.person_id, role_title, category, str(is_current))}",
                person_id=person.person_id, ticker=filing.ticker.upper(), issuer_cik=filing.issuer_cik,
                role=role_title, role_category=category, is_current=is_current,
                start_date=start_date if is_current else None,
                date_precision=_date_precision(start_date if is_current else None),
                title_as_reported=role_title, responsibilities=responsibilities_from_title(role_title),
                evidence_text=evidence or role_title, source_ids=[source.source_id],
            ) for category in categories
        )
        if board:
            result.boards.append(BoardMembership(
                board_id=f"board:{_stable(person.person_id, company_name, role_title)}",
                person_id=person.person_id, ticker=filing.ticker.upper(), board_company=company_name,
                issuer_cik=filing.issuer_cik, person_company_id=f"relationship:{_stable(person.person_id, filing.issuer_cik)}",
                board_ticker=filing.ticker.upper(), board_cik=filing.issuer_cik,
                board_classification="PUBLIC_COMPANY_CONFIRMED", board_role=role_title,
                start_date=start_date,
                date_precision=_date_precision(start_date),
                is_current=is_current, evidence_text=evidence or role_title, source_ids=[source.source_id],
            ))

    _flat_nominees()
    _flat_director_since_roster()
    if not result.boards:
        _narrative_nominee_roster()

    if nominees:
        for raw_name in re.split(r",|\band\b", nominees.group("names"), flags=re.I):
            name = _clean(raw_name).strip(" .")
            if _looks_like_name(name):
                role = board_role(name)
                add(name, role, board=True, evidence=f"Proxy nominee roster: {name} — {role}")

    # Named executive officers are often rendered as a prose list in PDF
    # text rather than an HTML table.  The list is an explicit SEC roster;
    # role details are enriched from nearby title statements when available.
    executive_list = re.search(
        r"(?:Named\s+Executives|named\s+executive\s+officers).*?\b(?:who\s+are|were)\s*:\s*(?P<names>[^.]{10,500})\.",
        text, re.I | re.S,
    )
    if executive_list:
        raw_names = re.split(r",|\band\b", executive_list.group("names"), flags=re.I)
        for raw_name in raw_names:
            name = _clean(raw_name).strip(" ;:")
            # A list entry can carry an explanatory prefix.  Keep only the
            # final person-shaped phrase and require an actual name.
            name = re.sub(r"^(?:our|the)\s+", "", name, flags=re.I)
            if not _looks_like_name(name):
                continue
            window = text[max(0, executive_list.start() - 1000):min(len(text), executive_list.end() + 5000)]
            title_match = re.search(
                re.escape(name) + r"[^.\n]{0,100}\b(?P<title>(?:Chairman|Chair|Chief|CEO|CFO|President|Executive Vice President|Vice President)[^.\n]{0,100})",
                window, re.I,
            )
            role = _clean(title_match.group("title")) if title_match else "Named Executive Officer"
            add(name, role, board=False, evidence=f"Named executive officer roster: {name} — {role}")

    executive_section = re.search(
        r"our\s+named\s+executive\s+officers\s+for\s+fiscal\s+year\s+\d{4}\s+were:\s*"
        r"(?P<section>.*?)(?:This\s+section\s+also\s+discusses|This\s+section\s+discusses)",
        text, re.I,
    )
    if executive_section:
        section = executive_section.group("section")
        previous_end = 0
        resignation_marker = re.search(r"\b(?:resigned|retired|departed)\b", section, re.I)
        former_surnames = {
            match.group("surname").lower()
            for match in re.finditer(
                r"(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?)?\s*(?P<surname>[A-Z][A-Za-z'’-]+)\s+"
                r"(?:resigned|retired|departed)\b",
                section,
                re.I,
            )
        }
        for match in _TEXT_PROXY_EXECUTIVE_ROLES.finditer(section):
            # Explanatory footnotes can repeat an officer title after the
            # roster (for example, a former officer's resignation).  Only
            # consume the contiguous roster before that disclosure.
            if resignation_marker and match.start() > resignation_marker.start():
                break
            # Between consecutive role matches the PDF text contains only the
            # next officer's name (possibly with a footnote marker).  This is
            # more reliable than taking title-cased words from the whole
            # prefix, which can absorb the previous officer's role.
            name_segment = section[previous_end:match.start()]
            previous_end = match.end()
            words = re.findall(r"[A-Z][A-Za-z'’\-]+", name_segment)
            candidate = " ".join(words[-4:]) if words else None
            if candidate and not _looks_like_name(candidate):
                for width in (3, 2):
                    value = " ".join(words[-width:]) if len(words) >= width else ""
                    if _looks_like_name(value):
                        candidate = value
                        break
                    candidate = None
            if not candidate:
                continue
            role = _clean(match.group(0))
            current = normalize_name(candidate).split()[-1].lower() not in former_surnames
            add(candidate, role, board=False, is_current=current, evidence=f"Named executive officer roster: {candidate} — {role}")

    _narrative_neo_roster()

    # Some SEC PDFs use a compact NEO roster immediately before the
    # compensation discussion.  Parse only that explicit bounded list so
    # compensation-table names and biography mentions cannot become officers.
    neo_section = re.search(
        r"Our\s+\d{4}\s+(?:NEOs|Named\s+Executive\s+Officers)\s+are:\s*"
        r"(?P<section>.*?)(?=\n\d{4}\s+Firm\s+performance\b|\n(?:Executive\s+compensation|Compensation)\b)",
        text, re.I | re.S,
    )
    if neo_section:
        layout_neo = re.search(
            r"Our\s+\d{4}\s+(?:NEOs|Named\s+Executive\s+Officers)\s+are:\s*"
            r"(?P<section>.*?)(?=\n\s*\d{4}\s+Firm\s+performance\b|\n\s*(?:Executive\s+compensation|Compensation)\b)",
            layout_text, re.I | re.S,
        )
        neo_source = layout_neo.group("section") if layout_neo else neo_section.group("section")
        neo_lines = [line.strip() for line in neo_source.splitlines() if line.strip()]
        title_pattern = re.compile(
            r"\b(?:chair(?:man)?|co[- ]?ceo|ceo|cfo|chief\s+\w+(?:\s+\w+){0,3}\s+officer|"
            r"president|vice(?:\s+chair)?|chair\d*|executive\s+vice\s+president|secretary|treasurer)\b",
            re.I,
        )
        layout_added = False
        # Layout extraction represents the explicit roster as aligned columns:
        # one name row followed by two or more title rows.  Cell order is
        # stable even when a person's surname or title wraps to the next row.
        if len(neo_lines) >= 3:
            cell_rows = [re.split(r"\s{2,}", line.strip()) for line in neo_lines[:4]]
            if len(cell_rows[0]) >= 2 and any(title_pattern.search(cell) for row in cell_rows[1:] for cell in row):
                has_wrapped_chair = any(re.search(r"^Chair\d*$", cell, re.I) for row in cell_rows[1:] for cell in row)
                for column, raw_name in enumerate(cell_rows[0]):
                    parts = [row[column] for row in cell_rows[1:] if column < len(row)]
                    name = _clean(raw_name)
                    if len(name.split()) == 1 and parts and _looks_like_name(f"{name} {parts[0]}"):
                        name = f"{name} {parts.pop(0)}"
                    if not _looks_like_name(name):
                        continue
                    role = _clean(" ".join(parts)).strip(" ,;:")
                    role = re.sub(r"\s*Chair\d*\b", "", role, flags=re.I).strip()
                    if has_wrapped_chair and role.lower() == "vice":
                        role = "Vice Chair"
                    if not role or not title_pattern.search(role):
                        continue
                    add(name, role, board=False, evidence=f"Explicit NEO roster: {name} — {role}")
                    layout_added = True
        if layout_added:
            return

        runs: list[tuple[int, int]] = []
        idx = 0
        while idx < len(neo_lines):
            if not title_pattern.search(neo_lines[idx]):
                idx += 1
                continue
            end = idx + 1
            while end < len(neo_lines) and title_pattern.search(neo_lines[end]):
                end += 1
            runs.append((idx, end))
            idx = end
        previous_end = 0
        neo_blocked = re.compile(
            r"\b(?:bank|management|membership|development|responsibility|committee|risk|"
            r"audit|compensation|public|company|board|chair|chief|executive|officer|"
            r"president|vice|of|and|for|the|to|in|on|with|from)\b|[&,;�]", re.I,
        )
        for start, end in runs:
            name_lines = neo_lines[previous_end:start]
            previous_end = end
            candidates: list[tuple[int, str]] = []
            for width in range(1, min(4, len(name_lines)) + 1):
                for begin in range(max(0, len(name_lines) - 5), len(name_lines) - width + 1):
                    value = _clean(" ".join(name_lines[begin:begin + width]))
                    if _looks_like_name(value) and not neo_blocked.search(value):
                        candidates.append((width, value))
            if not candidates:
                continue
            name = max(candidates, key=lambda item: item[0])[1]
            role = _clean(" ".join(neo_lines[start:end])).strip(" ,;:")
            role = re.sub(r"\bChair\d*\b", "Chair", role, flags=re.I)
            # A proxy may retain a former officer in the NEO list while
            # reporting a current transition role.  Preserve the person and
            # classify only the explicitly former executive title as
            # historical; the reported current title remains current.
            current = not bool(re.search(r"ceased\s+to\s+be|former\s+executive\s+officer", role, re.I))
            add(name, role or "Named Executive Officer", board=False, is_current=current, evidence=f"Explicit NEO roster: {name} — {role}")


def parse_proxy_people(html: str | bytes, filing: SECFiling, company_name: str) -> ManagementParseResult:
    """Parse structured proxy role tables and retain biography evidence.

    The parser intentionally emits only names with an explicit role/board
    context.  Free-form biographies enrich those records but cannot create a
    current officer by themselves.
    """
    soup = BeautifulSoup(html, "html.parser")
    source = OfficialSource(
        source_id=f"sec:{filing.issuer_cik}:{filing.accession_number}",
        source_authority="sec_official_filing",
        source_type=filing.form_type.upper(),
        url=filing.source_url,
        local_path=filing.local_source_path,
        issuer_cik=filing.issuer_cik,
        accession_number=filing.accession_number,
        filing_date=filing.filing_date,
        report_date=filing.report_date,
    )
    result = ManagementParseResult(source=source)
    by_key: dict[str, ManagementPerson] = {}
    for table in soup.find_all("table"):
        table_text = _clean(table.get_text(" ", strip=True))
        lower_table = table_text.lower()
        rows_preview = table.find_all("tr", limit=3)
        header_parts = [_clean(row.get_text(" ", strip=True)) for row in rows_preview]
        header_parts = [item for item in header_parts if item]
        header_text = " | ".join(header_parts).lower()
        # Restrict role extraction to roster tables.  A proxy contains many
        # compensation, ownership and table-of-contents tables that mention
        # directors or executives but do not assert a person's role.
        board_context = bool(
            re.search(r"(?:^|\|\s*)(?:directors with terms|director nominees?\b|nominees?\b|continuing directors\b)", header_text)
            and re.search(r"\bposition\b|director since", header_text)
            or bool(re.search(r"\|\s*directors?\s*$", header_text))
            or bool(re.search(r"\bname\b.*\bdirector\s+since\b", header_text))
        )
        executive_context = bool(
            re.search(r"(?:^|\|\s*)name\b", header_text)
            and re.search(r"\bposition(?:\(s\))?\b", header_text)
            and not re.search(r"salary|compensation|award|stock|fiscal year|grant date", header_text)
        )
        if not board_context and not executive_context:
            continue
        for row in table.find_all("tr"):
            cells = [_clean(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"])]
            cells = [cell for cell in cells if cell]
            if len(cells) < 2:
                continue
            name = re.sub(r"\s*\([^)]*\)", "", cells[0]).strip(" ,")
            if not _looks_like_name(name):
                continue
            row_text = " | ".join(cells)
            categories = classify_role(row_text)
            if board_context and "director" not in categories and "chair" not in categories and "lead_independent_director" not in categories:
                categories.append("director")
            if not categories or (not executive_context and not board_context):
                continue
            # Rows labelled former/director emeritus are historical assertions.
            is_current = not bool(re.search(r"\bformer\b|\bem(eritus|erita)\b|\bretired\b", row_text, re.I))
            role_cells = [cell for cell in cells[1:] if classify_role(cell)]
            role_title = role_cells[0] if role_cells else ("director" if board_context else (cells[1] if len(cells) > 1 else row_text))
            if re.fullmatch(r"\d+(?:\.\d+)?", role_title.strip()):
                role_title = "director" if board_context else role_title
            if len(role_cells) > 1:
                role_title = " | ".join(role_cells)
            key = normalize_name(name)
            current_since = _date_from_text(row_text) or (_year_cell(cells[2:]) if board_context else None)
            person = by_key.get(key)
            if person is None:
                person = ManagementPerson(
                    person_id=person_key(filing.ticker, name),
                    ticker=filing.ticker.upper(),
                    company_name=company_name,
                    full_name=name,
                    normalized_name=key,
                    issuer_cik=filing.issuer_cik,
                    current_roles=[role_title] if is_current else [],
                    role_categories=sorted(set(categories)),
                    current_since=current_since if is_current else None,
                    is_founder="founder" in categories,
                    founder_scope="issuer" if "founder" in categories else "unknown",
                    founder_context=row_text if "founder" in categories else None,
                    relevant_industries=_industries(row_text),
                    relevant_responsibilities=responsibilities_from_title(role_title),
                    source_ids=[source.source_id],
                    created_at=filing.filing_date or "",
                    updated_at=filing.filing_date or "",
                )
                by_key[key] = person
            else:
                if is_current and role_title not in person.current_roles:
                    person.current_roles.append(role_title)
                person.role_categories = sorted(set(person.role_categories + categories))
                person.relevant_industries = sorted(set(person.relevant_industries + _industries(row_text)))
                person.relevant_responsibilities = sorted(set(person.relevant_responsibilities + responsibilities_from_title(role_title)))
                person.is_founder = person.is_founder or "founder" in categories
                if "founder" in categories:
                    person.founder_scope = "issuer"
                person.source_ids = sorted(set(person.source_ids + [source.source_id]))
            evidence = row_text
            for category in categories:
                result.roles.append(RoleAssertion(
                    assertion_id=f"role:{_stable(person.person_id, role_title, category, str(is_current))}",
                    person_id=person.person_id,
                    ticker=filing.ticker.upper(),
                    issuer_cik=filing.issuer_cik,
                    role=role_title,
                    role_category=category,
                    is_current=is_current,
                    start_date=current_since if is_current else None,
                    date_precision=_date_precision(current_since if is_current else None),
                    title_as_reported=role_title,
                    responsibilities=responsibilities_from_title(role_title),
                    evidence_text=evidence,
                    source_ids=[source.source_id],
                ))
            if board_context and is_current:
                result.boards.append(BoardMembership(
                    board_id=f"board:{_stable(person.person_id, company_name, role_title)}",
                    person_id=person.person_id,
                    ticker=filing.ticker.upper(),
                    board_company=company_name,
                    issuer_cik=filing.issuer_cik,
                    person_company_id=f"relationship:{_stable(person.person_id, filing.issuer_cik)}",
                    board_ticker=filing.ticker.upper(),
                    board_cik=filing.issuer_cik,
                    board_classification="PUBLIC_COMPANY_CONFIRMED",
                    board_role="chair" if "chair" in categories else ("lead independent director" if "lead_independent_director" in categories else "director"),
                    start_date=current_since,
                    date_precision=_date_precision(current_since),
                    is_current=True,
                    evidence_text=evidence,
                    source_ids=[source.source_id],
                ))
    # PDF extraction preserves the explicit roster text but removes HTML table
    # structure.  Use the constrained text adapter only when no structured
    # tables produced a person; HTML proxy behavior remains unchanged.
    # A flattened proxy can produce accidental officer rows from text that
    # BeautifulSoup interprets as a partial table.  If no board roster was
    # recovered, always run the constrained plain-text adapter so an explicit
    # nominee section can repair that partial result.
    if not result.boards and not soup.find_all("table"):
        plain_payload = html if isinstance(html, str) else html.decode("utf-8", errors="replace")
        _plain_text_proxy_roster(plain_payload, filing, company_name, source, by_key, result)

    # Flattened officer tables can turn a wrapped title into a pseudo-name
    # (for example ``Chief Financial Officer`` or ``Walmart U``).  Keep only
    # person-shaped identities; their role assertions are discarded with the
    # invalid identity so they cannot pollute management or governance stores.
    invalid_identity = re.compile(r"\b(?:chief|executive|officer|president|vice|ceo|cfo|walmart|company|board|director|chairman|chair|named|neo)\b", re.I)
    invalid_keys = {key for key, person in by_key.items() if invalid_identity.search(person.full_name) or "," in person.full_name}
    if invalid_keys:
        invalid_ids = {by_key[key].person_id for key in invalid_keys}
        for key in invalid_keys:
            by_key.pop(key, None)
        result.roles = [item for item in result.roles if item.person_id not in invalid_ids]
        result.boards = [item for item in result.boards if item.person_id not in invalid_ids]

    for person in by_key.values():
        biography = _bio_for_name(soup, person.full_name)
        surname = normalize_name(person.full_name).split()[-1] if normalize_name(person.full_name) else ""
        # Parent tables and ownership rows often contain several names and
        # happen to mention the word founder.  A biography assertion must
        # identify this person near its beginning; otherwise retain only the
        # structured role row as evidence.
        if biography and surname not in normalize_name(biography[:220]).split():
            biography = ""
        if biography and not re.search(r"\b(?:has served|has been|served|joined|previously|prior to|currently|founded|co[- ]?founder|since\s+(?:19|20)\d{2})\b", biography[:500], re.I):
            biography = ""
        if biography:
            if person.current_since is None:
                person.current_since = _date_from_text(biography)
            for role_title in person.current_roles:
                role_date = _role_start_from_biography(role_title, biography)
                if role_date:
                    person.role_start_dates[role_title] = role_date
            person.role_start_dates = dict(person.role_start_dates)
            person.biography = biography
            person.relevant_industries = sorted(set(person.relevant_industries + _industries(biography)))
            person.relevant_responsibilities = sorted(set(person.relevant_responsibilities + _responsibility_phrases(biography)))
            biography_is_roster = bool(re.match(r"\s*(?:name\s+age|director nominees|proposal\s+snapshot|questions\s+and\s+answers)", biography, re.I))
            if not biography_is_roster and re.search(r"\bco[- ]?founder\b|\bfounded\b|\bfounder\b", biography, re.I):
                person.is_founder = True
                person.founder_scope = _founder_scope(company_name, biography, structured=False)
                person.founder_context = person.founder_context or biography
        for assertion in result.roles:
            if assertion.person_id == person.person_id and assertion.is_current and assertion.start_date is None:
                assertion.start_date = person.role_start_dates.get(assertion.role, person.current_since)
                assertion.date_precision = _date_precision(assertion.start_date)
        relationship_id = f"relationship:{_stable(person.person_id, filing.issuer_cik)}"
        person.issuer_relationship_ids = sorted(set(person.issuer_relationship_ids + [relationship_id]))
        result.relationships.append(PersonCompanyRelationship(
            person_company_id=relationship_id,
            person_id=person.person_id,
            issuer_cik=filing.issuer_cik,
            ticker=filing.ticker.upper(),
            company_name=company_name,
            current_roles=list(person.current_roles),
            role_categories=list(person.role_categories),
            relationship_type="management_and_board",
            status="current" if person.current_roles else "historical",
            start_date=person.current_since,
            date_precision=_date_precision(person.current_since),
            source_ids=[source.source_id],
            evidence_text=biography or "Structured proxy roster evidence.",
        ))
        result.education.extend(extract_education(person.person_id, filing.ticker.upper(), biography, source))
        result.people.append(person)
        result.career.extend(_career_entries(person, biography, source))
        # Explicit external board service is retained separately from the
        # issuer board.  The phrase must name an organization and is never
        # inferred from a generic use of the word "director".
        board_pattern = r"(?:serves?|served|sits?|sat|has served)\s+(?:as\s+(?:a\s+)?(?:member\s+of\s+)?(?:Chair(?:man|person)?\s+of\s+)?|on\s+)(?:the\s+)?board(?: of directors)?\s+of\s+([A-Z][A-Za-z0-9&.' -]{2,100})"
        for match in re.finditer(board_pattern, biography, re.I):
            board_company = _clean(match.group(1)).strip(" .,;")
            if not board_company or not board_company[0].isupper() or board_company.lower() in {"directors", "visitors", "several", "the"}:
                continue
            if board_company.lower() == company_name.lower():
                continue
            # SEC prose often places the date before the person clause
            # ("Since 2019, ... has served ...") or after a legal suffix
            # ("Inc., ... since 2022"). Keep the local clause so dates from a
            # neighbouring board assertion cannot bleed into this one.
            before = biography[max(0, match.start() - 260):match.start()]
            after = biography[match.end(): min(len(biography), match.end() + 240)]
            context = before + match.group(0) + after
            since_match = re.search(r"\bsince\s+(?:[A-Z][a-z]+\s+)?((?:19|20)\d{2})", before[-120:] + after, re.I)
            range_match = re.search(r"\bfrom\s+(?:[A-Z][a-z]+\s+)?((?:19|20)\d{2})\s+to\s+(?:[A-Z][a-z]+\s+)?((?:19|20)\d{2})", after, re.I)
            result.boards.append(BoardMembership(
                board_id=f"board:{_stable(person.person_id, board_company)}",
                person_id=person.person_id,
                ticker=filing.ticker.upper(),
                board_company=board_company,
                issuer_cik=filing.issuer_cik,
                person_company_id=f"relationship:{_stable(person.person_id, filing.issuer_cik)}",
                board_classification=resolve_organization(board_company).classification,
                board_ticker=resolve_organization(board_company).ticker,
                board_cik=resolve_organization(board_company).cik,
                board_role="director",
                start_date=since_match.group(1) if since_match else (range_match.group(1) if range_match else None),
                end_date=range_match.group(2) if range_match else None,
                date_precision=_date_precision(since_match.group(1) if since_match else (range_match.group(1) if range_match else None)),
                is_current=bool(since_match or re.search(r"\bcurrently\b", before[-80:] + after[:80], re.I) or re.match(r"serves\b", match.group(0), re.I)) and not bool(range_match),
                evidence_text=match.group(0),
                source_ids=[source.source_id],
            ))
    if not result.people:
        result.warnings.append(f"No structured management rows found in {filing.accession_number}")
    # Deduplicate rows produced by split proxy tables deterministically.
    result.roles = list({item.assertion_id: item for item in result.roles}.values())
    result.career = list({item.career_id: item for item in result.career}.values())
    result.boards = list({item.board_id: item for item in result.boards}.values())
    result.relationships = list({item.person_company_id: item for item in result.relationships}.values())
    result.education = list({item.education_id: item for item in result.education}.values())
    if source is not None:
        for item in [*result.people, *result.roles, *result.career, *result.boards, *result.relationships, *result.education]:
            item.source_urls = [source.url]
            item.source_local_paths = [source.local_path] if source.local_path else []
            item.source_accession_numbers = [source.accession_number] if source.accession_number else []
            item.source_form_types = [source.source_type]
            item.source_url = source.url
            item.local_source_path = source.local_path
            item.accession_number = source.accession_number
            item.form_type = source.source_type
    result.people.sort(key=lambda item: item.person_id)
    return result


__all__ = ["ManagementParseResult", "parse_proxy_people"]
