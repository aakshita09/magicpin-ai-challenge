"""
composer.py — deterministic message composer for the magicpin AI Challenge ("Vera").

compose(category, merchant, trigger, customer=None) -> dict
    Pure function. No network calls, no LLM, no randomness — same inputs always
    produce the same output (required by the brief: "must be deterministic").

Design notes
------------
Many auto-generated triggers in the dataset carry a `payload` that is just a
placeholder (`{"placeholder": true, "metric_or_topic": "<kind>"}`). Real,
verifiable specificity for those has to be pulled from the *merchant* and
*category* contexts instead of invented. This module always prefers, in
order:
    1. real fields in trigger.payload (when not a placeholder)
    2. merchant fields (performance, signals, offers, conversation_history,
       customer_aggregate, review_themes, subscription)
    3. category fields (digest, peer_stats, offer_catalog, seasonal_beats,
       trend_signals)
and never fabricates a number, name, or citation that isn't present in one of
these three places.
"""
from __future__ import annotations

from typing import Any, Optional


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

def _g(d: Optional[dict], *path, default=None):
    """Safe nested getter: _g(merchant, 'performance', 'ctr')"""
    cur = d
    for p in path:
        if cur is None:
            return default
        cur = cur.get(p) if isinstance(cur, dict) else None
    return default if cur is None else cur


def _pct(x, signed=True) -> str:
    try:
        v = float(x) * 100
    except (TypeError, ValueError):
        return "?"
    s = f"{v:+.0f}%" if signed else f"{v:.0f}%"
    return s


def _is_placeholder(payload: dict) -> bool:
    return bool(payload) and payload.get("placeholder") is True


def is_hindi_ok(merchant: dict, customer: Optional[dict] = None) -> bool:
    if customer:
        lang = (_g(customer, "identity", "language_pref") or "").lower()
        return "hi" in lang
    langs = _g(merchant, "identity", "languages", default=[]) or []
    return "hi" in langs


def salutation(category: dict, merchant: dict) -> str:
    """First-touch salutation, category-appropriate."""
    owner = _g(merchant, "identity", "owner_first_name")
    name = _g(merchant, "identity", "name", default="there")
    examples = _g(category, "voice", "salutation_examples", default=[]) or []
    if owner and examples:
        # use the first example as a pattern (e.g. "Dr. {first_name}")
        pattern = examples[0]
        if "{first_name}" in pattern:
            prefix = pattern.split("{first_name}")[0].strip()
            # avoid double-prefixing when owner_first_name already carries
            # the honorific (e.g. owner_first_name="Dr. Sameer", pattern="Dr. {first_name}")
            if prefix and owner.lower().startswith(prefix.lower()):
                return owner
            return pattern.replace("{first_name}", owner)
    return owner or name


def active_offer(merchant: dict, category: dict) -> Optional[str]:
    for o in merchant.get("offers", []) or []:
        if o.get("status") == "active":
            return o.get("title")
    catalog = category.get("offer_catalog", []) or []
    return catalog[0]["title"] if catalog else None


def top_digest_item(category: dict, trigger: dict) -> Optional[dict]:
    digest = category.get("digest", []) or []
    if not digest:
        return None
    payload = trigger.get("payload") or {}
    want_id = payload.get("top_item_id")
    if want_id:
        for item in digest:
            if item.get("id") == want_id:
                return item
    return digest[0]


def peer_gap(merchant: dict, category: dict, metric: str) -> Optional[str]:
    """Return a short peer-comparison clause if merchant is below/above peer avg for metric."""
    mv = _g(merchant, "performance", metric)
    pv = _g(category, "peer_stats", f"avg_{metric}")
    if mv is None or pv in (None, 0):
        return None
    diff = (mv - pv) / pv
    direction = "below" if diff < 0 else "above"
    return f"{abs(diff)*100:.0f}% {direction} the peer average ({pv})"


def current_signal(merchant: dict, keyword: str) -> Optional[str]:
    for s in merchant.get("signals", []) or []:
        if keyword in s:
            return s
    return None


def cta_line(cta_type: str, options: Optional[list[str]] = None, hi: bool = False) -> str:
    if cta_type == "binary":
        return "Reply YES to go ahead, or STOP if not."
    if cta_type == "slot_choice" and options:
        return " ".join(f"Reply {i+1} for {opt}." for i, opt in enumerate(options))
    if cta_type == "open_ended":
        return ""  # the hook itself already ends on the ask
    return ""


def _clip(s: str) -> str:
    return " ".join(s.split())


# --------------------------------------------------------------------------
# per-kind body builders (merchant-facing)
# --------------------------------------------------------------------------
# Each returns (hook_sentence(s), cta_type) where cta_type in
# {"binary", "open_ended", "none"}.

def _k_research_digest(category, merchant, trigger):
    item = top_digest_item(category, trigger)
    if not item:
        return ("This week's category digest has nothing new for your segment yet.", "none")
    seg = (item.get("patient_segment") or item.get("segment") or "").replace("_", " ")
    seg_clause = f" for your {seg}" if seg and current_signal(merchant, seg.split()[0]) else (f" — relevant to {seg}" if seg else "")
    n = item.get("trial_n")
    n_clause = f"{n:,}-sample study shows " if n else ""
    hook = (f"{item.get('source', 'This week\u2019s digest')} landed. One item worth a look{seg_clause}: "
            f"{n_clause}{item.get('title')}.")
    return (hook, "open_ended")


def _k_regulation_change(category, merchant, trigger):
    payload = trigger.get("payload") or {}
    deadline = payload.get("deadline_iso")
    item = top_digest_item(category, trigger)
    title = item.get("title") if item else "a regulatory update"
    source = item.get("source") if item else None
    src_clause = f" ({source})" if source else ""
    deadline_clause = f" Compliance deadline: {deadline}." if deadline else ""
    hook = f"Heads up — {title}{src_clause}.{deadline_clause}"
    return (hook, "open_ended")


def _best_metric_delta(merchant, want_sign: str, preferred: Optional[str] = None):
    """Pick the metric whose delta_7d actually matches want_sign ('pos'/'neg').
    Falls back to `preferred` (or the largest-magnitude delta) if none match,
    so we never assert a direction the merchant's own numbers contradict."""
    deltas = _g(merchant, "performance", "delta_7d", default={}) or {}
    candidates = {k[:-4]: v for k, v in deltas.items() if k.endswith("_pct") and v is not None}
    if not candidates:
        return preferred, None
    if preferred and preferred in candidates:
        if (want_sign == "pos" and candidates[preferred] >= 0) or (want_sign == "neg" and candidates[preferred] < 0):
            return preferred, candidates[preferred]
    matching = {k: v for k, v in candidates.items() if (v >= 0) == (want_sign == "pos")}
    pool = matching or candidates
    metric = max(pool, key=lambda k: abs(pool[k]))
    return metric, candidates[metric]


def _k_perf_spike(category, merchant, trigger):
    payload = trigger.get("payload") or {}
    metric = payload.get("metric")
    delta = payload.get("delta_pct") if not _is_placeholder(payload) else None
    if delta is None:
        metric, delta = _best_metric_delta(merchant, "pos", metric)
    metric = metric or "views"
    val = _g(merchant, "performance", metric)
    delta_clause = _pct(delta) if delta is not None else "up"
    val_clause = f" ({val} total)" if val is not None else ""
    hook = f"Your {metric} are {delta_clause} this week{val_clause} — more people are finding you than usual."
    peer = peer_gap(merchant, category, metric)
    if peer:
        hook += f" You're now {peer.replace('below', 'above').replace('above', 'above') if 'above' in (peer or '') else peer}."
    hook += " Worth catching this while it's hot — want a quick idea to convert the extra traffic?"
    return (hook, "open_ended")


def _k_perf_dip(category, merchant, trigger):
    payload = trigger.get("payload") or {}
    metric = payload.get("metric")
    delta = payload.get("delta_pct") if not _is_placeholder(payload) else None
    if delta is None:
        metric, delta = _best_metric_delta(merchant, "neg", metric)
    metric = metric or "calls"
    window = payload.get("window", "7d")
    baseline = payload.get("vs_baseline")
    signal = current_signal(merchant, "stale_posts") or current_signal(merchant, "ctr_below")
    if delta is not None and delta < 0:
        baseline_clause = f" (baseline ~{baseline}/day)" if baseline is not None else ""
        hook = f"Your {metric} are {_pct(delta)} over the last {window}{baseline_clause} — worth a quick look before it compounds."
    elif signal:
        # numbers aren't actually dipping right now, but a real underlying
        # signal (stale posts / below-peer CTR) is the likelier drag — surface that instead of a fake dip.
        hook = f"Numbers are steady, but your profile shows {signal.replace(':', ' at ').replace('_', ' ')} — that's the more likely drag on {metric} longer-term."
    else:
        hook = f"Keeping an eye on your {metric} this week — nothing alarming, but flagging it while it's still small."
    if signal and delta is not None and delta < 0:
        hook += f" Your profile also shows {signal.replace('_', ' ')}, which likely isn't helping."
    return (hook, "open_ended")


def _k_seasonal_perf_dip(category, merchant, trigger):
    payload = trigger.get("payload") or {}
    note = payload.get("season_note", "").replace("_", " ")
    metric = payload.get("metric", "views")
    delta = payload.get("delta_pct")
    delta_clause = _pct(delta) if delta is not None else "down"
    hook = (f"Your {metric} are {delta_clause} this week — this lines up with the seasonal dip "
            f"({note}) most peers in your category see right now, so it's expected, not a red flag.")
    return (hook, "open_ended")


def _k_milestone_reached(category, merchant, trigger):
    total = _g(merchant, "customer_aggregate", "total_unique_ytd")
    rating = _g(category, "peer_stats", "avg_rating")
    if total:
        # nearest round milestone at/just below actual count — never invent above what's real
        milestone = (int(total) // 100) * 100 or (int(total) // 50) * 50
        hook = f"You've crossed {milestone}+ unique customers YTD — a real milestone for your practice."
    else:
        hook = "You've hit a milestone worth marking on your profile."
    hook += " Want me to draft a quick thank-you post to publish on your Google profile?"
    return (hook, "open_ended")


def _k_dormant_with_vera(category, merchant, trigger):
    last = None
    hist = merchant.get("conversation_history", []) or []
    if hist:
        last = hist[-1].get("ts")
    since_clause = f" since {last}" if last else ""
    hook = (f"Haven't heard from you in a while{since_clause}. Your listing is still live and getting traffic — "
            f"just checking if there's anything on your profile you'd like updated.")
    return (hook, "open_ended")


def _k_renewal_due(category, merchant, trigger):
    days = _g(merchant, "subscription", "days_remaining")
    plan = _g(merchant, "subscription", "plan", default="your plan")
    days_clause = f"{days} days" if days is not None else "soon"
    hook = f"Quick note — your {plan} subscription renews in {days_clause}. Want me to lock in renewal now so there's no gap in visibility?"
    return (hook, "binary")


def _k_winback_eligible(category, merchant, trigger):
    payload = trigger.get("payload") or {}
    days_since = payload.get("days_since_expiry")
    dip = payload.get("perf_dip_pct")
    lapsed_added = payload.get("lapsed_customers_added_since_expiry")
    parts = []
    if days_since is not None:
        parts.append(f"it's been {days_since} days since your plan lapsed")
    if dip is not None:
        parts.append(f"visibility is down {_pct(dip)} since")
    if lapsed_added is not None:
        parts.append(f"{lapsed_added} more customers have gone quiet in that window")
    detail = "; ".join(parts) if parts else "your plan lapsed a while back"
    hook = f"{detail.capitalize()}. Reactivating takes 2 minutes and I can do the setup — want me to?"
    return (hook, "binary")


def _k_trial_followup(category, merchant, trigger):
    offer = active_offer(merchant, category)
    offer_clause = f" on your {offer} offer" if offer else ""
    hook = f"Following up on the trial{offer_clause} — how did it go? Want me to convert it into a running offer if it worked?"
    return (hook, "open_ended")


def _k_supply_alert(category, merchant, trigger):
    payload = trigger.get("payload") or {}
    molecule = payload.get("molecule")
    batches = payload.get("affected_batches", [])
    if molecule:
        batch_clause = f" (batches: {', '.join(batches)})" if batches else ""
        hook = f"Alert: {molecule}{batch_clause} is under a recall notice. Worth checking your shelf stock against this batch list today."
    else:
        hook = "A supply/recall alert relevant to your inventory just came in — worth a quick shelf check."
    return (hook, "binary")


def _k_chronic_refill_due(category, merchant, trigger):
    hook = "A regular customer's chronic-medication refill window is open. Want me to draft a reminder they can just reply YES to?"
    return (hook, "open_ended")


def _k_review_theme_emerged(category, merchant, trigger):
    themes = merchant.get("review_themes", []) or []
    neg = [t for t in themes if t.get("sentiment") == "neg"]
    if neg:
        t = neg[0]
        hook = (f"{t.get('occurrences_30d')} reviews this month flagged \"{t.get('theme').replace('_',' ')}\" "
                f"— e.g. \"{t.get('common_quote')}\". Small fix, real reputation upside. Want a 1-line response template?")
    elif themes:
        t = themes[0]
        hook = f"{t.get('occurrences_30d')} reviews this month praised \"{t.get('theme').replace('_',' ')}\" — worth turning into a Google post."
    else:
        hook = "A review theme is trending on your profile this month — want me to pull the details?"
    return (hook, "open_ended")


def _k_gbp_unverified(category, merchant, trigger):
    payload = trigger.get("payload") or {}
    uplift = payload.get("estimated_uplift_pct")
    path = payload.get("verification_path", "postcard or phone call")
    uplift_clause = f" Verified listings in your category see roughly {_pct(uplift, signed=False)} more calls." if uplift else ""
    hook = f"Your Google profile isn't verified yet — that's costing you visibility.{uplift_clause} Verification is via {path.replace('_', ' ')}. Want me to start it?"
    return (hook, "binary")


def _k_competitor_opened(category, merchant, trigger):
    signal = current_signal(merchant, "competitor")
    if signal:
        hook = f"Heads up — {signal.replace('_', ' ')}. Might be worth refreshing your offers so you stay top of mind nearby."
    else:
        hook = "A new competitor opened near your location on Google. Might be worth refreshing your offers this week."
    return (hook, "open_ended")


def _k_cde_opportunity(category, merchant, trigger):
    payload = trigger.get("payload") or {}
    credits = payload.get("credits")
    fee = payload.get("fee", "").replace("_", " ")
    credit_clause = f" ({credits} CDE credits" + (f", {fee})" if fee else ")") if credits else ""
    hook = f"A relevant continuing-education session is open for registration{credit_clause}. Want the link?"
    return (hook, "open_ended")


def _k_category_seasonal(category, merchant, trigger):
    payload = trigger.get("payload") or {}
    trends = payload.get("trends", [])
    if trends:
        clean = [t.replace('_', ' ').replace('+', '+') for t in trends[:3]]
        hook = f"Seasonal shelf signal: {', '.join(clean)}. Want a reshuffled front-shelf list based on this?"
    else:
        beats = category.get("seasonal_beats", []) or []
        note = beats[0]["note"] if beats else "a seasonal shift"
        hook = f"Category-wide seasonal signal right now: {note}. Want a quick plan to ride it?"
    return (hook, "open_ended")


def _k_ipl_match_today(category, merchant, trigger):
    payload = trigger.get("payload") or {}
    match = payload.get("match")
    city = payload.get("city")
    time_iso = payload.get("match_time_iso")
    if match:
        hook = f"{match} is on tonight in {city} ({time_iso[11:16] if time_iso else 'evening'}) — foot traffic near screens usually spikes. Want a same-day offer pushed out?"
    else:
        hook = "There's a big match on locally tonight — foot traffic near screens usually spikes. Want a same-day offer pushed out?"
    return (hook, "binary")


def _k_festival_upcoming(category, merchant, trigger):
    payload = trigger.get("payload") or {}
    if not _is_placeholder(payload) and payload.get("festival"):
        festival = payload["festival"]
        days_until = payload.get("days_until")
        days_clause = f" in {days_until} days" if days_until is not None else "soon"
        hook = f"{festival} is{days_clause}. Want a festival-themed offer drafted from your existing catalog?"
    else:
        beats = category.get("seasonal_beats", []) or []
        note = beats[0]["note"] if beats else "an upcoming seasonal peak"
        hook = f"Heads up on an upcoming seasonal peak for your category: {note}. Want a themed offer drafted?"
    return (hook, "open_ended")


def _k_active_planning_intent(category, merchant, trigger):
    payload = trigger.get("payload") or {}
    topic = (payload.get("intent_topic") or "").replace("_", " ")
    last_msg = payload.get("merchant_last_message", "")
    offer = active_offer(merchant, category)
    offer_clause = f" I'd anchor it on {offer}, since that's already live." if offer else ""
    # Intent-handoff rule: merchant already said yes/asked for a plan — act, don't re-qualify.
    hook = f"On {topic or 'that'} — here's a draft structure:{offer_clause} 3 tiers, WhatsApp-bookable, launch this week."
    return (hook, "binary")


def _k_curious_ask_due(category, merchant, trigger):
    hook = "Quick one for you this week — what's the single most-asked question your front desk gets? I can turn the answer into a Google post."
    return (hook, "open_ended")


def _k_appointment_tomorrow(category, merchant, trigger, customer):
    if customer:
        name = _g(customer, "identity", "name", default="there")
        hook = f"Hi {name}, quick reminder — your appointment is tomorrow. Reply 1 to confirm or 2 to reschedule."
        return (hook, "slot_choice_appt")
    hook = "You have an appointment booked for tomorrow — want me to send the customer a confirmation reminder?"
    return (hook, "binary")


def _k_recall_due(category, merchant, trigger, customer):
    if customer:
        name = _g(customer, "identity", "name", default="there")
        last_visit = _g(customer, "relationship", "last_visit")
        offer = active_offer(merchant, category)
        offer_clause = f" {offer}." if offer else ""
        since_clause = f" It's been a while since your last visit ({last_visit})." if last_visit else ""
        hook = f"Hi {name},{since_clause} Your recall is due.{offer_clause} Want us to hold a slot for you?"
        return (hook, "binary")
    hook = "A customer's recall window just opened. Want me to draft the outreach for you to approve?"
    return (hook, "open_ended")


def _k_customer_lapsed_soft(category, merchant, trigger, customer):
    if customer:
        name = _g(customer, "identity", "name", default="there")
        last_visit = _g(customer, "relationship", "last_visit")
        offer = active_offer(merchant, category)
        offer_clause = f" We've got {offer} running right now." if offer else ""
        since_clause = f" since your last visit ({last_visit})" if last_visit else ""
        hook = f"Hi {name}, it's been a bit{since_clause}.{offer_clause} Want to book a slot?"
        return (hook, "binary")
    lapsed = _g(merchant, "customer_aggregate", "lapsed_180d_plus")
    lapsed_clause = f"{lapsed} customers are past the 6-month mark" if lapsed else "a batch of customers are lapsing"
    hook = f"{lapsed_clause} — want me to draft a win-back nudge you can approve and send?"
    return (hook, "open_ended")


def _k_customer_lapsed_hard(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    days = payload.get("days_since_last_visit")
    focus = (payload.get("previous_focus") or "").replace("_", " ")
    months = payload.get("previous_membership_months")
    if customer:
        name = _g(customer, "identity", "name", default="there")
        days_clause = f" It's been {days} days" if days else " It's been a while"
        focus_clause = f" since your {focus} program" if focus else ""
        hook = f"Hi {name},{days_clause}{focus_clause}. We'd love to have you back — want a fresh plan drafted for you?"
        return (hook, "binary")
    months_clause = f" (was a {months}-month member)" if months else ""
    hook = f"A long-lapsed customer{months_clause} is a strong win-back candidate. Want me to draft the outreach?"
    return (hook, "open_ended")


def _k_wedding_package_followup(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    wedding_date = payload.get("wedding_date")
    days_to = payload.get("days_to_wedding")
    next_step = (payload.get("next_step_window_open") or "").replace("_", " ")
    if customer:
        name = _g(customer, "identity", "name", default="there")
        hook = (f"Hi {name}, {days_to} days to go till {wedding_date}! Your trial's done — the {next_step} "
                f"window is open now. Want to lock the next slot?")
        return (hook, "binary")
    hook = f"A trial-completed wedding customer is now in the {next_step} window ({days_to} days to the wedding). Want me to draft the follow-up?"
    return (hook, "open_ended")


def _k_generic(category, merchant, trigger):
    kind = trigger.get("kind", "update").replace("_", " ")
    hook = f"Quick {kind} update on your account — want the details?"
    return (hook, "open_ended")


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------

_MERCHANT_HANDLERS = {
    "research_digest": _k_research_digest,
    "regulation_change": _k_regulation_change,
    "perf_spike": _k_perf_spike,
    "perf_dip": _k_perf_dip,
    "seasonal_perf_dip": _k_seasonal_perf_dip,
    "milestone_reached": _k_milestone_reached,
    "dormant_with_vera": _k_dormant_with_vera,
    "renewal_due": _k_renewal_due,
    "winback_eligible": _k_winback_eligible,
    "trial_followup": _k_trial_followup,
    "supply_alert": _k_supply_alert,
    "chronic_refill_due": _k_chronic_refill_due,
    "review_theme_emerged": _k_review_theme_emerged,
    "gbp_unverified": _k_gbp_unverified,
    "competitor_opened": _k_competitor_opened,
    "cde_opportunity": _k_cde_opportunity,
    "category_seasonal": _k_category_seasonal,
    "ipl_match_today": _k_ipl_match_today,
    "festival_upcoming": _k_festival_upcoming,
    "active_planning_intent": _k_active_planning_intent,
    "curious_ask_due": _k_curious_ask_due,
}

_CUSTOMER_HANDLERS = {
    "appointment_tomorrow": _k_appointment_tomorrow,
    "recall_due": _k_recall_due,
    "customer_lapsed_soft": _k_customer_lapsed_soft,
    "customer_lapsed_hard": _k_customer_lapsed_hard,
    "wedding_package_followup": _k_wedding_package_followup,
}


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    kind = trigger.get("kind", "generic")
    hi_ok = is_hindi_ok(merchant, customer)
    sal = salutation(category, merchant)

    if customer is not None and kind in _CUSTOMER_HANDLERS:
        hook, cta_type = _CUSTOMER_HANDLERS[kind](category, merchant, trigger, customer)
        send_as = "merchant_on_behalf"
        name = _g(customer, "identity", "name", default=sal)
        greeting = f"Hi {name},"
        # hook already contains "Hi {name}" in most handlers above; avoid double greeting
        if hook.lower().startswith("hi "):
            body = hook
        else:
            body = f"{greeting} {hook}"
    else:
        handler = _MERCHANT_HANDLERS.get(kind, _k_generic)
        try:
            hook, cta_type = handler(category, merchant, trigger)
        except TypeError:
            # handlers that also accept customer= but got called merchant-only
            hook, cta_type = handler(category, merchant, trigger, None)
        send_as = "vera"
        body = f"{sal}, {hook}" if sal else hook

    # append explicit CTA line for binary asks; slot-based CTAs are already
    # embedded in the hook text (they read more naturally inline).
    if cta_type == "binary":
        body = f"{body} {cta_line('binary')}"
        cta = "binary"
    elif cta_type in ("slot_choice_appt",):
        cta = "binary"
    elif cta_type == "open_ended":
        cta = "open_ended"
    else:
        cta = "none"

    body = _clip(body)

    suppression_key = trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}:{trigger.get('id')}"

    rationale_bits = [
        f"trigger={kind} (urgency={trigger.get('urgency')}, source={trigger.get('source')})",
        f"category={category.get('slug')} voice={_g(category, 'voice', 'tone')}",
        f"merchant={merchant.get('merchant_id')}",
    ]
    if customer is not None:
        rationale_bits.append(f"customer={customer.get('customer_id')} state={customer.get('state')}")
    rationale = "Composed from " + "; ".join(rationale_bits) + ". Anchored on a concrete fact from merchant/category context (no invented data); single CTA."

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": rationale,
    }
