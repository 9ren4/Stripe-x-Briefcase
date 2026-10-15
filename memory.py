"""Agent memory: policy + every decision, so the agent can cite why it paid.

Local mode (default) keeps the audit records on disk. With USE_COGNEE=1 every decision is also written to
Cognee's knowledge graph and questions are answered from Cognee search first.
"""
import asyncio
import json
import os
import threading

USE_COGNEE = os.environ.get("USE_COGNEE") == "1"
DATASET = "refundpilot"


def case_summary(case):
    d, ev, f = case.get("decision", {}), case.get("evidence", {}), case.get("facts", {})
    refund = case.get("stripe", {})
    approvals = [e for e in case.get("events", []) if e["event"] in ("human_approved", "human_rejected")]
    return (
        f"Case {case['case_id']} | status {case.get('status')} | order {ev.get('order_id')} ({ev.get('item')}) | "
        f"requester {ev.get('requester_email')} | payer {ev.get('payment_email')} | reason {f.get('reason')} | "
        f"paid {ev.get('amount_paid_pence')}p on {ev.get('order_date')} (age {ev.get('order_age_days')}d) | "
        f"customer refunds in window {ev.get('customer_refunds_last_window')} | "
        f"auto-refunded today before this {ev.get('auto_refunded_today_pence')}p | "
        f"decision {d.get('action')} under clause {d.get('clause')}: \"{d.get('clause_text')}\" | "
        f"rationale: {d.get('rationale')} | amount {d.get('amount_pence')}p | "
        f"stripe refund {refund.get('refund_id')} on {ev.get('payment_intent')} | "
        f"human actions: {json.dumps(approvals)}"
    )


def _run(coro):
    return asyncio.run(coro)


def _cognee_remember(text):
    import cognee
    _run(cognee.add(text, dataset_name=DATASET))
    _run(cognee.cognify(datasets=[DATASET]))


def remember_case(case):
    if USE_COGNEE:
        # cognify is slow (LLM calls), keep it off the request path
        threading.Thread(target=_safe, args=(_cognee_remember, case_summary(case)), daemon=True).start()


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        print("cognee error:", repr(e))


def seed_policy(policy):
    if USE_COGNEE:
        text = "\n".join(f"Policy {policy['name']} clause {k}: {v}" for k, v in policy["clauses"].items())
        threading.Thread(target=_safe, args=(_cognee_remember, text), daemon=True).start()


def _cognee_search(question):
    import cognee
    from cognee import SearchType
    results = _run(cognee.search(query_text=question, query_type=SearchType.GRAPH_COMPLETION, datasets=[DATASET]))
    return "\n".join(str(r) for r in results)


def recall(question):
    from agent import load_cases
    local = "\n".join(case_summary(c) for c in load_cases().values())
    if USE_COGNEE:
        graph = _safe(_cognee_search, question)
        if graph:
            return f"[Cognee memory]\n{graph}\n\n[Audit log]\n{local}"
    return local
