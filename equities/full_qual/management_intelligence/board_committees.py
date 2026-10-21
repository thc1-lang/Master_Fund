"""Extract official committee membership and committee leadership statements."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable
from html.parser import HTMLParser

from pypdf import PdfReader

from .board_governance_models import BoardCommittee, GovernanceBoardMembership
from .governance_sources import CachedGovernanceSource
from .models import ManagementPerson


_COMMITTEE_PATTERNS = (
    ("audit", r"Audit Committee"),
    ("compensation", r"Compensation Committee"),
    ("nominating_governance", r"Nominating(?: and| &) Corporate Governance Committee|Compensation, Nominating & Governance Committee|Nominating & Governance Committee"),
    ("nominating_governance", r"Governance Committee"),
    ("executive", r"Executive Committee"),
    ("finance", r"Finance Committee"),
    ("risk", r"(?:Enterprise )?Risk Committee"),
    ("risk", r"Cybersecurity Risk Management Committee"),
    ("technology", r"Technology Committee"),
    ("science", r"Science Committee"),
    ("science", r"Research & Development Committee"),
)


class _CommitteeHTMLParser(HTMLParser):
    """Collect table cells, including image markers lost by text extraction."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[dict]]] = []
        self._table_depth = 0
        self._table: list[list[dict]] | None = None
        self._row: list[dict] | None = None
        self._cell: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == "table":
            if self._table_depth == 0:
                self._table = []
            self._table_depth += 1
        elif tag == "tr" and self._table_depth == 1:
            self._row = []
        elif tag in {"td", "th"} and self._table_depth == 1 and self._row is not None:
            self._cell = {
                "text": [], "has_img": False,
                "colspan": max(1, int(attributes.get("colspan") or 1) if str(attributes.get("colspan") or "1").isdigit() else 1),
                "rowspan": max(1, int(attributes.get("rowspan") or 1) if str(attributes.get("rowspan") or "1").isdigit() else 1),
                "tag": tag,
            }
        elif tag == "img" and self._cell is not None:
            self._cell["has_img"] = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._cell["text"] = " ".join(self._cell["text"]).strip()
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._table_depth == 1 and self._row is not None and self._table is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0 and self._table is not None:
                if self._table:
                    self.tables.append(self._table)
                self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None and data.strip():
            self._cell["text"].append(data.strip())


def _expanded_table(rows: list[list[dict]]) -> list[list[dict | None]]:
    """Expand rowspan/colspan while retaining each cell's marker metadata."""
    grid: list[list[dict | None]] = []
    for row_index, row in enumerate(rows):
        while len(grid) <= row_index:
            grid.append([])
        column = 0
        for cell in row:
            while column < len(grid[row_index]) and grid[row_index][column] is not None:
                column += 1
            colspan, rowspan = int(cell.get("colspan", 1)), int(cell.get("rowspan", 1))
            for offset_row in range(rowspan):
                target_row = row_index + offset_row
                while len(grid) <= target_row:
                    grid.append([])
                while len(grid[target_row]) < column + colspan:
                    grid[target_row].append(None)
                for offset_col in range(colspan):
                    grid[target_row][column + offset_col] = cell
            column += colspan
    return grid


def _committee_alias(label: str) -> tuple[str, str] | None:
    normalized = re.sub(r"\s+", " ", str(label or "")).strip().lower()
    if normalized in {"director", "directors", "specific purpose", "specific purpose 1", "specific purpose 2"}:
        return None
    if "audit" in normalized:
        return "audit", "Audit Committee"
    if "cmdc" in normalized or ("compensation" in normalized and "management" in normalized):
        return "compensation", "Compensation, Management Development and Compensation Committee"
    if "public responsibility" in normalized or re.search(r"\bprc\b", normalized):
        return "public_responsibility", "Public Responsibility Committee"
    if "risk" in normalized:
        return "risk", "Risk Committee"
    if "governance" in normalized or "nominating" in normalized:
        return "nominating_governance", "Governance Committee"
    if "compensation" in normalized:
        return "compensation", "Compensation Committee"
    return None


def _html_committee_matrix(html: str, people: list[ManagementPerson]) -> list[dict]:
    """Read explicit committee matrices from official HTML, including icons."""
    if not html:
        return []
    parser = _CommitteeHTMLParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    output: list[dict] = []
    for raw_rows in parser.tables:
        grid = _expanded_table(raw_rows)
        header_index = None
        headers: list[tuple[int, str, str, str]] = []
        for row_index, row in enumerate(grid[:12]):
            unique: list[dict] = []
            seen: set[int] = set()
            for cell in row:
                if cell is not None and id(cell) not in seen:
                    unique.append(cell); seen.add(id(cell))
            labels = []
            for col, cell in enumerate(row):
                if cell is None or any(id(item[0]) == id(cell) for item in labels):
                    continue
                labels.append((cell, col))
            label_values = [str(cell.get("text") or "") for cell, _ in labels]
            if not any(re.search(r"\bdirectors?\b", value, re.I) for value in label_values):
                continue
            aliases = [(col, *_committee_alias(str(cell.get("text") or ""))) for cell, col in labels if _committee_alias(str(cell.get("text") or ""))]
            if len(aliases) < 2:
                continue
            header_index = row_index
            director_col = next(col for cell, col in labels if re.search(r"\bdirectors?\b", str(cell.get("text") or ""), re.I))
            headers = [(director_col, "director", "", "")]
            headers.extend((col, alias[0], alias[1], str(cell.get("text") or "")) for cell, col in labels for alias in [_committee_alias(str(cell.get("text") or ""))] if alias)
            break
        if header_index is None:
            continue
        for row_index in range(header_index + 1, len(grid)):
            row = grid[row_index]
            if not row or len(row) <= headers[0][0]:
                continue
            director_cell = row[headers[0][0]]
            director_text = str((director_cell or {}).get("text") or "").strip()
            if not director_text or not re.search(r"[A-Za-z]{2,}", director_text):
                continue
            matched_people = _person_ids(director_text, people)
            if not matched_people:
                continue
            person_id = matched_people[0]
            for col, committee_type, committee_name, source_label in headers[1:]:
                cell = row[col] if col < len(row) else None
                if not cell:
                    continue
                cell_text = re.sub(r"\s+", " ", str(cell.get("text") or "")).strip()
                marker = bool(cell.get("has_img")) or bool(re.search(r"^(?:member|chair|c|m|x|✓|✔|yes)$", cell_text, re.I))
                if not marker:
                    continue
                output.append({
                    "person_id": person_id, "committee_type": committee_type,
                    "committee_name": committee_name, "chair": bool(re.search(r"\bchair\b|^c$", cell_text, re.I)),
                    "source_label": source_label, "row": director_text, "marker": "ICON" if cell.get("has_img") else cell_text,
                    "evidence": f'HTML committee matrix; row="{director_text}"; column="{source_label or committee_name}"; marker={"ICON" if cell.get("has_img") else cell_text}',
                })
    return output


def _person_ids(fragment: str, people: list[ManagementPerson]) -> list[str]:
    found: list[str] = []
    lowered = fragment.lower()
    for person in people:
        tokens = [token.lower() for token in re.findall(r"[a-zA-Z]{3,}", person.full_name)]
        if not tokens:
            continue
        # A last name plus either first name or a unique occurrence is enough
        # for the terse committee tables used by SEC proxies.
        last = tokens[-1]
        last_present = re.search(r"\b" + re.escape(last) + r"\b", lowered)
        first_present = re.search(r"\b" + re.escape(tokens[0]) + r"\b", lowered)
        if last_present and (len(tokens) == 1 or first_present or sum(1 for p in people if re.search(r"\b" + re.escape(tokens[-1]) + r"\b", p.full_name.lower())) == 1):
            found.append(person.person_id)
    return sorted(set(found))


def _chair_id(fragment: str, people: list[ManagementPerson]) -> str | None:
    match = re.search(r"chair(?:man|person)?(?: of [^.]*)?\s+(?:is|:|was)\s+(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?)?\s*([A-Z][A-Za-z-]+)", fragment, re.IGNORECASE)
    if not match:
        return None
    ids = _person_ids(match.group(1), people)
    return ids[0] if ids else None


def _committee_chair_id(name: str, text: str, people: list[ManagementPerson]) -> str | None:
    escaped = re.escape(name)
    match = re.search(rf"chair(?:man|person)?\s+of\s+(?:our\s+)?{escaped}\s+is\s+(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?)?\s*([A-Z][A-Za-z-]+)", text, re.IGNORECASE)
    if match:
        ids = _person_ids(match.group(1), people)
        if ids:
            return ids[0]
    explicit_reverse = re.search(rf"\b([A-Z][A-Za-z-]+)\s+is\s+the\s+chair(?:man|person)?\s+of\s+(?:our\s+)?{escaped}", text, re.IGNORECASE)
    if explicit_reverse:
        ids = _person_ids(explicit_reverse.group(1), people)
        if ids:
            return ids[0]
    reverse = re.search(rf"(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?)\s*([A-Z][A-Za-z-]+)[^.]{0,80}(?:is\s+the\s+)?chair(?:man|person)?\s+of\s+(?:our\s+)?{escaped}", text, re.IGNORECASE)
    if reverse:
        ids = _person_ids(reverse.group(1), people)
        if ids:
            return ids[0]
    tagged = re.search(r"([A-Z][A-Za-z.'-]{2,}(?:\s+[A-Z][A-Za-z.'-]{2,}){0,4})\s*\(Chair\)", text, re.IGNORECASE)
    if tagged:
        ids = _person_ids(tagged.group(1), people)
        if ids:
            return ids[0]
    return _chair_id(text, people) if name.lower() in text.lower() else None


def _type(name: str) -> str:
    lowered = name.lower()
    if "audit" in lowered:
        return "audit"
    if "cmdc" in lowered or "compensation, management" in lowered:
        return "compensation"
    if "compensation" in lowered and "nominating" not in lowered:
        return "compensation"
    if "public responsibility" in lowered or re.search(r"\bprc\b", lowered):
        return "public_responsibility"
    if "nominating" in lowered or "governance" in lowered:
        return "nominating_governance"
    for value, pattern in _COMMITTEE_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            return value
    return "other"


def extract_committees(
    ticker: str,
    proxy: CachedGovernanceSource | None,
    people: Iterable[ManagementPerson],
    memberships: Iterable[GovernanceBoardMembership],
) -> list[BoardCommittee]:
    if proxy is None:
        return []
    people_list = list(people)
    member_map = {item.person_id: item for item in memberships}
    text = proxy.text
    result: list[BoardCommittee] = []

    # When the authoritative normalized source was materialized from a PDF,
    # re-read only the committee page in pypdf's layout mode.  This preserves
    # the column positions that ordinary text extraction necessarily loses;
    # it is still the same cached official artifact and performs no network
    # access.
    layout_text = ""
    try:
        path = Path(proxy.local_path or "")
        if path.suffix.lower() == ".pdf" and path.exists():
            layout_text = "\n".join((page.extract_text(extraction_mode="layout") or "") for page in PdfReader(str(path)).pages)
    except Exception:
        layout_text = ""
    table_source = layout_text or text
    html_matrix_rows = _html_committee_matrix(proxy.html or "", people_list)
    # Director profiles in flattened SEC/PDF proxies retain a compact,
    # explicit ``Board Committees`` block even when the cross-board matrix
    # loses its cell markers.  Recover those assignments from bounded profile
    # lines; no committee membership is inferred from a committee name alone.
    profile_aliases = (
        (r"\bAudit\b", "audit", "Audit Committee"),
        (r"\b(?:CMDC|Compensation(?:\s+and\s+Management\s+Development)?)\b", "compensation", "Compensation and Management Development Committee"),
        (r"\b(?:NGC|Nominating\s+and\s+Governance)\b", "nominating_governance", "Nominating and Governance Committee"),
        (r"\b(?:SPFC|Strategic\s+Planning\s+and\s+Finance)\b", "finance", "Strategic Planning and Finance Committee"),
        (r"\b(?:TeCC|Technology\s+and\s+eCommerce)\b", "technology", "Technology and eCommerce Committee"),
        (r"\b(?:PRC|Public\s+Responsibility)\b", "public_responsibility", "Public Responsibility Committee"),
        (r"\bExecutive\s+Committee\b", "executive", "Executive Committee"),
    )
    profile_rows: list[dict[str, object]] = []
    profile_lines = text.splitlines()
    # A flattened PDF can place the preceding nominee's name in the same
    # page window as the next ``Board Committees`` marker.  Associating the
    # marker with the nearest preceding surname therefore cross-wires rows.
    # Anchor on the operative nominee section and parse each known nominee's
    # bounded profile instead.  This remains issuer-neutral and is safe for
    # layouts where the table of contents repeats the section heading.
    nominee_starts = [m.start() for m in re.finditer(r"\bDirector\s+Nominees\s+for\s+\d{4}\b", text, re.I)]
    operative_nominees = [
        value for value in nominee_starts
        if value > 30000 and re.search(
            r"Based\s+on\s+the\s+recommendation.*?nominated\s+the\s+following",
            text[value:value + 1800],
            re.I | re.DOTALL,
        )
    ]
    nominee_start = (operative_nominees[0] if operative_nominees else next((value for value in nominee_starts if value > 30000), (nominee_starts[-1] if nominee_starts else 0)))
    nominee_text = text[nominee_start:]
    for person in people_list:
        tokens = re.findall(r"[A-Za-z]{3,}", person.full_name)
        if len(tokens) < 2:
            continue
        first, last = tokens[0], tokens[-1]
        # Allow a middle initial in the source (e.g. Sarah J. Friar) while
        # requiring both first and last names to avoid surname-only matches.
        name_pattern = rf"\b{re.escape(first)}(?:\s+[A-Z]\.)?(?:\s+[A-Za-z'-]+)?\s+{re.escape(last)}\b"
        name_match = None
        marker_distance = None
        # A nominee can be mentioned in a committee overview or footnote
        # before the actual profile.  Choose the occurrence with the nearest
        # following Board Committees marker; the operative profile puts that
        # marker immediately after the role, age, and joined-board fields.
        for candidate in re.finditer(name_pattern, nominee_text, re.I):
            marker = re.search(r"\bBoard\s+Committees\s*:", nominee_text[candidate.end():candidate.end() + 1400], re.I)
            if marker and (marker_distance is None or marker.start() < marker_distance):
                name_match = candidate
                marker_distance = marker.start()
        if not name_match:
            continue
        window = nominee_text[name_match.end():name_match.end() + 2200]
        committee_marker = re.search(r"\bBoard\s+Committees\s*:", window, re.I)
        if not committee_marker:
            continue
        assignment = window[committee_marker.end():]
        # Some flattened proxy pages place the committee labels after the
        # ``Other Current Public Company Directorships`` heading.  Stop at
        # the next profile section rather than truncating at that heading.
        stop = re.search(r"\b(?:Skills|Further\s+Information|Age|Joined\s+the\s+Board|Proposal\s+No\.)\b", assignment, re.I)
        assignment_text = assignment[:stop.start()] if stop else assignment[:900]
        assignment_text = re.sub(r"\s+", " ", assignment_text).strip()
        for pattern, committee_type, committee_name in profile_aliases:
            marker = re.search(pattern, assignment_text, re.I)
            if not marker:
                continue
            profile_rows.append({
                "person_id": person.person_id, "committee_type": committee_type,
                "committee_name": committee_name,
                "chair": bool(re.search(r"\bchair\b", assignment_text[max(0, marker.start() - 40):marker.end() + 80], re.I)),
                "evidence": f"Director profile committee assignment: {assignment_text}",
            })
    for row in profile_rows:
        existing = next((item for item in result if item.committee_type == row["committee_type"]), None)
        if existing is None:
            existing = BoardCommittee(
                committee_id=f"governance-committee:{ticker.upper()}:{row['committee_type']}:{re.sub(r'[^a-z0-9]+','-',str(row['committee_name']).lower()).strip('-')}",
                ticker=ticker.upper(), committee_type=str(row["committee_type"]), committee_name=str(row["committee_name"]),
                source_url=proxy.source_url, source_accession=proxy.accession_number, source_form=proxy.form_type,
                source_date=proxy.filing_date, local_source_path=proxy.local_path, required_independence="UNKNOWN",
                evidence_text=str(row["evidence"]), source_status="AVAILABLE", membership_status="AVAILABLE",
            )
            result.append(existing)
        person_id = str(row["person_id"])
        if person_id not in existing.members:
            existing.members.append(person_id)
        existing.member_independence[person_id] = member_map.get(person_id).is_independent if person_id in member_map else "UNKNOWN"
        if bool(row.get("chair")) and existing.chair_person_id is None:
            existing.chair_person_id = person_id
        if str(row["evidence"]) not in existing.evidence_text:
            existing.evidence_text = (existing.evidence_text + "\n" + str(row["evidence"])).strip()
    table_match = re.search(
        r"The\s+following\s+table\s+provides\s+current\s+membership.*?"
        r"(?P<section>.*?)(?:Number\s+of\s+meetings|Executive\s+Compensation|Committee\s+Responsibilities)",
        table_source, re.IGNORECASE | re.DOTALL,
    )
    if table_match and layout_text:
        section = table_match.group("section")
        header_lines = [line for line in section.splitlines() if "Audit" in line and "Compensation" in line]
        if header_lines:
            header = header_lines[0]
            columns = [(header.find("Audit"), "Audit Committee"), (header.find("Compensation"), "Compensation Committee")]
            environmental = header.find("Environmental")
            governance = header.find("Governance")
            # The final two headings wrap onto adjacent lines in some PDFs.
            if environmental < 0:
                environmental = next((line.find("Environmental") for line in section.splitlines() if "Environmental" in line), -1)
            if governance < 0:
                governance = next((line.find("Governance") for line in section.splitlines() if "Governance" in line), -1)
            if governance < 0:
                governance = next((line.find("Nominating") for line in section.splitlines() if "Nominating" in line), -1)
            if environmental >= 0:
                columns.append((environmental, "Environmental, Social, and Public Policy Committee"))
            if governance >= 0:
                columns.append((governance, "Governance and Nominating Committee"))
            columns = sorted((pos, name) for pos, name in columns if pos >= 0)
            occurrences: list[tuple[int, str]] = []
            for person in people_list:
                match = re.search(re.escape(person.full_name) + r"\*?", section, re.IGNORECASE)
                if match:
                    occurrences.append((match.start(), person.full_name))
            occurrences.sort()
            for index, (start, full_name) in enumerate(occurrences):
                end = occurrences[index + 1][0] if index + 1 < len(occurrences) else len(section)
                fragment = section[start:end]
                ids_by_name = {person.full_name: person.person_id for person in people_list}
                person_id = ids_by_name.get(full_name)
                if not person_id:
                    continue
                committee_cells: dict[str, list[str]] = {}
                for line in fragment.splitlines():
                    for role_match in re.finditer(r"\b(?:Member|Chair|Financial\s+Expert)(?:,|\s+and\s+Member)?\b", line, re.IGNORECASE):
                        position = role_match.start()
                        # PDF layout extraction can shift a wrapped cell by a
                        # character or two.  Assign to the nearest header
                        # column rather than requiring the role to begin
                        # after the header's exact glyph position.
                        column = min(columns, key=lambda item: abs(item[0] - position), default=None)
                        if column is None:
                            continue
                        committee_cells.setdefault(column[1], []).append(role_match.group(0))
                for committee_name, roles_found in committee_cells.items():
                    result.append(BoardCommittee(
                        committee_id=f"governance-committee:{ticker.upper()}:{_type(committee_name)}:{re.sub(r'[^a-z0-9]+','-',committee_name.lower()).strip('-')}",
                        ticker=ticker.upper(), committee_type=_type(committee_name), committee_name=committee_name,
                        members=[person_id],
                        chair_person_id=person_id if any(re.match(r"chair", role, re.I) for role in roles_found) else None,
                        financial_expert_person_ids=[person_id] if any("financial expert" in role.lower() for role in roles_found) else [],
                        financial_background={person_id: "explicit audit committee financial expert"} if any("financial expert" in role.lower() for role in roles_found) else {},
                        required_independence="UNKNOWN", member_independence={person_id: member_map.get(person_id).is_independent if person_id in member_map else "UNKNOWN"},
                        source_url=proxy.source_url, source_accession=proxy.accession_number, source_form=proxy.form_type,
                        source_date=proxy.filing_date, local_source_path=proxy.local_path, evidence_text=fragment[:1800],
                    ))
    # Layout-preserving PDF extraction keeps the row and column coordinates
    # for many proxy committee matrices, but it often drops the glyph used for
    # an ordinary member.  Recover the explicit chair/member markers that do
    # survive (usually ``C`` for chair and a lettered specific-purpose marker)
    # and retain the other rows as unknown rather than inferring membership.
    # The header vocabulary is intentionally issuer-neutral; committee names
    # are mapped from the labels present in the source table.
    matrix_match = re.search(
        r"(?:Current\s+(?:board\s+)?committee\s+membership|committee\s+membership)"
        r"(?P<section>.*?)(?:All\s+directors\s+of\s+the\s+Firm|All\s+directors\s+of\s+the\s+|\n\s*Footnote)",
        table_source, re.IGNORECASE | re.DOTALL,
    )
    if matrix_match and layout_text:
        matrix = matrix_match.group("section")
        header = next((line for line in matrix.splitlines() if re.search(r"\bDirector\b", line) and re.search(r"\b(?:Audit|Governance|Risk|Compensation|Executive|Finance|Technology|PRC|CMDC)\b", line, re.I)), "")
        label_aliases = (
            ("Audit", "Audit Committee"),
            ("CMDC", "Compensation, Management Development and Compensation Committee"),
            ("Compensation", "Compensation Committee"),
            ("Governance", "Governance Committee"),
            ("Nominating", "Nominating and Governance Committee"),
            ("PRC", "Public Responsibility Committee"),
            ("Risk", "Risk Committee"),
            ("Executive", "Executive Committee"),
            ("Finance", "Finance Committee"),
            ("Technology", "Technology Committee"),
        )
        columns = [(match.start(), canonical) for alias, canonical in label_aliases if (match := re.search(rf"\b{re.escape(alias)}\b", header, re.I))]
        # A wrapped header such as "Specific / Purpose" has no useful
        # committee identity beyond the marker; retain it under one generic
        # source-backed committee name.
        specific = re.search(r"\bSpecific\b", header, re.I)
        if specific:
            columns.append((specific.start(), "Specific Purpose Committee"))
        columns.sort()
        for line in matrix.splitlines():
            person_matches = [person for person in people_list if re.search(rf"\b{re.escape(person.full_name)}\b", line, re.I)]
            if not person_matches or not columns:
                continue
            person = person_matches[0]
            for marker in re.finditer(r"\b(?:C|A|B)\b", line):
                column = min(columns, key=lambda item: abs(item[0] - marker.start()))
                committee_name = column[1]
                committee_type = _type(committee_name)
                person_id = person.person_id
                existing = next((item for item in result if item.committee_type == committee_type and item.committee_name == committee_name), None)
                if existing is None:
                    existing = BoardCommittee(
                        committee_id=f"governance-committee:{ticker.upper()}:{committee_type}:{re.sub(r'[^a-z0-9]+','-',committee_name.lower()).strip('-')}",
                        ticker=ticker.upper(), committee_type=committee_type, committee_name=committee_name,
                        source_url=proxy.source_url, source_accession=proxy.accession_number,
                        source_form=proxy.form_type, source_date=proxy.filing_date,
                        local_source_path=proxy.local_path,
                        required_independence="UNKNOWN", evidence_text=matrix[:5000],
                    )
                    result.append(existing)
                if person_id not in existing.members:
                    existing.members.append(person_id)
                if marker.group(0).upper() == "C" and existing.chair_person_id is None:
                    existing.chair_person_id = person_id
                existing.member_independence[person_id] = member_map.get(person_id).is_independent if person_id in member_map else "UNKNOWN"
    # SEC proxy HTML often uses a repeated image as the ordinary-member mark.
    # The image has no semantic alt text, but the matrix structure and legend
    # make the membership evidence explicit.  Keep the source row/column in
    # evidence and accept only marked cells; blank cells remain unknown.
    for row in html_matrix_rows:
        existing = next((item for item in result if item.committee_type == row["committee_type"]), None)
        if existing is None:
            existing = BoardCommittee(
                committee_id=f"governance-committee:{ticker.upper()}:{row['committee_type']}:{re.sub(r'[^a-z0-9]+','-',row['committee_name'].lower()).strip('-')}",
                ticker=ticker.upper(), committee_type=row["committee_type"], committee_name=row["committee_name"],
                source_url=proxy.source_url, source_accession=proxy.accession_number, source_form=proxy.form_type,
                source_date=proxy.filing_date, local_source_path=proxy.local_path, required_independence="UNKNOWN",
                evidence_text=row["evidence"], source_status="AVAILABLE", membership_status="AVAILABLE",
            )
            result.append(existing)
        if row["person_id"] not in existing.members:
            existing.members.append(row["person_id"])
        if row["chair"]:
            existing.chair_person_id = row["person_id"]
        existing.member_independence[row["person_id"]] = member_map.get(row["person_id"]).is_independent if row["person_id"] in member_map else "UNKNOWN"
        if row["evidence"] not in existing.evidence_text:
            existing.evidence_text = (existing.evidence_text + "\n" + row["evidence"]).strip()
    if html_matrix_rows:
        # When a source exposes a complete committee matrix, its marked rows
        # are the authoritative membership set.  Narrative regex matches can
        # mention historical or cross-referenced directors; they must not add
        # people beyond the explicit current matrix.
        matrix_members: dict[str, set[str]] = {}
        for row in html_matrix_rows:
            matrix_members.setdefault(row["committee_type"], set()).add(row["person_id"])
        for item in result:
            if item.committee_type not in matrix_members:
                continue
            allowed = matrix_members[item.committee_type]
            item.members = sorted(allowed)
            item.member_independence = {person_id: member_map.get(person_id).is_independent if person_id in member_map else "UNKNOWN" for person_id in item.members}
    # Explicit proxy language is stronger than a flattened table marker for
    # audit expertise.  Preserve the named members and their source evidence.
    for expert_match in re.finditer(
        r"each\s+member\s+of\s+the\s+(?P<name>[^()]{0,100}audit\s+committee[^()]*)\s*\((?P<members>[^)]{3,500})\)\s+to\s+be\s+an\s+audit\s+committee\s+financial\s+expert",
        text, re.IGNORECASE,
    ):
        ids = _person_ids(expert_match.group("members"), people_list)
        if not ids:
            continue
        committee = next((item for item in result if item.committee_type == "audit"), None)
        if committee is None:
            committee = BoardCommittee(
                committee_id=f"governance-committee:{ticker.upper()}:audit:audit-committee",
                ticker=ticker.upper(), committee_type="audit", committee_name="Audit Committee",
                source_url=proxy.source_url, source_accession=proxy.accession_number,
                source_form=proxy.form_type, source_date=proxy.filing_date,
                local_source_path=proxy.local_path, evidence_text=expert_match.group(0),
            )
            result.append(committee)
        committee.members = sorted(set(committee.members + ids))
        committee.financial_expert_person_ids = sorted(set(committee.financial_expert_person_ids + ids))
        committee.financial_background.update({person_id: "explicit audit committee financial expert" for person_id in ids})
        committee.member_independence.update({person_id: member_map.get(person_id).is_independent if person_id in member_map else "UNKNOWN" for person_id in ids})
    for expert_match in re.finditer(
        r"designated\s+each\s+of\s+(?P<members>.{3,500}?)\s+as\s+an?\s+[^A-Za-z]{0,3}audit\s+committee\s+financial\s+expert",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        ids = _person_ids(expert_match.group("members"), people_list)
        committee = next((item for item in result if item.committee_type == "audit"), None)
        if not ids or committee is None:
            continue
        committee.financial_expert_person_ids = sorted(set(committee.financial_expert_person_ids + ids))
        committee.financial_background.update({person_id: "explicit audit committee financial expert" for person_id in ids})
    for _, pattern in _COMMITTEE_PATTERNS:
        for match in re.finditer(rf"(?P<name>{pattern})[^.\n]{{0,360}}?(?:consists of|is composed of|members?\s+(?:are|include)|composed of)\s+(?P<members>.*?)(?=\.\s+(?:Our|The|During|Specific|Members|Each)\b|\.\s*$|$)", text, re.IGNORECASE):
            name = re.sub(r"\s+", " ", match.group("name")).strip()
            fragment = match.group(0)
            ids = _person_ids(match.group("members"), people_list)
            if not ids:
                continue
            member_independence = {person_id: member_map.get(person_id).is_independent if person_id in member_map else "UNKNOWN" for person_id in ids}
            section_for_roles = text[match.start():match.end() + 1200]
            independent_words = section_for_roles.lower()
            required = "YES" if "each member" in independent_words and "independent" in independent_words else "UNKNOWN"
            financial_ids: list[str] = []
            if _type(name) == "audit":
                expert_match = re.search(r"(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?)\s+([A-Z][A-Za-z-]+)\s+is an?\s+[^.]{0,80}audit committee financial expert", text, re.IGNORECASE)
                if expert_match:
                    financial_ids = _person_ids(expert_match.group(1), people_list)
                else:
                    expert_window = section_for_roles
                    if re.search(r"all audit committee financial experts", expert_window, re.IGNORECASE):
                        financial_ids = list(ids)
            result.append(BoardCommittee(
                committee_id=f"governance-committee:{ticker.upper()}:{_type(name)}:{re.sub(r'[^a-z0-9]+','-',name.lower()).strip('-')}",
                ticker=ticker.upper(),
                committee_type=_type(name),
                committee_name=name,
                members=ids,
                chair_person_id=_committee_chair_id(name, section_for_roles, people_list),
                financial_expert_person_ids=financial_ids,
                financial_background={person_id: "explicit audit committee financial expert" for person_id in financial_ids},
                required_independence=required,
                member_independence=member_independence,
                source_url=proxy.source_url,
                source_accession=proxy.accession_number,
                source_form=proxy.form_type,
                source_date=proxy.filing_date,
                local_source_path=proxy.local_path,
                evidence_text=fragment,
            ))
    # Some proxy layouts use the phrase "members are" rather than "consists
    # of".  Preserve a committee record with an unknown membership rather than
    # manufacturing names.
    if not result:
        for _, pattern in _COMMITTEE_PATTERNS:
            match = re.search(rf"(?P<name>{pattern})", text, re.IGNORECASE)
            if match:
                name = match.group("name")
                result.append(BoardCommittee(
                    committee_id=f"governance-committee:{ticker.upper()}:{_type(name)}:{re.sub(r'[^a-z0-9]+','-',name.lower()).strip('-')}",
                    ticker=ticker.upper(), committee_type=_type(name), committee_name=name,
                    source_url=proxy.source_url, source_accession=proxy.accession_number,
                    source_form=proxy.form_type, source_date=proxy.filing_date,
                    local_source_path=proxy.local_path, evidence_text=match.group(0),
                ))
    # Issuers use compact "Current Members:" tables or phrases such as
    # "current members ... are". Search every occurrence so a table of
    # contents heading cannot hide the operative section.
    for member_match in re.finditer(r"(?:Current Members?\s*:\s*|current members? of [^.]{0,120}\s+(?:are|:|include)\s+)", text, re.IGNORECASE):
        prefix = text[max(0, member_match.start() - 260):member_match.start()]
        candidates = []
        for _, pattern in _COMMITTEE_PATTERNS:
            candidates.extend(re.finditer(rf"(?P<name>{pattern})", prefix, re.IGNORECASE))
        if not candidates:
            continue
        name = re.sub(r"\s+", " ", max(candidates, key=lambda item: (item.end(), item.end() - item.start())).group("name")).strip()
        committee_type = _type(name)
        body = text[member_match.end():member_match.end() + 700]
        body = re.split(r"\.\s+(?=(?:Mr|Ms|Mrs|Dr|Our|The|This)\b)", body, maxsplit=1)[0]
        ids = _person_ids(body, people_list)
        if not ids:
            continue
        financial_ids = ids if committee_type == "audit" and re.search(r"all audit committee financial experts", body, re.IGNORECASE) else []
        existing = next((item for item in result if item.committee_type == committee_type), None)
        if existing is not None:
            existing.members = sorted(set(existing.members + ids))
            existing.member_independence.update({person_id: member_map.get(person_id).is_independent if person_id in member_map else "UNKNOWN" for person_id in ids})
            existing.financial_expert_person_ids = sorted(set(existing.financial_expert_person_ids + financial_ids))
            existing.financial_background.update({person_id: "explicit audit committee financial expert" for person_id in financial_ids})
            if body not in existing.evidence_text:
                existing.evidence_text = (existing.evidence_text + "\n" + body).strip()
            continue
        result.append(BoardCommittee(
            committee_id=f"governance-committee:{ticker.upper()}:{committee_type}:{re.sub(r'[^a-z0-9]+','-',name.lower()).strip('-')}", ticker=ticker.upper(), committee_type=committee_type, committee_name=name,
            members=ids, chair_person_id=_committee_chair_id(name, body + text[member_match.end():member_match.end() + 800], people_list),
            financial_expert_person_ids=financial_ids, financial_background={person_id: "explicit audit committee financial expert" for person_id in financial_ids},
            required_independence="YES" if "all independent" in body.lower() else "UNKNOWN", source_url=proxy.source_url,
            source_accession=proxy.accession_number, source_form=proxy.form_type, source_date=proxy.filing_date, local_source_path=proxy.local_path, evidence_text=body,
        ))
    # SEC/IR PDFs commonly flatten the committee overview tables into the
    # sequence "Chair <name> Other Members <names> Independent: 100%".  The
    # formal committee heading follows that sequence, so use the bounded
    # table text to recover current membership and chair evidence without
    # guessing from unrelated narrative mentions.
    table_pattern = re.compile(
        r"Board\s+and\s+Governance\s+Matters\s+Chair\s+"
        r"(?P<chair>[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){0,3})\s+"
        r"Other\s+Members\s+(?P<members>.*?)\s+Independent:\s*(?P<ind>\d+)%",
        re.IGNORECASE,
    )
    committee_headings = (
        "Audit Committee",
        "Compensation and Leadership Development Committee",
        "Compliance, Privacy and Quality Committee",
        "Nominating and Governance Committee",
    )
    for match in table_pattern.finditer(text):
        heading_match = re.search(
            r"\b(" + "|".join(re.escape(item) for item in committee_headings) + r")\b",
            text[match.end():match.end() + 600],
            re.IGNORECASE,
        )
        if not heading_match:
            continue
        name = re.sub(r"\s+", " ", heading_match.group(1)).strip()
        fragment = match.group(0)
        members_text = f"{match.group('chair')} {match.group('members')}"
        ids = _person_ids(members_text, people_list)
        if not ids:
            continue
        member_independence = {person_id: member_map.get(person_id).is_independent if person_id in member_map else "UNKNOWN" for person_id in ids}
        section_after_heading = text[match.end() + heading_match.end(): match.end() + heading_match.end() + 900]
        financial_ids = ids if _type(name) == "audit" and re.search(r"identified\s+all\s+members.*?financial\s+experts", section_after_heading, re.IGNORECASE | re.DOTALL) else []
        result.append(BoardCommittee(
            committee_id=f"governance-committee:{ticker.upper()}:{_type(name)}:{re.sub(r'[^a-z0-9]+','-',name.lower()).strip('-')}",
            ticker=ticker.upper(), committee_type=_type(name), committee_name=name,
            members=ids, chair_person_id=(_person_ids(match.group("chair"), people_list) or [None])[0],
            financial_expert_person_ids=financial_ids,
            financial_background={person_id: "explicit audit committee financial expert" for person_id in financial_ids},
            required_independence="YES" if match.group("ind") == "100" else "UNKNOWN",
            member_independence=member_independence, source_url=proxy.source_url,
            source_accession=proxy.accession_number, source_form=proxy.form_type,
            source_date=proxy.filing_date, local_source_path=proxy.local_path,
            evidence_text=fragment,
        ))
    # Prefer an explicit membership record over a table-of-contents placeholder
    # for the same committee type.  Keep one empty record only when the source
    # names a committee but does not expose its membership.
    by_type: dict[str, list[BoardCommittee]] = {}
    for item in result:
        by_type.setdefault(item.committee_type, []).append(item)
    explicit_members_seen = bool(re.search(r"(?:Current Members?\s*:|current members? of [^.]{0,120}\s+(?:are|:|include)\s+)", text, re.IGNORECASE))
    chosen: list[BoardCommittee] = []
    for items in by_type.values():
        populated = [item for item in items if item.members]
        if populated:
            # Nested regex matches such as "Governance Committee" inside
            # "Nominating and Corporate Governance Committee" describe one
            # committee; merge the explicitly extracted member rows into one
            # committee record instead of retaining only the longest row.
            merged = max(populated, key=lambda item: len(item.committee_name))
            for item in populated:
                if item is merged:
                    continue
                merged.members = sorted(set(merged.members + item.members))
                merged.financial_expert_person_ids = sorted(set(merged.financial_expert_person_ids + item.financial_expert_person_ids))
                merged.financial_background.update(item.financial_background)
                merged.member_independence.update(item.member_independence)
                if merged.chair_person_id is None:
                    merged.chair_person_id = item.chair_person_id
                if item.evidence_text and item.evidence_text not in merged.evidence_text:
                    merged.evidence_text += "\n" + item.evidence_text
            chosen.append(merged)
        elif not explicit_members_seen and not bool(table_pattern.search(text)):
            chosen.extend(items[:1])
    if any("compensation" in item.committee_name.lower() and "nominating" in item.committee_name.lower() for item in chosen):
        chosen = [item for item in chosen if item.members or "compensation committee" not in item.committee_name.lower()]

    # A proxy may explicitly enumerate the principal standing committees even
    # when its flattened membership matrix preserves only a few marker cells.
    # Keep source-backed shells for the named committees whose memberships are
    # not recoverable.  This lets coverage report PARTIAL instead of silently
    # turning a five-committee board into a two-committee result.
    declaration = re.search(
        r"(?:principal|standing)\s+committees\s*:\s*(?P<names>[^.]{20,900})\.",
        text, re.IGNORECASE,
    )
    if declaration:
        names_text = declaration.group("names")
        declared: list[str] = []
        for alias, canonical in (
            (r"\bAudit\s+Committee\b", "Audit Committee"),
            (r"\bCMDC\b", "CMDC"),
            (r"\bCompensation\s+Committee\b", "Compensation Committee"),
            (r"\bGovernance\s+Committee\b", "Governance Committee"),
            (r"\bPublic\s+Responsibility\s+Committee\b", "Public Responsibility Committee"),
            (r"\bPRC\b", "Public Responsibility Committee"),
            (r"\bRisk\s+Committee\b", "Risk Committee"),
            (r"\bExecutive\s+Committee\b", "Executive Committee"),
        ):
            if re.search(alias, names_text, re.IGNORECASE) and canonical not in declared:
                declared.append(canonical)
        for committee_name in declared:
            committee_type = _type(committee_name)
            if any(item.committee_type == committee_type for item in chosen):
                continue
            chosen.append(BoardCommittee(
                committee_id=f"governance-committee:{ticker.upper()}:{committee_type}:{re.sub(r'[^a-z0-9]+','-',committee_name.lower()).strip('-')}",
                ticker=ticker.upper(), committee_type=committee_type, committee_name=committee_name,
                source_url=proxy.source_url, source_accession=proxy.accession_number,
                source_form=proxy.form_type, source_date=proxy.filing_date,
                local_source_path=proxy.local_path, evidence_text=declaration.group(0),
            ))
    for item in chosen:
        if item.committee_type != "audit" or not item.members:
            continue
        direct = re.search(r"(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?)\s+([A-Z][A-Za-z-]+)\s+is an?\s+[^.]{0,80}audit committee financial expert", text, re.IGNORECASE)
        if direct:
            ids = [person_id for person_id in _person_ids(direct.group(1), people_list) if person_id in item.members]
            if ids:
                item.financial_expert_person_ids = ids
                item.financial_background = {person_id: "explicit audit committee financial expert" for person_id in ids}
        elif re.search(r"all audit committee financial experts", item.evidence_text, re.IGNORECASE):
            item.financial_expert_person_ids = list(item.members)
            item.financial_background = {person_id: "explicit audit committee financial expert" for person_id in item.members}

    # Flattened SEC/PDF text commonly preserves the committee membership
    # table's headings and row evidence but drops the cell grid.  Preserve
    # those explicitly named committees as source-backed records with unknown
    # membership rather than returning an empty list or inventing assignments.
    table_match = re.search(
            r"(?:table|summary)\s+(?:below|of)\s+provides\s+current\s+membership.*?"
            r"(?P<section>.*?)(?:Number\s+of\s+meetings|Executive\s+Compensation|Committee\s+Responsibilities)",
            text, re.IGNORECASE | re.DOTALL,
        )
    if table_match and not any(item.members for item in chosen):
        chosen = []
    if not chosen:
        table_text = table_match.group("section") if table_match else text
        flat_names = (
            ("Audit Committee", r"\bAudit(?:\s+Committee)?\b"),
            ("Compensation Committee", r"\bCompensation(?:\s+Committee)?\b"),
            ("Environmental, Social, and Public Policy Committee", r"Environmental[\s,]+Social[\s,]+(?:and\s+)?Public\s+Policy|Environmental,\s*Social"),
            ("Governance and Nominating Committee", r"Governance\s+and\s+(?:Nominating|Corporate\s+Governance)|Nominating\s+and\s+Corporate\s+Governance"),
        )
        for name, pattern in flat_names:
            heading = re.search(pattern, table_text, re.IGNORECASE)
            if not heading:
                continue
            chosen.append(BoardCommittee(
                committee_id=f"governance-committee:{ticker.upper()}:{_type(name)}:{re.sub(r'[^a-z0-9]+','-',name.lower()).strip('-')}",
                ticker=ticker.upper(), committee_type=_type(name), committee_name=name,
                source_url=proxy.source_url, source_accession=proxy.accession_number,
                source_form=proxy.form_type, source_date=proxy.filing_date,
                local_source_path=proxy.local_path,
                evidence_text=table_text[max(0, heading.start() - 500):min(len(table_text), heading.end() + 1200)],
            ))
    # An explicitly visible matrix with no accepted rows is a parser failure;
    # a named committee without a membership disclosure is a source limitation.
    matrix_visible = bool(html_matrix_rows) or bool(re.search(r"current\s+(?:board\s+)?committee\s+membership", table_source, re.I))
    for item in chosen:
        if item.members:
            item.source_status = "AVAILABLE"
            item.membership_status = "AVAILABLE"
        elif matrix_visible:
            item.source_status = "EXTRACTION_FAILED"
            item.membership_status = "EXTRACTION_FAILED"
        else:
            item.source_status = "SOURCE_COVERAGE_INCOMPLETE"
            item.membership_status = "NOT_DISCLOSED"
    return sorted({item.committee_id: item for item in chosen}.values(), key=lambda item: item.committee_id)


__all__ = ["extract_committees"]
