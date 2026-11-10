"""The intermediate language between *understanding text* and *changing quotes*.

Both the deterministic rules and the LLM produce an :class:`Interpretation`.
Notice what is NOT in here: no SKU and no quote_id. An interpreter only says what
the customer *said* - a product in their own words, quantities, sizes, intent.
Mapping words to a catalog SKU and deciding which quote is affected is the job of
the deterministic resolver (:mod:`axial.resolve.resolver`).
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter


class Size(BaseModel):
    width_m: float | None = None
    length_m: float | None = None
    thickness_mm: float | None = None

    def is_complete_roll(self) -> bool:
        return self.width_m is not None and self.length_m is not None

    def __str__(self) -> str:
        parts: list[str] = []
        if self.width_m is not None and self.length_m is not None:
            parts.append(f"{self.width_m:g}x{self.length_m:g}")
        elif self.width_m is not None:
            parts.append(f"{self.width_m:g} m")
        elif self.length_m is not None:
            parts.append(f"x{self.length_m:g}")
        if self.thickness_mm is not None:
            parts.append(f"{self.thickness_mm:g}mm")
        return " ".join(parts) or "?"


class ProductMention(BaseModel):
    #: The customer's words, normalized. Never a SKU.
    text: str
    #: Sizes stated with the product ("3.20 x 100", "3mm"). May be several ("1.37 y 1.50").
    sizes: list[Size] = Field(default_factory=list)


class RequestedItem(BaseModel):
    mention: ProductMention
    #: None while the quantity has not been stated yet.
    qty: int | None = None


class RequestItems(BaseModel):
    """'cotizame 2 acrilicos' - asks for products. ``separate`` = 'aparte': a new quote."""

    kind: Literal["request_items"] = "request_items"
    items: list[RequestedItem]
    separate: bool = False


class ReviseQty(BaseModel):
    """'mejor que sean 15, no 20' - changes a quantity. ``previous_qty`` is a cross-check."""

    kind: Literal["revise_qty"] = "revise_qty"
    mention: ProductMention | None
    new_qty: int
    previous_qty: int | None = None


class Inquiry(BaseModel):
    """'que lleva la cotizacion de las lonas?' - asks about an existing quote."""

    kind: Literal["inquiry"] = "inquiry"
    mention: ProductMention | None


class Cancel(BaseModel):
    """'mejor cancelalo'."""

    kind: Literal["cancel"] = "cancel"
    mention: ProductMention | None


class AvailabilityCheck(BaseModel):
    """'si cuentas con microperforado de 1.37' - asks whether a product exists."""

    kind: Literal["availability_check"] = "availability_check"
    mention: ProductMention


class ProvideQty(BaseModel):
    """'Necesitamos 5 rollos' - a bare quantity answering an earlier question."""

    kind: Literal["provide_qty"] = "provide_qty"
    qty: int


class ProvideSize(BaseModel):
    """'De momento seria de 1.37x50' - a bare size refining an earlier mention."""

    kind: Literal["provide_size"] = "provide_size"
    size: Size


class Chatter(BaseModel):
    """Greetings, acknowledgements - nothing that affects a quote."""

    kind: Literal["chatter"] = "chatter"


Operation = Annotated[
    Union[RequestItems, ReviseQty, Inquiry, Cancel, AvailabilityCheck, ProvideQty, ProvideSize, Chatter],
    Field(discriminator="kind"),
]
OperationAdapter: TypeAdapter[Operation] = TypeAdapter(Operation)

Confidence = Literal["high", "low"]


class Interpretation(BaseModel):
    operations: list[Operation]
    #: Only "high" interpretations are ever acted upon. "low" goes to a human.
    confidence: Confidence
    source: Literal["rules", "llm"]
    #: Human-readable trace of how the interpretation was reached.
    rationale: list[str] = Field(default_factory=list)

    def is_actionable(self) -> bool:
        return self.confidence == "high"
