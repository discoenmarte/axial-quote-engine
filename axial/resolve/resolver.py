"""Tier 0 - the deterministic heart: turn an Interpretation into concrete quote changes.

Input:  what the customer said (an Interpretation), plus the customer's open quotes.
Output: a Decision - either a list of changes to apply, or "a human must look at this".

Rules of the house:
  * A SKU only ever comes from the catalog matcher. Never from text, never from a model.
  * Which quote a message targets is decided by its CONTENT (which products it names),
    never by the chat it arrived in. Event 4 in the brief is the reason.
  * When there is more than one plausible answer, we do not guess. We raise NeedsReview.
"""

from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, Field

from ..catalog import CatalogItem, Sku
from ..models import LineItem, Quote
from ..nlp.normalize import extract_sizes, normalize_text
from ..nlp.types import Interpretation, Operation, ProductMention, RequestedItem
from .catalog_match import CatalogMatch, match_catalog


# --- the context the resolver looks at -------------------------------------------------

class Context(BaseModel):
    customer_id: str
    conversation_id: str
    #: The customer's quotes that are still drafts, oldest first.
    open_quotes: list[Quote]
    #: Set when a human resolved a review item by picking a quote. Overrides all lookups.
    forced_quote_id: str | None = None

    def linked_to_this_conversation(self) -> list[Quote]:
        return [q for q in self.open_quotes if self.conversation_id in q.conversations]


# --- what the resolver can decide to do ------------------------------------------------

class CreateQuote(BaseModel):
    type: Literal["create_quote"] = "create_quote"
    items: list[LineItem]


class AddItems(BaseModel):
    type: Literal["add_items"] = "add_items"
    quote_id: str
    items: list[LineItem]


class SetQty(BaseModel):
    type: Literal["set_qty"] = "set_qty"
    quote_id: str
    sku: str
    from_qty: int
    to_qty: int


class LinkConversation(BaseModel):
    type: Literal["link_conversation"] = "link_conversation"
    quote_id: str


class CancelQuote(BaseModel):
    type: Literal["cancel_quote"] = "cancel_quote"
    quote_id: str


class NoteInquiry(BaseModel):
    type: Literal["note_inquiry"] = "note_inquiry"
    quote_id: str


Change = Union[CreateQuote, AddItems, SetQty, LinkConversation, CancelQuote, NoteInquiry]


class Decision(BaseModel):
    outcome: Literal["apply", "review", "noop"]
    changes: list[Change] = Field(default_factory=list)
    #: Why we stopped (review / noop), in words a sales rep can read.
    reason: str = ""
    #: Quotes the message might refer to - shown to the human reviewer.
    candidate_quote_ids: list[str] = Field(default_factory=list)
    explanation: list[str] = Field(default_factory=list)


class NeedsReview(Exception):
    """Raised anywhere the resolver would otherwise have to guess."""

    def __init__(self, reason: str, candidates: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        #: None = "could be any open quote"; [] = "none of them fits".
        self.candidates = candidates


# --- entry point -------------------------------------------------------------------------

def resolve(interp: Interpretation, ctx: Context) -> Decision:
    all_ids = [q.quote_id for q in ctx.open_quotes]
    if not interp.is_actionable():
        why = "; ".join(r.removeprefix("unsure: ") for r in interp.rationale if r.startswith("unsure"))
        return Decision(outcome="review", reason=f"Could not interpret the message confidently: {why}",
                        candidate_quote_ids=all_ids)

    changes: list[Change] = []
    notes: list[str] = []
    try:
        for op in interp.operations:
            changes.extend(_resolve_operation(op, ctx, notes))
    except NeedsReview as stop:
        return Decision(outcome="review", reason=stop.reason,
                        candidate_quote_ids=all_ids if stop.candidates is None else stop.candidates,
                        explanation=notes)

    if not changes:
        return Decision(outcome="noop", reason="; ".join(notes) or "Nothing in this message affects a quote",
                        explanation=notes)
    return Decision(outcome="apply", changes=changes, explanation=notes)


def _resolve_operation(op: Operation, ctx: Context, notes: list[str]) -> list[Change]:
    if op.kind == "chatter":
        return []

    if op.kind == "availability_check":
        notes.append(f"'{op.mention.text}': availability question; the rep answers in chat")
        return []

    if op.kind in ("provide_qty", "provide_size"):
        # A bare number only means something next to the question it answers. We only
        # reconstruct that inside Phase 1 (whole conversation), not for single messages.
        raise NeedsReview("A quantity or size arrived without a product we recognize")

    if op.kind == "request_items":
        items = [LineItem(sku=resolve_item_sku(it), qty=_require_qty(it)) for it in op.items]
        target = _quote_for_new_items(op.separate, ctx, notes)
        if target is None:
            return [CreateQuote(items=items)]
        return [AddItems(quote_id=target.quote_id, items=items), LinkConversation(quote_id=target.quote_id)]

    if op.kind == "revise_qty":
        quote, match = _find_quote(ctx, op.mention, op.previous_qty)
        line = _find_line(quote, match, op.previous_qty)
        notes.append(f"'{op.mention.text if op.mention else '(no product named)'}' -> {line.sku} on {quote.quote_id}")
        return [SetQty(quote_id=quote.quote_id, sku=line.sku, from_qty=line.qty, to_qty=op.new_qty),
                LinkConversation(quote_id=quote.quote_id)]

    if op.kind == "inquiry":
        quote, _ = _find_quote(ctx, op.mention)
        notes.append(f"inquiry about {quote.quote_id}")
        return [NoteInquiry(quote_id=quote.quote_id), LinkConversation(quote_id=quote.quote_id)]

    if op.kind == "cancel":
        quote, _ = _find_quote(ctx, op.mention)
        return [CancelQuote(quote_id=quote.quote_id), LinkConversation(quote_id=quote.quote_id)]

    raise NeedsReview(f"Unknown operation {op.kind!r}")  # unreachable; keeps mypy honest


# --- words -> SKU ----------------------------------------------------------------------

def mention_match(mention: ProductMention) -> CatalogMatch:
    """Run the catalog matcher on a mention (text + any sizes)."""
    sizes_in_text, rest = extract_sizes(normalize_text(mention.text))
    return match_catalog(rest, mention.sizes or sizes_in_text)


def resolve_item_sku(item: RequestedItem) -> Sku:
    """Exactly one catalog item, or NeedsReview. Generic words fall back to the family default."""
    match = mention_match(item.mention)
    if not match.candidates:
        if match.unmatched_sizes:
            sizes = ", ".join(str(s) for s in match.unmatched_sizes)
            raise NeedsReview(f"'{item.mention.text}': we sell that product but not in size {sizes}")
        raise NeedsReview(f"'{item.mention.text}': no catalog product matches these words")
    if len(match.candidates) == 1 or match.generic:
        return match.candidates[0].sku  # default-for-family is sorted first
    names = ", ".join(c.description for c in match.candidates)
    raise NeedsReview(f"'{item.mention.text}' could be any of: {names}")


def _require_qty(item: RequestedItem) -> int:
    if item.qty is None:
        raise NeedsReview(f"'{item.mention.text}': no quantity given")
    return item.qty


# --- which quote? ------------------------------------------------------------------------

def _quote_for_new_items(separate: bool, ctx: Context, notes: list[str]) -> Quote | None:
    """None => create a new quote. Otherwise the quote to add the items to."""
    if ctx.forced_quote_id:
        return _forced(ctx)
    if separate:
        notes.append("customer said 'aparte': new quote")
        return None
    linked = ctx.linked_to_this_conversation()
    if len(linked) > 1:
        raise NeedsReview("Add to which quote? This chat feeds several",
                          [q.quote_id for q in linked])
    if linked:
        notes.append(f"adding to the quote this chat is already working on: {linked[0].quote_id}")
    return linked[0] if linked else None


def _find_quote(ctx: Context, mention: ProductMention | None, previous_qty: int | None = None
                ) -> tuple[Quote, CatalogMatch | None]:
    """The ONE open quote this message talks about, found by content - or NeedsReview."""
    match = mention_match(mention) if mention else None
    if ctx.forced_quote_id:
        return _forced(ctx), match

    if match is not None and not match.candidates:
        raise NeedsReview(f"'{mention.text}' does not name a product we sell")  # type: ignore[union-attr]
    skus = {c.sku for c in match.candidates} if match else None

    hits = [q for q in ctx.open_quotes if skus is None or any(li.sku in skus for li in q.line_items)]
    if previous_qty is not None:
        exact = [q for q in hits if any((skus is None or li.sku in skus) and li.qty == previous_qty
                                        for li in q.line_items)]
        hits = exact or hits

    what = f"'{mention.text}'" if mention else "This message"
    if not hits:
        raise NeedsReview(f"No open quote for this customer contains {what}", [])
    if len(hits) > 1:
        raise NeedsReview(f"{what} could refer to {len(hits)} open quotes", [q.quote_id for q in hits])
    return hits[0], match


def _find_line(quote: Quote, match: CatalogMatch | None, previous_qty: int | None) -> LineItem:
    """The ONE line on the quote that the revision is about."""
    skus = {c.sku for c in match.candidates} if match else None
    lines = [li for li in quote.line_items if skus is None or li.sku in skus]
    if not lines:
        raise NeedsReview(f"{quote.quote_id} has no line for that product", [quote.quote_id])

    if previous_qty is not None:
        exact = [li for li in lines if li.qty == previous_qty]
        if not exact:
            have = ", ".join(f"{li.sku}={li.qty}" for li in lines)
            raise NeedsReview(f"Message says the current quantity is {previous_qty}, but the quote has {have}",
                              [quote.quote_id])
        lines = exact

    if len(lines) == 1:
        return lines[0]
    if match and match.generic:
        default: CatalogItem = match.candidates[0]
        for li in lines:
            if li.sku == default.sku:
                return li
    have = ", ".join(f"{li.sku}={li.qty}" for li in lines)
    raise NeedsReview(f"Which line on {quote.quote_id}? Several match: {have}", [quote.quote_id])


def _forced(ctx: Context) -> Quote:
    for q in ctx.open_quotes:
        if q.quote_id == ctx.forced_quote_id:
            return q
    raise NeedsReview(f"{ctx.forced_quote_id} is not an open quote for this customer")
