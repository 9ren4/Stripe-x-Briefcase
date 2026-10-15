"""Create test-mode orders (PaymentIntents) for the demo inbox.

Run once before the demo:  python seed.py
"""
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import stripe
from dotenv import load_dotenv

load_dotenv()
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
assert stripe.api_key.startswith("sk_test_"), "Test mode keys only!"

# order_id, customer email, amount (pence), days ago, item
ORDERS = [
    ("ORD-1001", "alice@example.com", 2499, 3, "Ceramic mug"),
    ("ORD-1002", "bob@example.com", 8900, 5, "Navy winter jacket"),
    ("ORD-1003", "carol@example.com", 129900, 7, "Laptop"),
    ("ORD-1004", "dan@example.com", 4500, 60, "Headphones"),
    ("ORD-1005", "grace@example.com", 3000, 4, "Scarf"),
    ("ORD-1006", "erin@example.com", 3500, 6, "Desk lamp"),
    ("ORD-1007", "frank@example.com", 1950, 2, "Notebook set"),
    ("ORD-1008", "henry@example.com", 4800, 8, "Dining chair"),
    ("ORD-1009", "ivy@example.com", 4900, 14, "Plant pot set"),
    ("ORD-1010", "jack@example.com", 4500, 9, "Backpack"),
    # Frank's earlier order, already refunded -> trips the fraud guard (R5)
    ("ORD-0999", "frank@example.com", 1200, 12, "Pen set"),
]


BATCH = uuid.uuid4().hex[:8]


def create_order(order_id, email, amount, days_ago, item):
    order_date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()
    return stripe.PaymentIntent.create(
        amount=amount,
        currency="gbp",
        # Funds become available immediately, so refunds never fail on balance
        payment_method="pm_card_bypassPending",
        confirm=True,
        automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
        receipt_email=email,
        description=f"{order_id}: {item}",
        metadata={
            "app": "refundpilot",
            "batch": BATCH,
            "order_id": order_id,
            "customer_email": email,
            "order_date": order_date,
            "item": item,
        },
    )


if __name__ == "__main__":
    # The agent only looks at orders from the latest seed batch, so re-seeding gives a clean demo
    with open(".seed_batch", "w") as f:
        f.write(BATCH)
    if os.path.exists("cases.json"):
        os.remove("cases.json")
    print(f"batch {BATCH}")
    for order in ORDERS:
        pi = create_order(*order)
        print(f"{order[0]:9} {pi.id} {pi.status} £{order[2] / 100:.2f}")
        if order[0] == "ORD-0999":
            r = stripe.Refund.create(
                payment_intent=pi.id,
                reason="requested_by_customer",
                metadata={"app": "refundpilot", "batch": BATCH, "customer_email": order[1],
                          "order_id": order[0], "clause": "R1", "seeded": "true"},
            )
            print(f"          prior refund {r.id} for frank")

    import memory  # imported late: it reads .seed_batch to pick this batch's Cognee dataset
    if memory.USE_COGNEE:
        print("loading policy + customer history into Cognee...")
        memory.seed_policy(json.load(open("policy.json")))
        memory.remember(f"Customer frank@example.com previously received a refund of 1200p (£12.00) on order "
                        f"ORD-0999 (Pen set) {ORDERS[-1][3]} days ago, issued automatically under clause R1.")
        memory.flush()
        print("Cognee memory ready")
