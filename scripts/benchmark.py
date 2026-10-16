"""Reproducible API workflow benchmark; run against an isolated live stack."""
import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


def call(base, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data,
                                     headers={"Accept": "application/json",
                                              **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 3)


def definition(scenario):
    if scenario == "parallel":
        return {"name": "benchmark-parallel", "tasks": [
            {"key": "left", "type": "demo.delay", "input": {"seconds": 0.2}},
            {"key": "right", "type": "demo.delay", "input": {"seconds": 0.2}},
            {"key": "join", "type": "demo.echo", "input": {"joined": True},
             "depends_on": ["left", "right"]}]}
    if scenario == "retries":
        return {"name": "benchmark-retries", "tasks": [
            {"key": "flaky", "type": "demo.fail_n_times", "input": {"failures": 1},
             "max_attempts": 2, "retry_base_seconds": 0.05, "max_retry_delay_seconds": 0.5}]}
    return {"name": "benchmark-sequential", "tasks": [
        {"key": "first", "type": "demo.echo", "input": {"step": 1}},
        {"key": "second", "type": "demo.echo", "input": {"step": 2}, "depends_on": ["first"]},
        {"key": "third", "type": "demo.echo", "input": {"step": 3}, "depends_on": ["second"]}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--scenario", choices=["sequential", "parallel", "retries"], default="sequential")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--workers", type=int, default=None, help="Record configured worker count for reproducibility")
    parser.add_argument("--output", default="artifacts/benchmark-results.json")
    args = parser.parse_args()
    if not 1 <= args.iterations <= 500:
        parser.error("--iterations must be between 1 and 500")
    base = args.base_url.rstrip("/")
    health = call(base, "/api/v1/health")
    created = call(base, "/api/v1/workflows", definition(args.scenario))
    started = time.monotonic()
    enqueued = []
    enqueue_ms = []
    for index in range(args.iterations):
        tick = time.perf_counter()
        run = call(base, f"/api/v1/workflows/{created['id']}/runs", {"input": {"iteration": index}})
        enqueue_ms.append((time.perf_counter() - tick) * 1000)
        enqueued.append((run["id"], time.monotonic()))

    completed = []
    pending = dict((run_id, at) for run_id, at in enqueued)
    deadline = time.monotonic() + args.timeout
    while pending and time.monotonic() < deadline:
        for run_id in list(pending):
            run = call(base, f"/api/v1/runs/{run_id}")
            if run["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                completed.append((run, time.monotonic() - pending.pop(run_id)))
        if pending:
            time.sleep(0.2)
    elapsed = time.monotonic() - started
    durations = [duration for _, duration in completed]
    scheduling_ms = []
    for run, _ in completed:
        starts = [datetime.fromisoformat(task["started_at"]) for task in run["tasks"] if task["started_at"]]
        if starts:
            created_at = datetime.fromisoformat(run["created_at"])
            scheduling_ms.append(max(0, (min(starts) - created_at).total_seconds() * 1000))
    retry_count = sum(max(0, len(run["attempts"]) - len(run["tasks"])) for run, _ in completed)
    output = {"scenario": args.scenario, "environment": {"api_base_url": base, "database": health.get("database"),
               "workers": args.workers, "iterations": args.iterations, "recorded_at": datetime.now().astimezone().isoformat()},
              "completed_workflows": sum(run["status"] == "SUCCEEDED" for run, _ in completed),
              "failed_workflows": sum(run["status"] == "FAILED" for run, _ in completed),
              "unfinished_workflows": len(pending),
              "completed_tasks": sum(task["status"] == "SUCCEEDED" for run, _ in completed for task in run["tasks"]),
              "throughput_workflows_per_second": round(len(completed) / elapsed, 3) if elapsed else None,
              "workflow_duration_seconds": {"p50": percentile(durations, .50), "p95": percentile(durations, .95),
                                             "p99": percentile(durations, .99)},
              "enqueue_latency_ms": {"p50": percentile(enqueue_ms, .50), "p95": percentile(enqueue_ms, .95),
                                     "p99": percentile(enqueue_ms, .99)},
              "scheduling_latency_ms": {"p50": percentile(scheduling_ms, .50), "p95": percentile(scheduling_ms, .95),
                                        "p99": percentile(scheduling_ms, .99)},
              "retry_count": retry_count, "run_ids": [run["id"] for run, _ in completed] + list(pending)}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))
    return 1 if output["unfinished_workflows"] or output["failed_workflows"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
