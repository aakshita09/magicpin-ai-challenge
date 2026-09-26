"""
bot.py — magicpin AI Challenge candidate bot ("Vera, but better").

Implements the 5-endpoint contract from challenge-testing-brief.md:
    POST /v1/context     — receive/replace category|merchant|customer|trigger context
    POST /v1/tick        — periodic wake-up; bot may initiate messages
    POST /v1/reply       — receive a merchant/customer reply; respond synchronously
    GET  /v1/healthz     — liveness probe
    GET  /v1/metadata    — bot identity

Composition itself is delegated to composer.compose() (deterministic,
rule/template based over the 4 context layers — no external LLM or network
calls, so it is fast, reproducible, and can never leak context data to a
third party, per the privacy rule in the testing brief §11).

Run:
    uvicorn bot:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from composer import compose
from conversation_handlers import respond as handle_reply

# short, human phrase per trigger kind for continuity references
# ("on it — setting up {last_offer} now") without re-quoting the whole body.
_SHORT_OFFER_PHRASE = {
    "active_planning_intent": "the plan we discussed",
    "renewal_due": "your renewal",
    "winback_eligible": "your reactivation",
    "gbp_unverified": "your Google verification",
    "recall_due": "your slot",
    "customer_lapsed_soft": "your slot",
    "customer_lapsed_hard": "your plan",
    "appointment_tomorrow": "your reminder",
    "supply_alert": "the shelf check",
    "ipl_match_today": "the same-day offer",
    "festival_upcoming": "the festival offer",
    "milestone_reached": "the thank-you post",
    "research_digest": "the patient-ed draft",
    "review_theme_emerged": "the response template",
    "wedding_package_followup": "your next slot",
}


def _short_offer(trigger: Optional[dict]) -> str:
    if not trigger:
        return "that"
    return _SHORT_OFFER_PHRASE.get(trigger.get("kind"), "that")

app = FastAPI(title="Vera-Better Bot")
START = time.time()

# --------------------------------------------------------------------------
# in-memory state (per §2.1: in-memory is fine; just don't restart mid-test)
# --------------------------------------------------------------------------

# (scope, context_id) -> {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict[str, Any]] = {}

# conversation_id -> {
#   "merchant_id", "customer_id", "send_as", "trigger_id",
#   "history": [{"from": "vera"|"merchant"|"customer", "msg": str}],
#   "unanswered_nudges": int, "auto_reply_strikes": int,
#   "last_offer": str, "sent_bodies": set[str], "turn_number": int,
# }
conversations: dict[str, dict[str, Any]] = {}

# suppression_key -> last-sent timestamp (or True), to avoid re-sending the
# same trigger to the same merchant/customer more than once per test window
sent_suppression_keys: set[str] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get(scope: str, context_id: Optional[str]) -> Optional[dict]:
    if not context_id:
        return None
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


# --------------------------------------------------------------------------
# 2.4 GET /v1/healthz
# --------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": counts,
    }


# --------------------------------------------------------------------------
# 2.5 GET /v1/metadata
# --------------------------------------------------------------------------

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Akshita",
        "team_members": ["Akshita Anand"],
        "model": "rule-based deterministic composer (no LLM call in the hot path)",
        "approach": (
            "Template/retrieval composer keyed on trigger.kind, always grounded in real "
            "fields from category/merchant/customer context (never invented). Separate "
            "rule-based conversation manager handles auto-reply detection, intent "
            "hand-off, hostile/off-topic deflection, and graceful exit."
        ),
        "contact_email": "akshita_23cs483@dtu.ac.in",
        "version": "1.0.0",
        "submitted_at": _now_iso(),
    }


# --------------------------------------------------------------------------
# 2.1 POST /v1/context
# --------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"}

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": _now_iso(),
    }


# --------------------------------------------------------------------------
# helpers shared by /v1/tick and /v1/reply
# --------------------------------------------------------------------------

def _resolve_contexts(merchant_id: str, customer_id: Optional[str], trigger: Optional[dict] = None):
    merchant = _get("merchant", merchant_id)
    if not merchant:
        return None, None, None
    category = _get("category", merchant.get("category_slug"))
    customer = _get("customer", customer_id) if customer_id else None
    return category, merchant, customer


def _record_send(conversation_id: str, entry: dict, body_text: str, cta: str, last_offer: str):
    entry["history"].append({"from": "vera", "msg": body_text})
    entry["sent_bodies"].add(body_text)
    entry["last_offer"] = last_offer
    entry["turn_number"] = entry.get("turn_number", 0) + 1


# --------------------------------------------------------------------------
# 2.2 POST /v1/tick
# --------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    for trigger_id in body.available_triggers[:20]:  # respect the 20-action cap defensively
        trigger = _get("trigger", trigger_id)
        if not trigger:
            continue

        suppression_key = trigger.get("suppression_key") or trigger_id
        if suppression_key in sent_suppression_keys:
            continue  # already sent this exact trigger — restraint over spam

        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")
        category, merchant, customer = _resolve_contexts(merchant_id, customer_id, trigger)
        if not (category and merchant):
            continue  # don't compose on incomplete context

        composed = compose(category, merchant, trigger, customer)

        conversation_id = f"conv_{merchant_id}_{trigger_id}"
        if conversation_id in conversations:
            continue  # existing conversation — must go through /v1/reply, not a fresh tick action

        entry = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trigger_id,
            "history": [],
            "unanswered_nudges": 0,
            "auto_reply_strikes": 0,
            "last_offer": composed["body"],
            "sent_bodies": set(),
            "turn_number": 0,
        }
        conversations[conversation_id] = entry
        _record_send(conversation_id, entry, composed["body"], composed["cta"], _short_offer(trigger))
        sent_suppression_keys.add(suppression_key)

        first_touch = len(merchant.get("conversation_history", []) or []) == 0

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trigger_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1" if first_touch else None,
            "template_params": [merchant.get("identity", {}).get("name", "")] if first_touch else None,
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": suppression_key,
            "rationale": composed["rationale"],
        })

    return {"actions": actions}


# --------------------------------------------------------------------------
# 2.3 POST /v1/reply
# --------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    entry = conversations.get(body.conversation_id)
    if entry is None:
        # unseen conversation (e.g. bot restarted or judge referencing an id we
        # never issued) — start minimal state defensively rather than 500ing
        entry = {
            "merchant_id": body.merchant_id,
            "customer_id": body.customer_id,
            "send_as": "merchant_on_behalf" if body.customer_id else "vera",
            "trigger_id": None,
            "history": [],
            "unanswered_nudges": 0,
            "auto_reply_strikes": 0,
            "last_offer": "that",
            "sent_bodies": set(),
            "turn_number": 0,
        }
        conversations[body.conversation_id] = entry

    entry["history"].append({"from": body.from_role, "msg": body.message})

    outcome = handle_reply(entry, body.message)

    if outcome["action"] == "send":
        candidate_body = outcome["body"]
        # anti-repetition guard: never resend a body verbatim in this conversation
        if candidate_body in entry["sent_bodies"]:
            candidate_body = candidate_body + " (following up)"
        entry["history"].append({"from": "vera", "msg": candidate_body})
        entry["sent_bodies"].add(candidate_body)
        entry["turn_number"] = entry.get("turn_number", 0) + 1
        if outcome.get("cta") in ("binary", "open_ended"):
            entry["unanswered_nudges"] = entry.get("unanswered_nudges", 0)
        return {
            "action": "send",
            "body": candidate_body,
            "cta": outcome.get("cta", "none"),
            "rationale": outcome["rationale"],
        }

    if outcome["action"] == "wait":
        return {
            "action": "wait",
            "wait_seconds": outcome.get("wait_seconds", 1800),
            "rationale": outcome["rationale"],
        }

    # "end"
    return {"action": "end", "rationale": outcome["rationale"]}


# --------------------------------------------------------------------------
# optional teardown (§11 of testing brief)
# --------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    sent_suppression_keys.clear()
    return {"status": "wiped"}
