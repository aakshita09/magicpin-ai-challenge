"""
conversation_handlers.py — multi-turn reply logic for an in-flight conversation.

respond(state, merchant_message) -> dict with keys: action, body?, cta?,
wait_seconds?, rationale.

Handles the three "open challenges" called out in the brief:
  - auto-reply detection (same canned text repeating, or known canned phrases)
  - intent-transition (merchant says yes/go-ahead -> act immediately, don't re-qualify)
  - graceful exit (not-interested, or 3 unanswered nudges, or auto-reply confirmed)
It also handles hostile/off-topic replies by staying polite and steering back
to the one open thread, without adding a second CTA.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from composer import _clip, is_hindi_ok  # reuse small utilities


# --------------------------------------------------------------------------
# lightweight classifiers (rule-based, deterministic — no LLM call needed)
# --------------------------------------------------------------------------

_AUTO_REPLY_PATTERNS = [
    r"thank you for contacting",
    r"we('| a)?ll get back to you",
    r"automated (assistant|reply|response)",
    r"shukriya.{0,40}team tak pahuncha",
    r"aapki jaankari ke liye.{0,20}shukriya",
    r"business hours",
    r"currently unavailable",
]

_POSITIVE_INTENT_PATTERNS = [
    r"\byes\b", r"\bok(ay)?\b", r"\bsure\b", r"go ahead", r"let'?s do it",
    r"\bhaan\b", r"chalo", r"karo", r"kar do", r"kar dijiye", r"theek hai",
    r"\bsend\b", r"\b1\b", r"i want to join", r"join karna hai",
]

_NEGATIVE_INTENT_PATTERNS = [
    r"not interested", r"\bstop\b", r"no thanks", r"nahi chahiye",
    r"\bnahi\b", r"leave me alone", r"unsubscribe", r"band karo",
]

_HOSTILE_PATTERNS = [
    r"\bidiot\b", r"\bstupid\b", r"\bshut up\b", r"f\*?u\*?c\*?k",
    r"bakwas", r"bewakoof",
]


def _matches_any(text: str, patterns: list[str]) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in patterns)


def classify_reply(message: str, history: list[dict]) -> str:
    """Return one of: 'auto_reply', 'positive_intent', 'negative_intent',
    'hostile', 'off_topic_question', 'engaged'."""
    text = (message or "").strip()

    # same verbatim body already seen 2+ times before -> 3rd occurrence = auto-reply
    prior_from_other_side = [h["msg"] for h in history if h.get("from") in ("merchant", "customer")]
    same_count = sum(1 for m in prior_from_other_side if m.strip() == text)
    if same_count >= 2 or _matches_any(text, _AUTO_REPLY_PATTERNS):
        return "auto_reply"

    if _matches_any(text, _NEGATIVE_INTENT_PATTERNS):
        return "negative_intent"

    if _matches_any(text, _HOSTILE_PATTERNS):
        return "hostile"

    if _matches_any(text, _POSITIVE_INTENT_PATTERNS):
        return "positive_intent"

    if "?" in text and not _matches_any(text, _POSITIVE_INTENT_PATTERNS):
        return "off_topic_question"

    return "engaged"


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------

def respond(state: dict, merchant_message: str) -> dict:
    """
    state: {
        "history": [{"from": "vera"|"merchant"|"customer", "msg": str}, ...],
        "unanswered_nudges": int,
        "auto_reply_strikes": int,
        "last_offer": str | None,   # what we last proposed, for continuity
        "turn_number": int,
        "send_as": "vera" | "merchant_on_behalf",
    }
    """
    history = state.get("history", [])
    label = classify_reply(merchant_message, history)
    last_offer = state.get("last_offer") or "that"

    if label == "auto_reply":
        state["auto_reply_strikes"] = state.get("auto_reply_strikes", 0) + 1
        strikes = state["auto_reply_strikes"]
        if strikes >= 2:
            return {
                "action": "end",
                "rationale": "Confirmed auto-reply (canned WhatsApp Business text repeating) after one retry; "
                              "exiting gracefully rather than burning further turns.",
            }
        return {
            "action": "send",
            "body": "Understood — before this goes to your team, want to take 2 minutes yourself to see exactly what's involved? Quick to check.",
            "cta": "binary",
            "rationale": "First auto-reply detected; one polite retry aimed at the owner directly, per anti-pollution guidance.",
        }

    if label == "negative_intent":
        return {
            "action": "end",
            "rationale": "Merchant signaled not interested / opt-out; exiting gracefully without further nudges.",
        }

    if label == "positive_intent":
        # Intent-handoff: act immediately, do not re-ask a qualifying question.
        return {
            "action": "send",
            "body": f"Great — on it. Setting up {last_offer} now; I'll confirm here once it's live.",
            "cta": "none",
            "rationale": "Merchant gave explicit go-ahead; routing straight to action instead of re-qualifying "
                         "(this is the #1 production Vera failure mode called out in the brief).",
        }

    if label == "hostile":
        return {
            "action": "send",
            "body": "Sorry that landed badly — not my intent. Still happy to help whenever useful; no pressure either way.",
            "cta": "none",
            "rationale": "De-escalate once, stay polite, leave the door open without pushing another ask.",
        }

    if label == "off_topic_question":
        return {
            "action": "send",
            "body": f"That's outside what I can help with here — best to check with support/your accountant on that. "
                     f"On {last_offer}: still want me to go ahead?",
            "cta": "binary",
            "rationale": "Politely declines the unrelated ask and returns to the single open thread, avoiding scope creep.",
        }

    # engaged but ambiguous — nudge count-aware
    state["unanswered_nudges"] = state.get("unanswered_nudges", 0) + 1
    nudges = state["unanswered_nudges"]
    if nudges >= 3:
        return {
            "action": "end",
            "rationale": "3 unanswered/ambiguous turns — stopping per the 'know when to stop' guidance rather than spamming.",
        }
    return {
        "action": "send",
        "body": f"No rush — just say the word whenever you'd like me to move on {last_offer}.",
        "cta": "binary",
        "rationale": "Ambiguous engaged reply; low-pressure single-binary nudge, tracking toward the 3-nudge stop limit.",
    }
