# The code, in plain words

Read this top to bottom once and you can explain every file. Order follows the data:
catalog → what the customer said → which SKU and quote → storage → the pipeline → HTTP.

---

## `axial/catalog.py` — the mock ERP

```python
Sku = NewType("Sku", str)
```
A `Sku` is just a string, but giving it a name lets the reader (and a type checker) see
where a *checked* SKU is expected versus any old string.

```python
@dataclass(frozen=True, slots=True)
class CatalogItem:
    sku, description, family, aliases, variant_aliases, default_for_family, unit, width_m, length_m, thickness_mm
```
One product. `aliases` are the words a customer uses for it ("lona", "lonas"). `variant_aliases`
are words that pick it out from siblings ("mesh" picks the Mesh lona, not the Frontlit).
`default_for_family=True` says: if the customer only says the family word, this is the one.

```python
CATALOG = ( CatalogItem(...), CatalogItem(...), CatalogItem(...), CatalogItem(...) )
_BY_SKU = {item.sku: item for item in CATALOG}
```
The four products from the brief, and a dictionary for fast lookup.

```python
def to_sku(value: str) -> Sku:
    if value not in _BY_SKU:
        raise UnknownSkuError(...)
    return Sku(value)
```
The one door from "some string" to "a real SKU". Anything not in the catalog is refused.
The store calls this before every write, so a fake SKU can never reach the database.

---

## `axial/models.py` — what we store

Pydantic models with no logic: `Message` (one WhatsApp message), `LineItem` (sku + qty),
`Quote` (id, customer, status, the list of linked `conversations`, `line_items`),
`QuoteEvent` (one history entry: which quote, which message caused it, what changed),
`ReviewItem` (a message a human must decide on, with the candidate quotes).

`Quote.conversations` is a list because a quote can be fed by many chats. It is filled
from the link table when the quote is read; the quote row itself does not store it.

---

## `axial/nlp/types.py` — the contract between "reading" and "doing"

```python
class ProductMention(BaseModel):
    text: str          # the customer's words, never a SKU
    sizes: list[Size]  # "3.20 x 100", "3mm" ... possibly several ("1.37 y 1.50")
```

Then one small class per kind of thing a message can do:

| Class | Example | Meaning |
|---|---|---|
| `RequestItems(items, separate)` | "cotizame 2 acrilicos" | wants products; `separate=True` when they said "aparte" |
| `ReviseQty(mention, new_qty, previous_qty)` | "que sean 15, no 20" | change a quantity; `previous_qty` is the "no 20" part |
| `Inquiry(mention)` | "que lleva la cotizacion de las lonas?" | asking about a quote |
| `Cancel(mention)` | "mejor cancelalo" | cancel a quote |
| `AvailabilityCheck(mention)` | "si cuentas con microperforado" | asking if we sell something |
| `ProvideQty(qty)` | "Necesitamos 5 rollos" | a bare number answering a question |
| `ProvideSize(size)` | "seria de 1.37x50" | a bare size answering a question |
| `Chatter()` | "gracias!" | nothing |

```python
Operation = Annotated[Union[...], Field(discriminator="kind")]
```
A single type that can be any of the above; the `kind` field says which. This is what lets
the resolver do `if op.kind == "revise_qty": ...` safely.

```python
class Interpretation(BaseModel):
    operations: list[Operation]
    confidence: "high" | "low"   # only "high" is ever acted upon
    source: "rules" | "llm"
    rationale: list[str]         # the trace a human can read
```

Look for what is *missing*: there is no SKU field and no quote id field anywhere. An
interpreter cannot express "this is SKU X on quote Y" even if it wanted to.

---

## `axial/nlp/normalize.py` — cleaning text and pulling out numbers

```python
def normalize_text(raw):
```
Lowercase, remove accents (so "cotizó" and "cotizo" are the same), turn "×" into "x",
turn "3,20" into "3.20", squash whitespace. Every regex after this runs on this clean form.

```python
def extract_sizes(text) -> (sizes, rest):
```
Three passes. `3.20 x 100 m` → width+length. `3mm` → thickness. A leftover decimal like
`1.37` → a width. Every match is **replaced with a space** in the returned `rest`. Why: so
that the next function cannot mistake the `100` in `3.20 x 100` for "100 rolls".

```python
def extract_quantities(rest) -> [int]:
```
Whole numbers left in the text after sizes were removed, optionally followed by a unit word
("rollos", "piezas"). Those are quantities.

```python
def segment(text) -> [clauses]:
```
Splits a message into clauses on `. , ; ? !` and on the word "y" — **except** when "y"
sits between two numbers (`1.37 y 1.50`), because that is one list of sizes, not two
sentences. It also drops leading filler words ("y", "oye", "bueno").

---

## `axial/nlp/lexicon.py` — the Spanish cue words

One compiled regex per idea, each with a comment. The ones that matter most:

- `REQUEST_CUE`: verb forms like "cotizame", "necesito", "agrega". Deliberately **not**
  "cotizacion" (a noun: *talking about* a quote) and not "cotizo" (past tense: "que cotizó
  Alice"). This single distinction is what makes event 2 an inquiry and event 4 a revision
  rather than new requests.
- `SEPARATE_CUE`: "aparte", "por separado".
- `INQUIRY_CUE`, `CANCEL_CUE`, `AVAILABILITY_CUE`, `ASK_QTY_CUE`: what they say.
- `REVISE`: "(mejor) que sean 15(, no 20)", "cambialo a 15", "dejalo en 15". Captures the
  new number and, if present, the old one.

---

## `axial/nlp/rules.py` — tier 1, reading one message

```python
def interpret_message(text) -> Interpretation:
```

Step by step:

1. `norm = normalize_text(text)`.
2. **Revisions first.** `REVISE.finditer(norm)` finds "que sean 15, no 20". We record
   `(15, 20)` and blank that span out of the text. We do this *before* splitting into
   clauses because the pattern contains a comma.
3. `separate = SEPARATE_CUE in norm`; `has_request_cue = REQUEST_CUE in rest`.
4. For each clause from `segment(rest)`, call `_classify_segment`.
5. **Bind revisions to a product.** If there was one revision and exactly one clause that
   just named a product ("de las lonas"), the revision is about that product. One revision
   and several products, or two revisions: we mark "unsure".
6. If any requested items were collected, their `RequestItems` operation already sits in the
   list (created when the first item appeared, so order is preserved); set its `separate`.
7. No operations at all → `Chatter()`.
8. `confidence = "low"` if anything was marked unsure, else `"high"`.

```python
def _classify_segment(seg, has_request_cue, draft):
```
For one clause: pull sizes, then quantities, then ask the catalog matcher whether the clause
names a product. Then a ladder of checks, first match wins:

1. names a product but no catalog item fits the size → unsure ("we sell that, not in 1.50").
2. names two different products in one clause → unsure.
3. cancel words → `Cancel(mention)`.
4. inquiry words → `Inquiry(mention)`.
5. product + "cuantas" and no number → it's a question about quantity; ignore.
6. product + "tienes / cuentas con" and no number → `AvailabilityCheck`.
7. product + (a number or a request verb somewhere in the message) → a requested item.
8. product alone → remember it as a "bare mention" (a revision may be about it).
9. a single number, no product → `ProvideQty`.
10. a single size, no product → `ProvideSize`.
11. several numbers/sizes without a product → unsure.

Everything else is ignored (greetings, names, "porfa").

---

## `axial/nlp/assemble.py` — Phase 1, folding a whole conversation

```python
def assemble_conversation(steps: [(Message, Interpretation)]) -> AssembledConversation:
```
Walks the messages in order and keeps a growing list of `RequestedItem`s:

- inbound `request_items` → add the items. Outbound ones are ignored (the rep edits the ERP,
  not the chat).
- inbound `availability_check` → add the item **with no quantity yet**.
- `provide_size` (either direction) → attach to the most recent item without a full size.
  This is how the rep's "seria de 1.37x50" lands on the customer's "microperforado".
- inbound `provide_qty` → fill the most recent item with no quantity. "Necesitamos 5
  rollos" → the microperforado.
- inbound `revise_qty` → change the matching item.
- anything unsure gets recorded in `unsure`, which later becomes a review item.

---

## `axial/resolve/catalog_match.py` — words → catalog candidates

```python
def match_catalog(normalized_text, sizes) -> CatalogMatch:
```
1. For every catalog item, check which of its `variant_aliases` and `aliases` appear as
   whole words in the text.
2. If any **variant** word matched ("mesh"), the candidates are those items only. Otherwise
   the candidates are every item whose plain alias matched ("lona" → both lonas), and the
   match is flagged `generic=True`.
3. If sizes were stated, keep only candidates compatible with at least one size
   (`size_compatible`: each stated dimension must agree with the item, within 1 cm). Sizes
   nobody satisfies are reported in `unmatched_sizes` (the 1.50 m width).
4. Sort: family default first.

Returns catalog *objects*. It cannot return a SKU that does not exist.

---

## `axial/resolve/resolver.py` — the decision

```python
class Context: customer_id, conversation_id, open_quotes, forced_quote_id
```
Everything the resolver is allowed to look at. `open_quotes` are **all of the customer's
draft quotes**, not the chat's. `forced_quote_id` is set only when a human resolved a
review item by picking a quote.

```python
class Decision: outcome ("apply" | "review" | "noop"), changes, reason, candidate_quote_ids
class NeedsReview(Exception)
```
`NeedsReview` is raised anywhere we would otherwise guess; `resolve` catches it and turns
it into a `review` decision. `candidates=None` means "could be any open quote", `[]` means
"none of them fits".

```python
def resolve(interp, ctx) -> Decision:
```
Low confidence → review immediately. Otherwise loop over the operations and collect changes;
the first `NeedsReview` aborts the whole message (all or nothing — a half-applied message is
harder for a human to reason about than an unapplied one). No changes → `noop`.

```python
def _resolve_operation(op, ctx, notes) -> [Change]:
```
- `chatter` → nothing. `availability_check` → nothing (the rep answers in chat).
- `provide_qty` / `provide_size` → `NeedsReview` (a lone number means nothing without the
  question it answers; only Phase 1 reconstructs that).
- `request_items` → every item must resolve to one SKU and have a quantity
  (`resolve_item_sku`, `_require_qty`). Then `_quote_for_new_items` decides: a forced quote;
  or a new quote if "aparte"; or the single quote this chat already feeds; or a new quote if
  none; or `NeedsReview` if the chat feeds several.
- `revise_qty` → `_find_quote` then `_find_line` → `SetQty` + `LinkConversation`.
- `inquiry` → `_find_quote` → `NoteInquiry` + `LinkConversation`.
- `cancel` → `_find_quote` → `CancelQuote` + `LinkConversation`.

```python
def resolve_item_sku(item) -> Sku:
```
No candidates → `NeedsReview` (with a different message if the product exists but not in
that size). One candidate, or several but generic → the first (the family default).
Several specific candidates → `NeedsReview`.

```python
def _find_quote(ctx, mention, previous_qty) -> (Quote, match):
```
The heart of "how a message finds its quote":
1. Forced quote? Return it.
2. Turn the mention into candidate SKUs. A mention that names nothing we sell → `NeedsReview`.
3. `hits` = open quotes with at least one line in those SKUs. No mention → every open quote.
4. If the message said "no 20", prefer quotes whose matching line actually has 20.
5. Exactly one hit → that quote. Zero → `NeedsReview` with `[]`. Several → `NeedsReview`
   with their ids.

```python
def _find_line(quote, match, previous_qty) -> LineItem:
```
Lines on the quote in the candidate SKUs. If "no 20" was said and no line has 20 →
`NeedsReview` ("message says 20, quote has 15"). One line → done. Several lines and the
word was generic → the family default (Frontlit for "lona"). Otherwise `NeedsReview`.

---

## `axial/store/sqlite.py` — persistence

`SCHEMA` creates the tables drawn in the README. Notable:

- `quote_conversations(quote_id, conversation_id, linked_by_message_id)` — the many-to-many
  link, remembering which message made it.
- `ingestions(ingestion_id, result)` — the idempotency ledger.
- `quote_events` — the history.

```python
@contextmanager
def transaction(self):  BEGIN ... COMMIT / ROLLBACK
```
Everything one message does is inside one of these.

Reads build pydantic models (`_hydrate` assembles a `Quote` from three tables). Writes are
one SQL statement each. `add_line_items` and `set_line_qty` call `to_sku()` first: the last
line of defence against a bad SKU. `next_quote_id` is a counter row → `Q-001`, `Q-002`.

---

## `axial/nlp/llm.py` and `axial/nlp/interpreter.py` — the optional model and the tiering

`LLMOperation` / `LLMReading` are the fixed shape Claude must answer in: `kind`,
`product_words`, sizes, `qty`, `previous_qty`, `separate`, `confident`, `reasoning`. There is
no SKU field, and the system prompt lists product *descriptions*, not codes.
`to_interpretation` converts that flat answer into the same `Operation` classes the rules
produce, so the resolver cannot tell who wrote it. `ClaudeReader._ask` calls
`client.messages.parse(..., output_format=LLMReading)`; any API error or refusal → `None` →
a low-confidence interpretation → review.

`Interpreter.interpret_message`: run the rules; if confident or no model configured,
return that; otherwise ask the model. That one `if` is the token-saving policy.

---

## `axial/engine.py` — the pipeline

```python
def ingest_message(self, message):
    with self._lock:                                   # one message at a time
        if previous := self.store.get_ingestion(message.message_id):
            return {**previous, "duplicate": True}     # same webhook twice -> no change
        interp = self.interpreter.interpret_message(message, recent)   # read (may call LLM)
        with self.store.transaction():
            save conversation + message
            outbound? -> noop (rep messages are context only)
            else: decision = resolve(interp, self._context(message))
            result = self._apply(message, interp, decision)
            self.store.save_ingestion(message.message_id, result)
        return result
```

`_apply` is a loop over `decision.changes` with one branch per change type; each branch
does the store write and adds the matching `quote_events` row with the message id. A
`review` decision creates a `ReviewItem` instead and changes nothing.

`ingest_conversation` (Phase 1): builds `Message`s from the history, runs
`interpret_conversation` (rules per message + `assemble_conversation`), resolves each item
to a SKU, creates the quote with the ones that resolved, and opens a review item listing
the ones that did not. Idempotent on a hash of the payload.

`resolve_review`: a human says "assign Q-002" (or "dismiss"). We reload the *stored*
interpretation of the original message, run `resolve` again with `forced_quote_id`, apply,
mark the review resolved, and add a `review_resolved` event. The machine still does the
applying; the human only supplied the one fact it could not infer.

---

## `axial/api/app.py` — HTTP

Seven routes, each one line of logic: validate the body with the pydantic model, call the
engine, return the dict. `EngineError` carries an HTTP status (404, 409, 400). `main.py`
builds the app from environment variables so `uvicorn axial.main:app` just works.

---

## `tests/`

- `test_rules.py` — the reader alone, message by message from the brief plus traps.
- `test_resolver.py` — when the engine acts and when it refuses: the "no 20" mismatch,
  two open quotes, generic "lona", "aparte" vs not, unknown products, outbound messages,
  cancelled quotes, review dismiss.
- `test_scenario.py` — the entire brief through the HTTP API, with a process restart in the
  middle, both extra credits, and the expected final JSON compared exactly.
- `test_llm_tier.py` — with a fake model: the model is not called for clear messages, is
  called for unsure ones, and cannot smuggle a SKU.
