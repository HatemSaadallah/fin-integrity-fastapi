import sys
import types
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fin_integrity import init, get_client
from fin_integrity_fastapi import create_stripe_webhook_router


def event(type, obj):
    return {"type": type, "data": {"object": obj}}


class WebhookTestCase(unittest.TestCase):
    def setUp(self):
        # A fresh dry-run client per test, so inspect() only ever shows what
        # this test's request recorded.
        init(dry_run=True)
        self.addCleanup(sys.modules.pop, "stripe", None)

        app = FastAPI()
        app.include_router(create_stripe_webhook_router(secret="whsec_test"))
        self.client = TestClient(app)

    def stub_stripe(self, event=None, raises=None):
        """Stub the stripe module so signature verification returns our event."""
        def construct_event(body, sig, secret):
            if raises is not None:
                raise raises
            return event

        fake = types.ModuleType("stripe")
        fake.Webhook = types.SimpleNamespace(construct_event=construct_event)
        sys.modules["stripe"] = fake

    def post(self):
        return self.client.post(
            "/webhooks/stripe", content=b"{}", headers={"stripe-signature": "sig"},
        )

    def recorded(self):
        return get_client().inspect()

    def post_and_expect_nothing(self, stripe_event):
        """The webhook must still ack, but record no event at all."""
        self.stub_stripe(stripe_event)
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.recorded(), [])


class TestSignature(WebhookTestCase):
    def test_invalid_signature_is_400_and_records_nothing(self):
        self.stub_stripe(raises=ValueError("bad signature"))
        resp = self.post()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.recorded(), [])

    def test_missing_stripe_package_is_500(self):
        sys.modules["stripe"] = None  # makes `import stripe` raise ImportError
        resp = self.post()
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(self.recorded(), [])


class TestPayments(WebhookTestCase):
    def test_charge_succeeded_records_processor_event(self):
        self.stub_stripe(event("charge.succeeded", {
            "id": "ch_1", "amount": 4999, "amount_refunded": 0, "currency": "usd",
            "metadata": {"reference": "order_1"},
        }))
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        ev = self.recorded()[-1]
        self.assertEqual(ev["side"], "processor")
        self.assertEqual(ev["event_type"], "payment")
        self.assertEqual(ev["reference"], "order_1")
        self.assertEqual(ev["external_id"], "ch_1")
        self.assertEqual(ev["status"], "succeeded")
        self.assertEqual(ev["amount"], {"minor": "4999", "currency": "usd"})

    def test_refund_records_amount_refunded(self):
        self.stub_stripe(event("charge.refunded", {
            "id": "ch_2", "amount": 4999, "amount_refunded": 500, "currency": "eur",
            "metadata": {"reference": "order_2"},
        }))
        self.assertEqual(self.post().status_code, 200)
        ev = self.recorded()[-1]
        self.assertEqual(ev["event_type"], "refund")
        self.assertEqual(ev["reference"], "order_2")
        self.assertEqual(ev["amount"], {"minor": "500", "currency": "eur"})

    def test_reference_falls_back_to_object_id(self):
        self.stub_stripe(event("payment_intent.succeeded", {
            "id": "pi_1", "amount_received": 100, "currency": "usd",
        }))
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.recorded()[-1]["reference"], "pi_1")

    def test_amount_received_zero_is_recorded_as_zero_not_intended_amount(self):
        # The processor is telling us nothing was captured. Reporting `amount`
        # instead would fabricate a payment that reconciles clean.
        self.stub_stripe(event("payment_intent.succeeded", {
            "id": "pi_2", "amount": 4999, "amount_received": 0, "currency": "usd",
        }))
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.recorded()[-1]["amount"], {"minor": "0", "currency": "usd"})

    def test_payload_with_no_amount_at_all_is_skipped(self):
        self.post_and_expect_nothing(event("charge.succeeded", {
            "id": "ch_3", "currency": "usd",
        }))


class TestDisputes(WebhookTestCase):
    def test_dispute_lost_records_status_and_parent_charge(self):
        self.stub_stripe(event("charge.dispute.closed", {
            "id": "du_1", "status": "lost", "charge": "ch_5",
            "amount": 4999, "currency": "usd", "metadata": {},
        }))
        self.assertEqual(self.post().status_code, 200)
        ev = self.recorded()[-1]
        self.assertEqual(ev["event_type"], "dispute")
        self.assertEqual(ev["status"], "lost")
        self.assertEqual(ev["external_id"], "du_1")
        self.assertEqual(ev["parent_external_id"], "ch_5")
        # Joins the same reference as the charge it acts on.
        self.assertEqual(ev["reference"], "ch_5")
        self.assertEqual(ev["amount"], {"minor": "4999", "currency": "usd"})

    def test_dispute_won_records_status_won(self):
        self.stub_stripe(event("charge.dispute.closed", {
            "id": "du_2", "status": "won", "charge": "ch_6",
            "amount": 1000, "currency": "usd",
        }))
        self.assertEqual(self.post().status_code, 200)
        ev = self.recorded()[-1]
        self.assertEqual(ev["event_type"], "dispute")
        self.assertEqual(ev["status"], "won")

    def test_dispute_warning_status_maps_onto_valid_enum(self):
        self.stub_stripe(event("charge.dispute.created", {
            "id": "du_3", "status": "warning_needs_response", "charge": "ch_7",
            "amount": 2000, "currency": "usd",
        }))
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.recorded()[-1]["status"], "needs_response")

    def test_dispute_metadata_reference_wins_over_charge(self):
        self.stub_stripe(event("charge.dispute.updated", {
            "id": "du_4", "status": "under_review", "charge": "ch_8",
            "amount": 2000, "currency": "usd", "metadata": {"reference": "order_8"},
        }))
        self.assertEqual(self.post().status_code, 200)
        ev = self.recorded()[-1]
        self.assertEqual(ev["reference"], "order_8")
        self.assertEqual(ev["parent_external_id"], "ch_8")

    def test_unmappable_dispute_status_is_skipped(self):
        # charge_refunded is not in the server's enum, and the money already
        # left via charge.refunded.
        self.post_and_expect_nothing(event("charge.dispute.closed", {
            "id": "du_5", "status": "charge_refunded", "charge": "ch_9",
            "amount": 4999, "currency": "usd",
        }))


class TestSubscriptions(WebhookTestCase):
    def subscription(self, **overrides):
        obj = {
            "id": "sub_1",
            "status": "active",
            "current_period_start": 1700000000,
            "current_period_end": 1702678400,
            "items": {"data": [{"price": {
                "unit_amount": 2500, "currency": "eur",
                "recurring": {"interval": "month"},
            }}]},
        }
        obj.update(overrides)
        return obj

    def test_subscription_created_records_container(self):
        self.stub_stripe(event("customer.subscription.created", self.subscription()))
        self.assertEqual(self.post().status_code, 200)
        ev = self.recorded()[-1]
        self.assertEqual(ev["event_type"], "subscription")
        self.assertEqual(ev["external_id"], "sub_1")
        self.assertEqual(ev["status"], "active")
        self.assertEqual(ev["interval"], "month")
        self.assertEqual(ev["amount"], {"minor": "2500", "currency": "eur"})
        # Stripe sends UNIX seconds; the SDK must receive real timestamps.
        self.assertEqual(ev["current_period_start"], "2023-11-14T22:13:20.000Z")
        self.assertEqual(ev["current_period_end"], "2023-12-15T22:13:20.000Z")

    def test_subscription_deleted_records_canceled(self):
        self.stub_stripe(event(
            "customer.subscription.deleted", self.subscription(status="canceled"),
        ))
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.recorded()[-1]["status"], "canceled")

    def test_unpaid_maps_to_past_due(self):
        self.stub_stripe(event(
            "customer.subscription.updated", self.subscription(status="unpaid"),
        ))
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.recorded()[-1]["status"], "past_due")

    def test_unmappable_subscription_status_is_skipped(self):
        self.post_and_expect_nothing(event(
            "customer.subscription.updated", self.subscription(status="incomplete_expired"),
        ))

    def test_malformed_payload_without_items_does_not_crash(self):
        self.post_and_expect_nothing(event(
            "customer.subscription.updated", self.subscription(items={}),
        ))

    def test_payload_with_empty_items_data_does_not_crash(self):
        self.post_and_expect_nothing(event(
            "customer.subscription.updated", self.subscription(items={"data": []}),
        ))

    def test_missing_periods_are_omitted_rather_than_faked(self):
        obj = self.subscription()
        del obj["current_period_end"]
        self.stub_stripe(event("customer.subscription.updated", obj))
        self.assertEqual(self.post().status_code, 200)
        self.assertNotIn("current_period_end", self.recorded()[-1])


class TestInvoices(WebhookTestCase):
    def test_invoice_paid_records_payment_tagged_with_subscription_id(self):
        # The pairing missing_subscription_charge depends on: without the tag,
        # every renewal looks unpaid and fires a false incident.
        self.stub_stripe(event("invoice.paid", {
            "id": "in_1", "subscription": "sub_1", "charge": "ch_9",
            "amount_paid": 2500, "currency": "eur",
        }))
        self.assertEqual(self.post().status_code, 200)
        ev = self.recorded()[-1]
        self.assertEqual(ev["event_type"], "payment")
        self.assertEqual(ev["status"], "succeeded")
        self.assertEqual(ev["subscription_id"], "sub_1")
        self.assertEqual(ev["amount"], {"minor": "2500", "currency": "eur"})
        # Its own external_id keeps it off charge.succeeded's idempotency key,
        # while the reference still joins it to that same charge.
        self.assertEqual(ev["external_id"], "in_1")
        self.assertEqual(ev["reference"], "ch_9")

    def test_subscription_and_its_invoice_charge_pair_up(self):
        self.stub_stripe(event("customer.subscription.created", {
            "id": "sub_7", "status": "active",
            "current_period_start": 1700000000, "current_period_end": 1702678400,
            "items": {"data": [{"price": {
                "unit_amount": 2500, "currency": "eur",
                "recurring": {"interval": "month"},
            }}]},
        }))
        self.assertEqual(self.post().status_code, 200)
        self.stub_stripe(event("invoice.paid", {
            "id": "in_7", "subscription": "sub_7", "charge": "ch_7",
            "amount_paid": 2500, "currency": "eur",
        }))
        self.assertEqual(self.post().status_code, 200)

        sub, payment = self.recorded()
        self.assertEqual(sub["event_type"], "subscription")
        self.assertEqual(payment["event_type"], "payment")
        # The rule matches payments whose subscription_id is the subscription's
        # external_id. If this ever drifts, every renewal fires a false incident.
        self.assertEqual(payment["subscription_id"], sub["external_id"])

    def test_invoice_paid_without_subscription_is_not_a_subscription_charge(self):
        # One-off invoice: its money already arrives via charge.succeeded.
        self.post_and_expect_nothing(event("invoice.paid", {
            "id": "in_2", "charge": "ch_10", "amount_paid": 999, "currency": "usd",
        }))

    def test_expanded_subscription_object_is_reduced_to_its_id(self):
        self.stub_stripe(event("invoice.paid", {
            "id": "in_3", "subscription": {"id": "sub_3"}, "charge": {"id": "ch_3"},
            "amount_paid": 700, "currency": "usd",
        }))
        self.assertEqual(self.post().status_code, 200)
        ev = self.recorded()[-1]
        self.assertEqual(ev["subscription_id"], "sub_3")
        self.assertEqual(ev["reference"], "ch_3")


class TestUnknownEvents(WebhookTestCase):
    def test_unknown_event_type_is_ignored_but_acked(self):
        self.post_and_expect_nothing(event("customer.created", {"id": "cus_1"}))


class TestEnvironment(unittest.TestCase):
    def setUp(self):
        init(dry_run=True)
        self.addCleanup(sys.modules.pop, "stripe", None)

    def _client(self, environment=None):
        app = FastAPI()
        app.include_router(create_stripe_webhook_router(secret="whsec_test", environment=environment))
        return TestClient(app)

    def _stub(self, livemode):
        ev = event("charge.succeeded", {"id": "ch_1", "amount": 100, "amount_refunded": 0, "currency": "usd"})
        ev["livemode"] = livemode
        fake = types.ModuleType("stripe")
        fake.Webhook = types.SimpleNamespace(construct_event=lambda *a: ev)
        sys.modules["stripe"] = fake

    def _post(self, client):
        return client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "sig"})

    def test_livemode_maps_to_production_and_test(self):
        self._stub(True); self._post(self._client())
        self.assertEqual(get_client().inspect()[-1]["environment"], "production")
        init(dry_run=True)
        self._stub(False); self._post(self._client())
        self.assertEqual(get_client().inspect()[-1]["environment"], "test")

    def test_string_override(self):
        self._stub(True); self._post(self._client(environment="staging"))
        self.assertEqual(get_client().inspect()[-1]["environment"], "staging")

    def test_callable_override(self):
        self._stub(True); self._post(self._client(environment=lambda e: "eu" if e["livemode"] else "eu-test"))
        self.assertEqual(get_client().inspect()[-1]["environment"], "eu")


if __name__ == "__main__":
    unittest.main()
