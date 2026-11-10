"""Text normalization and number extraction for colloquial Mexican-Spanish WhatsApp text."""

from __future__ import annotations

import re
import unicodedata

from .types import Size


def normalize_text(raw: str) -> str:
    """Lowercase, strip accents, unify 'x', decimal commas -> dots, collapse whitespace."""
    text = unicodedata.normalize("NFD", raw)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.lower().replace("×", "x").replace("*", "x")
    text = re.sub(r"(\d),(\d)", r"\1.\2", text)
    return re.sub(r"\s+", " ", text).strip()


_NUM = r"(\d+(?:\.\d+)?)"
_UNIT_M = r"(?:\s*(?:m|mts?|metros?)\b\.?)?"
_PAIR_RE = re.compile(rf"\b{_NUM}{_UNIT_M}\s*x\s*{_NUM}{_UNIT_M}")
_MM_RE = re.compile(rf"\b{_NUM}\s*mm\b")
_DECIMAL_RE = re.compile(rf"\b(\d+\.\d+){_UNIT_M}")


def extract_sizes(text: str) -> tuple[list[Size], str]:
    """Pull dimensions out of normalized text.

    Returns the sizes and the text with those spans blanked, so that quantity
    extraction cannot mistake the "100" in "3.20 x 100" for a quantity.
    """
    sizes: list[Size] = []

    def pair(m: re.Match[str]) -> str:
        sizes.append(Size(width_m=float(m.group(1)), length_m=float(m.group(2))))
        return " "

    def mm(m: re.Match[str]) -> str:
        sizes.append(Size(thickness_mm=float(m.group(1))))
        return " "

    def decimal(m: re.Match[str]) -> str:
        sizes.append(Size(width_m=float(m.group(1))))
        return " "

    rest = _PAIR_RE.sub(pair, text)
    rest = _MM_RE.sub(mm, rest)
    rest = _DECIMAL_RE.sub(decimal, rest)
    return sizes, re.sub(r"\s+", " ", rest).strip()


_QTY_RE = re.compile(
    r"\b(\d+)\b(?:\s*(?:rollos?|piezas?|pzas?|pz|unidades?|uds?|hojas?|placas?|laminas?|tramos?)\b)?"
)


def extract_quantities(rest_without_sizes: str) -> list[int]:
    """Integers left after sizes were removed are quantities."""
    return [int(m.group(1)) for m in _QTY_RE.finditer(rest_without_sizes) if int(m.group(1)) > 0]


_SPLIT_RE = re.compile(r"(?<!\d)\.|\.(?!\d)|[;,?!¿¡:]|\s+y\s+(?=\D)|\s+e\s+(?=\D)")
_LEAD_RE = re.compile(r"^(?:y|e|pero|entonces|oye|bueno|ok|tambien|ademas|porfa|por favor)\s+")


def segment(text: str) -> list[str]:
    """Split a message into clauses.

    Splits on sentence punctuation and on the conjunction "y" - but NOT when "y"
    joins two numbers ("1.37 y 1.50 m"), which is one size list, not two clauses.
    """
    out: list[str] = []
    for piece in _SPLIT_RE.split(text):
        piece = _LEAD_RE.sub("", piece.strip()).strip()
        if piece:
            out.append(piece)
    return out
