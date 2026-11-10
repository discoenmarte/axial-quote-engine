"""Deterministic mapping from the customer's words to catalog candidates.

This is the only bridge between free text and SKUs. It never invents: a mention
either matches catalog aliases or it does not. When the words are generic
("lona" could be Frontlit or Mesh) it returns *all* candidates and flags the
match as generic; the resolver then uses quote context (which of those SKUs is
actually on the quote?) and finally the catalog's declared family default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..catalog import CATALOG, CatalogItem
from ..nlp.types import Size

_WIDTH_TOLERANCE_M = 0.011
_LENGTH_TOLERANCE_M = 0.011


@dataclass(slots=True)
class CatalogMatch:
    #: Catalog items compatible with the words and sizes. Default-for-family first.
    candidates: list[CatalogItem]
    #: True when the words alone did not single out one item (e.g. "lona").
    generic: bool
    #: Alias words that were found in the text. Empty => the text names no product we sell.
    matched_terms: list[str] = field(default_factory=list)
    #: Sizes the customer stated that no candidate satisfies (e.g. a 1.50 m width).
    unmatched_sizes: list[Size] = field(default_factory=list)

    @property
    def names_a_product(self) -> bool:
        return bool(self.matched_terms)

    @property
    def families(self) -> set[str]:
        return {c.family for c in self.candidates}


def _has_word(text: str, alias: str) -> bool:
    return re.search(rf"\b{re.escape(alias)}\b", text) is not None


def size_compatible(item: CatalogItem, size: Size) -> bool:
    """Every dimension the customer stated must agree with the catalog item."""
    if size.width_m is not None:
        if item.width_m is None or abs(item.width_m - size.width_m) > _WIDTH_TOLERANCE_M:
            return False
    if size.length_m is not None:
        if item.length_m is None or abs(item.length_m - size.length_m) > _LENGTH_TOLERANCE_M:
            return False
    if size.thickness_mm is not None:
        if item.thickness_mm is None or abs(item.thickness_mm - size.thickness_mm) > 1e-9:
            return False
    return True


def match_catalog(normalized_text: str, sizes: list[Size] | None = None) -> CatalogMatch:
    sizes = sizes or []

    variant_hits: list[CatalogItem] = []
    alias_hits: list[CatalogItem] = []
    terms: list[str] = []
    for item in CATALOG:
        matched_variant = [a for a in item.variant_aliases if _has_word(normalized_text, a)]
        matched_alias = [a for a in item.aliases if _has_word(normalized_text, a)]
        if matched_variant:
            variant_hits.append(item)
        if matched_alias:
            alias_hits.append(item)
        terms.extend(matched_variant + matched_alias)

    if not alias_hits and not variant_hits:
        return CatalogMatch(candidates=[], generic=False)

    # A variant word ("mesh") is decisive; the generic family word is not.
    candidates = variant_hits if variant_hits else alias_hits
    generic = not variant_hits and len(candidates) > 1

    unmatched: list[Size] = []
    if sizes:
        narrowed = [c for c in candidates if any(size_compatible(c, s) for s in sizes)]
        unmatched = [s for s in sizes if not any(size_compatible(c, s) for c in candidates)]
        candidates = narrowed

    candidates.sort(key=lambda c: (not c.default_for_family, CATALOG.index(c)))
    return CatalogMatch(
        candidates=candidates,
        generic=generic and len(candidates) > 1,
        matched_terms=sorted(set(terms)),
        unmatched_sizes=unmatched,
    )
