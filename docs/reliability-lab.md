# Demo & Reliability Lab

The lab creates definitions and runs through the same API, scheduler, workers, and database as user-created workflows. A status is `PASS` only when the result endpoint finds the required persisted assertions. `FAIL` means a terminal run violated an assertion. `INCONCLUSIVE` is used when the environment cannot prove the behavior. In-flight runs are `RUNNING`.

| Scenario | Persisted pass condition | Environment / procedure |
|---|---|---|
| Sequential | Three tasks succeed and child start timestamps follow parent finish timestamps. | Local or PostgreSQL. |
| Parallel | Independent branch task intervals overlap and join starts after both finish. | PostgreSQL, at least two workers. SQLite reports inconclusive. |
| Automatic retries | Three attempt rows (two failed, one succeeded) and two `task.retry_scheduled` events. | Local or PostgreSQL; development fault injection. |
| Permanent failure | Error classification is permanent, downstream task is skipped, run is failed. | Local or PostgreSQL; development fault injection. |
| Payment idempotency | Simulated post-ledger failure is retried, result is reused, one ledger row exists for the unique run key. | Development environment. |
| Concurrent workflows | Two separate runs succeed and have distinct task/event identities. | Local tests state isolation; PostgreSQL required to prove worker concurrency. |
| Cancellation | Start the long delay and press **Cancel this run**; persisted run status must become cancelled. | Lab manual action. |
| Worker recovery | Stop the isolated worker while its long task is running, wait beyond the lease, then start a worker. Requires lease-expiry event, reclaimed attempt, successful run. | Development PostgreSQL only. Do not stop shared/prod workers. |
| Restart recovery | Restart the scheduler with an incomplete run and inspect the same run ID and stored task state. | Development PostgreSQL/Docker. The API does not claim to infer a process restart from workflow events. |

Worker crash and service restart are intentionally operator-driven and are not automatically reported as passes. The result endpoint only uses evidence that can be read from saved run, task, attempt, event, and ledger rows.
