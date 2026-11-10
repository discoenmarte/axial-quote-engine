# Presentation notes (45 min with the CTO)

These are the six points the brief asks the presentation to cover, with the answer in
the form I would say it out loud. Nothing here is not also in the code.

## 1. System design and rationale

**Ten plain lines, webhook to stored state:**

1. A webhook event arrives at `POST /messages` with a `message_id`.
2. If that id is in the `ingestions` table, return the stored result. Done. (Idempotency.)
3. Otherwise the *rules* read the text: intent, the customer's words for a product,
   quantities, sizes, "aparte". Microseconds, no network.
4. If the rules are unsure and an API key is set, Claude reads it, answering in a fixed
   schema that has no field for a SKU or a quote id.
5. If still unsure: the message goes to the review queue. Nothing changes.
6. Otherwise the *resolver* maps the words to catalog candidates via an alias table,
   then finds the one open quote of this customer whose lines contain those SKUs.
7. Zero or several quotes → `NeedsReview`, with the candidate ids. Nothing changes.
8. Exactly one → a list of concrete changes: set a quantity, add lines, create a quote,
   link the conversation, cancel.
9. One SQLite transaction applies them and writes one `quote_events` row per change,
   each carrying the `message_id` that caused it.
10. The result is stored under the `message_id` so step 2 works next time.

**The data model** is in the README (section 2). The sentence to remember: *conversations
and quotes are joined by a link table that records which message made the link*. Neither
side owns the other, which is exactly what events 2 and 3 require.

**Why this over the alternatives.**

- *LLM-first (send every message to a model with the quote state in the prompt).* Simple to
  write, expensive per message, and the hard failure is silent: a confident wrong SKU or a
  confident wrong quote. I wanted the SKU and the quote decided by code I can read, and the
  model, if present, limited to language.
- *Rules-only.* What is shipped runs rules-only, and passes the brief. But a rules-only
  design has to decide what to do when it cannot parse. "Review" is that answer; the LLM is
  an optional second reader before the human, not a replacement for the rules.
- *A conversation-centric model (state machine per chat).* Natural for Phase 1, breaks on
  event 4: Mario's chat has no "current quote" that is Q-002. Hence: the customer's open
  quotes are the search space, the message's content is the key.

## 2. Deterministic vs. model call

The table is in README section 4. The headline for each box:

- **Rules** may decide intent and numbers. Must never produce a SKU or a quote id (the types
  don't have fields for them). When wrong *and* confident, the damage is one wrong line
  change, recorded with its causing message, reversible in the ERP. The `no 20` cross-check
  and the all-or-nothing transaction limit the blast radius.
- **Claude** (optional) may decide the same things, only for messages the rules flagged.
  Must never pick a SKU (it is not shown the codes) or a quote. When wrong, its words match
  nothing in the catalog and the message lands in review; when it says `confident=false`,
  same. When the API fails or refuses: review.
- **Catalog matcher** may return candidate catalog entries. Cannot invent one: its outputs
  are objects from the catalog list. `Store.add_line_items` re-validates anyway.
- **Resolver** may pick the one quote and one line that the content points to. Must never
  choose between two. It gives up by raising `NeedsReview` — to the review queue, i.e. to
  Izzy.

## 3. How a message finds its quote: event 4, function by function

`"perfecto. y de los acrilicos que cotizó Alice, mejor que sean 4"` from `wa-thread-662`.

```
api/app.py        ingest_message(event)                 validates the body as a Message
engine.py         QuoteEngine.ingest_message            lock; get_ingestion("wamid.662.0002") -> None
engine.py           store.recent_messages(...)          context for the LLM (unused: rules will be confident)
interpreter.py    Interpreter.interpret_message         -> rules.interpret_message(text)
rules.py            interpret_message
normalize.py          normalize_text                    "perfecto. y de los acrilicos que cotizo alice, mejor que sean 4"
lexicon.py            REVISE.finditer                   matches "mejor que sean 4"  -> (new=4, previous=None); span removed
normalize.py          segment(rest)                     ["perfecto", "de los acrilicos que cotizo alice"]
rules.py              _classify_segment("perfecto")     no sizes, no qty, no catalog words -> nothing
rules.py              _classify_segment("de los acrilicos que cotizo alice")
catalog_match.py        match_catalog                   alias "acrilicos" -> [Acrilico Blanco 3mm]; names_a_product
lexicon.py              REQUEST_CUE?                    "cotizo" is NOT a request verb (past tense)  -> no
rules.py                -> bare_mentions = [that clause]
rules.py              bind revision                     1 revision, 1 bare mention -> ReviseQty(mention, 4, None)
rules.py              confidence                        no "unsure" flags -> "high"      (LLM never called)
engine.py           store.transaction(): save message
engine.py           _context(message)                   open_quotes = list_quotes("C-10029", status="draft") = [Q-001, Q-002]
resolver.py       resolve(interp, ctx)                  interp is actionable -> loop operations
resolver.py         _resolve_operation(ReviseQty)
resolver.py           _find_quote(ctx, mention, None)
resolver.py             mention_match(mention)          -> candidates {ARACBL-03-122244}
resolver.py             hits = quotes with a line in {ARACBL} -> [Q-002]       (Q-001 has lonas and vinil only)
resolver.py             len(hits) == 1 -> Q-002
resolver.py           _find_line(Q-002, match, None)    one matching line -> ARACBL qty 2
resolver.py           -> [SetQty(Q-002, ARACBL, 2->4), LinkConversation(Q-002)]
resolver.py         Decision(outcome="apply", changes=[...])
engine.py           _apply                              set_line_qty; add_event qty_changed {from 2, to 4}
                                                        link_conversation(Q-002, wa-thread-662) -> new -> add_event conversation_linked
engine.py           save_ingestion("wamid.662.0002", result)   commit
```

Note what was *not* consulted: which quotes `wa-thread-662` was linked to (Q-001 only, at
that point). The content found Q-002; the link to Mario's chat is a *consequence*, not an
input.

## 4. Edge cases

The two tables in README section 6 (handled / deliberately not handled). The three I would
bring up unprompted:

- **`mejor cancelalo`** → review with both candidates. Live demo: `scripts/demo.py` shows it,
  then a human assigns Q-002 and the cancel applies with a `review_resolved` event.
- **`no 20` when the line says 15** → review. It is the cheapest cross-check in the system
  and it catches the most dangerous situation: customer and system looking at different
  versions of the quote.
- **Bare `Necesitamos 5 rollos` as a lone Phase 2 webhook** → review. Honest gap: handled in
  Phase 1 (whole conversation), not in Phase 2. The fix is a `pending_questions` table per
  conversation; I chose not to build it inside the timebox.

## 5. Trade-offs

README section 7. In one breath each:

- *Change first:* pending questions per conversation, so Phase 2 handles scattered answers
  like Phase 1 does; and an eval set from real messages.
- *Rip out:* the regex lexicon, once the catalog is real-sized. The `Interpretation`
  contract means the resolver and store don't change when it goes.
- *1,000 conversations a day:* the review queue's human cost breaks first, then the single
  process lock (go per-customer), then SQLite (go Postgres, mechanically), then LLM latency
  in the webhook path (ack first, process async). None of these is a rewrite; the
  boundaries are already where they need to be.

## 6. Other things worth saying

- The whole thing runs with `uv sync && uv run pytest` and no API key. The LLM is a
  capability I can switch on, not a dependency I need to demo.
- Everything that changed a quote can be traced to a message: `GET /quotes/Q-001` returns
  the history. That is the "human can see what changed and which message caused it"
  requirement, and it is also how I'd debug a wrong read in production.
- Line order in a quote is insertion order on purpose: the draft looks like the
  conversation the rep remembers.

## Questions I expect, and the short answer

- *Why not store quote_id on the conversation and just update it?* Event 3: Alice's chat
  then feeds two quotes. Which is "current"? And event 4 needs Mario's chat to point at Q-002
  while it was "on" Q-001. A link table is the model that doesn't need a special case.
- *Isn't the "lona means Frontlit" default a guess?* It is a catalog fact, declared next to
  the SKU, not something the pipeline infers. In the ERP this is a product hierarchy. And
  it is overridden by evidence: the word "mesh", or a `previous_qty` matching the mesh line.
- *What if two customers' quotes are mixed up?* They can't be: the resolver's search space
  is `list_quotes(customer_id, status="draft")`. The customer id comes from the webhook.
- *Concurrency?* One lock around the whole pipeline. At under one message a minute, that is
  the correct amount of engineering. The path to per-customer partitioning is described.
- *Why pydantic models for Operations instead of plain dicts?* Because the LLM output, the
  rules output and the stored review interpretation all have to be the same shape, and I
  want the type checker to tell me when a new operation kind is not handled in the resolver.
