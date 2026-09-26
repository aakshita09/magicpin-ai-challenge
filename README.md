# Vera-Better — magicpin AI Challenge submission

## Approach

`composer.compose(category, merchant, trigger, customer=None)` is a **pure,
deterministic, rule-based** function — no LLM call in the hot path. It's
organized as one small handler per `trigger.kind` (23 kinds total, covering
every kind in the dataset), each of which:

1. Looks for real data in `trigger.payload` first (many auto-generated
   triggers in the dataset are placeholders — `{"placeholder": true, ...}` —
   so this often falls through).
2. Falls back to real fields on `merchant` (performance + `delta_7d`,
   `signals`, `offers`, `conversation_history`, `customer_aggregate`,
   `review_themes`, `subscription`).
3. Falls back to `category` (digest items, `peer_stats`, `offer_catalog`,
   `seasonal_beats`, `trend_signals`).
4. Never invents a number, name, or citation that isn't present in one of
   the three layers above (a hallucinated "JIDA paper" or discount is a
   0-score offense per the rubric, so the handlers are written defensively —
   e.g. `_k_perf_dip` will *not* claim a dip if the merchant's own
   `delta_7d` doesn't actually show one for any metric; it reframes around
   a real signal instead of contradicting the data).

`bot.py` is the FastAPI server implementing the 5-endpoint contract
(`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`) plus
an optional `/v1/teardown`. It keeps in-memory context + conversation state,
is idempotent on `(scope, context_id, version)`, tracks `suppression_key`s so
the same trigger is never sent twice, and never re-sends an identical body
in one conversation (anti-repetition, checked in `/v1/reply`).

`conversation_handlers.py` is a small rule-based dialogue manager (no LLM):
regex classifiers for auto-reply (verbatim-repeat detection + known canned
phrases), positive/negative intent, hostile, and off-topic replies. It
implements the three "open challenges" called out in the brief directly:
- **Auto-reply detection**: one polite retry, then graceful exit.
- **Intent hand-off**: an explicit "yes/go ahead" routes straight to
  `action: send` with a "doing it now" message — never back to a
  qualifying question (the #1 miss called out for production Vera).
- **Knowing when to stop**: exits after a hard "not interested", or after
  3 unanswered/ambiguous turns.

`generate_submission.py` calls `compose()` directly (no HTTP) over the 30
canonical `(merchant, trigger[, customer])` pairs in `test_pairs.json` to
produce `submission.jsonl`.

## Trade-offs

- **Deterministic templates over an LLM call.** This guarantees
  reproducibility and the sub-30s response budget with zero API-key/rate-limit
  risk, at the cost of some phrasing variety an LLM would add. The
  architecture is intentionally LLM-swappable: every handler in
  `composer.py` returns `(hook_text, cta_type)` from structured facts, so a
  single-prompt LLM composer could be dropped in behind the same
  fact-extraction layer without touching `bot.py`.
- **Hindi-English code-mix** is handled lightly (detected via
  `merchant.identity.languages` / `customer.identity.language_pref`, then a
  couple of Hinglish connector phrases) rather than full bilingual
  generation, since accurate code-mixed generation without an LLM call is
  hard to keep both natural and deterministic.
- **Placeholder trigger payloads** (most of the 75 auto-generated triggers)
  mean specificity often has to come from merchant/category data instead of
  the trigger itself; a few kinds (e.g. `milestone_reached`) approximate a
  round-number milestone from `customer_aggregate.total_unique_ytd` rather
  than a literal "you crossed N" event, since no crossing event is in the
  data — this is flagged in the rationale field for judge transparency.

## What additional context would have helped most

- A real (non-placeholder) `payload` on every trigger instance, not just one
  per kind — several kinds currently lean entirely on merchant/category
  fallbacks.
- An explicit "milestone" field on `MerchantContext` (e.g.
  `milestones_crossed: ["100_reviews"]`) rather than deriving one.
- A per-merchant `preferred_offer_id` or "most successful past offer" signal,
  to remove the guesswork in picking which active offer to anchor a message on.

## Running locally

```bash
pip install fastapi uvicorn pydantic
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Regenerate `submission.jsonl` against the expanded dataset:

```bash
python dataset/generate_dataset.py --out ./expanded   # from challenge dataset/
python generate_submission.py ./expanded > submission.jsonl
```
