# fin-integrity-fastapi

FastAPI integration for [**fin-integrity**](https://github.com/HatemSaadallah/fin-integrity-python) — reconciliation-as-you-code. A lifespan initialiser wires up the client for you, and a drop-in router records processor-side Stripe events automatically.

## Install

```bash
pip install fin-integrity-fastapi
```

## Setup

```python
import os
from fastapi import FastAPI
from fin_integrity_fastapi import (
    create_stripe_webhook_router,
    finintegrity_lifespan,
    get_client,
)

app = FastAPI(
    lifespan=finintegrity_lifespan(
        api_key=os.environ["FIN_INTEGRITY_KEY"],
        environment="production",
    ),
)

# Records charge.succeeded / payment_intent.succeeded / charge.refunded
# on the processor side. Point a Stripe webhook at POST /webhooks/stripe.
app.include_router(
    create_stripe_webhook_router(secret=os.environ["STRIPE_WEBHOOK_SECRET"]),
)
```

The lifespan initialises the singleton on startup and drains queued events on shutdown (fail-open — nothing here blocks your app).

## Record your ledger writes

Wherever you post to your books, record the ledger side with the **same reference**:

```python
from fin_integrity_fastapi import get_client

get_client().ledger.record(
    type="payment",
    reference=order.reference,
    external_id=journal_entry.id,
    amount_minor=order.total_minor,
    currency=order.currency,
)
```

fin-integrity matches the two sides by `reference` + `type`, then compares amount and currency — surfacing missing entries, duplicates, missing refunds, and mismatches as incidents.

> Tip: set `metadata={"reference": order.reference}` on the Stripe PaymentIntent so both sides share the same key. Otherwise the Stripe object id is used.

## License

MIT © fin-integrity
