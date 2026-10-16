# Workflow definition format

Definitions are JSON objects with `name`, optional `description`, optional JSON `metadata`, and 1–100 `tasks`. Task keys are unique and match letters, digits, underscore, period, and hyphen. Dependencies must reference another task, cannot repeat or self-reference, and cannot form a cycle. The whole definition is limited to 1 MB and each handler input to 256 KB.

```json
{
  "name": "data-processing-pipeline",
  "description": "Generate, transform, and summarize numbers",
  "metadata": {"team": "analytics", "example": true},
  "tasks": [
    {"key": "load", "type": "demo.generate_data", "input": {"count": 100, "start": 1},
     "depends_on": [], "timeout_seconds": 10, "max_attempts": 2,
     "retry_base_seconds": 0.5, "retry_policy": "exponential", "max_retry_delay_seconds": 8},
    {"key": "transform", "type": "demo.transform_data", "input": {"multiplier": 3},
     "depends_on": ["load"], "timeout_seconds": 10, "max_attempts": 3},
    {"key": "summarize", "type": "demo.summarize_data", "input": {},
     "depends_on": ["transform"], "timeout_seconds": 10, "max_attempts": 2}
  ]
}
```

Retry strategies are `exponential` (`base * 2^(attempt-1)`) or `fixed` (`base`); both are capped by `max_retry_delay_seconds` and receive bounded jitter. `timeout_seconds` is 1–3600, `max_attempts` is 1–20, and `retry_base_seconds` is 0–3600.

## Dependency results

The worker passes a context with `dependencies`, a mapping from each completed parent key to its persisted JSON output. For the example above, `demo.transform_data` reads the `load` output's `data` list and multiplies it. `demo.summarize_data` reads `transform`'s `data` list. Inputs can also provide their own `data`/`values`. No global or in-memory result state is used.

## Handler catalog

Fetch `/api/v1/task-types` for authoritative descriptions and JSON Schemas. Built-ins include `demo.generate_data`, `demo.validate_data`, `demo.transform_data`, `demo.summarize_data`, `demo.http_simulation` (simulated, no network request), `demo.delay`, `demo.echo`, `demo.fail_once`, `demo.fail_n_times`, `demo.permanent_failure`, `demo.validate_order`, `demo.check_inventory`, `demo.process_payment`, and `demo.send_confirmation`. All task results must be JSON serializable and at most 1 MB.

The UI's Workflow Builder validates with the same backend schema, then saves a definition. Saving an already loaded definition creates the next version; old runs retain their original snapshot.
