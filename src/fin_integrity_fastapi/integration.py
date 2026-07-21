"""FastAPI helpers for fin-integrity: a lifespan initialiser and a drop-in
Stripe webhook router."""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

from fin_integrity import get_client, init

logger = logging.getLogger(__name__)

# Stripe event type -> (fin-integrity type, status)
_TYPES = {
    "charge.succeeded": ("payment", "succeeded"),
    "payment_intent.succeeded": ("payment", "succeeded"),
    "charge.refunded": ("refund", "succeeded"),
}

_DISPUTE_EVENTS = (
    "charge.dispute.created",
    "charge.dispute.closed",
    "charge.dispute.updated",
)

# Stripe dispute status -> fin-integrity status. The server's enum accepts
# exactly needs_response | under_review | won | lost and rejects anything else
# at insert, so an unmapped status is skipped rather than sent and dropped.
# Only `lost` is settled money-out.
_DISPUTE_STATUSES = {
    "warning_needs_response": "needs_response",
    "warning_under_review": "under_review",
    "needs_response": "needs_response",
    "under_review": "under_review",
    "won": "won",
    "lost": "lost",
    # `charge_refunded` is deliberately absent: that dispute ended because the
    # charge was refunded, and the refund already books the money out via
    # charge.refunded. Recording it here too would count the same money twice.
}

_SUBSCRIPTION_EVENTS = (
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
)

# Stripe subscription status -> fin-integrity status (active | past_due |
# canceled | paused | trialing).
_SUBSCRIPTION_STATUSES = {
    "active": "active",
    "past_due": "past_due",
    # Retries exhausted. Still a live container owed money, which is exactly
    # what past_due means to reconciliation.
    "unpaid": "past_due",
    "canceled": "canceled",
    "paused": "paused",
    "trialing": "trialing",
    # incomplete / incomplete_expired are absent on purpose: billing never
    # started, so there is no period a charge is owed for.
}


def _amount(obj):
    """Amount in minor units, or None when the payload carries none.

    Stripe amounts are already integer minor units.

    `amount_received` is tested by key presence, not truthiness: a payment
    intent that says `amount_received: 0` is the processor telling us nothing
    was captured. Falling through to `amount` there would report the *intended*
    amount as money we received — a fabricated clean event.

    `amount_refunded` is tested by truthiness on purpose: a Charge object
    always carries it (0 on charge.succeeded), so key presence would zero out
    every payment.
    """
    if "amount_received" in obj:
        return obj["amount_received"]
    if obj.get("amount_refunded"):
        return obj["amount_refunded"]
    # No default: silently defaulting money to 0 in a reconciliation tool
    # fabricates a clean-looking zero event instead of surfacing the problem.
    return obj.get("amount")


def _id_of(value):
    """Stripe sends a bare id, or the expanded object when the caller expands."""
    if isinstance(value, dict):
        return value.get("id")
    return value


def _reference(obj, fallback=None):
    """Cross-side match key: metadata.reference, else the charge this event acts
    on (so a dispute/invoice joins the same reference as its charge), else the
    object's own id."""
    reference = (obj.get("metadata") or {}).get("reference")
    return reference or fallback or obj["id"]


def _unix_to_datetime(value):
    """Stripe period bounds are UNIX seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _price_of(obj):
    """items.data[0].price — every hop guarded; real payloads have surprises."""
    items = (obj.get("items") or {}).get("data") or []
    if not items or not isinstance(items[0], dict):
        return {}
    return items[0].get("price") or {}


def _environment_for(event, override=None):
    """Environment to tag captured events with. A callable receives the Stripe
    event; a string pins one; otherwise Stripe's own livemode decides (live ->
    "production", test -> "test") so the two never reconcile against each other."""
    if callable(override):
        return override(event)
    if isinstance(override, str):
        return override
    return "production" if event.get("livemode") else "test"


def _record(fi, event, environment=None):
    """Map one Stripe event onto fin-integrity. Unknown events are ignored."""
    event_type = event["type"]
    obj = event["data"]["object"]

    mapped = _TYPES.get(event_type)
    if mapped:
        amount = _amount(obj)
        if amount is None:
            logger.warning(
                "fin-integrity: %s %s carries no amount — skipped",
                event_type, obj.get("id"),
            )
            return
        fi.processor.record(
            type=mapped[0],
            source="stripe",
            reference=_reference(obj),
            external_id=obj["id"],
            amount_minor=amount,
            currency=obj.get("currency", "usd"),
            status=mapped[1],
            environment=environment,
        )
        return

    if event_type in _DISPUTE_EVENTS:
        status = _DISPUTE_STATUSES.get(obj.get("status"))
        if status is None:
            logger.warning(
                "fin-integrity: dispute %s has unmappable status %r — skipped",
                obj.get("id"), obj.get("status"),
            )
            return
        amount = _amount(obj)
        if amount is None:
            logger.warning(
                "fin-integrity: dispute %s carries no amount — skipped", obj.get("id"),
            )
            return
        charge = _id_of(obj.get("charge"))
        fi.processor.record(
            type="dispute",
            source="stripe",
            reference=_reference(obj, charge),
            external_id=obj["id"],
            amount_minor=amount,
            currency=obj.get("currency", "usd"),
            status=status,
            parent_external_id=charge,
            environment=environment,
        )
        return

    if event_type in _SUBSCRIPTION_EVENTS:
        status = _SUBSCRIPTION_STATUSES.get(obj.get("status"))
        if status is None:
            logger.warning(
                "fin-integrity: subscription %s has unmappable status %r — skipped",
                obj.get("id"), obj.get("status"),
            )
            return
        price = _price_of(obj)
        amount = price.get("unit_amount")
        if amount is None:
            logger.warning(
                "fin-integrity: subscription %s carries no price amount — skipped",
                obj.get("id"),
            )
            return
        fi.processor.record_subscription(
            source="stripe",
            external_id=obj["id"],
            amount_minor=amount,
            currency=price.get("currency") or obj.get("currency") or "usd",
            status=status,
            interval=(price.get("recurring") or {}).get("interval"),
            current_period_start=_unix_to_datetime(obj.get("current_period_start")),
            current_period_end=_unix_to_datetime(obj.get("current_period_end")),
            environment=environment,
        )
        return

    if event_type == "invoice.paid":
        subscription = _id_of(obj.get("subscription"))
        if not subscription:
            # A one-off invoice is not a subscription charge; its money already
            # arrives via charge.succeeded.
            return
        amount = obj.get("amount_paid")
        if amount is None:
            logger.warning(
                "fin-integrity: invoice %s carries no amount_paid — skipped",
                obj.get("id"),
            )
            return
        # This is the subscription's actual charge. It MUST carry
        # subscription_id: reconciliation's missing_subscription_charge rule
        # matches subscriptions against payments tagged with their external_id,
        # so an untagged renewal charge makes every subscription look unpaid.
        #
        # external_id is the invoice's own id so this event gets its own
        # idempotency key and is never dropped as a duplicate of the underlying
        # charge.succeeded; `reference` still resolves to that same charge, so
        # the two collapse onto one financial_events row rather than counting
        # the renewal twice.
        fi.processor.record(
            type="payment",
            source="stripe",
            reference=_reference(obj, _id_of(obj.get("charge"))),
            external_id=obj["id"],
            amount_minor=amount,
            currency=obj.get("currency", "usd"),
            status="succeeded",
            subscription_id=subscription,
            environment=environment,
        )


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


def create_stripe_webhook_router(secret, path="/webhooks/stripe", environment=None):
    """APIRouter with a POST endpoint that verifies the Stripe signature and
    records processor-side payment/refund/dispute events and subscriptions.

    `environment` tags captured events: a string pins one, a callable receives
    the Stripe event, and the default derives it from Stripe's livemode."""
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

        fi = get_client()
        if fi:
            _record(fi, event, _environment_for(event, environment))
        return Response(status_code=200)

    return router
