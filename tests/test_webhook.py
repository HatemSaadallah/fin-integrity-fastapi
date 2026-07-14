import sys
import types
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fin_integrity import init, get_client
from fin_integrity_fastapi import create_stripe_webhook_router


class TestWebhook(unittest.TestCase):
    def setUp(self):
        init(dry_run=True)
        fake = types.ModuleType("stripe")
        fake.Webhook = types.SimpleNamespace(
            construct_event=lambda body, sig, secret: {
                "type": "charge.refunded",
                "data": {"object": {
                    "id": "ch_2", "amount_refunded": 500, "currency": "eur",
                    "metadata": {"reference": "order_2"},
                }},
            }
        )
        sys.modules["stripe"] = fake

        app = FastAPI()
        app.include_router(create_stripe_webhook_router(secret="whsec_test"))
        self.client = TestClient(app)

    def test_refund_records_processor_event(self):
        resp = self.client.post(
            "/webhooks/stripe", content=b"{}",
            headers={"stripe-signature": "sig"},
        )
        self.assertEqual(resp.status_code, 200)
        events = get_client().inspect()
        self.assertEqual(events[-1]["event_type"], "refund")
        self.assertEqual(events[-1]["reference"], "order_2")
        self.assertEqual(events[-1]["amount"], {"minor": "500", "currency": "eur"})


if __name__ == "__main__":
    unittest.main()
