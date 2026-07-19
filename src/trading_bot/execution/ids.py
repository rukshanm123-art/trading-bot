"""Client order ID generation: unique, idempotent handles for every order.

Binance allows up to 36 chars for newClientOrderId. Format:
``tb-<purpose>-<24 hex chars>`` — e.g. ``tb-en-9f2c...``. The id is generated
once at risk-approval time, persisted with the intent BEFORE submission, and
reused for queries, so a restart can always find the order on the exchange.
"""

from __future__ import annotations

import uuid


def new_client_order_id(purpose: str) -> str:
    tag = "".join(ch for ch in purpose.lower() if ch.isalnum())[:4] or "or"
    return f"tb-{tag}-{uuid.uuid4().hex[:24]}"
