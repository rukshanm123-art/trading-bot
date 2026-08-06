#!/usr/bin/env python3
"""Download and verify BTCUSDT 1h klines from Binance's public archive.

Research-only: this module is NEVER imported by the trading engine. The
dependency direction is one-way (research -> trading_bot, never the reverse)
so nothing here can affect live behaviour.

Guarantees, per research_spec.yaml:
  * every monthly zip is verified against its published SHA-256 CHECKSUM;
  * timestamps are normalised by MAGNITUDE, not by date, which handles
    Binance's switch to microseconds for spot data from 2025-01-01 without a
    hardcoded cutover;
  * gaps are NEVER forward-filled. Missing hours split the series into
    contiguous segments, and downstream code must refuse to build a feature
    or a label that spans a segment boundary;
  * the manifest records file checksums, row counts and every gap, so a
    result can be tied back to the exact bytes it was computed from.

Stdlib only — no pandas/numpy needed to establish the go/no-go.

Usage:
    python3 research/import_binance.py                 # range from the spec
    python3 research/import_binance.py --start 2024-01 --end 2024-12
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import itertools
import json
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "research" / "data"
BASE_URL = "https://data.binance.vision/data/spot/monthly/klines"

# Binance monthly kline CSV column order (no header row in the archive files).
COL_OPEN_TIME, COL_OPEN, COL_HIGH, COL_LOW, COL_CLOSE, COL_VOLUME = 0, 1, 2, 3, 4, 5

# A millisecond timestamp for any plausible year is ~1e12-1e13; a microsecond
# one is ~1e15-1e16. Anything above this threshold is microseconds.
MICROSECOND_THRESHOLD = 1e14

INTERVAL_SECONDS = {"1h": 3600, "5m": 300, "15m": 900, "1d": 86400}


def month_range(start: str, end: str) -> list[str]:
    """Inclusive list of 'YYYY-MM' strings."""
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    out, y, m = [], sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _fetch(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def fetch_month(symbol: str, interval: str, month: str) -> tuple[bytes, str] | None:
    """Return (zip_bytes, sha256) for a month, or None if the archive lacks it.

    The published CHECKSUM is verified before the bytes are used; a mismatch
    is fatal rather than a warning.
    """
    name = f"{symbol}-{interval}-{month}.zip"
    url = f"{BASE_URL}/{symbol}/{interval}/{name}"
    blob = _fetch(url)
    if blob is None:
        return None
    digest = hashlib.sha256(blob).hexdigest()

    checksum_blob = _fetch(url + ".CHECKSUM")
    if checksum_blob is None:
        raise SystemExit(f"{name}: archive present but CHECKSUM missing — refusing to trust it")
    published = checksum_blob.decode().split()[0].strip().lower()
    if published != digest:
        raise SystemExit(
            f"{name}: CHECKSUM MISMATCH (published {published[:16]}…, got {digest[:16]}…)"
        )
    return blob, digest


def normalise_timestamp(raw: str) -> datetime:
    """ms or µs epoch -> aware UTC datetime, detected by magnitude."""
    value = float(raw)
    if value > MICROSECOND_THRESHOLD:
        value /= 1000.0  # µs -> ms
    return datetime.fromtimestamp(value / 1000.0, tz=UTC)


def rows_from_zip(blob: bytes) -> list[dict]:
    out: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        inner = zf.namelist()[0]
        with zf.open(inner) as fh:
            for row in csv.reader(io.TextIOWrapper(fh, "utf-8")):
                if not row or not row[COL_OPEN_TIME].strip():
                    continue
                if not row[COL_OPEN_TIME][0].isdigit():
                    continue  # some months ship a header line
                out.append(
                    {
                        "open_time": normalise_timestamp(row[COL_OPEN_TIME]),
                        "open": row[COL_OPEN],
                        "high": row[COL_HIGH],
                        "low": row[COL_LOW],
                        "close": row[COL_CLOSE],
                        "volume": row[COL_VOLUME],
                    }
                )
    return out


def find_gaps(rows: list[dict], interval_s: int) -> list[dict]:
    """Every discontinuity, so labels/features spanning one can be discarded."""
    gaps = []
    step = timedelta(seconds=interval_s)
    for prev, cur in itertools.pairwise(rows):
        delta = cur["open_time"] - prev["open_time"]
        if delta != step:
            gaps.append(
                {
                    "after": prev["open_time"].isoformat(),
                    "before": cur["open_time"].isoformat(),
                    "missing_bars": int(delta.total_seconds() // interval_s) - 1,
                }
            )
    return gaps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--start", default=None, help="YYYY-MM (default: from research_spec.yaml)")
    ap.add_argument("--end", default=None, help="YYYY-MM (default: from research_spec.yaml)")
    args = ap.parse_args()

    start, end = args.start, args.end
    if start is None or end is None:
        spec_path = ROOT / "research" / "research_spec.yaml"
        text = spec_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("start:") and start is None:
                start = s.split(":", 1)[1].strip().strip('"')
            elif s.startswith("end:") and end is None:
                end = s.split(":", 1)[1].strip().strip('"')
    if not start or not end:
        raise SystemExit("could not resolve --start/--end")

    interval_s = INTERVAL_SECONDS[args.interval]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict] = []
    files: list[dict] = []
    missing_months: list[str] = []

    for month in month_range(start, end):
        got = fetch_month(args.symbol, args.interval, month)
        if got is None:
            missing_months.append(month)
            print(f"  {month}  (not published)")
            continue
        blob, digest = got
        rows = rows_from_zip(blob)
        all_rows.extend(rows)
        files.append({"month": month, "sha256": digest, "rows": len(rows)})
        print(f"  {month}  {len(rows):>5} rows  sha256={digest[:12]}…")

    if not all_rows:
        raise SystemExit("no data downloaded")

    all_rows.sort(key=lambda r: r["open_time"])
    # de-duplicate on open_time (month boundaries can overlap by a bar)
    deduped: list[dict] = []
    seen = set()
    for r in all_rows:
        if r["open_time"] in seen:
            continue
        seen.add(r["open_time"])
        deduped.append(r)

    gaps = find_gaps(deduped, interval_s)
    missing_bars = sum(g["missing_bars"] for g in gaps)

    csv_path = OUT_DIR / f"{args.symbol}-{args.interval}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["open_time", "open", "high", "low", "close", "volume"])
        for r in deduped:
            w.writerow(
                [
                    r["open_time"].isoformat(),
                    r["open"],
                    r["high"],
                    r["low"],
                    r["close"],
                    r["volume"],
                ]
            )

    manifest = {
        "symbol": args.symbol,
        "interval": args.interval,
        "requested_range": [start, end],
        "rows": len(deduped),
        "first_open_time": deduped[0]["open_time"].isoformat(),
        "last_open_time": deduped[-1]["open_time"].isoformat(),
        "months_downloaded": len(files),
        "months_not_published": missing_months,
        "gap_count": len(gaps),
        "missing_bars": missing_bars,
        "gaps": gaps[:200],
        "files": files,
        "data_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "forward_filled": False,
    }
    manifest_path = OUT_DIR / f"{args.symbol}-{args.interval}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"\nrows           : {len(deduped)}")
    print(f"range          : {manifest['first_open_time']} -> {manifest['last_open_time']}")
    print(f"gaps           : {len(gaps)} ({missing_bars} missing bars, NOT filled)")
    print(f"data sha256    : {manifest['data_sha256']}")
    print(f"wrote          : {csv_path}")
    print(f"manifest       : {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
