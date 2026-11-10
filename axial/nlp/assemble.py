"""Phase 1: fold the operations of a whole conversation into one list of requested items.

Details of one line item arrive scattered: the product in one message, the size
in another (often from the rep, answering availability), the quantity in a third.
This folds them in order. Only *inbound* messages can add items or quantities;
the rep's outbound messages can only refine sizes (answering "do you have 1.50?"
with "only 1.37x50").
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Message
from ..resolve.catalog_match import match_catalog
from .normalize import extract_sizes, normalize_text
from .types import Interpretation, ProductMention, RequestedItem, Size


@dataclass
class AssembledConversation:
    items: list[RequestedItem]
    separate: bool
    notes: list[str] = field(default_factory=list)
    unsure: list[str] = field(default_factory=list)


def _same_product(a: ProductMention, b: ProductMention) -> bool:
    ca = {c.sku for c in match_catalog(extract_sizes(normalize_text(a.text))[1]).candidates}
    cb = {c.sku for c in match_catalog(extract_sizes(normalize_text(b.text))[1]).candidates}
    return bool(ca & cb)


def assemble_conversation(steps: list[tuple[Message, Interpretation]]) -> AssembledConversation:
    out = AssembledConversation(items=[], separate=False)
    for message, interp in steps:
        inbound = message.direction == "inbound"
        for op in interp.operations:
            if op.kind == "request_items":
                if not inbound:
                    out.notes.append(f"{message.message_id}: rep listed products; ignored (reps edit quotes directly)")
                    continue
                out.items.extend(op.items)
                out.separate = out.separate or op.separate
            elif op.kind == "availability_check":
                if inbound:
                    out.items.append(RequestedItem(mention=op.mention, qty=None))
                    out.notes.append(f"{message.message_id}: '{op.mention.text}' asked about; pending quantity")
            elif op.kind == "provide_size":
                target = _last_item_needing_size(out.items)
                if target is None:
                    out.unsure.append(f"{message.message_id}: size {op.size} given but no item to attach it to")
                else:
                    target.mention.sizes = [_merge_size(target.mention.sizes, op.size)]
                    out.notes.append(f"{message.message_id}: size {op.size} applied to '{target.mention.text}'")
            elif op.kind == "provide_qty":
                if not inbound:
                    continue
                target = next((i for i in reversed(out.items) if i.qty is None), None)
                if target is None:
                    out.unsure.append(f"{message.message_id}: quantity {op.qty} given but nothing was waiting for one")
                else:
                    target.qty = op.qty
                    out.notes.append(f"{message.message_id}: qty {op.qty} applied to '{target.mention.text}'")
            elif op.kind == "revise_qty":
                if not inbound:
                    continue
                matches = [i for i in out.items if op.mention is None or _same_product(i.mention, op.mention)]
                if len(matches) == 1:
                    matches[0].qty = op.new_qty
                    out.notes.append(f"{message.message_id}: qty revised to {op.new_qty}")
                else:
                    out.unsure.append(f"{message.message_id}: revision to {op.new_qty} but {len(matches)} items match")
            elif op.kind in ("inquiry", "cancel"):
                out.unsure.append(f"{message.message_id}: '{op.kind}' inside an initial conversation; needs a human")
            # chatter: nothing
        if interp.confidence == "low":
            out.unsure.extend(f"{message.message_id}: {r}" for r in interp.rationale if r.startswith("unsure"))
    return out


def _last_item_needing_size(items: list[RequestedItem]) -> RequestedItem | None:
    for item in reversed(items):
        if not any(s.is_complete_roll() for s in item.mention.sizes):
            return item
    return items[-1] if items else None


def _merge_size(existing: list[Size], new: Size) -> Size:
    """A clarified size replaces the earlier guesses but keeps a thickness stated before."""
    thickness = next((s.thickness_mm for s in existing if s.thickness_mm is not None), None)
    return Size(width_m=new.width_m, length_m=new.length_m, thickness_mm=new.thickness_mm or thickness)
