"""RefundPilot: an agent that refunds customers under a written policy and can explain every pound."""
import json
import os
import re
import threading
from datetime import date, datetime, timezone

import anthropic
import stripe
from dotenv import load_dotenv

import memory

load_dotenv()
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
MODEL = "claude-opus-5-5"
CASES_FILE = "cases.json"
_lock = threading.Lock()

POLICY = json.load(open("policy.json"))
P = POLICY["params"]
CLAUSES = POLICY["clauses"]


def gbp(pence):
    return f"£{pence / 100:,.2f}"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_batch():
    try:
        return open(".seed_batch").read().strip()
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------- case store (audit trail)

def load_cases():
    try:
        return json.load(open(CASES_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_case(case):
    with _lock:
        cases = load_cases()
        cases[case["case_id"]] = case
        json.dump(cases, open(CASES_FILE, "w"), indent=2)


def log(case, event, **detail):
    case.setdefault("events", []).append({"ts": now_iso(), "event": event, **detail})


# ---------------------------------------------------------------- 1. understand the email (Claude)

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": ["string", "null"], "description": "Order id like ORD-1234, or null"},
        "requested_amount_pence": {"type": ["integer", "null"],
                                   "description": "Amount asked for in pence if stated, else null (= full refund)"},
        "reason": {"type": "string",
                   "enum": ["damaged", "not_delivered", "wrong_item", "duplicate_charge", "changed_mind", "other"]},
        "summary": {"type": "string", "description": "One sentence summary of the request"},
    },
    "required": ["order_id", "requested_amount_pence", "reason", "summary"],
    "additionalProperties": False,
}


def extract(msg):
    """Turn a free-text email into structured facts. Claude only reads; it never decides or moves money."""
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
            system="You extract structured refund-request facts from customer emails. "
                   "Classify the reason faithfully; do not decide whether a refund is deserved.",
            messages=[{"role": "user",
                       "content": f"From: {msg['from']}\nSubject: {msg['subject']}\n\n{msg['body']}"}],
        )
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text), "claude"
    except Exception as e:  # keep the demo alive without the LLM
        print("extract fallback:", repr(e))
        return _extract_fallback(msg), "regex-fallback"


def _extract_fallback(msg):
    body = (msg["subject"] + " " + msg["body"]).lower()
    m = re.search(r"ord-\d+", body)
    reason = "other"
    for key, words in {"duplicate_charge": ["twice", "duplicate"], "not_delivered": ["never", "delivered", "showed up"],
                       "wrong_item": ["wrong", "instead"], "damaged": ["damaged", "smashed", "broken", "snapped"],
                       "changed_mind": ["don't like", "changed my mind"]}.items():
        if any(w in body for w in words):
            reason = key
            break
    return {"order_id": m.group(0).upper() if m else None, "requested_amount_pence": None,
            "reason": reason, "summary": msg["subject"]}


# ---------------------------------------------------------------- 2. gather evidence (Stripe)

def meta(obj):
    return obj.metadata.to_dict() if obj.metadata else {}


def find_payment(order_id):
    batch = current_batch()
    for pi in stripe.PaymentIntent.list(limit=100).auto_paging_iter():
        md = meta(pi)
        if md.get("app") == "refundpilot" and md.get("order_id") == order_id and md.get("batch") == batch:
            return pi
    return None


def refunded_on(pi_id):
    return sum(r.amount for r in stripe.Refund.list(payment_intent=pi_id, limit=100).data
               if r.status in ("succeeded", "pending"))


def batch_refunds():
    batch = current_batch()
    return [r for r in stripe.Refund.list(limit=100).auto_paging_iter()
            if meta(r).get("app") == "refundpilot" and meta(r).get("batch") == batch
            and r.status in ("succeeded", "pending")]


def gather_evidence(msg, facts):
    ev = {"requester_email": msg["from"].lower(), "order_id": facts["order_id"], "reason": facts["reason"]}
    pi = find_payment(facts["order_id"]) if facts["order_id"] else None
    if not pi:
        ev["payment_found"] = False
        return ev, None
    md = meta(pi)
    already = refunded_on(pi.id)
    window_cutoff = datetime.now(timezone.utc).timestamp() - P["window_days"] * 86400
    today = date.today().isoformat()
    refunds = batch_refunds()
    ev.update({
        "payment_found": True,
        "payment_intent": pi.id,
        "item": md.get("item"),
        "payment_email": md.get("customer_email", "").lower(),
        "order_date": md.get("order_date"),
        "order_age_days": (date.today() - date.fromisoformat(md["order_date"])).days,
        "amount_paid_pence": pi.amount_received,
        "already_refunded_pence": already,
        "refundable_pence": pi.amount_received - already,
        "customer_refunds_last_window": sum(
            1 for r in refunds if meta(r).get("customer_email") == md.get("customer_email")
            and r.created >= window_cutoff),
        "auto_refunded_today_pence": sum(
            r.amount for r in refunds if meta(r).get("mode") == "auto"
            and datetime.fromtimestamp(r.created, timezone.utc).date().isoformat() == today),
    })
    return ev, pi


# ---------------------------------------------------------------- 3. decide (deterministic policy engine)

def decide(facts, ev):
    """Returns (action, clause, amount_pence, rationale). action in auto_refund | needs_approval | deny."""
    if not ev.get("payment_found"):
        return "deny", "R10", 0, f"No payment found for order {ev.get('order_id')}."
    if ev["requester_email"] != ev["payment_email"]:
        return "deny", "R7", 0, (f"Requester {ev['requester_email']} does not match the payer "
                                 f"{ev['payment_email']} on {ev['payment_intent']}.")
    if facts["reason"] not in P["allowed_reasons"]:
        return "deny", "R9", 0, f"Reason '{facts['reason']}' is not in the allowed list."
    if ev["order_age_days"] > P["window_days"]:
        return "deny", "R4", 0, (f"Order placed {ev['order_age_days']} days ago "
                                 f"({ev['order_date']}), outside the {P['window_days']}-day window.")
    if ev["refundable_pence"] <= 0:
        return "deny", "R6", 0, f"Nothing left to refund on {ev['payment_intent']}."

    amount = facts.get("requested_amount_pence") or ev["refundable_pence"]
    note = ""
    if amount > ev["refundable_pence"]:
        note = f" Capped from {gbp(amount)} to {gbp(ev['refundable_pence'])} under R6."
        amount = ev["refundable_pence"]

    if amount > P["approval_limit_pence"]:
        return "deny", "R3", amount, (f"{gbp(amount)} exceeds the {gbp(P['approval_limit_pence'])} agent ceiling; "
                                      f"routed to the finance lead.{note}")
    if amount > P["auto_limit_pence"]:
        return "needs_approval", "R2", amount, (f"{gbp(amount)} is above the {gbp(P['auto_limit_pence'])} "
                                                f"auto-refund limit.{note}")
    if ev["customer_refunds_last_window"] >= P["max_refunds_per_customer_window"]:
        return "needs_approval", "R5", amount, (f"Customer already received {ev['customer_refunds_last_window']} "
                                                f"refund(s) in the last {P['window_days']} days.{note}")
    if ev["auto_refunded_today_pence"] + amount > P["daily_auto_budget_pence"]:
        return "needs_approval", "R8", amount, (f"Auto-refunded {gbp(ev['auto_refunded_today_pence'])} today; "
                                                f"{gbp(amount)} more would exceed the "
                                                f"{gbp(P['daily_auto_budget_pence'])} daily budget.{note}")
    return "auto_refund", "R1", amount, (f"{gbp(amount)} for '{facts['reason']}' on a {ev['order_age_days']}-day-old "
                                         f"order, within all limits.{note}")


# ---------------------------------------------------------------- 4. act (Stripe)

def execute_refund(case, mode, approver=None):
    ev, d = case["evidence"], case["decision"]
    refund = stripe.Refund.create(
        payment_intent=ev["payment_intent"],
        amount=d["amount_pence"],
        reason="duplicate" if case["facts"]["reason"] == "duplicate_charge" else "requested_by_customer",
        metadata={"app": "refundpilot", "batch": current_batch(), "case_id": case["case_id"],
                  "customer_email": ev["payment_email"], "order_id": ev["order_id"],
                  "clause": d["clause"], "mode": mode, "approver": approver or ""},
        idempotency_key=f"refundpilot-{current_batch()}-{case['case_id']}",
    )
    case["stripe"] = {"refund_id": refund.id, "status": refund.status, "amount_pence": refund.amount,
                      "dashboard": f"https://dashboard.stripe.com/test/payments/{ev['payment_intent']}"}
    case["status"] = "refunded"
    log(case, "refund_issued", refund_id=refund.id, mode=mode, approver=approver, clause=d["clause"])


def process(msg):
    case = {"case_id": f"case_{msg['id']}", "message": msg, "created": now_iso(), "status": "processing"}
    log(case, "email_received", sender=msg["from"])

    facts, extractor = extract(msg)
    case["facts"] = facts
    log(case, "facts_extracted", by=extractor, **facts)

    ev, _ = gather_evidence(msg, facts)
    case["evidence"] = ev
    log(case, "evidence_gathered", source="stripe")

    action, clause, amount, rationale = decide(facts, ev)
    case["decision"] = {"action": action, "clause": clause, "clause_text": CLAUSES[clause],
                        "amount_pence": amount, "rationale": rationale, "policy": POLICY["name"]}
    log(case, "decision", action=action, clause=clause, amount_pence=amount)

    if action == "auto_refund":
        execute_refund(case, mode="auto")
    elif action == "needs_approval":
        case["status"] = "awaiting_approval"
    else:
        case["status"] = "denied"

    save_case(case)
    memory.remember_case(case)
    return case


def approve(case_id, approver):
    case = load_cases()[case_id]
    if case["status"] != "awaiting_approval":
        raise ValueError(f"{case_id} is {case['status']}")
    log(case, "human_approved", approver=approver)
    # Re-check the hard limits at approval time: approval can lift R2/R5/R8, never R3/R6
    ev, _ = gather_evidence(case["message"], case["facts"])
    case["evidence"] = ev
    if case["decision"]["amount_pence"] > ev["refundable_pence"]:
        case["status"] = "denied"
        log(case, "blocked", clause="R6", reason="Payment no longer has enough refundable balance.")
    else:
        execute_refund(case, mode="approved", approver=approver)
    save_case(case)
    memory.remember_case(case)
    return case


def reject(case_id, approver):
    case = load_cases()[case_id]
    case["status"] = "rejected"
    log(case, "human_rejected", approver=approver)
    save_case(case)
    memory.remember_case(case)
    return case


# ---------------------------------------------------------------- 5. explain ("Why did you pay this?")

def explain(question):
    context = memory.recall(question)
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            output_config={"effort": "low"},
            system=(
                "You are the audit voice of RefundPilot, a refunds agent. Answer the finance team's question using "
                "ONLY the audit records and policy provided. For every payment you mention, cite: the case id, the "
                "policy clause id and its wording, the evidence numbers (amounts, dates, emails), the Stripe refund id, "
                "and who approved it (agent auto-approval or the named human). If the records don't answer the "
                "question, say so. Be concise; use short bullet points."),
            messages=[{"role": "user", "content": f"POLICY:\n{json.dumps(POLICY)}\n\nAUDIT RECORDS:\n{context}"
                                                  f"\n\nQUESTION: {question}"}],
        )
        return next(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        return f"(LLM unavailable: {e!r})\n\nRelevant records:\n{context}"
