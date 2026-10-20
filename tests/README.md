# Test scope

Keep tests for failures that change a user decision, lose portfolio edits, expose secrets, break provider interoperability, misdate returns or duplicate delivery. Use the narrowest meaningful boundary.

- Arithmetic and pricing: exact quantities/cash, finite prices, real quote timestamps and session-calendar behavior. Retrieval time must never masquerade as observation time.
- Model integration: the actual ChatOpenAI SDK sends a tool request, receives its result and finalizes the current report schema. Readiness uses that same schema. Mocking only the narrative function cannot establish provider compatibility.
- Portfolio and operations: editable imports, save boundaries, latest saved positions at schedule time, cancellation, optional delivery and idempotency.
- Security: authorization/CSRF, safe HTML, source redirects and private-address blocking, credential isolation and email recipient restrictions.
- Browser smoke: core editing/report behavior and small-screen layout; no snapshots of generated prose.

Removed suites enforced obsolete report templates, event grades, fixed search queues, publisher ranking and formatting internals. They were protecting constraints the product no longer wants. Do not replace them with assertions about exact analyst headings, prose length or call counts. Live configured-provider checks are manual workspace probes, not credential-dependent unit tests.
