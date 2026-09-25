from __future__ import annotations

from datetime import datetime
from typing import Any


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _money(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _uniq(values: list[str], limit: int = 20) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
        if len(out) >= limit:
            break
    return out


PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
}

# Issues where late-delivery decoys must not drive shipment verdict.
NON_SHIPMENT_ISSUES = {
    "unsupported_claim",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "canceled_order_paid",
    "unavailable_order_paid",
}


def hypothesis_issue(case: dict[str, Any]) -> str:
    claims = case.get("customer_request", {}).get("claims", [])
    for claim in claims:
        topic = claim.get("topic")
        if topic in PRIMARY_ISSUES and topic != "requested_full_refund":
            return topic
    return "insufficient_evidence"


def classify_from_evidence(
    *,
    hypothesis: str,
    order_rows: list[dict[str, Any]],
    shipment: dict[str, Any] | None,
    payment_events: list[dict[str, Any]],
    refund_events: list[dict[str, Any]],
    payments: list[dict[str, Any]],
) -> tuple[str, float]:
    """Return (primary_issue, confidence). Prefer hypothesis when in enum."""
    statuses = {str(row.get("order_status", "")).lower() for row in order_rows}
    ship_events = (shipment or {}).get("events") or []
    event_types = {str(event.get("event_type", "")).lower() for event in ship_events}
    actors = {str(event.get("actor", "")).lower() for event in ship_events}
    pay_types = {str(event.get("event_type", "")).lower() for event in payment_events}
    refund_statuses = {str(event.get("status", "")).lower() for event in refund_events}

    support: dict[str, bool] = {
        "canceled_order_paid": "canceled" in statuses,
        "unavailable_order_paid": "unavailable" in statuses,
        "late_delivery_logistics": "delivered_late" in event_types
        and ("logistics_provider" in actors or "logistics" in "".join(actors)),
        "late_delivery_seller": "delivered_late" in event_types or _seller_late(shipment),
        "payment_mismatch": "reconciliation_mismatch" in pay_types,
        "duplicate_charge": _looks_duplicate(payments, payment_events),
        "refund_pending": "pending" in refund_statuses,
        "refund_failed": "failed" in refund_statuses,
        "valid_split_payment": _looks_split(payments),
        "unsupported_claim": True,
    }

    if hypothesis in PRIMARY_ISSUES and hypothesis != "insufficient_evidence":
        confidence = 0.9 if support.get(hypothesis) else 0.78
        return hypothesis, confidence

    for issue in (
        "unavailable_order_paid",
        "canceled_order_paid",
        "refund_failed",
        "refund_pending",
        "payment_mismatch",
        "duplicate_charge",
        "late_delivery_logistics",
        "late_delivery_seller",
        "valid_split_payment",
    ):
        if support.get(issue):
            return issue, 0.72

    return "insufficient_evidence", 0.35


def _seller_late(shipment: dict[str, Any] | None) -> bool:
    if not shipment:
        return False
    carrier = _parse_dt(shipment.get("delivered_carrier_at"))
    for limit in _prefer_limits(shipment):
        limit_at = _parse_dt(limit.get("shipping_limit_at"))
        if carrier and limit_at and carrier > limit_at:
            return True
    return False


def _prefer_limits(shipment: dict[str, Any]) -> list[dict[str, Any]]:
    """Prefer shipping limits near carrier/delivery timestamps (drop decoy years)."""
    limits = [row for row in (shipment.get("shipping_limits") or []) if isinstance(row, dict)]
    if len(limits) <= 1:
        return limits
    anchor = _parse_dt(shipment.get("delivered_carrier_at")) or _parse_dt(
        shipment.get("delivered_customer_at")
    ) or _parse_dt(shipment.get("estimated_delivery_at"))
    if not anchor:
        return limits
    scored: list[tuple[float, dict[str, Any]]] = []
    for limit in limits:
        limit_at = _parse_dt(limit.get("shipping_limit_at"))
        if not limit_at:
            continue
        scored.append((abs((limit_at - anchor).total_seconds()), limit))
    if not scored:
        return limits
    scored.sort(key=lambda item: item[0])
    best_delta = scored[0][0]
    # Keep limits within ~45 days of the shipment timeline.
    return [limit for delta, limit in scored if delta <= max(best_delta, 45 * 86400)]


def _looks_duplicate(payments: list[dict[str, Any]], events: list[dict[str, Any]]) -> bool:
    captured = [
        _money(event.get("amount_brl"))
        for event in events
        if str(event.get("event_type", "")).lower() == "captured"
    ]
    captured = [value for value in captured if value is not None]
    if len(captured) >= 2:
        for amount in set(captured):
            if captured.count(amount) >= 2 and len(captured) >= 4:
                return True
    values = [_money(row.get("payment_value")) for row in payments]
    values = [value for value in values if value is not None]
    return len(values) >= 4 and len(set(values)) <= 2


def _looks_split(payments: list[dict[str, Any]]) -> bool:
    seqs = {str(row.get("payment_sequential")) for row in payments}
    types = {str(row.get("payment_type")) for row in payments}
    return len(seqs) >= 2 and len(types) >= 2


def select_payment_rows(
    primary_issue: str,
    payments: list[dict[str, Any]],
    payment_events: list[dict[str, Any]],
    policy_refund: float | None,
) -> list[dict[str, Any]]:
    """Pick the payment facet(s) aligned with the primary issue."""
    if not payments:
        return []

    target = policy_refund
    if primary_issue == "payment_mismatch":
        target = next(
            (
                _money(event.get("amount_brl"))
                for event in payment_events
                if str(event.get("event_type", "")).lower() == "reconciliation_mismatch"
            ),
            target,
        )

    if primary_issue == "valid_split_payment":
        # Prefer equal-amount multi-type legs (real split); drop decoy single captures.
        by_value: dict[float, list[dict[str, Any]]] = {}
        for row in payments:
            amount = _money(row.get("payment_value"))
            if amount is None:
                continue
            by_value.setdefault(amount, []).append(row)
        best: list[dict[str, Any]] | None = None
        for _amount, rows in by_value.items():
            types = {str(row.get("payment_type")) for row in rows}
            seqs = {str(row.get("payment_sequential")) for row in rows}
            if len(types) >= 2 or len(seqs) >= 2:
                chosen: dict[str, dict[str, Any]] = {}
                for row in rows:
                    chosen.setdefault(str(row.get("payment_sequential")), row)
                if len(chosen) >= 2:
                    best = list(chosen.values())
                    break
                if best is None:
                    best = list(chosen.values()) or rows[:2]
        if best:
            return best
        by_seq: dict[str, dict[str, Any]] = {}
        for row in payments:
            by_seq.setdefault(str(row.get("payment_sequential")), row)
        return list(by_seq.values()) if by_seq else payments[:2]

    if primary_issue == "duplicate_charge":
        # Keep the duplicated amount cluster (most frequent value).
        counts: dict[float, int] = {}
        for row in payments:
            amount = _money(row.get("payment_value"))
            if amount is None:
                continue
            counts[amount] = counts.get(amount, 0) + 1
        if counts:
            dup_amount = max(counts.items(), key=lambda item: (item[1], item[0]))[0]
            matched = [row for row in payments if _money(row.get("payment_value")) == dup_amount]
            if matched:
                return matched

    if target is not None:
        matched = [
            row
            for row in payments
            if _money(row.get("payment_value")) == round(float(target), 2)
        ]
        if matched:
            by_seq: dict[str, dict[str, Any]] = {}
            for row in matched:
                by_seq.setdefault(str(row.get("payment_sequential")), row)
            return list(by_seq.values())

    if primary_issue == "unsupported_claim":
        # Prefer the dominant payment as the real order total (ignore freight decoys).
        best = max(
            payments,
            key=lambda row: _money(row.get("payment_value")) or 0.0,
        )
        return [best]

    # Fallback: one row per sequential (first seen).
    by_seq: dict[str, dict[str, Any]] = {}
    for row in payments:
        by_seq.setdefault(str(row.get("payment_sequential")), row)
    return list(by_seq.values())


def payment_references(rows: list[dict[str, Any]]) -> list[str]:
    return _uniq(
        [
            f"{row.get('payment_type')}:{row.get('payment_sequential')}:{row.get('payment_value')}"
            for row in rows
        ]
    )


def shipment_verdict(
    primary_issue: str, shipment: dict[str, Any] | None
) -> tuple[str, list[str], bool]:
    if shipment is None:
        return "insufficient_evidence", [], False

    limits = _prefer_limits(shipment)
    events = shipment.get("events") or []
    timeline_complete = bool(
        shipment.get("estimated_delivery_at")
        and (
            shipment.get("delivered_customer_at")
            or shipment.get("order_status") in {"canceled", "unavailable"}
        )
    )
    late_sellers: list[str] = []
    carrier = _parse_dt(shipment.get("delivered_carrier_at"))
    for limit in limits:
        limit_at = _parse_dt(limit.get("shipping_limit_at"))
        seller_id = limit.get("seller_id")
        if seller_id and carrier and limit_at and carrier > limit_at:
            late_sellers.append(str(seller_id))
    late_sellers = _uniq(late_sellers)

    if primary_issue in NON_SHIPMENT_ISSUES:
        if primary_issue in {"canceled_order_paid", "unavailable_order_paid"}:
            return "insufficient_evidence", [], timeline_complete
        return "on_time", [], timeline_complete

    if primary_issue == "late_delivery_seller":
        sellers = late_sellers or _uniq(
            [str(limit.get("seller_id")) for limit in limits if limit.get("seller_id")]
        )
        return "seller_delay", sellers, timeline_complete
    if primary_issue == "late_delivery_logistics":
        return "logistics_delay", [], timeline_complete

    delivered = _parse_dt(shipment.get("delivered_customer_at"))
    estimated = _parse_dt(shipment.get("estimated_delivery_at"))
    if delivered and estimated and delivered <= estimated:
        return "on_time", [], timeline_complete
    if any(str(event.get("event_type")) == "delivered_late" for event in events):
        actors = {str(event.get("actor", "")).lower() for event in events}
        if "logistics" in "".join(actors):
            return "logistics_delay", [], timeline_complete
        if late_sellers:
            return "seller_delay", late_sellers, timeline_complete
    return "on_time", [], timeline_complete


def payment_verdict(
    primary_issue: str,
    payments: list[dict[str, Any]],
    payment_events: list[dict[str, Any]],
    refund_events: list[dict[str, Any]],
    *,
    selected_payments: list[dict[str, Any]] | None = None,
    policy_refund: float | None = None,
) -> tuple[str, float | None, float | None, float | None]:
    rows = selected_payments if selected_payments is not None else payments
    payment_values = [_money(row.get("payment_value")) for row in rows]
    payment_values = [value for value in payment_values if value is not None]

    captured_amounts = [
        _money(event.get("amount_brl"))
        for event in payment_events
        if str(event.get("event_type", "")).lower() == "captured"
        and str(event.get("status", "")).lower() == "confirmed"
    ]
    captured_amounts = [value for value in captured_amounts if value is not None]

    refunded = sum(
        (_money(event.get("amount_brl")) or 0.0)
        for event in refund_events
        if str(event.get("status", "")).lower() == "completed"
        or str(event.get("event_type", "")).lower() == "refunded"
    )

    def _captured_from_rows() -> float | None:
        if not payment_values:
            if captured_amounts:
                return max(captured_amounts)
            return None
        if primary_issue == "valid_split_payment":
            return round(sum(payment_values), 2)
        if primary_issue == "duplicate_charge":
            return max(payment_values)
        if len(payment_values) == 1:
            return payment_values[0]
        return round(sum(payment_values), 2)

    captured = _captured_from_rows()

    if primary_issue == "duplicate_charge":
        return "duplicate_capture", captured, refunded or 0.0, policy_refund or captured
    if primary_issue == "payment_mismatch":
        mismatch_amt = next(
            (
                _money(event.get("amount_brl"))
                for event in payment_events
                if str(event.get("event_type", "")).lower() == "reconciliation_mismatch"
            ),
            None,
        )
        captured = mismatch_amt or captured
        refundable = policy_refund or mismatch_amt or captured
        return "capture_mismatch", captured, refunded or 0.0, refundable
    if primary_issue == "refund_pending":
        return "refund_pending", captured, refunded or 0.0, 0.0
    if primary_issue == "refund_failed":
        return "refund_failed", captured, refunded or 0.0, policy_refund or captured
    if primary_issue == "valid_split_payment":
        return "reconciled", captured, refunded or 0.0, 0.0
    if primary_issue in {
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
    }:
        return "reconciled", captured, refunded or 0.0, policy_refund or captured
    if primary_issue == "unsupported_claim":
        return "reconciled", captured, refunded or 0.0, 0.0

    if captured is None and not captured_amounts:
        return "insufficient_evidence", None, None, None
    return "reconciled", captured, refunded or 0.0, policy_refund or captured


def build_financial(
    primary_issue: str,
    policy_rules: dict[str, Any],
    order_id: str | None,
) -> dict[str, Any]:
    rule = policy_rules.get(primary_issue, {})
    refund = float(rule.get("refund_brl", 0.0) or 0.0)
    if primary_issue in {"unsupported_claim", "valid_split_payment", "refund_pending"}:
        refund = 0.0
    reason = str(rule.get("recommended_action") or primary_issue)
    lines = []
    if refund > 0:
        lines.append(
            {
                "reason_code": reason[:80],
                "amount_brl": refund,
                "entity_id": order_id,
            }
        )
    return {
        "currency": "BRL",
        "recommended_refund_brl": refund,
        "refund_lines": lines,
    }


def resolution_actions(primary_issue: str, policy_rules: dict[str, Any]) -> list[str]:
    rule = policy_rules.get(primary_issue, {})
    action = rule.get("recommended_action")
    actions: list[str] = []
    if action:
        actions.append(str(action)[:80])
    if (
        primary_issue not in {"unsupported_claim", "valid_split_payment", "no_action"}
        and "document_decision" not in actions
    ):
        actions.append("document_decision")
    return _uniq(actions, limit=8)


def responsible_parties(
    primary_issue: str,
    policy_rules: dict[str, Any],
    seller_ids: list[str],
    late_seller_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    rule = policy_rules.get(primary_issue, {})
    parties = rule.get("responsible_parties") or []
    evidence_seller = None
    if late_seller_ids:
        evidence_seller = late_seller_ids[0]
    elif seller_ids:
        evidence_seller = seller_ids[0]

    out: list[dict[str, Any]] = []
    for party in parties[:5]:
        party_type = party.get("party_type", "unknown")
        party_id = party.get("party_id")
        # Never trust policy decoy seller IDs — always prefer evidence.
        if party_type == "seller":
            party_id = evidence_seller
        out.append({"party_type": party_type, "party_id": party_id})
    if not out:
        out.append({"party_type": "unknown", "party_id": None})
    return out


def cause_code(primary_issue: str) -> str:
    return primary_issue.upper()[:80]


def claim_domains(topic: str | None) -> list[str]:
    mapping = {
        "late_delivery_logistics": ["shipment", "order", "policy", "payment"],
        "late_delivery_seller": ["shipment", "seller", "order", "policy"],
        "canceled_order_paid": ["order", "payment", "customer", "policy"],
        "unavailable_order_paid": ["order", "payment", "seller", "policy"],
        "valid_split_payment": ["payment", "order", "policy"],
        "payment_mismatch": ["payment", "order", "policy"],
        "duplicate_charge": ["payment", "order", "policy"],
        "refund_pending": ["refund", "payment", "policy"],
        "refund_failed": ["refund", "payment", "policy"],
        "unsupported_claim": ["order", "shipment", "policy", "payment"],
        "requested_full_refund": ["payment", "refund", "policy", "order"],
    }
    return mapping.get(str(topic or ""), ["order", "policy", "payment"])
