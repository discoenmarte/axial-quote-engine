"""Tier 2 (optional): ask Claude to read a message the rules could not.

What the model MAY decide: the intent of the message, the customer's words for a
product, quantities and sizes, whether "aparte" was meant.
What it may NEVER decide: a SKU (it is not even shown the codes) or a quote id.
Its answer goes through the same catalog matcher and resolver as the rules' output.

Only used when ANTHROPIC_API_KEY is set; otherwise unsure messages go to a human.
"""

from __future__ import annotations

import os
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from ..catalog import CATALOG
from ..models import Message
from .assemble import AssembledConversation
from .types import (
    AvailabilityCheck,
    Cancel,
    Chatter,
    Inquiry,
    Interpretation,
    Operation,
    ProductMention,
    RequestedItem,
    RequestItems,
    ReviseQty,
    Size,
)


class LLMReader(Protocol):
    def read_message(self, message: Message, recent: list[Message]) -> Interpretation: ...
    def read_conversation(self, messages: list[Message]) -> AssembledConversation: ...


# --- the shape the model must answer in (flat on purpose: easy to validate) -------------

class LLMOperation(BaseModel):
    kind: Literal["request_items", "revise_qty", "inquiry", "cancel", "availability_check", "chatter"]
    product_words: str | None = Field(
        None, description="The customer's own words for the product (e.g. 'lona mesh'). Never a code. Null if none."
    )
    width_m: float | None = None
    length_m: float | None = None
    thickness_mm: float | None = None
    qty: int | None = Field(None, description="Requested quantity, or the NEW quantity for revise_qty.")
    previous_qty: int | None = Field(None, description="For revise_qty: the quantity being replaced, if stated.")
    separate: bool = Field(False, description="True if the customer wants a separate/new quote ('aparte').")


class LLMReading(BaseModel):
    operations: list[LLMOperation]
    confident: bool = Field(description="False when the message is ambiguous and a human should decide.")
    reasoning: str


SYSTEM_PROMPT = (
    "You read WhatsApp messages between a Mexican industrial-materials distributor and its customers. "
    "Report ONLY what the message says, as structured operations. Do not infer products that are not named. "
    "Quote the customer's own words for products; never invent codes.\n"
    "Products we sell: " + "; ".join(item.description for item in CATALOG) + ".\n"
    "Set confident=false whenever a reasonable reader could disagree about what is meant."
)


def _mention(op: LLMOperation) -> ProductMention | None:
    if not op.product_words:
        return None
    size = Size(width_m=op.width_m, length_m=op.length_m, thickness_mm=op.thickness_mm)
    sizes = [size] if any(v is not None for v in (op.width_m, op.length_m, op.thickness_mm)) else []
    return ProductMention(text=op.product_words, sizes=sizes)


def to_interpretation(reading: LLMReading) -> Interpretation:
    """Translate the model's flat answer into the same Operation types the rules produce."""
    ops: list[Operation] = []
    items: list[RequestedItem] = []
    separate = False
    for op in reading.operations:
        m = _mention(op)
        if op.kind == "request_items":
            if m:
                items.append(RequestedItem(mention=m, qty=op.qty))
            separate = separate or op.separate
        elif op.kind == "revise_qty" and op.qty is not None:
            ops.append(ReviseQty(mention=m, new_qty=op.qty, previous_qty=op.previous_qty))
        elif op.kind == "inquiry":
            ops.append(Inquiry(mention=m))
        elif op.kind == "cancel":
            ops.append(Cancel(mention=m))
        elif op.kind == "availability_check" and m:
            ops.append(AvailabilityCheck(mention=m))
    if items:
        ops.append(RequestItems(items=items, separate=separate))
    if not ops:
        ops.append(Chatter())
    return Interpretation(
        operations=ops,
        confidence="high" if reading.confident else "low",
        source="llm",
        rationale=[f"llm: {reading.reasoning}"] + ([] if reading.confident else ["unsure: the model was not confident"]),
    )


class ClaudeReader:
    def __init__(self, model: str | None = None, client: object | None = None):
        import anthropic  # imported here so the rest of the system runs without the SDK configured

        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")

    def read_message(self, message: Message, recent: list[Message]) -> Interpretation:
        lines = [f"[{m.direction}] {m.text}" for m in recent if m.message_id != message.message_id]
        prompt = (
            "Earlier messages in this chat (context only):\n" + ("\n".join(lines) or "(none)") +
            f"\n\nInterpret ONLY this new message:\n[{message.direction}] {message.text}"
        )
        reading = self._ask(prompt)
        if reading is None:
            return Interpretation(operations=[Chatter()], confidence="low", source="llm",
                                  rationale=["unsure: model unavailable or refused"])
        return to_interpretation(reading)

    def read_conversation(self, messages: list[Message]) -> AssembledConversation:
        prompt = (
            "Read this whole conversation and list every product the CUSTOMER wants quoted, "
            "with the final quantity and size agreed in the chat (one request_items operation per product):\n"
            + "\n".join(f"[{m.direction}] {m.text}" for m in messages)
        )
        reading = self._ask(prompt)
        if reading is None:
            return AssembledConversation(items=[], separate=False, unsure=["model unavailable or refused"])
        interp = to_interpretation(reading)
        req = next((op for op in interp.operations if op.kind == "request_items"), None)
        unsure = [] if interp.is_actionable() else ["the model was not confident"]
        return AssembledConversation(items=req.items if req else [], separate=bool(req and req.separate),
                                     notes=interp.rationale, unsure=unsure)

    def _ask(self, prompt: str) -> LLMReading | None:
        try:
            response = self.client.messages.parse(  # type: ignore[attr-defined]
                model=self.model,
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_format=LLMReading,
            )
        except (self._anthropic.APIConnectionError, self._anthropic.APIStatusError):
            return None
        if response.stop_reason == "refusal":
            return None
        return response.parsed_output


def reader_from_env() -> ClaudeReader | None:
    return ClaudeReader() if os.environ.get("ANTHROPIC_API_KEY") else None
