"""Tier 1: deterministic interpretation of one WhatsApp message.

Cheap, explainable, and the only tier most messages ever need. When it is not
sure, it says so (``confidence="low"``) instead of guessing; the pipeline then
escalates to the LLM tier (if configured) and finally to a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..resolve.catalog_match import match_catalog
from . import lexicon
from .normalize import extract_quantities, extract_sizes, normalize_text, segment
from .types import (
    AvailabilityCheck,
    Cancel,
    Chatter,
    Inquiry,
    Interpretation,
    Operation,
    ProductMention,
    ProvideQty,
    ProvideSize,
    RequestedItem,
    RequestItems,
    ReviseQty,
)


@dataclass
class _Draft:
    operations: list[Operation] = field(default_factory=list)
    #: The request_items operation, created when the first item shows up so order is kept.
    request: RequestItems | None = None
    #: Product mentions that stood alone (no quantity, no cue) - they qualify a revision.
    bare_mentions: list[ProductMention] = field(default_factory=list)
    low: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)


def interpret_message(text: str) -> Interpretation:
    norm = normalize_text(text)
    draft = _Draft()

    # 1. Revisions first: "que sean 15, no 20" spans a comma, so take it before segmenting.
    revisions: list[tuple[int, int | None]] = []
    rest = norm
    for m in lexicon.REVISE.finditer(norm):
        revisions.append((int(m.group(1)), int(m.group(2)) if m.group(2) else None))
        rest = rest.replace(m.group(0), " ", 1)
    if revisions and revisions[0][1] is None:
        pm = lexicon.PREVIOUS_QTY.search(rest)
        if pm:
            revisions[0] = (revisions[0][0], int(pm.group(1)))
            rest = rest.replace(pm.group(0), " ", 1)
    if revisions:
        draft.trace.append(f"revision pattern(s): {revisions}")

    separate = lexicon.SEPARATE_CUE.search(norm) is not None
    has_request_cue = lexicon.REQUEST_CUE.search(rest) is not None

    # 2. Classify each clause independently.
    for seg in segment(rest):
        _classify_segment(seg, has_request_cue, draft)

    # 3. Bind revisions to the product they talk about.
    for new_qty, prev in revisions:
        mention: ProductMention | None = None
        if len(revisions) > 1:
            draft.low.append("more than one quantity revision in one message")
        elif len(draft.bare_mentions) == 1:
            mention = draft.bare_mentions[0]
        elif len(draft.bare_mentions) > 1:
            draft.low.append("one revision but several products mentioned; cannot tell which")
        draft.operations.append(ReviseQty(mention=mention, new_qty=new_qty, previous_qty=prev))
    if not revisions and draft.bare_mentions and draft.request is None:
        # A product named with no quantity and no verb: "lona 3.20" - what about it?
        draft.low.append("product named without a quantity or an intent")

    if draft.request is not None:
        draft.request.separate = separate
        if separate:
            draft.trace.append("'aparte' => separate quote")

    if not draft.operations:
        draft.operations.append(Chatter())
        draft.trace.append("no actionable content")

    return Interpretation(
        operations=draft.operations,
        confidence="low" if draft.low else "high",
        source="rules",
        rationale=draft.trace + [f"unsure: {reason}" for reason in draft.low],
    )


def _classify_segment(seg: str, has_request_cue: bool, draft: _Draft) -> None:
    sizes, seg_rest = extract_sizes(seg)
    qtys = extract_quantities(seg_rest)
    match = match_catalog(seg_rest, sizes)
    mention = ProductMention(text=seg, sizes=sizes) if match.names_a_product else None

    if mention and not match.candidates:
        draft.low.append(
            f"'{seg}' names a product we sell but no catalog item has that size"
            if match.unmatched_sizes
            else f"'{seg}' names a product but matches nothing in the catalog"
        )
        return
    if mention and len(match.families) > 1:
        draft.low.append(f"'{seg}' mixes two products in one clause")
        return

    if lexicon.CANCEL_CUE.search(seg_rest):
        draft.operations.append(Cancel(mention=mention))
        draft.trace.append(f"cancel: '{seg}'")
        return
    if lexicon.INQUIRY_CUE.search(seg_rest):
        draft.operations.append(Inquiry(mention=mention))
        draft.trace.append(f"inquiry: '{seg}'")
        return
    if mention and lexicon.ASK_QTY_CUE.search(seg_rest) and not qtys:
        draft.trace.append(f"question about quantity, no effect: '{seg}'")
        return
    if mention and lexicon.AVAILABILITY_CUE.search(seg_rest) and not qtys:
        draft.operations.append(AvailabilityCheck(mention=mention))
        draft.trace.append(f"availability question: '{seg}'")
        return
    if mention and (qtys or has_request_cue):
        if len(qtys) > 1:
            draft.low.append(f"'{seg}' has several numbers; cannot tell the quantity")
            return
        if draft.request is None:
            draft.request = RequestItems(items=[])
            draft.operations.append(draft.request)
        draft.request.items.append(RequestedItem(mention=mention, qty=qtys[0] if qtys else None))
        draft.trace.append(f"requested item: '{seg}' qty={qtys[0] if qtys else '?'}")
        return
    if mention:
        draft.bare_mentions.append(mention)
        draft.trace.append(f"product mentioned: '{seg}'")
        return
    if qtys and len(qtys) == 1 and not sizes:
        draft.operations.append(ProvideQty(qty=qtys[0]))
        draft.trace.append(f"bare quantity: {qtys[0]}")
        return
    if qtys:
        draft.low.append(f"'{seg}' has numbers but no product")
        return
    if sizes and len(sizes) == 1:
        draft.operations.append(ProvideSize(size=sizes[0]))
        draft.trace.append(f"bare size: {sizes[0]}")
        return
    if sizes:
        draft.low.append(f"'{seg}' has several sizes but no product")
