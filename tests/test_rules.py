"""Tier 1 alone: does the deterministic reader say what the customer said?"""

from axial.nlp.normalize import extract_quantities, extract_sizes, normalize_text, segment
from axial.nlp.rules import interpret_message


def ops(text: str):
    return interpret_message(text).operations


def test_normalize_strips_accents_and_decimal_commas():
    assert normalize_text("Acrílico 3,20 × 100 M.") == "acrilico 3.20 x 100 m."


def test_sizes_are_not_mistaken_for_quantities():
    sizes, rest = extract_sizes("20 rollos lona 3.20 x 100 m y 2 acrilicos de 3mm y micro de 1.37")
    assert [(s.width_m, s.length_m, s.thickness_mm) for s in sizes] == [(3.2, 100.0, None), (None, None, 3.0), (1.37, None, None)]
    assert extract_quantities(rest) == [20, 2]


def test_segment_keeps_number_lists_together():
    assert segment("lona 3.20 x 100 m., mesh y micro de 1.37 y 1.50 m.") == ["lona 3.20 x 100 m", "mesh", "micro de 1.37 y 1.50 m"]


def test_phase1_first_message():
    [req, avail] = ops("Hola buenos dias, me ayudas a cotizar 20 rollos lona 3.20 x 100 m., "
                       "5 rollos de mesh de 3.20 x 100 m. y si cuentas con microperforado de 1.37 y 1.50 m.")
    assert req.kind == "request_items" and [i.qty for i in req.items] == [20, 5]
    assert avail.kind == "availability_check" and [s.width_m for s in avail.mention.sizes] == [1.37, 1.5]


def test_rep_question_and_bare_answers():
    assert ops("Hola buen día. Si. Cuantas piezas de microperforado")[0].kind == "chatter"
    [size] = ops("De momento sería de 1.37x50")
    assert size.kind == "provide_size" and (size.size.width_m, size.size.length_m) == (1.37, 50.0)
    [qty] = ops("Necesitamos 5 rollos")
    assert qty.kind == "provide_qty" and qty.qty == 5


def test_event1_revision_with_cross_check():
    [rev] = ops("Oye de las lonas mejor que sean 15, no 20")
    assert rev.kind == "revise_qty" and (rev.new_qty, rev.previous_qty) == (15, 20)
    assert "lonas" in rev.mention.text


def test_event2_is_an_inquiry_not_a_request():
    [inq] = ops("Buen dia, soy Mario de compras de ACME. Alice me pasó con ustedes, "
                "me confirmas qué lleva la cotización de las lonas?")
    assert inq.kind == "inquiry" and "lonas" in inq.mention.text


def test_event3_aparte_means_separate():
    [req] = ops("y aparte cotizame 2 acrilicos blancos de 3mm porfa")
    assert req.kind == "request_items" and req.separate is True
    assert req.items[0].qty == 2 and req.items[0].mention.sizes[0].thickness_mm == 3


def test_event4_past_tense_cotizo_is_not_a_new_request():
    [rev] = ops("perfecto. y de los acrilicos que cotizó Alice, mejor que sean 4")
    assert rev.kind == "revise_qty" and rev.new_qty == 4 and rev.previous_qty is None
    assert "acrilicos" in rev.mention.text


def test_cancel_without_product():
    [c] = ops("mejor cancelalo")
    assert c.kind == "cancel" and c.mention is None


def test_chatter():
    assert ops("gracias, perfecto!")[0].kind == "chatter"


def test_unknown_size_is_low_confidence():
    interp = interpret_message("cotizame 3 rollos de microperforado de 1.50")
    assert interp.confidence == "low"
    assert any("size" in r for r in interp.rationale)


def test_two_revisions_in_one_message_is_low_confidence():
    assert interpret_message("las lonas que sean 15 y los acrilicos que sean 4").confidence == "low"
