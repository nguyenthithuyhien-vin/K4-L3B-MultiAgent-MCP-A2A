from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def _cache_key(
    tool_name: str, arguments: dict[str, str]
) -> tuple[str, tuple[tuple[str, str], ...]]:
    return tool_name, tuple(sorted(arguments.items()))


@dataclass
class CaseContext:
    case: dict[str, Any]
    gateway: EvidenceGateway
    trace: TraceWriter
    available_tools: set[str]
    evidence_by_ref: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_by_tool: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    evidence_by_domain: dict[str, list[str]] = field(default_factory=dict)
    _cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = field(
        default_factory=dict
    )
    call_count: int = 0
    data_conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def case_id(self) -> str:
        return str(self.case["case_id"])

    def emit(
        self,
        event_type: str,
        actor: str,
        *,
        target: str | None = None,
        decision_code: str | None = None,
        tool_name: str | None = None,
        evidence_refs: list[str] | None = None,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        self.trace.emit(
            case_id=self.case_id,
            event_type=event_type,
            actor=actor,
            target=target,
            decision_code=decision_code,
            tool_name=tool_name,
            evidence_refs=evidence_refs,
            attributes=attributes,
        )

    async def call(
        self,
        tool_name: str,
        actor: str,
        *,
        allow_missing: bool = False,
        retries: int | None = None,
        **arguments: str,
    ) -> dict[str, Any] | None:
        if tool_name not in self.available_tools:
            if allow_missing:
                return None
            raise RuntimeError(f"Unknown MCP tool: {tool_name}")

        # Missing/optional tools: no retries (failed calls still hit MCP audit).
        if retries is None:
            retries = 0 if allow_missing else 1

        payload = {"case_id": self.case_id, **arguments}
        key = _cache_key(tool_name, payload)
        if key in self._cache:
            return self._cache[key]

        last_error: Exception | None = None
        evidence: dict[str, Any] | None = None
        for attempt in range(retries + 1):
            try:
                evidence = await self.gateway.call(tool_name, **payload)
                last_error = None
                break
            except (RuntimeError, ValueError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                if allow_missing:
                    return None
                raise

        if evidence is None:
            if allow_missing:
                return None
            assert last_error is not None
            raise last_error

        self.call_count += 1
        self._cache[key] = evidence
        ref = evidence["evidence_ref"]
        self.evidence_by_ref[ref] = evidence
        self.evidence_by_tool.setdefault(tool_name, []).append(evidence)
        domain = str(evidence.get("domain") or "unknown")
        self.evidence_by_domain.setdefault(domain, []).append(ref)
        self.emit(
            "tool_result_consumed",
            actor,
            tool_name=tool_name,
            evidence_refs=[ref],
        )
        return evidence

    def evidence_refs(self, limit: int = 30) -> list[str]:
        return list(self.evidence_by_ref)[:limit]

    def refs_for_domains(self, domains: list[str], limit: int = 8) -> list[str]:
        refs: list[str] = []
        for domain in domains:
            for ref in self.evidence_by_domain.get(domain, []):
                if ref not in refs:
                    refs.append(ref)
                if len(refs) >= limit:
                    return refs
        if not refs:
            return self.evidence_refs(limit)
        return refs

    def add_conflict(
        self,
        field: str,
        sources: list[str],
        selected_source: str | None,
        resolution_code: str,
    ) -> None:
        if len(self.data_conflicts) >= 5:
            return
        unique_sources = list(dict.fromkeys(sources))
        if len(unique_sources) < 2:
            return
        self.data_conflicts.append(
            {
                "field": field[:100],
                "sources": unique_sources[:5],
                "selected_source": selected_source,
                "resolution_code": resolution_code[:80],
            }
        )
