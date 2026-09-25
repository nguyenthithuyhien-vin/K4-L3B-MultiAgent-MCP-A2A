# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

Luồng chạy trong `solve_case` ([src/student_agent/workflow.py](src/student_agent/workflow.py)):

1. `CaseContext` bọc MCP gateway + cache theo `(tool, args)` trong phạm vi case.
2. Entity agent resolve/reject candidate order IDs và lấy customer history.
3. Coordinator giao việc lần lượt cho order / shipment / payment / policy agents.
4. Conflict resolver chọn facet dữ liệu khớp `primary_issue`, ghi `data_conflicts`.
5. Verifier siết invariants rồi emit `verification_completed`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | case candidates, customer hint | Resolve/reject order IDs; customer context | `get_order`, `get_customer_history` | `entity_resolution`, `customer_context` → coordinator |
| Coordinator | entity result | Assign tasks, emit handoff/task_assigned | none | orchestration only |
| Order/product | resolved `order_id` | Items, sellers, product context | `get_order_items`, `get_sellers`, `get_product_context` | entity ID sets → shipment/payment |
| Shipment | resolved `order_id` | Timeline, late seller detection | `get_shipment_summary` | shipment_analysis inputs |
| Payment/refund | resolved `order_id` | Payments, capture/refund lifecycle | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | payment_analysis inputs |
| Policy | `policy_version` | Machine-readable refund/action rules | `get_policy` | financial + case_status defaults |
| Conflict resolver | specialist bags + claim topics | Choose primary_issue, resolve source conflicts | none (uses cached evidence) | decision package → verifier |
| Verifier | decision + evidence bag | Schema/consistency invariants before finalize | none | L3B output JSON |

## 3. Entity resolution và A2A protocol

- Thử `claimed_order_id` và từng `candidate_order_ids` qua `get_order`.
- Candidate trả lỗi / not_found → `rejected_candidates`.
- Order hợp lệ đầu tiên → `resolved`; không có order hợp lệ → `not_found` + `insufficient_evidence`.
- Handoff envelope: `trace.emit(event_type=handoff|task_assigned, actor, target, decision_code, evidence_refs)`.
- Correlation always keyed by `case_id`. Không vòng lặp agent: pipeline một chiều.
- Timeout/retry: tối đa 1 lần thất bại được nuốt khi `allow_missing=True`; không đoán dữ liệu thiếu.

## 4. Evidence và conflict lifecycle

- Mọi MCP response đi qua `CaseContext.call` → validate envelope → lưu `evidence_ref` → emit `tool_result_consumed`.
- Cache chỉ trong case; không tái sử dụng evidence giữa case.
- Khi item/payment/shipment có ≥2 facet lệch nhau, ghi `data_conflicts` với `resolution_code` và `selected_source` theo issue đang xét (policy-aligned / event-aligned).
- `evidence_refs` trong output ⊆ refs đã consume (≤30).

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / tool error | 0 (allow_missing) | Skip tool; degrade confidence | `tool_result_consumed` + `tool_unavailable` |
| Entity not found/ambiguous | 0 | `insufficient_evidence`, `needs_investigation`, refund 0 | handoff `not_found`/`ambiguous` |
| Source conflict | 0 | Record conflict; pick policy/event-aligned facet | conflict fields in output |
| Invalid specialist result | 0 | Empty list / null analysis fields; verifier clamps confidence | `verification_completed` |

Query budget: khoảng 8–11 MCP calls/case (order×candidates, history, items, shipment, payments, payment timeline, optional refund, sellers, product, policy). Không scan tool catalog ngoài discovery một lần ở đầu run.

## 6. Verification invariants

Trước finalize verifier kiểm:

- `case_id` khớp input; `schema_version` = `day09-l3b-output-v2`
- `rejected_candidates` ∩ `resolved_order_ids` = ∅
- mọi `evidence_refs` thuộc bag MCP của case
- `case_status=no_action` ⇒ `recommended_refund_brl=0` và không có refund lines
- confidence ∈ [0,1]; hạ confidence khi thiếu entity/evidence
- financial + resolution_actions lấy từ policy rule của `primary_issue`

## 7. Reproducibility

- Runtime: Python ≥3.11, deps pin trong `pyproject.toml`
- Không LLM; deterministic rule engine trong `classify.py`
- Concurrency: sequential per case trong `day09 run`
- Lệnh: `day09 validate-inputs && day09 run && day09 validate && day09 package --output dist/submission.zip`
- Không ghi API key vào tài liệu hoặc artifact nộp bài
