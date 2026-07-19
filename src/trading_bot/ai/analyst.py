"""Advisory-only narrator.

This module turns structured records into human-readable text for reports and
notifications. It is DETERMINISTIC (templates, no model calls) by default.

If you later plug an LLM into AdvisoryProvider, the contract is:
- input: structured, already-redacted data
- output: prose stored/displayed as text
- the output is NEVER parsed, interpreted as a command, or fed into the
  pipeline. Nothing in this module (or anywhere) can place, modify, or
  approve an order. See docs/SECURITY.md ("LLM containment").
"""

from __future__ import annotations

import json
from typing import Any, Protocol


class AdvisoryProvider(Protocol):
    def narrate(self, context: dict[str, Any]) -> str: ...


class TemplateAnalyst:
    """Deterministic explanation generator."""

    def explain_decision(self, decision: dict[str, Any]) -> str:
        action = decision.get("signal_action") or "HOLD"
        reason = decision.get("signal_reason") or "no signal"
        approved = decision.get("risk_approved")
        codes = decision.get("risk_codes_json")
        try:
            code_list = json.loads(codes) if codes else []
        except (TypeError, json.JSONDecodeError):
            code_list = []
        if action == "HOLD":
            return f"Held: {reason}."
        if approved:
            return f"Signal {action} ({reason}) approved by risk engine and submitted."
        pretty = ", ".join(code_list) or "no reason recorded"
        return f"Signal {action} ({reason}) was REJECTED by the risk engine: {pretty}."

    def narrate_day(self, report: dict[str, Any]) -> str:
        parts: list[str] = []
        pnl = report.get("realized_pnl_today", "0")
        trades_closed = report.get("trades_closed_count", 0)
        rejected = report.get("rejected_entries", {})
        parts.append(
            f"Closed {trades_closed} trade(s) for a realised P&L of {pnl} "
            f"{report.get('quote_asset', '')}.".strip()
        )
        if rejected:
            top = sorted(rejected.items(), key=lambda kv: -kv[1])[:3]
            reasons = "; ".join(f"{k} x{v}" for k, v in top)
            parts.append(f"Risk engine rejections: {reasons}.")
        if report.get("kill_switch_active"):
            parts.append("A kill switch is ACTIVE — no new entries will occur until reset.")
        rec = report.get("recommendation", "continue")
        parts.append(
            f"Recommendation: {rec.replace('_', ' ')} (advisory only — the deterministic "
            "risk engine remains the final authority)."
        )
        return " ".join(parts)
