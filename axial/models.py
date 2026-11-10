"""Domain records as they are stored and returned by the API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Direction = Literal["inbound", "outbound"]
QuoteStatus = Literal["draft", "cancelled"]
ReviewStatus = Literal["pending", "resolved", "dismissed"]
QuoteEventType = Literal[
    "created",
    "line_added",
    "qty_changed",
    "conversation_linked",
    "inquiry",
    "cancelled",
    "review_resolved",
]


class Message(BaseModel):
    message_id: str
    conversation_id: str
    customer_id: str
    direction: Direction
    timestamp: str
    text: str


class LineItem(BaseModel):
    sku: str
    qty: int = Field(gt=0)


class Quote(BaseModel):
    document_type: Literal["Oferta_de_Venta"] = "Oferta_de_Venta"
    quote_id: str
    customer_id: str
    status: QuoteStatus
    #: Every conversation that has fed or asked about this quote, in link order.
    conversations: list[str]
    line_items: list[LineItem]
    created_at: str
    updated_at: str


class QuoteEvent(BaseModel):
    event_id: int
    quote_id: str
    #: The message that caused the change. Null only for human actions without a message.
    message_id: str | None
    type: QuoteEventType
    detail: dict[str, Any]
    at: str


class ReviewItem(BaseModel):
    review_id: str
    message_id: str
    conversation_id: str
    customer_id: str
    text: str
    reason: str
    candidate_quote_ids: list[str]
    status: ReviewStatus
    created_at: str
    resolved_at: str | None = None
    resolution: dict[str, Any] | None = None
