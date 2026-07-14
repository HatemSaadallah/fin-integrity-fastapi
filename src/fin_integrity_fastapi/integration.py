"""FastAPI helpers for fin-integrity: a lifespan initialiser and a drop-in
Stripe webhook router."""
from contextlib import asynccontextmanager

from fastapi import APIRouter, Request, Response

from fin_integrity import get_client, init

# Stripe event type -> (fin-integrity type, status)
_TYPES = {
    "charge.succeeded": ("payment", "succeeded"),
    "payment_intent.succeeded": ("payment", "succeeded"),
    "charge.refunded": ("refund", "succeeded"),
}


def _amount(obj):
    if obj.get("amount_received"):
        return obj["amount_received"]
    if obj.get("amount_refunded"):
        return obj["amount_refunded"]
    return obj.get("amount", 0)


def finintegrity_lifespan(**config):
    """Return a FastAPI lifespan that inits the client on startup and drains
    it on shutdown.

        app = FastAPI(lifespan=finintegrity_lifespan(api_key=...))
    """

    @asynccontextmanager
    async def lifespan(app):
        try:
            init(**config)
        except Exception:  # pragma: no cover - fail-open on boot
            pass
        try:
            yield
        finally:
            client = get_client()
            if client:
                client.shutdown()

    return lifespan


def create_stripe_webhook_router(secret, path="/webhooks/stripe"):
    """APIRouter with a POST endpoint that verifies the Stripe signature and
    records processor-side payment/refund events."""
    router = APIRouter()

    @router.post(path)
    async def stripe_webhook(request: Request):
        body = await request.body()
        sig = request.headers.get("stripe-signature")
        try:
            import stripe

            event = stripe.Webhook.construct_event(body, sig, secret)
        except ImportError:
            return Response("stripe package not installed", status_code=500)
        except Exception:
            return Response("invalid signature", status_code=400)

        mapped = _TYPES.get(event["type"])
        if mapped:
            obj = event["data"]["object"]
            fi = get_client()
            if fi:
                fi.processor.record(
                    type=mapped[0],
                    source="stripe",
                    reference=obj.get("metadata", {}).get("reference") or obj["id"],
                    external_id=obj["id"],
                    amount_minor=_amount(obj),
                    currency=obj.get("currency", "usd"),
                    status=mapped[1],
                )
        return Response(status_code=200)

    return router
