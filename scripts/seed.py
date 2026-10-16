import json
import urllib.request

BASE = "http://localhost:8000"
DEMO = {
    "name": "order-processing", "description": "Parallel work, dependencies and retry example",
    "tasks": [
        {"key": "validate_order", "type": "demo.validate_order", "input": {"order_id": "order-42"}},
        {"key": "check_inventory", "type": "demo.check_inventory", "input": {"sku": "widget"}},
        {"key": "process_payment", "type": "demo.process_payment", "depends_on": ["validate_order", "check_inventory"], "input": {"amount": 42, "fail_first": 2}, "retry_base_seconds": 0.4},
        {"key": "send_confirmation", "type": "demo.send_confirmation", "depends_on": ["process_payment"]},
    ],
}

def post(path, value):
    req = urllib.request.Request(BASE + path, data=json.dumps(value).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        return json.load(response)

if __name__ == "__main__":
    definition = post("/workflows", DEMO)
    run = post(f"/workflows/{definition['id']}/runs", {"input": {"demo": True}})
    print(json.dumps({"workflow": definition, "run": run, "inspect": f"{BASE}/runs/{run['id']}"}, indent=2))
