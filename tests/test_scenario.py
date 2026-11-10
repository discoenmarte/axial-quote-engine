"""The exact story from the brief, through the HTTP API, with a process restart in the middle."""

import json
import pathlib

from fastapi.testclient import TestClient

from axial.api.app import create_app
from axial.engine import QuoteEngine
from axial.nlp.interpreter import Interpreter
from axial.store.sqlite import Store

from .conftest import load

EXPECTED_FINAL = {
    "quotes": [
        {"quote_id": "Q-001", "status": "draft",
         "conversations": ["wa-thread-411", "wa-thread-662"],
         "line_items": [{"sku": "ALFBTC-013-320100", "qty": 15},
                        {"sku": "ALMCLC-0909-320100", "qty": 5},
                        {"sku": "AVMICS-1A-137050", "qty": 5}]},
        {"quote_id": "Q-002", "status": "draft",
         "conversations": ["wa-thread-411", "wa-thread-662"],
         "line_items": [{"sku": "ARACBL-03-122244", "qty": 4}]},
    ]
}


def slim(quotes: list[dict]) -> list[dict]:
    keep = ("quote_id", "status", "conversations", "line_items")
    return [{k: q[k] for k in keep} for q in quotes]


def client_for(db_path: str) -> TestClient:
    return TestClient(create_app(QuoteEngine(Store(db_path), Interpreter())))


def test_full_story(tmp_path: pathlib.Path):
    db = str(tmp_path / "axial.db")
    api = client_for(db)

    # Phase 1
    r = api.post("/conversations", json=load("phase1_conversation.json"))
    assert r.status_code == 201, r.text
    quote = r.json()["quote"]
    assert quote["document_type"] == "Oferta_de_Venta"
    assert quote["quote_id"] == "Q-001" and quote["status"] == "draft"
    assert quote["conversations"] == ["wa-thread-411"]
    assert quote["line_items"] == [{"sku": "ALFBTC-013-320100", "qty": 20},
                                   {"sku": "ALMCLC-0909-320100", "qty": 5},
                                   {"sku": "AVMICS-1A-137050", "qty": 5}]

    # Event 1: 20 lonas -> 15, nothing else changes
    r = api.post("/messages", json=load("event1_revise_lonas.json")).json()
    assert r["outcome"] == "apply"
    q1 = api.get("/quotes/Q-001").json()
    assert q1["line_items"][0]["qty"] == 15 and q1["line_items"][1:] == quote["line_items"][1:]

    # Event 2: Mario's chat -> no line changes, chat linked to Q-001
    r = api.post("/messages", json=load("event2_mario_asks.json")).json()
    assert r["outcome"] == "apply" and {c["type"] for c in r["changes"]} == {"note_inquiry", "link_conversation"}
    q1 = api.get("/quotes/Q-001").json()
    assert q1["conversations"] == ["wa-thread-411", "wa-thread-662"]
    assert q1["line_items"][0]["qty"] == 15

    # --- process restart: a brand-new app on the same file must see the same state ---
    api = client_for(db)
    assert api.get("/quotes/Q-001").json()["conversations"] == ["wa-thread-411", "wa-thread-662"]

    # Event 3: "aparte" -> Q-002 from Alice's chat
    r = api.post("/messages", json=load("event3_aparte_acrilicos.json")).json()
    assert r["outcome"] == "apply" and r["quotes"][0]["quote_id"] == "Q-002"
    assert r["quotes"][0]["line_items"] == [{"sku": "ARACBL-03-122244", "qty": 2}]
    assert r["quotes"][0]["conversations"] == ["wa-thread-411"]

    # Event 4: Mario revises the acrilicos -> Q-002, found by content not by chat
    r = api.post("/messages", json=load("event4_mario_revises_acrilicos.json")).json()
    assert r["outcome"] == "apply" and r["quotes"][0]["quote_id"] == "Q-002"

    final = api.get("/quotes", params={"customer": "C-10029"}).json()
    assert slim(final["quotes"]) == EXPECTED_FINAL["quotes"]

    # Extra credit 1: byte-identical redelivery of event 4 changes nothing
    r = api.post("/messages", json=load("event4_mario_revises_acrilicos.json")).json()
    assert r["duplicate"] is True
    assert slim(api.get("/quotes", params={"customer": "C-10029"}).json()["quotes"]) == EXPECTED_FINAL["quotes"]

    # ...but the same text under a NEW message_id is a real message, processed again
    again = {**load("event4_mario_revises_acrilicos.json"), "message_id": "wamid.662.0003"}
    r = api.post("/messages", json=again).json()
    assert r["duplicate"] is False and r["outcome"] == "apply"

    # History: a human can see what changed and which message caused it
    history = api.get("/quotes/Q-001").json()["history"]
    assert [(h["type"], h["message_id"]) for h in history][:4] == [
        ("created", "wa-thread-411#4"), ("conversation_linked", "wa-thread-411#4"),
        ("qty_changed", "wamid.411.0231"), ("inquiry", "wamid.662.0001"),
    ]
    assert history[2]["detail"] == {"sku": "ALFBTC-013-320100", "from": 20, "to": 15}

    # Extra credit 2: "mejor cancelalo" with two open quotes -> review queue, nothing changes
    r = api.post("/messages", json=load("event5_cancelalo.json")).json()
    assert r["outcome"] == "review" and r["candidate_quote_ids"] == ["Q-001", "Q-002"]
    assert slim(api.get("/quotes", params={"customer": "C-10029"}).json()["quotes"]) == EXPECTED_FINAL["quotes"]
    pending = api.get("/review").json()["items"]
    assert [p["message_id"] for p in pending] == ["wamid.411.0233"]

    # A human decides: it was Q-002
    r = api.post(f"/review/{pending[0]['review_id']}", json={"action": "assign", "quote_id": "Q-002"})
    assert r.status_code == 200 and r.json()["outcome"] == "apply"
    assert api.get("/quotes/Q-002").json()["status"] == "cancelled"
    assert api.get("/quotes/Q-001").json()["status"] == "draft"
    assert api.get("/review").json()["items"] == []
    # resolving twice is refused
    assert api.post(f"/review/{pending[0]['review_id']}", json={"action": "assign", "quote_id": "Q-002"}).status_code == 409


def test_phase1_is_idempotent(tmp_path: pathlib.Path):
    api = client_for(str(tmp_path / "a.db"))
    first = api.post("/conversations", json=load("phase1_conversation.json")).json()
    second = api.post("/conversations", json=load("phase1_conversation.json")).json()
    assert second["duplicate"] is True and second["quote"]["quote_id"] == first["quote"]["quote_id"]
    assert len(api.get("/quotes").json()["quotes"]) == 1
