import json
import pathlib

import pytest

from axial.engine import ConversationPayload, QuoteEngine
from axial.models import Message
from axial.nlp.interpreter import Interpreter
from axial.store.sqlite import Store

EXAMPLES = pathlib.Path(__file__).parent.parent / "examples"


def load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


def event(message_id: str, conversation_id: str, text: str, direction: str = "inbound") -> Message:
    return Message(message_id=message_id, conversation_id=conversation_id, customer_id="C-10029",
                   direction=direction, timestamp="2026-02-11T10:00:00Z", text=text)


@pytest.fixture
def engine() -> QuoteEngine:
    """A fresh in-memory engine, rules only (no LLM)."""
    return QuoteEngine(Store(":memory:"), Interpreter(llm=None))


@pytest.fixture
def engine_with_q001(engine: QuoteEngine) -> QuoteEngine:
    """Engine after Phase 1: Q-001 exists with lona 20 / mesh 5 / micro 5."""
    engine.ingest_conversation(ConversationPayload(**load("phase1_conversation.json")))
    return engine
