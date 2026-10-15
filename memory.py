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
_sync_lock = threading.Lock()


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
    human = next((e for e in reversed(case.get("events", []))
                  if e["event"] in ("human_approved", "human_rejected")), None)
    who, cid = ev.get("requester_email"), case["case_id"]
    # One short fact per line so Cognee's extractor turns each into its own node + edge
    facts = [
        f"Customer {who} opened refund case {cid}.",
        f"Refund case {cid} is for order {ev.get('order_id')} ({ev.get('item')}).",
        f"Refund case {cid} reason is {f.get('reason')}.",
        f"Order {ev.get('order_id')} was paid {ev.get('amount_paid_pence')}p by {ev.get('payment_email')} "
        f"on {ev.get('order_date')} via Stripe payment {ev.get('payment_intent')}.",
        f"The agent decided {d.get('action')} for refund case {cid} under clause {d.get('clause')}.",
        f"Clause {d.get('clause')} says: {d.get('clause_text')}",
        f"Rationale for refund case {cid}: {d.get('rationale')}",
    ]
    if human:
        verb = "approved" if human["event"] == "human_approved" else "rejected"
        facts += [f"{human.get('approver')} {verb} refund case {cid} at {human['ts']}.",
                  f"STATUS UPDATE: refund case {cid} changed from awaiting_approval to {case.get('status')}. "
                  f"This replaces the earlier awaiting_approval record for {who}."]
    elif d.get("action") == "auto_refund":
        facts.append(f"The agent auto-refunded refund case {cid} without a human, as clause R1 allows.")
    facts.append(f"Final status of refund case {cid} is {case.get('status')}.")
    if refund.get("refund_id"):
        facts.append(f"Customer {who} received Stripe refund {refund['refund_id']} of {refund.get('amount_pence')}p "
                     f"for refund case {cid}.")
    else:
        facts.append(f"Customer {who} received no refund for refund case {cid}; no money moved.")
    return "\n".join(facts)


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


def _synced_file():
    return f".cognee_synced_{dataset()}.json"


def _mark_synced(case_id, version):
    with _sync_lock:
        try:
            synced = json.load(open(_synced_file()))
        except (FileNotFoundError, json.JSONDecodeError):
            synced = {}
        synced[case_id] = version
        json.dump(synced, open(_synced_file(), "w"))


def remember_case(case):
    """Queue a case for memory; it is marked synced only once Cognee confirms the write."""
    if not USE_COGNEE:
        return
    version = len(case.get("events", []))
    fut = _submit(_remember(case_summary(case)))
    fut.add_done_callback(lambda f: print("cognee remember error:", repr(f.exception())) if f.exception()
                          else _mark_synced(case["case_id"], version))
    _pending.append(fut)


def backfill(cases):
    """Replay any decision whose memory write never completed (e.g. the server restarted mid-write)."""
    try:
        synced = json.load(open(_synced_file()))
    except (FileNotFoundError, json.JSONDecodeError):
        synced = {}
    missing = [c for c in cases.values() if synced.get(c["case_id"]) != len(c.get("events", []))]
    for case in missing:
        remember_case(case)
    return len(missing)


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


def render_graph(path):
    """Render this batch's Cognee knowledge graph to a self-contained HTML file."""
    import cognee
    return _submit(cognee.visualize_graph(path, dataset=dataset(), full=True)).result(timeout=120)


def audit_log():
    from agent import load_cases
    return "\n".join(case_summary(c) for c in load_cases().values())
