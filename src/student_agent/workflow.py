from __future__ import annotations

from typing import Any

from .agents import run_conflict_and_classify, run_entity_agent, run_specialists, run_verifier
from .context import CaseContext
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """L3B coordinator + specialist workflow with MCP evidence and observable trace."""
    tools = set(await gateway.list_tools())
    ctx = CaseContext(case=case, gateway=gateway, trace=trace, available_tools=tools)

    entity = await run_entity_agent(ctx)
    ctx.emit("handoff", "coordinator", target="order-agent", decision_code="investigate")

    order_id = None
    resolved = entity["entity_resolution"]["resolved_order_ids"]
    if resolved:
        order_id = resolved[0]

    specialist = await run_specialists(ctx, order_id)
    decision = run_conflict_and_classify(ctx, entity, specialist)
    return run_verifier(ctx, entity, decision)
