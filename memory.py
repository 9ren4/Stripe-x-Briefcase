"""Agent memory backed by Cognee: the written policy, every decision, and each customer's history.

The agent uses it to recall a customer's past refunds before deciding, and to answer "why did you pay this?"
from its own memory. With USE_COGNEE=0 it falls back to the local audit log.
"""
import asyncio
import json
import os
import threading

from dotenv import load_dotenv

load_dotenv()
USE_COGNEE = os.environ.get("USE_COGNEE") == "1"

# Cognee is async; run it on one dedicated event loop so Flask threads never race on its databases
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()
_write_lock = None


def _submit(coro):
    return asyncio.run_coroutine_threadsafe(coro, _loop)


def dataset():
    try:
        batch = open(".seed_batch").read().strip()
    except FileNotFoundError:
        batch = "default"
    return f"refundpilot_{batch}"


def case_summary(case):
    d, ev, f = case.get("decision", {}), case.get("evidence", {}), case.get("facts", {})
    refund = case.get("stripe", {})
    humans = [f"{e['event']} by {e.get('approver')} at {e['ts']}" for e in case.get("events", [])
              if e["event"] in ("human_approved", "human_rejected")]
    return (
        f"Refund case {case['case_id']} for customer {ev.get('requester_email')} (payer on file "
        f"{ev.get('payment_email')}), order {ev.get('order_id')} ({ev.get('item')}), reason {f.get('reason')}. "
        f"Paid {ev.get('amount_paid_pence')}p on {ev.get('order_date')} ({ev.get('order_age_days')} days old). "
        f"Decision: {d.get('action')} for {d.get('amount_pence')}p under policy clause {d.get('clause')} "
        f"(\"{d.get('clause_text')}\"). Rationale: {d.get('rationale')} Final status: {case.get('status')}. "
        f"Stripe refund id: {refund.get('refund_id')} on payment {ev.get('payment_intent')}. "
        f"Human actions: {'; '.join(humans) or 'none (agent acted autonomously)'}."
    )


async def _remember(text):
    import cognee
    global _write_lock
    _write_lock = _write_lock or asyncio.Lock()
    async with _write_lock:  # one graph build at a time
        await cognee.remember(text, dataset_name=dataset(), self_improvement=False)


async def _recall(question, include_references=False):
    import cognee
    results = await cognee.recall(question, datasets=[dataset()], top_k=8, include_references=include_references)
    out = []
    for r in results:
        if r.source == "graph":
            out.append(r.text)
        elif r.source == "session":
            out.append(r.answer)
    return "\n".join(t for t in out if t)


_pending = []


def remember(text):
    """Write to Cognee in the background; the agent never waits on a graph build."""
    if USE_COGNEE:
        fut = _submit(_remember(text))
        fut.add_done_callback(lambda f: f.exception() and print("cognee remember error:", repr(f.exception())))
        _pending.append(fut)


def flush(timeout=600):
    """Block until queued memory writes finish (used by seed.py)."""
    for fut in _pending:
        fut.result(timeout=timeout)


def remember_case(case):
    remember(case_summary(case))


def seed_policy(policy):
    """Load the written policy into Cognee once per demo batch."""
    flag = f".cognee_policy_{dataset()}"
    if USE_COGNEE and not os.path.exists(flag):
        remember(f"{policy['name']}. Limits: {json.dumps(policy['params'])}. " +
                 " ".join(f"Clause {k}: {v}" for k, v in policy["clauses"].items()))
        open(flag, "w").close()


def recall(question, include_references=False, timeout=45):
    """Ask Cognee memory. Returns text, or None if memory is off/empty/slow."""
    if not USE_COGNEE:
        return None
    try:
        return _submit(_recall(question, include_references)).result(timeout=timeout) or None
    except Exception as e:
        print("cognee recall error:", repr(e))
        return None


def customer_history(email):
    return recall(f"What previous refunds or refund requests has customer {email} had? "
                  f"List dates, amounts, orders and policy clauses. If none are known, say 'No prior history'.")


def audit_log():
    from agent import load_cases
    return "\n".join(case_summary(c) for c in load_cases().values())
