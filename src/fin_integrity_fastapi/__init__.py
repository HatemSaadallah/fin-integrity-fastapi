"""FastAPI integration for fin-integrity."""
from .integration import (
    create_stripe_webhook_router,
    finintegrity_lifespan,
    get_client,
)

__all__ = [
    "create_stripe_webhook_router",
    "finintegrity_lifespan",
    "get_client",
]
__version__ = "0.1.0"
