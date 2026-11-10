"""HTTP surface. Thin: validate the body, call the engine, return JSON."""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Query

from ..engine import ConversationPayload, EngineError, QuoteEngine, ReviewDecision
from ..models import Message
from ..nlp.interpreter import Interpreter
from ..nlp.llm import reader_from_env
from ..store.sqlite import Store


def engine_from_env() -> QuoteEngine:
    db_path = os.environ.get("AXIAL_DB_PATH", "data/axial.db")
    return QuoteEngine(Store(db_path), Interpreter(llm=reader_from_env()))


def create_app(engine: QuoteEngine) -> FastAPI:
    app = FastAPI(title="Axial Quote & State Engine", version="0.1.0")

    @app.exception_handler(EngineError)
    async def _engine_error(_request, exc: EngineError):  # type: ignore[no-untyped-def]
        raise HTTPException(status_code=exc.status, detail=exc.detail)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "llm": "enabled" if engine.interpreter.llm else "disabled"}

    @app.post("/conversations", status_code=201)
    def ingest_conversation(payload: ConversationPayload) -> dict:
        """Phase 1: a whole WhatsApp conversation -> a draft quote."""
        return engine.ingest_conversation(payload)

    @app.post("/messages")
    def ingest_message(event: Message) -> dict:
        """Phase 2: one new message (a webhook event). Idempotent on message_id."""
        return engine.ingest_message(event)

    @app.get("/quotes")
    def list_quotes(customer: str | None = Query(None)) -> dict:
        return {"quotes": [q.model_dump() for q in engine.quotes(customer)]}

    @app.get("/quotes/{quote_id}")
    def get_quote(quote_id: str) -> dict:
        """One quote with its full history: what changed and which message caused it."""
        return engine.quote_detail(quote_id)

    @app.get("/review")
    def list_review(status: str = Query("pending")) -> dict:
        """Messages the system refused to guess about. A human decides here."""
        return {"items": [r.model_dump() for r in engine.store.list_reviews(status)]}

    @app.post("/review/{review_id}")
    def resolve_review(review_id: str, decision: ReviewDecision) -> dict:
        return engine.resolve_review(review_id, decision)

    return app
