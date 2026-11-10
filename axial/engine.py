"""The pipeline: webhook payload in, correct stored state out.

    ingest_message(event)
      1. seen this message_id before?  -> return the stored result, change nothing
      2. interpret   (rules -> optional LLM)         what did they say?
      3. resolve     (deterministic)                 which SKUs, which quote, or "ask a human"?
      4. apply       (one DB transaction)            change quotes, write history, or open a review item
      5. remember the result under the message_id    (step 1 next time)
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import Direction, LineItem, Message, Quote
from .nlp.interpreter import Interpreter
from .nlp.types import Chatter, Interpretation
from .resolve.resolver import Context, Decision, NeedsReview, resolve, resolve_item_sku
from .store.sqlite import Store


class EngineError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class HistoryEntry(BaseModel):
    direction: Direction
    timestamp: str
    text: str
    message_id: str | None = None


class ConversationPayload(BaseModel):
    customer_id: str
    conversation_id: str
    customer_name: str | None = None
    conversation_history: list[HistoryEntry] = Field(min_length=1)


class ReviewDecision(BaseModel):
    action: Literal["assign", "dismiss"]
    quote_id: str | None = None
    note: str | None = None


class QuoteEngine:
    def __init__(self, store: Store, interpreter: Interpreter):
        self.store = store
        self.interpreter = interpreter
        # One message at a time. At <1 message/minute this is the simplest correct thing.
        self._lock = threading.Lock()

    # --- Phase 1: a whole conversation -> a draft quote ------------------------------------

    def ingest_conversation(self, payload: ConversationPayload) -> dict[str, Any]:
        digest = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()[:16]
        ingestion_id = f"conv:{payload.conversation_id}:{digest}"
        with self._lock:
            if previous := self.store.get_ingestion(ingestion_id):
                return {**previous, "duplicate": True}

            messages = [
                Message(message_id=e.message_id or f"{payload.conversation_id}#{i}", conversation_id=payload.conversation_id,
                        customer_id=payload.customer_id, direction=e.direction, timestamp=e.timestamp, text=e.text)
                for i, e in enumerate(payload.conversation_history, start=1)
            ]
            assembled = self.interpreter.interpret_conversation(messages)

            with self.store.transaction():
                self.store.upsert_conversation(payload.conversation_id, payload.customer_id, payload.customer_name)
                for m in messages:
                    self.store.save_message(m)

                items: list[LineItem] = []
                problems = list(assembled.unsure)
                for requested in assembled.items:
                    try:
                        if requested.qty is None:
                            raise NeedsReview(f"'{requested.mention.text}': no quantity given")
                        items.append(LineItem(sku=resolve_item_sku(requested), qty=requested.qty))
                    except NeedsReview as stop:
                        problems.append(stop.reason)

                last_inbound = next(m for m in reversed(messages) if m.direction == "inbound")
                quote = self._create_quote(payload.customer_id, items, last_inbound) if items else None
                review = None
                if problems:
                    review = self.store.create_review(last_inbound, "; ".join(problems),
                                                      [quote.quote_id] if quote else [], {"phase": 1})
                result = {
                    "ingestion_id": ingestion_id,
                    "outcome": "created" if quote else "review",
                    "quote": self._view(quote.quote_id) if quote else None,
                    "review_id": review.review_id if review else None,
                    "notes": assembled.notes + problems,
                    "duplicate": False,
                }
                self.store.save_ingestion(ingestion_id, result)
            return result

    # --- Phase 2: one message at a time ------------------------------------------------------

    def ingest_message(self, message: Message) -> dict[str, Any]:
        with self._lock:
            if previous := self.store.get_ingestion(message.message_id):
                return {**previous, "duplicate": True}

            recent = self.store.recent_messages(message.conversation_id)
            interp = self.interpreter.interpret_message(message, recent)

            with self.store.transaction():
                self.store.upsert_conversation(message.conversation_id, message.customer_id)
                self.store.save_message(message)
                if message.direction == "outbound":
                    # The rep is the human in the loop; their changes are made in the ERP, not parsed from chat.
                    decision = Decision(outcome="noop", reason="Outbound messages are stored as context only")
                    interp = Interpretation(operations=[Chatter()], confidence="high", source="rules",
                                            rationale=["outbound: context only"])
                else:
                    decision = resolve(interp, self._context(message))
                result = self._apply(message, interp, decision)
                self.store.save_ingestion(message.message_id, result)
            return result

    # --- a human decides what the machine would not ----------------------------------------

    def resolve_review(self, review_id: str, decision: ReviewDecision) -> dict[str, Any]:
        with self._lock, self.store.transaction():
            review = self.store.get_review(review_id)
            if review is None:
                raise EngineError(404, f"{review_id} not found")
            if review.status != "pending":
                raise EngineError(409, f"{review_id} is already {review.status}")

            if decision.action == "dismiss":
                self.store.close_review(review_id, "dismissed", {"note": decision.note})
                return {"review_id": review_id, "status": "dismissed"}

            if not decision.quote_id:
                raise EngineError(400, "action 'assign' needs a quote_id")
            raw = self.store.get_review_interpretation(review_id)
            if raw.get("phase") == 1:
                raise EngineError(409, "Phase 1 review items are informational; fix the draft in the ERP and dismiss")
            interp = Interpretation.model_validate(raw)
            if not interp.is_actionable():
                raise EngineError(409, "The message could not be read; nothing to apply. Handle it manually and dismiss")

            message = self.store.get_message(review.message_id)
            assert message is not None
            outcome = resolve(interp, self._context(message, forced_quote_id=decision.quote_id))
            if outcome.outcome == "review":
                raise EngineError(409, f"Still cannot apply: {outcome.reason}")
            result = self._apply(message, interp, outcome, review_id=review_id)
            self.store.close_review(review_id, "resolved", {"quote_id": decision.quote_id, "note": decision.note})
            self.store.update_ingestion(message.message_id, result)
            return result

    # --- reads -----------------------------------------------------------------------------------

    def quotes(self, customer_id: str | None = None) -> list[Quote]:
        return self.store.list_quotes(customer_id)

    def quote_detail(self, quote_id: str) -> dict[str, Any]:
        quote = self.store.get_quote(quote_id)
        if quote is None:
            raise EngineError(404, f"{quote_id} not found")
        return {**quote.model_dump(), "history": [e.model_dump() for e in self.store.get_events(quote_id)]}

    # --- internals ------------------------------------------------------------------------------

    def _context(self, message: Message, forced_quote_id: str | None = None) -> Context:
        return Context(customer_id=message.customer_id, conversation_id=message.conversation_id,
                       open_quotes=self.store.list_quotes(message.customer_id, status="draft"),
                       forced_quote_id=forced_quote_id)

    def _apply(self, message: Message, interp: Interpretation, decision: Decision,
               review_id: str | None = None) -> dict[str, Any]:
        touched: list[str] = []
        new_review = None

        if decision.outcome == "review":
            new_review = self.store.create_review(message, decision.reason, decision.candidate_quote_ids,
                                                  interp.model_dump())

        for change in decision.changes:
            mid = message.message_id
            if change.type == "create_quote":
                quote = self._create_quote(message.customer_id, change.items, message)
                touched.append(quote.quote_id)
                continue
            qid = change.quote_id
            touched.append(qid)
            if change.type == "add_items":
                self.store.add_line_items(qid, change.items)
                for item in change.items:
                    self.store.add_event(qid, mid, "line_added", {"sku": item.sku, "qty": item.qty})
            elif change.type == "set_qty":
                self.store.set_line_qty(qid, change.sku, change.to_qty)
                self.store.add_event(qid, mid, "qty_changed", {"sku": change.sku, "from": change.from_qty, "to": change.to_qty})
            elif change.type == "link_conversation":
                if self.store.link_conversation(qid, message.conversation_id, mid):
                    self.store.add_event(qid, mid, "conversation_linked", {"conversation_id": message.conversation_id})
            elif change.type == "cancel_quote":
                self.store.set_status(qid, "cancelled")
                self.store.add_event(qid, mid, "cancelled", {})
            elif change.type == "note_inquiry":
                self.store.add_event(qid, mid, "inquiry", {"text": message.text})

        if review_id:
            for qid in dict.fromkeys(touched):
                self.store.add_event(qid, message.message_id, "review_resolved", {"review_id": review_id})

        return {
            "message_id": message.message_id,
            "outcome": decision.outcome,
            "reason": decision.reason,
            "interpretation": interp.model_dump(),
            "changes": [c.model_dump() for c in decision.changes],
            "quotes": [self._view(q) for q in dict.fromkeys(touched)],
            "review_id": new_review.review_id if new_review else None,
            "candidate_quote_ids": decision.candidate_quote_ids,
            "duplicate": False,
        }

    def _create_quote(self, customer_id: str, items: list[LineItem], message: Message) -> Quote:
        quote = self.store.create_quote(customer_id, items)
        self.store.add_event(quote.quote_id, message.message_id, "created",
                             {"line_items": [i.model_dump() for i in items]})
        self.store.link_conversation(quote.quote_id, message.conversation_id, message.message_id)
        self.store.add_event(quote.quote_id, message.message_id, "conversation_linked",
                             {"conversation_id": message.conversation_id})
        return quote

    def _view(self, quote_id: str) -> dict[str, Any]:
        quote = self.store.get_quote(quote_id)
        assert quote is not None
        return quote.model_dump()
