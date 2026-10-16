"""API-driven acceptance runner for persisted, non-destructive lab scenarios."""
import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def request_json(base: str, path: str, method: str = "GET"):
    request = urllib.request.Request(base + path, method=method,
                                     headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc


def run_scenario(base: str, scenario_id: str, deadline: float):
    started = request_json(base, f"/api/v1/lab/scenarios/{scenario_id}/runs", "POST")
    run_ids = [item["id"] for item in started["runs"]]
    query = "&".join("run_ids=" + urllib.parse.quote(run_id) for run_id in run_ids)
    endpoint = f"/api/v1/lab/scenarios/{scenario_id}/results?{query}"
    while time.monotonic() < deadline:
        result = request_json(base, endpoint)
        if result["status"] in {"PASS", "FAIL", "INCONCLUSIVE"}:
            return result
        time.sleep(0.5)
    return {"scenario_id": scenario_id, "status": "INCONCLUSIVE", "runs": [
        {"id": run_id, "status": "deadline_exceeded"} for run_id in run_ids
    ], "assertions": [{"name": "completion deadline", "passed": None}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--scenarios", nargs="+", default=[
        "sequential", "parallel", "retries", "permanent-failure", "idempotency", "concurrent"
    ])
    parser.add_argument("--output", default="artifacts/reliability-results.json")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    results = []
    for scenario_id in args.scenarios:
        results.append(run_scenario(base, scenario_id, time.monotonic() + args.timeout))
    summary = {"base_url": base, "results": results,
               "failed": sum(item["status"] == "FAIL" for item in results),
               "inconclusive": sum(item["status"] == "INCONCLUSIVE" for item in results)}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
