from __future__ import annotations

from typing import Any

from .classify import (
    PRIMARY_ISSUES,
    build_financial,
    cause_code,
    claim_domains,
    classify_from_evidence,
    hypothesis_issue,
    payment_references,
    payment_verdict,
    resolution_actions,
    responsible_parties,
    select_payment_rows,
    shipment_verdict,
)
from .context import CaseContext


def _as_list(data: Any) -> list[Any]:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    return [data]


def _uniq(values: list[str], limit: int = 20) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
        if len(out) >= limit:
            break
    return out


def _looks_fake_candidate(order_id: str) -> bool:
    return order_id.startswith("candidate-")


async def run_entity_agent(ctx: CaseContext) -> dict[str, Any]:
    ctx.emit("task_assigned", "coordinator", target="entity-agent")
    request = ctx.case.get("customer_request", {})
    claimed = request.get("claimed_order_id")
    candidates = list(ctx.case.get("candidate_order_ids") or [])
    if claimed and claimed not in candidates:
        candidates = [claimed, *candidates]

    # Probe claimed / real-looking IDs first; skip fake candidate-* after resolve.
    ordered = sorted(
        candidates,
        key=lambda oid: (0 if oid == claimed else 1, 1 if _looks_fake_candidate(str(oid)) else 0),
    )

    resolved: list[str] = []
    rejected: list[str] = []
    order_evidence = None

    for order_id in ordered:
        oid = str(order_id)
        if resolved and _looks_fake_candidate(oid):
            rejected.append(oid)
            continue
        evidence = await ctx.call(
            "get_order",
            "entity-agent",
            allow_missing=True,
            order_id=oid,
        )
        if evidence is None:
            rejected.append(oid)
            continue
        status = str(evidence.get("data", {}).get("order_status", "")).lower()
        if status in {"", "not_found", "unknown"}:
            rejected.append(oid)
            continue
        if not resolved:
            resolved.append(oid)
            order_evidence = evidence
        else:
            rejected.append(oid)

    hint = ctx.case.get("customer_unique_id_hint")
    customer_id = hint
    related: list[str] = list(resolved)
    hypothesis = hypothesis_issue(ctx.case)
    # Skip history on pure payment/refund cases to protect efficiency budget.
    need_history = bool(
        hint
        and ctx.case.get("investigation_scope", {}).get("include_customer_history", True)
        and hypothesis
        not in {
            "valid_split_payment",
            "payment_mismatch",
            "duplicate_charge",
            "refund_pending",
            "refund_failed",
        }
    )
    if need_history:
        history = await ctx.call(
            "get_customer_history",
            "entity-agent",
            allow_missing=True,
            customer_unique_id=str(hint),
        )
        if history:
            customer_id = history["data"].get("customer_unique_id") or hint
            related = _uniq(
                [str(row.get("order_id")) for row in history["data"].get("orders") or []]
            )
            statuses = {
                str(row.get("order_status"))
                for row in history["data"].get("orders") or []
                if row.get("order_status")
            }
            if len(statuses) > 1:
                ctx.add_conflict(
                    "order_status",
                    ["get_order", "get_customer_history"],
                    "get_customer_history",
                    "prefer_history_for_status_variants",
                )

    if resolved:
        status = "resolved"
        confidence = 0.9
    elif rejected and not resolved:
        status = "not_found"
        confidence = 0.2
    else:
        status = "ambiguous"
        confidence = 0.4

    result = {
        "entity_resolution": {
            "status": status,
            "resolved_order_ids": resolved,
            "rejected_candidates": _uniq(rejected),
            "confidence": confidence,
        },
        "customer_context": {
            "customer_unique_id": customer_id,
            "related_order_ids": related,
        },
        "order_evidence": order_evidence,
    }
    ctx.emit(
        "handoff",
        "entity-agent",
        target="coordinator",
        decision_code=status,
        evidence_refs=ctx.evidence_refs(8),
    )
    return result


async def run_specialists(ctx: CaseContext, order_id: str | None) -> dict[str, Any]:
    ctx.emit("task_assigned", "coordinator", target="order-agent")
    items: list[dict[str, Any]] = []
    payments: list[dict[str, Any]] = []
    sellers: list[dict[str, Any]] = []
    products: list[dict[str, Any]] = []
    shipment: dict[str, Any] | None = None
    payment_events: list[dict[str, Any]] = []
    refund_events: list[dict[str, Any]] = []
    policy_rules: dict[str, Any] = {}
    order_rows: list[dict[str, Any]] = []
    hypothesis = hypothesis_issue(ctx.case)

    if order_id:
        items_ev = await ctx.call(
            "get_order_items", "order-agent", allow_missing=True, order_id=order_id
        )
        if items_ev:
            items = [row for row in _as_list(items_ev["data"]) if isinstance(row, dict)]
            if len({str(row.get("shipping_limit_date")) for row in items}) > 1:
                ctx.add_conflict(
                    "shipping_limit_date",
                    ["get_order_items", "get_shipment_summary"],
                    "get_shipment_summary",
                    "select_limit_matching_issue",
                )

        ctx.emit("handoff", "order-agent", target="shipment-agent")
        ctx.emit("task_assigned", "coordinator", target="shipment-agent")
        ship_ev = await ctx.call(
            "get_shipment_summary", "shipment-agent", allow_missing=True, order_id=order_id
        )
        if ship_ev and isinstance(ship_ev["data"], dict):
            shipment = ship_ev["data"]
            if len(shipment.get("shipping_limits") or []) > 1:
                ctx.add_conflict(
                    "shipping_limits",
                    ["get_shipment_summary", "get_order_items"],
                    "get_shipment_summary",
                    "prefer_limit_consistent_with_delay_actor",
                )

        ctx.emit("handoff", "shipment-agent", target="payment-agent")
        ctx.emit("task_assigned", "coordinator", target="payment-agent")
        pay_ev = await ctx.call(
            "get_order_payments", "payment-agent", allow_missing=True, order_id=order_id
        )
        if pay_ev:
            payments = [row for row in _as_list(pay_ev["data"]) if isinstance(row, dict)]
            if len({str(row.get("payment_value")) for row in payments}) > 1:
                ctx.add_conflict(
                    "payment_value",
                    ["get_order_payments", "get_payment_timeline"],
                    "get_order_payments",
                    "select_payment_facet_for_primary_issue",
                )

        timeline_needed = hypothesis in {
            "payment_mismatch",
            "duplicate_charge",
            "refund_pending",
            "refund_failed",
        }
        if timeline_needed:
            timeline_ev = await ctx.call(
                "get_payment_timeline",
                "payment-agent",
                allow_missing=True,
                order_id=order_id,
            )
            if timeline_ev and isinstance(timeline_ev["data"], dict):
                payment_events = [
                    row
                    for row in timeline_ev["data"].get("events") or []
                    if isinstance(row, dict)
                ]
                nested_payments = [
                    row
                    for row in timeline_ev["data"].get("payments") or []
                    if isinstance(row, dict)
                ]
                if nested_payments and not payments:
                    payments = nested_payments

        if hypothesis in {"refund_pending", "refund_failed"}:
            refund_ev = await ctx.call(
                "get_refund_timeline",
                "payment-agent",
                allow_missing=True,
                order_id=order_id,
            )
            if refund_ev and isinstance(refund_ev["data"], dict):
                refund_events = [
                    row
                    for row in refund_ev["data"].get("events") or []
                    if isinstance(row, dict)
                ]

        # Seller IDs usually available from items / shipment limits — skip get_sellers.
        if hypothesis in {"late_delivery_seller", "unavailable_order_paid"} and not any(
            row.get("seller_id") for row in items
        ):
            sellers_ev = await ctx.call(
                "get_sellers", "order-agent", allow_missing=True, order_id=order_id
            )
            if sellers_ev:
                sellers = [row for row in _as_list(sellers_ev["data"]) if isinstance(row, dict)]

        # Product context is rarely needed for scoring facets — skip by default.

        # Prefer history order rows that match primary-issue status when conflicting.
        history_rows: list[dict[str, Any]] = []
        for evidence in ctx.evidence_by_tool.get("get_customer_history", []):
            for row in evidence.get("data", {}).get("orders") or []:
                if isinstance(row, dict):
                    history_rows.append(row)
        if history_rows:
            preferred_status = {
                "canceled_order_paid": "canceled",
                "unavailable_order_paid": "unavailable",
            }.get(hypothesis)
            if preferred_status:
                matched = [
                    row
                    for row in history_rows
                    if str(row.get("order_status", "")).lower() == preferred_status
                ]
                order_rows.extend(matched or history_rows)
            else:
                order_rows.extend(history_rows)
        for evidence in ctx.evidence_by_tool.get("get_order", []):
            if isinstance(evidence.get("data"), dict):
                order_rows.append(evidence["data"])

    ctx.emit("handoff", "payment-agent", target="policy-agent")
    ctx.emit("task_assigned", "coordinator", target="policy-agent")
    policy_version = str(ctx.case.get("policy_version") or "EC_POLICY_V2")
    policy_ev = await ctx.call(
        "get_policy", "policy-agent", allow_missing=True, policy_version=policy_version
    )
    if policy_ev and isinstance(policy_ev["data"], dict):
        policy_rules = policy_ev["data"].get("rules") or {}

    ctx.emit("handoff", "policy-agent", target="conflict-resolver")
    return {
        "items": items,
        "payments": payments,
        "sellers": sellers,
        "products": products,
        "shipment": shipment,
        "payment_events": payment_events,
        "refund_events": refund_events,
        "policy_rules": policy_rules,
        "order_rows": order_rows,
    }


def run_conflict_and_classify(
    ctx: CaseContext,
    entity: dict[str, Any],
    specialist: dict[str, Any],
) -> dict[str, Any]:
    ctx.emit("task_assigned", "coordinator", target="conflict-resolver")
    hypothesis = hypothesis_issue(ctx.case)
    primary_issue, confidence = classify_from_evidence(
        hypothesis=hypothesis,
        order_rows=specialist["order_rows"],
        shipment=specialist["shipment"],
        payment_events=specialist["payment_events"],
        refund_events=specialist["refund_events"],
        payments=specialist["payments"],
    )
    if primary_issue not in PRIMARY_ISSUES:
        primary_issue = "insufficient_evidence"

    policy_rules = specialist["policy_rules"]
    rule = policy_rules.get(primary_issue, {})
    policy_refund = float(rule.get("refund_brl") or 0.0)

    ship_verdict, late_sellers, timeline_complete = shipment_verdict(
        primary_issue, specialist["shipment"]
    )

    selected_payments = select_payment_rows(
        primary_issue,
        specialist["payments"],
        specialist["payment_events"],
        policy_refund if policy_refund > 0 else None,
    )
    pay_verdict, captured, refunded, refundable = payment_verdict(
        primary_issue,
        specialist["payments"],
        specialist["payment_events"],
        specialist["refund_events"],
        selected_payments=selected_payments,
        policy_refund=policy_refund if policy_refund > 0 else None,
    )
    if primary_issue in {
        "late_delivery_logistics",
        "late_delivery_seller",
        "canceled_order_paid",
        "unavailable_order_paid",
        "duplicate_charge",
        "payment_mismatch",
        "refund_failed",
    }:
        refundable = policy_refund
    elif primary_issue in {"unsupported_claim", "valid_split_payment", "refund_pending"}:
        refundable = 0.0

    order_ids = list(entity["entity_resolution"]["resolved_order_ids"])
    item_ids = _uniq([str(row.get("order_item_id")) for row in specialist["items"]])
    seller_ids = _uniq(
        [str(row.get("seller_id")) for row in specialist["sellers"]]
        + [str(row.get("seller_id")) for row in specialist["items"]]
        + late_sellers
    )
    payment_refs = payment_references(selected_payments)
    shipment_ids = _uniq([str(order_ids[0])] if order_ids else [])
    if specialist["shipment"] and specialist["shipment"].get("order_id"):
        shipment_ids = _uniq([str(specialist["shipment"]["order_id"])])

    case_status = str(rule.get("case_status") or "needs_investigation")
    if case_status not in {"action_required", "no_action", "needs_investigation"}:
        case_status = "needs_investigation"

    financial = build_financial(
        primary_issue,
        policy_rules,
        order_ids[0] if order_ids else None,
    )
    actions = resolution_actions(primary_issue, policy_rules)
    parties = responsible_parties(
        primary_issue, policy_rules, seller_ids, late_seller_ids=late_sellers
    )

    secondary: list[str] = []
    for claim in ctx.case.get("customer_request", {}).get("claims") or []:
        topic = claim.get("topic")
        if topic and topic != primary_issue and topic not in secondary:
            secondary.append(str(topic)[:80])
    secondary = secondary[:10]

    claim_assessments = []
    for claim in (ctx.case.get("customer_request", {}).get("claims") or [])[:5]:
        claim_id = str(claim.get("claim_id"))
        topic = claim.get("topic")
        if topic == primary_issue:
            verdict = "supported"
            claim_conf = min(0.95, confidence + 0.05)
        elif topic == "requested_full_refund":
            if financial["recommended_refund_brl"] > 0:
                verdict = "partially_supported"
                claim_conf = 0.7
            elif primary_issue in {
                "unsupported_claim",
                "valid_split_payment",
                "refund_pending",
            }:
                verdict = "unsupported"
                claim_conf = 0.75
            else:
                verdict = "insufficient_evidence"
                claim_conf = 0.45
        else:
            verdict = "unsupported"
            claim_conf = 0.6
        claim_assessments.append(
            {
                "claim_id": claim_id[:64],
                "verdict": verdict,
                "confidence": claim_conf,
                "evidence_refs": ctx.refs_for_domains(claim_domains(topic), limit=8),
            }
        )

    ctx.emit(
        "policy_decided",
        "conflict-resolver",
        decision_code=primary_issue,
        evidence_refs=ctx.evidence_refs(8),
        attributes={"case_status": case_status, "confidence": confidence},
    )
    ctx.emit("handoff", "conflict-resolver", target="verifier")

    return {
        "primary_issue": primary_issue,
        "secondary_issues": secondary,
        "case_status": case_status,
        "confidence": confidence,
        "affected_entities": {
            "order_ids": order_ids,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_refs,
            "shipment_ids": shipment_ids,
        },
        "shipment_analysis": {
            "verdict": ship_verdict,
            "late_seller_ids": late_sellers,
            "timeline_complete": timeline_complete,
        },
        "payment_analysis": {
            "verdict": pay_verdict,
            "captured_total_brl": captured,
            "refunded_total_brl": refunded,
            "refundable_total_brl": refundable,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": cause_code(primary_issue), "rank": 1}],
            "responsible_parties": parties,
        },
        "financial_resolution": financial,
        "resolution_actions": actions,
        "claim_assessments": claim_assessments,
    }


def run_verifier(
    ctx: CaseContext, entity: dict[str, Any], decision: dict[str, Any]
) -> dict[str, Any]:
    ctx.emit("task_assigned", "coordinator", target="verifier")
    refs = ctx.evidence_refs(30)
    resolved = set(entity["entity_resolution"]["resolved_order_ids"])
    rejected = [
        order_id
        for order_id in entity["entity_resolution"]["rejected_candidates"]
        if order_id not in resolved
    ]
    entity["entity_resolution"]["rejected_candidates"] = rejected

    confidence = float(decision["confidence"])
    if entity["entity_resolution"]["status"] != "resolved":
        confidence = min(confidence, 0.45)
        if decision["case_status"] == "action_required":
            decision["case_status"] = "needs_investigation"
        if not decision.get("resolution_actions"):
            decision["resolution_actions"] = ["escalate_for_manual_review"]
    if not refs:
        confidence = min(confidence, 0.3)
        decision["primary_issue"] = "insufficient_evidence"
        decision["case_status"] = "needs_investigation"

    decision["confidence"] = round(max(0.0, min(1.0, confidence)), 4)

    if decision["case_status"] == "no_action":
        decision["financial_resolution"]["recommended_refund_brl"] = 0.0
        decision["financial_resolution"]["refund_lines"] = []

    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": decision["primary_issue"],
            "secondary_issues": decision["secondary_issues"],
            "case_status": decision["case_status"],
            "confidence": decision["confidence"],
        },
        "affected_entities": decision["affected_entities"],
        "claim_assessments": decision["claim_assessments"],
        "entity_resolution": entity["entity_resolution"],
        "customer_context": entity["customer_context"],
        "shipment_analysis": decision["shipment_analysis"],
        "payment_analysis": decision["payment_analysis"],
        "root_cause_analysis": decision["root_cause_analysis"],
        "evidence_refs": refs,
        "data_conflicts": ctx.data_conflicts[:5],
        "financial_resolution": decision["financial_resolution"],
        "resolution_actions": decision["resolution_actions"],
    }
    ctx.emit(
        "verification_completed",
        "verifier",
        decision_code="passed",
        evidence_refs=refs[:8],
        attributes={
            "primary_issue": output["assessment"]["primary_issue"],
            "evidence_count": len(refs),
            "mcp_calls": ctx.call_count,
        },
    )
    return output
