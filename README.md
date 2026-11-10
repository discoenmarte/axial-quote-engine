# Axial take-home: the Quote & State Engine

A headless pipeline that turns colloquial WhatsApp conversations into structured draft
quotes, and keeps those quotes correct as more messages arrive — revisions, questions
from a second chat, new requests — without mixing them up.

Python 3.11+, FastAPI, SQLite from the standard library. No ORM, no queue, no framework
magic. Runs with or without an LLM.

```bash
uv sync                               # install (or: pip install -e ".[dev]")
uv run pytest                         # 33 tests: the brief's story + edge cases
uv run python scripts/demo.py         # the whole story, printed step by step
uv run uvicorn axial.main:app --reload # the API on http://localhost:8000  (docs at /docs)
```

```bash
curl -X POST localhost:8000/conversations -H 'content-type: application/json' -d @examples/phase1_conversation.json
curl -X POST localhost:8000/messages      -H 'content-type: application/json' -d @examples/event1_revise_lonas.json
curl "localhost:8000/quotes?customer=C-10029"
curl  localhost:8000/quotes/Q-001          # includes the change history
curl  localhost:8000/review                # messages waiting for a human
```

State lives in `data/axial.db` (override with `AXIAL_DB_PATH`) and survives restarts.
Set `ANTHROPIC_API_KEY` to enable the optional LLM tier; everything in the brief works
without it.

---

## 1. The pipeline

```
 WhatsApp webhook                                                        ERP (later)
      │                                                                       ▲
      ▼                                                                       │
 POST /messages ──► 1. seen this message_id? ── yes ──► return stored result, change nothing
      │                        │ no
      │                        ▼
      │              2. INTERPRET  "what did they say?"            (axial/nlp/)
      │                 ┌─ tier 1: rules  ── confident? ──────────────┐
      │                 │       │ unsure                              │
      │                 │       ▼                                     │
      │                 ├─ tier 2: Claude (optional) ─ confident? ────┤   output: an Interpretation
      │                 │       │ unsure / not configured             │   (intent, the customer's WORDS
      │                 │       ▼                                     │    for a product, qty, sizes)
      │                 └─ "low confidence" ──────────────────────────┤   never a SKU, never a quote id
      │                                                               ▼
      │              3. RESOLVE  "which SKU? which quote?"         (axial/resolve/)   deterministic
      │                 words ──catalog aliases──► candidate SKUs   ──► the ONE open quote
      │                 containing them ──► concrete changes, or NeedsReview
      │                                                               │
      │              4. APPLY in one transaction                   (axial/engine.py + store/)
      │                 ├─ apply : update quote, link chat, write history events
      │                 ├─ review: open a review item, change nothing
      │                 └─ noop  : store the message, change nothing
      │                                                               │
      └──────────────► 5. remember the result under message_id  (so step 1 works next time)
```

The LLM, when enabled, sits *inside* step 2 and only runs when the rules are unsure. It
is never asked which SKU or which quote: it is not even shown the SKU codes. Its answer
flows through exactly the same resolver as the rules' answer.

## 2. The data model

```
 conversations                 quote_conversations                   quotes
 ┌──────────────────┐          ┌──────────────────────┐          ┌─────────────────────┐
 │ conversation_id ●│◄────────►│ quote_id             │◄────────►│ quote_id ●          │
 │ customer_id      │   n : n  │ conversation_id      │   n : n  │ customer_id         │
 │ customer_name    │          │ linked_by_message_id │          │ status draft|cancel │
 └──────────────────┘          │ linked_at            │          └─────────────────────┘
        │ 1                    └──────────────────────┘                 │ 1          │ 1
        │                                                               │            │
        ▼ n                                                             ▼ n          ▼ n
 messages                                                    quote_line_items   quote_events
 ┌──────────────────┐                                        ┌──────────────┐  ┌──────────────────┐
 │ message_id ●     │                                        │ quote_id     │  │ quote_id         │
 │ conversation_id  │                                        │ sku  (catalog│  │ message_id  ◄── which message
 │ direction        │                                        │       only)  │  │ type             │   caused it
 │ timestamp, text  │                                        │ qty          │  │ detail (json)    │
 └──────────────────┘                                        └──────────────┘  └──────────────────┘

 ingestions   (message_id → stored result)   = idempotency ledger
 review_items (message, reason, candidate quotes, status)   = the human queue
```

The table that matters is `quote_conversations`. The brief points out that both obvious
shortcuts break: if a conversation carried a `quote_id`, event 3 (one chat, two quotes)
breaks it; if a quote carried a `conversation_id`, event 2 (two chats, one quote) breaks
it. A plain many-to-many link table has no owner on either side, so none of the four
events needs a workaround. Each link also records *which message* created it, so the
history answers "why is Mario's chat attached to Q-001?" — because of `wamid.662.0001`.

A quote's `conversations` list is just that table read in link order. The quote itself
only knows its customer, its status and its lines.

## 3. How a message finds its quote

The message's **content** decides, never the chat it arrived in. Walking event 4
(`"perfecto. y de los acrilicos que cotizó Alice, mejor que sean 4"`, from Mario's chat
`wa-thread-662`) through the code:

| Step | Function | What it looks at | Result |
|---|---|---|---|
| 1 | `QuoteEngine.ingest_message` | `ingestions` table | `wamid.662.0002` not seen before → continue |
| 2 | `rules.interpret_message` | the text | revision pattern `mejor que sean 4`; clause `de los acrilicos que cotizo alice` names a product; `cotizó` is past tense so it is **not** a new request. → `ReviseQty(mention="…acrilicos…", new_qty=4)`, confidence **high** (LLM never called) |
| 3 | `resolver.resolve` → `_find_quote` | `ctx.open_quotes` = **all of customer C-10029's draft quotes** (Q-001, Q-002), not the chat's | `mention_match("acrilicos")` → candidate SKU `ARACBL-03-122244` → which open quotes have a line with that SKU? Only Q-002 → exactly one → target |
| 4 | `_find_line` | Q-002's lines | one line matches → `ARACBL-03-122244`, qty 2 |
| 5 | `resolver` returns | — | `SetQty(Q-002, ARACBL, 2 → 4)` + `LinkConversation(Q-002)` |
| 6 | `QuoteEngine._apply` | one SQLite transaction | line updated; `wa-thread-662` linked to Q-002; two `quote_events` rows written with `message_id = wamid.662.0002`; result stored under the message id |

If step 3 had found two quotes with acrylics, the resolver raises `NeedsReview` with both
ids and nothing is changed. If it had found none, same thing with an empty candidate
list ("no open quote contains acrílicos").

Extra signals the resolver uses when present, in this order: the product words → the
sizes stated → the "no 20" cross-check (`previous_qty` must equal the current line
quantity, otherwise it refuses) → the catalog's declared default when the word is generic
("lona" means Frontlit unless the customer says "mesh"). The chat a message came from is
used for exactly one decision: when a customer adds items *without* saying "aparte", they
join the single quote that chat is already working on. If that chat feeds two quotes, it
goes to review.

## 4. Deterministic vs. model

| Place | Who decides | May decide | Must never decide | When it is wrong |
|---|---|---|---|---|
| Normalize + segment text | rules | accents, decimal commas, clause boundaries | — | a clause is mis-split → usually "unsure" → review |
| Intent + numbers (tier 1) | rules (`nlp/rules.py`) | request / revise / inquiry / cancel / chatter; quantities; sizes; "aparte" | SKU, quote | wrong confident read → wrong change, visible in history with the causing message; human reverts in ERP. Mitigated by the `no 20` cross-check and the all-or-nothing transaction |
| Intent + numbers (tier 2, optional) | Claude via structured output (`nlp/llm.py`) | the same, only for messages tier 1 marked unsure | SKU (not shown the codes), quote, anything not in the message | its words go through the same catalog matcher; nonsense words match nothing → review. Model error, refusal or `confident=false` → review |
| Words → SKU | catalog matcher (`resolve/catalog_match.py`) | candidate SKUs from aliases + sizes | inventing a SKU: outputs are catalog objects, and the store re-validates every SKU on write | a wrong alias is a config bug, fixed in one table |
| Which quote / which line | resolver (`resolve/resolver.py`) | the one quote whose lines contain the product | guessing between two | raises `NeedsReview`; a human picks, and the machine applies |
| Apply | engine + SQLite | nothing; it executes the decision atomically | — | transaction rolls back; message not marked processed; retry is safe |

A SKU that is not in the catalog is impossible in the output by construction: the
interpreters emit *words*, the matcher only returns catalog entries, and
`Store.add_line_items` calls `to_sku()` as the last line of defence.

## 5. "mejor cancelalo": knowing when not to guess

With two open quotes, `"mejor cancelalo"` names no product, so `_find_quote` finds two
candidates and raises `NeedsReview`. The engine stores the message, opens a **review item**
(`GET /review`) carrying the text, the reason ("This message could refer to 2 open quotes")
and the candidate ids `[Q-001, Q-002]`, and changes nothing. The quote states are exactly
what they were. A person then answers with `POST /review/{id}` `{"action": "assign",
"quote_id": "Q-002"}`; the engine re-runs the *same stored interpretation* with that quote
forced, applies the cancellation, writes a `review_resolved` event, and closes the item.
Or they `dismiss` it (say, the customer called). Nothing reaches the ERP on a guess. The
same path handles every other kind of doubt: a quantity the message says is "20" when the
line says 15, a product we sell but not in the stated size, a chat that feeds two quotes.

## 6. Edge cases

| Case | What the system does | Why |
|---|---|---|
| `mejor cancelalo`, two open quotes | review item with both candidates; no change | can't tell which; a wrong cancel is expensive |
| Same event delivered twice (same `message_id`) | returns the stored result with `duplicate: true`; no change | `ingestions` table keyed by message id |
| Same text, new `message_id` | processed as a new message | a real repeat request looks identical; dedupe by id only |
| `"no 20"` but the line says 15 | review ("message says 20, quote has 15") | the customer is looking at a different version than we are |
| `"las lonas"` when Q-001 has Frontlit *and* Mesh | Frontlit (catalog default), unless "mesh"/"malla" is said or `previous_qty` points at the mesh line | the catalog declares what the bare family word means; a product-hierarchy question, not an NLP one |
| New items without "aparte" | added to the one quote this chat already feeds; new quote if none; review if several | the sentence "cotizame 2 acrílicos" in an active chat means "add it", per the brief's own reasoning on "aparte" |
| Product we sell, size we don't (`microperforado de 1.50`) | review, never a near-miss SKU | sizes are part of identity |
| Words that match nothing (`papel kraft`) | review if a quantity came with it (customer wants something); chatter otherwise | nothing in the catalog → nothing to quote |
| Outbound (rep) messages | stored as context, never change quotes | the rep is the human in the loop and edits the ERP directly; parsing their chat would create a second source of truth |
| Revision mentioning a cancelled quote's product | review, empty candidates | cancelled quotes are not open |
| Two revisions in one message | review | pairing products to numbers in one sentence is where regexes start lying |

**Deliberately not handled** (said out loud rather than silently):

| Case | Why not |
|---|---|
| Bare answers in Phase 2 (`"Necesitamos 5 rollos"` as a lone webhook after the rep asked) | goes to review. Handling it means persisting "open questions" per conversation; a real feature, not a 6-hour one. Phase 1 does handle it inside a conversation |
| Number words (`"quince"`), ranges (`"15 o 20"`), units conversion (m² → rolls) | the LLM tier reads these when enabled; the rules say "unsure". Cheap to add once there is traffic to tell which matter |
| Out-of-order webhook delivery | processed in arrival order. Reordering by `timestamp` needs a per-conversation buffer and a timeout policy |
| Customer identity across chats | trusted from the payload's `customer_id`. Resolving "soy Mario de ACME" to a customer is an identity problem the CRM should own |
| Pricing, stock, sending to the ERP | out of scope per brief; the quote carries SKUs and quantities |

## 7. Trade-offs, and what breaks first at 1,000 conversations a day

**What I'd change first with another week.** Replace "unsure → review" for bare answers
with persisted pending questions per conversation, so Phase 2 handles the scattered-item
pattern the same way Phase 1 does. Second: an evaluation set of real anonymized messages
to measure the rules' precision and decide, with data, how often the LLM tier is worth
its latency.

**The one thing I'd rip out.** The regex lexicon in `nlp/lexicon.py`. It is the right
tool for a 4-SKU catalog and a 6-hour budget, and every rule is explainable in one line,
but it will not survive a 4,000-SKU catalog. The replacement is already shaped: the
`Interpretation` type is the contract, so the rules can be swapped for the model (or a
small fine-tuned classifier) one operation kind at a time, keeping the resolver intact.

**Scaling to 1,000 conversations/day.** That is well under one message a minute; CPU and
I/O are not the problem. What breaks first, in order:

1. **The human review queue**, not the software. If 5% of messages are unsure, that is
   dozens of decisions a day for Izzy. The lever is the catalog alias table and the rules'
   precision, measured against real traffic — not more infrastructure.
2. **The single process lock.** `QuoteEngine` serializes all messages with one
   `threading.Lock`, which is correct and plenty at this volume but ties you to one
   process. Next step: a lock *per customer* (messages for different customers never
   interact), then a per-customer partitioned queue (e.g. Redis streams or SQS FIFO with
   `customer_id` as the group) in front of stateless workers.
3. **SQLite** is a single-writer file. Fine here; moving to Postgres is a mechanical change
   because the store is plain SQL behind one class, and the idempotency and link tables
   stay exactly the same.
4. **LLM latency** (seconds) inside the webhook request. Acknowledge the webhook first,
   process asynchronously, and keep the idempotency ledger so retries stay safe.
5. **Observability.** The `quote_events` history already answers "what happened and why";
   add metrics on `outcome` (apply / review / noop) per day to watch the system drift.

## 8. How I used AI tools

I used Claude Code throughout. I wrote the brief for it, architecture, stack proposals, 
and set the two design rules the system is built on: the many-to-many link table instead
of either shortcut, and "content decides the quote, the chat never does". It drafted the
modules and the tests against those rules. I read every line, challenged the parts I did
not like (a bug in Phase 1 line ordering, the review candidate fallback), and changed the
test where my expectation was wrong rather than the behaviour. The README's reasoning is
mine; the typing is mostly its. I would not ship code I can't walk through, and
`docs/CODE_WALKTHROUGH.md` is the proof that I can.

## Project layout

```
axial/
  catalog.py            the mock ERP catalog; the ONLY place a SKU is born (to_sku)
  models.py             stored records: Message, Quote, LineItem, QuoteEvent, ReviewItem
  nlp/
    types.py            the Interpretation / Operation contract (no SKU, no quote id)
    normalize.py        accents, decimals, sizes vs quantities, clause splitting
    lexicon.py          the Spanish cue words, one regex each
    rules.py            tier 1: deterministic reading of one message
    assemble.py         Phase 1: fold a whole conversation into requested items
    llm.py              tier 2 (optional): Claude with structured output; never sees SKUs
    interpreter.py      the tiering policy
  resolve/
    catalog_match.py    words -> candidate catalog items (aliases + sizes)
    resolver.py         Interpretation + open quotes -> changes, or NeedsReview
  store/sqlite.py       the schema and plain SQL; re-validates SKUs on write
  engine.py             the pipeline: dedupe -> interpret -> resolve -> apply -> remember
  api/app.py, main.py   FastAPI routes
scripts/demo.py         the brief's story, end to end
examples/*.json         the payloads from the brief
tests/                  33 tests
docs/PRESENTATION.md    talking points for the CTO session
docs/CODE_WALKTHROUGH.md the code, file by file, in plain words
```
