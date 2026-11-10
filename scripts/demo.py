"""Run the whole story from the brief against a fresh database and print what happens.

    uv run python scripts/demo.py
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from axial.engine import ConversationPayload, QuoteEngine, ReviewDecision  # noqa: E402
from axial.models import Message  # noqa: E402
from axial.nlp.interpreter import Interpreter  # noqa: E402
from axial.nlp.llm import reader_from_env  # noqa: E402
from axial.store.sqlite import Store  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "demo.db"


def load(name: str) -> dict:
    return json.loads((ROOT / "examples" / name).read_text())


def show_quotes(engine: QuoteEngine) -> None:
    for q in engine.quotes("C-10029"):
        lines = ", ".join(f"{li.sku} x{li.qty}" for li in q.line_items)
        print(f"    {q.quote_id} [{q.status}] chats={q.conversations}  {lines}")


def step(title: str) -> None:
    print(f"\n=== {title}")


def main() -> None:
    DB.unlink(missing_ok=True)
    engine = QuoteEngine(Store(str(DB)), Interpreter(llm=reader_from_env()))
    print(f"state lives in {DB}  (LLM tier: {'on' if engine.interpreter.llm else 'off - rules only'})")

    step("Phase 1: the conversation -> Q-001")
    result = engine.ingest_conversation(ConversationPayload(**load("phase1_conversation.json")))
    print(json.dumps({k: result["quote"][k] for k in ("document_type", "quote_id", "status", "conversations", "line_items")}, indent=2))

    for name, title in [
        ("event1_revise_lonas.json", "Event 1: Alice: 'de las lonas mejor que sean 15, no 20'"),
        ("event2_mario_asks.json", "Event 2: Mario (new chat): 'que lleva la cotizacion de las lonas?'"),
        ("event3_aparte_acrilicos.json", "Event 3: Alice: 'y aparte cotizame 2 acrilicos blancos de 3mm'"),
        ("event4_mario_revises_acrilicos.json", "Event 4: Mario: 'de los acrilicos que cotizo Alice, mejor que sean 4'"),
    ]:
        step(title)
        r = engine.ingest_message(Message(**load(name)))
        print(f"  outcome={r['outcome']}  changes={[c['type'] for c in r['changes']]}  because: {r['interpretation']['rationale']}")
        show_quotes(engine)

    step("Process restart: reopen the same file")
    engine.store.close()
    engine = QuoteEngine(Store(str(DB)), Interpreter())
    show_quotes(engine)

    step("Extra credit 1: event 4 delivered twice (same message_id)")
    r = engine.ingest_message(Message(**load("event4_mario_revises_acrilicos.json")))
    print(f"  duplicate={r['duplicate']}  -> state unchanged")
    show_quotes(engine)

    step("Extra credit 2: 'mejor cancelalo' with two open quotes")
    r = engine.ingest_message(Message(**load("event5_cancelalo.json")))
    print(f"  outcome={r['outcome']}  reason: {r['reason']}  candidates={r['candidate_quote_ids']}")
    show_quotes(engine)
    review = engine.store.list_reviews()[0]
    print(f"  waiting for a human in the review queue as {review.review_id}")

    step("A human decides: it was Q-002")
    engine.resolve_review(review.review_id, ReviewDecision(action="assign", quote_id="Q-002"))
    show_quotes(engine)

    step("History of Q-001 (what changed, which message caused it)")
    for ev in engine.quote_detail("Q-001")["history"]:
        print(f"    {ev['type']:<20} by {ev['message_id']:<20} {ev['detail']}")


if __name__ == "__main__":
    main()
