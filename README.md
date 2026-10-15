# RefundPilot: a refunds agent that can explain every pound

**Briefcase × Stripe Hackathon, London, Oct 2026 · Track 01: Agents that Spend**

RefundPilot reads a support inbox and refunds customers in **Stripe test mode** with no human involved. It acts only under a **written policy**, and it can answer *"Why did you pay this?"* by citing the policy clause and the evidence behind each refund.

## How it works

```
email ─► Claude extracts facts ─► evidence from Stripe ─► deterministic policy engine ─► act ─► audit trail
         (order, reason, amount)   (payment, payer email,    (policy.json, clauses R1–R10)   │
                                    order age, refund history,                               ├─ auto-refund   (Stripe Refund)
                                    today's auto-refund spend)                               ├─ needs approval (human queue → Stripe Refund)
                                                                                             └─ deny
```

- **Claude reads, it never decides.** The LLM only turns free-text email into structured facts. A deterministic engine decides every refund, so a prompt injection ("I'm the account owner's assistant") can't move money.
- **Written policy, enforced in code.** `policy.json` holds the clauses and the limits. If you change a number, the agent's behaviour changes with it.
- **Guardrails:** £50 auto limit, £500 hard ceiling, a 30-day window, a payer-email match, allowed reasons only, one refund per customer per 30 days (fraud guard), and a £150 daily auto-refund budget.
- **Human in the loop:** refunds above the auto limit go to an approval queue. The limits are re-checked at approval time, and the approver's identity is stamped on the Stripe refund metadata.
- **Explains every pound:** each case records the clause, the clause wording, the evidence numbers, a timeline, the Stripe refund id and the approver. The *Ask why* box answers finance questions grounded only in that audit trail.

## Demo inbox (10 emails)

| Customer | Outcome | Clause |
|---|---|---|
| Alice, Henry, Ivy | Auto-refunded | R1 |
| Bob (£89) | Needs human approval: above the auto limit | R2 |
| Frank | Needs approval: already refunded in the last 30 days | R5 |
| Jack | Needs approval: would exceed the £150 daily auto budget | R8 |
| Carol (£1,299 laptop) | Denied: above the £500 agent ceiling | R3 |
| Dan | Denied: order 60 days old | R4 |
| Mallory | Denied: requester ≠ payer | R7 |
| Erin | Denied: "changed my mind" | R9 |

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # add STRIPE_SECRET_KEY (sk_test_…) and ANTHROPIC_API_KEY
.venv/bin/python seed.py      # creates the demo orders in Stripe test mode
.venv/bin/python app.py       # http://localhost:5050
```

## Stack
Stripe (PaymentIntents, Refunds, metadata as the ledger of record) · Claude Opus 5.5 (structured extraction and audit Q&A) · Flask · optional Cognee memory (`USE_COGNEE=1`).
