"""Deterministic fact matching without embeddings or model calls."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable

from myhermes_audit.contracts.ablation import (
    DistortionCandidate,
    FactMatchMode,
    FactProjection,
    RequiredFact,
)


def normalize_fact_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def fact_projection(value: str, *, include_value: bool = True) -> FactProjection:
    return FactProjection(
        sha256=hashlib.sha256(value.encode("utf-8")).hexdigest(),
        length=len(value),
        value=(value if include_value else None),
    )


def _matches(evidence: str, candidate: str, mode: FactMatchMode) -> bool:
    if mode is FactMatchMode.EXACT:
        return evidence == candidate
    if mode is FactMatchMode.NORMALIZED_EXACT:
        return normalize_fact_text(evidence) == normalize_fact_text(candidate)
    return normalize_fact_text(candidate) in normalize_fact_text(evidence)


def matches_fact_value(
    evidence: str,
    candidate: str,
    mode: FactMatchMode,
) -> bool:
    """Apply the public deterministic matching policy to one value."""

    return _matches(evidence, candidate, mode)


def match_required_fact(
    evidence: Iterable[str],
    fact: RequiredFact,
    *,
    include_value: bool = True,
) -> FactProjection | None:
    values = tuple(item for item in evidence if isinstance(item, str))
    for candidate in (fact.canonical_value, *fact.accepted_variants):
        if any(_matches(item, candidate, fact.match) for item in values):
            return fact_projection(candidate, include_value=include_value)
    return None


def match_distortion_candidate(
    evidence: Iterable[str],
    fact: RequiredFact,
    *,
    include_value: bool = True,
) -> tuple[DistortionCandidate, FactProjection] | None:
    values = tuple(item for item in evidence if isinstance(item, str))
    for candidate in fact.distortion_candidates:
        if any(_matches(item, candidate.value, fact.match) for item in values):
            return (
                candidate,
                fact_projection(candidate.value, include_value=include_value),
            )
    return None


__all__ = (
    "fact_projection",
    "match_distortion_candidate",
    "match_required_fact",
    "matches_fact_value",
    "normalize_fact_text",
)
