#!/usr/bin/env python3
"""Testnet setup helper: sell the faucet BTC to USDT so the bot starts flat
(holding only quote), mirroring a cash-funded live account.

TESTNET ONLY — fake money on https://testnet.binance.vision. This is an
operator setup step, NOT part of the trading engine: the engine never sells
holdings it did not itself buy. Run it once after creating the testnet
account, and again after each monthly testnet balance reset (the faucet
re-funds ~1 BTC each reset, which the engine correctly fail-closes on until
it is sold back to cash).

Reads BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET from the process
environment, falling back to ./.env. Never prints the secrets.

Uses Decimal flooring so no unsellable sub-minNotional BTC dust is left
behind (naive float flooring of 1.0/0.00001 rounds to 0.99999 and strands
0.00001 BTC).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

BASE = "https://testnet.binance.vision"


def _load_keys() -> tuple[str, str]:
    key = os.environ.get("BINANCE_TESTNET_API_KEY")
    sec = os.environ.get("BINANCE_TESTNET_API_SECRET")
    env_file = Path(".env")
    if (not key or not sec) and env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if k == "BINANCE_TESTNET_API_KEY" and not key:
                    key = v
                elif k == "BINANCE_TESTNET_API_SECRET" and not sec:
                    sec = v
    if not key or not sec:
        raise SystemExit("BINANCE_TESTNET_API_KEY / _SECRET not found in env or ./.env")
    return key, sec


KEY, SEC = _load_keys()


def _signed(path: str, params: dict, method: str = "GET"):
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    query = urllib.parse.urlencode(params)
    sig = hmac.new(SEC.encode(), query.encode(), hashlib.sha256).hexdigest()
    if method == "GET":
        url = f"{BASE}{path}?{query}&signature={sig}"
        req = urllib.request.Request(url, headers={"X-MBX-APIKEY": KEY})
    else:
        body = f"{query}&signature={sig}".encode()
        req = urllib.request.Request(
            BASE + path, data=body, headers={"X-MBX-APIKEY": KEY}, method="POST"
        )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def _public(path: str, params: dict):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.load(resp)


def _free(balances: list, asset: str) -> Decimal:
    return next((Decimal(b["free"]) for b in balances if b["asset"] == asset), Decimal(0))


def main() -> int:
    acct = _signed("/api/v3/account", {})
    btc = _free(acct["balances"], "BTC")
    print(f"BTC free before: {btc}")
    if btc <= Decimal("0.00000001"):
        print("Nothing to sell (BTC already ~0). Account is flat cash. Done.")
        return 0

    info = _public("/api/v3/exchangeInfo", {"symbol": "BTCUSDT"})
    step = Decimal("0.00001")
    for f in info["symbols"][0]["filters"]:
        if f["filterType"] == "LOT_SIZE" and Decimal(f["stepSize"]) > 0:
            step = Decimal(f["stepSize"])
    qty = (btc // step) * step  # exact floor to step; no float dust
    qty_str = format(qty.normalize(), "f")
    print(f"Placing MARKET SELL of {qty_str} BTC ...")

    res = _signed(
        "/api/v3/order",
        {"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": qty_str},
        method="POST",
    )
    print(f"Order status: {res.get('status')}")
    print(f"Executed: {res.get('executedQty')} BTC  ->  {res.get('cummulativeQuoteQty')} USDT")

    acct2 = _signed("/api/v3/account", {})
    btc2 = _free(acct2["balances"], "BTC")
    usdt2 = _free(acct2["balances"], "USDT")
    print(f"BTC free after: {btc2}  |  USDT free after: {usdt2.quantize(Decimal('0.01'))}")
    # Sub-minNotional dust can remain; testnet reconciliation tolerates it.
    print("DONE — account is cash (any remaining BTC is untradeable dust)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
