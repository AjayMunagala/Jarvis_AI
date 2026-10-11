# JARVIS Roadmap

Roadmap items are goals, not claims about current functionality.

## Core v0.1 (complete)

- Modular FastAPI monolith with validated settings, health/readiness, mock chat, bounded provider execution, and tests.

## Core v0.2 — Mother Orchestrator (implemented)

- Add explicit conversation/request context and a small orchestration service between HTTP and providers.
- Add provider contract conformance tests and a configurable fake provider for timeout/failure cases.
- Define capability registry and tool interface types without executing external actions.
- Define audit event schemas and document production boundaries; keep the demo runnable offline.

Core v0.2 currently uses deterministic intent rules and the mock provider. It does not implement specialist model reasoning, persistent audit storage, or tool execution. Tool configuration is empty by design.

## Core v0.3 — Real LLM Provider Foundation (implemented; live access unverified)

- Add one optional OpenAI Chat Completions adapter behind the existing typed provider protocol.
- Keep deterministic mock mode as the default; configure OpenAI credentials, model, endpoint, and connection/read timeouts through environment variables.
- Validate provider output, sanitize errors, and retry only eligible transient failures within the existing orchestration request deadline.
- Give the validation expert a distinct configured instruction for model providers, without claiming that the mock or all seven personas perform distinct model reasoning.
- Preserve the chat response fields and mark model responses with provider identity and `demo=false`.

The adapter is verified with mocked HTTP transport only. No live credentials or model endpoint were available for verification. It does not add tools, external actions, or persistence.

## Later milestones

1. Application persistence: the optional PostgreSQL provisioning and migration-ledger foundation now exists; later add a reviewed data model, user/conversation records, persistence integration, and retrieval foundations.
2. Real model adapters: optional local CPU model and compatible hosted providers behind provider interfaces, with explicit cost, privacy, timeout, and retry controls.
3. Safe tools and permissions: tool registry/runtime, permission and approval gateway, independent verification, bounded retries, and fault isolation.
4. User experience: React/TypeScript interface, natural conversation, voice input/output, and supported desktop/mobile integrations.
5. Personal knowledge: document analysis, web research, memory, retrieval, and personalized learning/tutoring.
6. Personal workflows: task management, reminders, scheduling, and proactive monitoring/notifications.
7. Specialized capabilities: travel planning, creative tools, and smart-home integrations behind explicit user permissions.
8. High-risk domains: financial planning, expense analysis, stock-market research, and experimental forecasting with clear limitations and no transactions without explicit authorization.
9. Defensive operations: system health monitoring and defensive cybersecurity, with careful authorization and auditability.
10. Controlled improvement: model evaluation, fine-tuning experiments, versioning, and rollback with human review.

The long-term vision includes natural conversation and voice; desktop and supported mobile-device integrations; coding, debugging, test execution, fixes and independent verification; document analysis, web research, personal knowledge and long-term memory; task management, reminders, scheduling and personal workflows; financial planning, expense analysis, stock-market research and experimental forecasting; proactive monitoring and notifications; smart-home integrations; defensive cybersecurity and system health monitoring; personalized learning and tutoring; travel planning and creative tools; and controlled self-improvement, model evaluation, fine-tuning experiments and rollback.
