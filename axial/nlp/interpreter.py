"""The tiering policy: rules first, model only when the rules are unsure, human last.

This is how the LLM is kept from burning tokens on "mejor que sean 15": the rules
read it in microseconds with full confidence, so the model is never called.
"""

from __future__ import annotations

from ..models import Message
from .assemble import AssembledConversation, assemble_conversation
from .llm import LLMReader
from .rules import interpret_message
from .types import Interpretation


class Interpreter:
    def __init__(self, llm: LLMReader | None = None):
        self.llm = llm

    def interpret_message(self, message: Message, recent: list[Message]) -> Interpretation:
        reading = interpret_message(message.text)
        if reading.is_actionable() or self.llm is None:
            return reading
        second = self.llm.read_message(message, recent)
        second.rationale = reading.rationale + second.rationale
        return second

    def interpret_conversation(self, messages: list[Message]) -> AssembledConversation:
        steps = [(m, interpret_message(m.text)) for m in messages]
        assembled = assemble_conversation(steps)
        if not assembled.unsure or self.llm is None:
            return assembled
        return self.llm.read_conversation(messages)
