"""The tiering contract, with a fake model: rules first, model only when unsure, never a SKU."""

from axial.engine import QuoteEngine
from axial.models import Message
from axial.nlp.assemble import AssembledConversation
from axial.nlp.interpreter import Interpreter
from axial.nlp.llm import LLMOperation, LLMReading, to_interpretation
from axial.nlp.types import Interpretation, ProductMention, RequestedItem, RequestItems
from axial.store.sqlite import Store

from .conftest import event


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    def read_message(self, message: Message, recent: list[Message]) -> Interpretation:
        self.calls += 1
        # The model paraphrases a strange message into plain words. Still no SKU.
        item = RequestedItem(mention=ProductMention(text="acrilicos de 3mm"), qty=3)
        return Interpretation(operations=[RequestItems(items=[item], separate=True)], confidence="high",
                              source="llm", rationale=["llm: customer wants three acrylic sheets, separately"])

    def read_conversation(self, messages: list[Message]) -> AssembledConversation:
        self.calls += 1
        return AssembledConversation(items=[], separate=False)


def test_rules_handle_clear_messages_without_calling_the_model():
    fake = FakeLLM()
    engine = QuoteEngine(Store(), Interpreter(llm=fake))
    engine.ingest_message(event("m1", "wa-1", "cotizame 2 acrilicos de 3mm"))
    assert fake.calls == 0


def test_model_is_consulted_only_when_rules_are_unsure():
    fake = FakeLLM()
    engine = QuoteEngine(Store(), Interpreter(llm=fake))
    # "lonas" named with no verb and no quantity -> rules are unsure -> model reads it
    r = engine.ingest_message(event("m1", "wa-1", "lonas 3.20"))
    assert fake.calls == 1 and r["interpretation"]["source"] == "llm"
    assert r["outcome"] == "apply" and r["quotes"][0]["line_items"] == [{"sku": "ARACBL-03-122244", "qty": 3}]


def test_model_output_cannot_carry_a_sku():
    reading = LLMReading(confident=True, reasoning="x", operations=[
        LLMOperation(kind="request_items", product_words="ARACBL-03-122244", qty=1),
    ])
    interp = to_interpretation(reading)
    # The "SKU" is just words to us. It matches no alias, so the resolver will refuse it.
    from axial.resolve.resolver import mention_match
    assert mention_match(interp.operations[0].items[0].mention).candidates == []


def test_unconfident_model_means_human():
    reading = LLMReading(confident=False, reasoning="could be either", operations=[LLMOperation(kind="cancel")])
    assert to_interpretation(reading).confidence == "low"
