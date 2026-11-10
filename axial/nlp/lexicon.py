"""Cue words for colloquial Spanish sales chat. Everything here runs on *normalized* text
(lowercase, no accents - see :func:`axial.nlp.normalize.normalize_text`)."""

from __future__ import annotations

import re

# "cotizame", "necesito", "quiero", "agrega"... Verb forms only. Deliberately NOT
# "cotizacion" (a noun: talking about a quote) and NOT "cotizo" (past tense: "que cotizo Alice").
REQUEST_CUE = re.compile(
    r"\b(?:"
    r"cotiza(?:me|nos|r|rme|rnos|s|rias?|n)?"
    r"|necesit(?:o|amos|aria|ariamos)"
    r"|quier(?:o|emos)|quisiera(?:mos)?"
    r"|ocup(?:o|amos)"
    r"|agrega(?:me|le|nos|r)?|anad(?:e|ir|eme|ele)"
    r"|sum(?:a|ale|ame)|incluye(?:me)?"
    r"|manda(?:me|nos)|pasa(?:me|nos)"
    r"|me (?:das|pasas|mandas) (?:precio|precios|cotizacion)"
    r"|precio de"
    r")\b"
)

# "aparte", "por separado" - the customer wants a different quote.
SEPARATE_CUE = re.compile(
    r"\b(?:aparte|por separado|otra cotizacion|en otra|separad[oa]s?|cotizacion (?:nueva|distinta|diferente|independiente))\b"
)

INQUIRY_CUE = re.compile(
    r"\b(?:"
    r"que lleva|que incluye|que tiene|que trae|que contiene"
    r"|me confirmas|confirmame|me recuerdas|me dices que"
    r"|como va|como quedo|como quedamos"
    r"|estatus|status|estado de"
    r"|me (?:pasas|reenvias|compartes|mandas) la cotizacion"
    r"|ya (?:esta|quedo) (?:la cotizacion|lista)"
    r")\b"
)

CANCEL_CUE = re.compile(
    r"\b(?:"
    r"cancel\w*"
    r"|olvida(?:lo|la|los|las)"
    r"|ya no (?:lo|la|los|las) (?:quiero|queremos|necesito|necesitamos|ocupo|ocupamos)"
    r"|no (?:lo|la) (?:quiero|queremos)"
    r"|descarta(?:lo|la)?"
    r")\b"
)

AVAILABILITY_CUE = re.compile(
    r"\b(?:cuentas? con|cuentan con|tienes|tienen|manejas|manejan|hay|disponibles?|existencias?)\b"
)

ASK_QTY_CUE = re.compile(r"\bcuant[oa]s\b")

# "mejor que sean 15, no 20" / "cambialo a 15" / "mejor 15" / "dejalo en 15"
_UNIT = r"(?:\s*(?:rollos?|piezas?|pzas?|pz|unidades?|uds?|hojas?|placas?|laminas?)\b)?"
REVISE = re.compile(
    r"\b(?:(?:mejor|ahora|entonces|al final)\s+)?"
    r"(?:que\s+sean?|cambia(?:lo|la|los|las|r)?\s+a|ponle|dejal[oa]s?\s+en|quedamos\s+en|mejor)"
    r"\s+(\d+)\b" + _UNIT +
    r"(?:\s*,?\s*(?:y\s+)?(?:no|ya\s+no|en\s+vez\s+de|en\s+lugar\s+de)\s+(\d+)\b)?"
)
# "en vez de 20" anywhere else in the message
PREVIOUS_QTY = re.compile(r"\b(?:en\s+vez\s+de|en\s+lugar\s+de|ya\s+no)\s+(\d+)\b")
