"""Edge cases: when the engine acts, when it refuses to guess, and why."""

from axial.engine import QuoteEngine, ReviewDecision

from .conftest import event


def lines(engine: QuoteEngine, quote_id: str) -> dict[str, int]:
    return {li.sku: li.qty for li in engine.store.get_quote(quote_id).line_items}


def test_revision_without_product_and_one_open_quote_applies(engine_with_q001: QuoteEngine):
    r = engine_with_q001.ingest_message(event("m1", "wa-thread-411", "mejor que sean 15, no 20"))
    assert r["outcome"] == "apply" and lines(engine_with_q001, "Q-001")["ALFBTC-013-320100"] == 15


def test_revision_without_product_and_two_open_quotes_goes_to_review(engine_with_q001: QuoteEngine):
    engine_with_q001.ingest_message(event("m1", "wa-thread-411", "aparte cotizame 2 acrilicos"))
    r = engine_with_q001.ingest_message(event("m2", "wa-thread-411", "mejor que sean 3"))
    assert r["outcome"] == "review" and r["candidate_quote_ids"] == ["Q-001", "Q-002"]
    assert lines(engine_with_q001, "Q-001")["ALFBTC-013-320100"] == 20


def test_previous_qty_mismatch_is_not_applied(engine_with_q001: QuoteEngine):
    r = engine_with_q001.ingest_message(event("m1", "wa-thread-411", "de las lonas que sean 15, no 30"))
    assert r["outcome"] == "review" and "30" in r["reason"]
    assert lines(engine_with_q001, "Q-001")["ALFBTC-013-320100"] == 20


def test_generic_lona_prefers_the_catalog_default_frontlit(engine_with_q001: QuoteEngine):
    r = engine_with_q001.ingest_message(event("m1", "wa-thread-411", "de las lonas mejor que sean 12"))
    assert r["outcome"] == "apply"
    assert lines(engine_with_q001, "Q-001") == {"ALFBTC-013-320100": 12, "ALMCLC-0909-320100": 5, "AVMICS-1A-137050": 5}


def test_mesh_is_specific(engine_with_q001: QuoteEngine):
    engine_with_q001.ingest_message(event("m1", "wa-thread-411", "la mesh mejor que sean 7"))
    assert lines(engine_with_q001, "Q-001")["ALMCLC-0909-320100"] == 7


def test_new_items_without_aparte_join_the_quote_this_chat_feeds(engine_with_q001: QuoteEngine):
    r = engine_with_q001.ingest_message(event("m1", "wa-thread-411", "agrega 2 acrilicos de 3mm"))
    assert r["outcome"] == "apply" and r["quotes"][0]["quote_id"] == "Q-001"
    assert lines(engine_with_q001, "Q-001")["ARACBL-03-122244"] == 2


def test_new_items_from_a_chat_feeding_two_quotes_goes_to_review(engine_with_q001: QuoteEngine):
    engine_with_q001.ingest_message(event("m1", "wa-thread-411", "aparte cotizame 2 acrilicos"))
    r = engine_with_q001.ingest_message(event("m2", "wa-thread-411", "agrega 3 rollos de mesh"))
    assert r["outcome"] == "review" and r["candidate_quote_ids"] == ["Q-001", "Q-002"]


def test_new_items_from_an_unknown_chat_create_a_quote(engine_with_q001: QuoteEngine):
    r = engine_with_q001.ingest_message(event("m1", "wa-thread-999", "necesito 4 rollos de lona 3.20x100"))
    assert r["outcome"] == "apply" and r["quotes"][0]["quote_id"] == "Q-002"
    assert r["quotes"][0]["conversations"] == ["wa-thread-999"]


def test_unknown_product_never_becomes_a_sku(engine_with_q001: QuoteEngine):
    r = engine_with_q001.ingest_message(event("m1", "wa-thread-411", "aparte cotizame 10 rollos de papel kraft"))
    assert r["outcome"] == "review"  # a quantity for a product we do not recognize -> a human
    r = engine_with_q001.ingest_message(event("m2", "wa-thread-411", "aparte cotizame 3 microperforados de 1.50"))
    assert r["outcome"] == "review" and "size" in r["reason"]
    assert [q.quote_id for q in engine_with_q001.quotes()] == ["Q-001"]


def test_inquiry_about_product_not_on_any_quote_goes_to_review(engine_with_q001: QuoteEngine):
    r = engine_with_q001.ingest_message(event("m1", "wa-thread-662", "que lleva la cotizacion de los acrilicos?"))
    assert r["outcome"] == "review" and r["candidate_quote_ids"] == []


def test_outbound_messages_never_change_quotes(engine_with_q001: QuoteEngine):
    r = engine_with_q001.ingest_message(event("m1", "wa-thread-411", "listo, te dejo las lonas en 3", direction="outbound"))
    assert r["outcome"] == "noop" and lines(engine_with_q001, "Q-001")["ALFBTC-013-320100"] == 20


def test_bare_quantity_as_a_single_message_goes_to_review(engine_with_q001: QuoteEngine):
    r = engine_with_q001.ingest_message(event("m1", "wa-thread-411", "Necesitamos 5 rollos"))
    assert r["outcome"] == "review"


def test_cancelled_quotes_are_no_longer_targets(engine_with_q001: QuoteEngine):
    engine_with_q001.ingest_message(event("m1", "wa-thread-411", "cancela las lonas"))
    assert engine_with_q001.store.get_quote("Q-001").status == "cancelled"
    r = engine_with_q001.ingest_message(event("m2", "wa-thread-411", "de las lonas que sean 3"))
    assert r["outcome"] == "review" and r["candidate_quote_ids"] == []


def test_review_dismiss_and_bad_assign(engine_with_q001: QuoteEngine):
    engine_with_q001.ingest_message(event("m1", "wa-thread-411", "aparte cotizame 2 acrilicos"))
    r = engine_with_q001.ingest_message(event("m2", "wa-thread-411", "mejor cancelalo"))
    review_id = r["review_id"]
    try:
        engine_with_q001.resolve_review(review_id, ReviewDecision(action="assign", quote_id="Q-999"))
    except Exception as exc:  # noqa: BLE001
        assert "Q-999" in str(exc)
    out = engine_with_q001.resolve_review(review_id, ReviewDecision(action="dismiss", note="customer called"))
    assert out["status"] == "dismissed"
    assert all(q.status == "draft" for q in engine_with_q001.quotes())
