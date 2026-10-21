"""Conservative generic entity resolution for people, issuers and metrics."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable


def normalize_name(value: str | None) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    # SEC owner names are commonly ``SURNAME, Given Middle``.  Degrees and
    # suffixes after a comma are metadata, not a surname reversal.
    if "," in raw:
        left, right = (part.strip() for part in raw.split(",", 1))
        right_tokens = re.findall(r"[a-z]+", right)
        if not right_tokens or not all(token in {"ph", "d", "phd", "md", "jd", "esq"} for token in right_tokens):
            raw = f"{right} {left}"
    text = re.sub(r"\b(mr|mrs|ms|dr|prof|ph|d|phd|md|jd|esq)\.?\b", " ", raw)
    text = re.sub(r"\b(jr|sr|ii|iii|iv)\.?\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    parts = text.split()
    # SEC commonly emits "SURNAME, Given".  Normalize it to the same order as
    # an issuer biography before comparing aliases.
    return " ".join(parts)


def name_tokens(value: str | None) -> tuple[str, ...]:
    return tuple(normalize_name(value).split())


def surname_first_key(value: str | None) -> tuple[str, ...]:
    # The function name is retained for compatibility; the returned key is a
    # canonical token order after ``normalize_name`` has handled SEC commas.
    return name_tokens(value)


@dataclass
class CanonicalPerson:
    person_id: str
    normalized_name: str
    aliases: list[str] = field(default_factory=list)
    issuer_ids: list[str] = field(default_factory=list)
    strong_identifiers: list[str] = field(default_factory=list)
    status: str = "resolved"


@dataclass(frozen=True)
class EntityMatch:
    person_id: str | None
    method: str
    confidence: str
    ambiguous: bool = False


class EntityResolver:
    """Resolve aliases while refusing unsafe same-name merges."""

    def __init__(self):
        self.people: dict[str, CanonicalPerson] = {}

    def add_person(self, person: CanonicalPerson) -> CanonicalPerson:
        self.people[person.person_id] = person
        return person

    def resolve_person(self, name: str, *, issuer_id: str | None = None, strong_identifier: str | None = None) -> EntityMatch:
        normalized = normalize_name(name)
        if strong_identifier:
            matches = [p for p in self.people.values() if strong_identifier in p.strong_identifiers]
            if len(matches) == 1:
                return EntityMatch(matches[0].person_id, "strong_identifier", "high")
        exact = []
        for person in self.people.values():
            names = {normalize_name(person.normalized_name), *(normalize_name(alias) for alias in person.aliases)}
            if normalized in names and (issuer_id is None or not person.issuer_ids or issuer_id in person.issuer_ids):
                exact.append(person)
        if len(exact) == 1:
            return EntityMatch(exact[0].person_id, "exact_alias", "high")
        if len(exact) > 1:
            return EntityMatch(None, "ambiguous_exact_alias", "low", True)
        # Surname-first spellings are safe only when the complete token set is
        # the same and issuer context agrees.  Similar names never merge.
        equivalent = [p for p in self.people.values() if surname_first_key(p.normalized_name) == surname_first_key(name) and (issuer_id is None or issuer_id in p.issuer_ids)]
        if len(equivalent) == 1:
            return EntityMatch(equivalent[0].person_id, "surname_first_alias", "high")
        if len(equivalent) > 1:
            return EntityMatch(None, "ambiguous_name", "low", True)
        return EntityMatch(None, "unresolved", "none")

    def merge_alias(self, person_id: str, alias: str) -> None:
        if person_id not in self.people:
            raise KeyError(person_id)
        if normalize_name(alias) != normalize_name(self.people[person_id].normalized_name):
            self.people[person_id].aliases = sorted(set(self.people[person_id].aliases + [alias]))

    def resolve_many(self, values: Iterable[tuple[str, str | None, str | None]]) -> list[EntityMatch]:
        return [self.resolve_person(name, issuer_id=issuer, strong_identifier=identifier) for name, issuer, identifier in values]


__all__ = ["normalize_name", "name_tokens", "surname_first_key", "CanonicalPerson", "EntityMatch", "EntityResolver"]
