# Benchmarking

Run `python scripts/benchmark.py --scenario sequential --iterations 20 --workers 4` against an isolated live stack. Options include `parallel` and `retries`, `--base-url`, `--timeout`, and `--output`. Raw JSON defaults to `artifacts/benchmark-results.json` and records environment URL, configured worker count, iteration count, completion/failure/task/retry counts, throughput, and p50/p95/p99 workflow, enqueue, and scheduling latency.

No benchmark has been run for this generalization change. Throughput and latency depend on hardware, PostgreSQL settings, worker count, task duration, and competing load. Do not compare results unless those parameters and the raw JSON are retained. Small sample percentiles are descriptive only.
