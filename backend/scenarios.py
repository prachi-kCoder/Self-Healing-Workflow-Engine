"""Recruiter reliability-lab workflows; all runs use the regular engine path."""
from copy import deepcopy


SCENARIOS = {
    "sequential": {
        "name": "Sequential execution", "description": "Three tasks must execute in dependency order.",
        "expected": "All tasks succeed and each child starts after its parent finishes.",
        "definition": {"name": "lab-sequential", "tasks": [
            {"key": "start", "type": "demo.echo", "input": {"step": "start"}},
            {"key": "process", "type": "demo.echo", "input": {"step": "process"}, "depends_on": ["start"]},
            {"key": "finish", "type": "demo.echo", "input": {"step": "finish"}, "depends_on": ["process"]},
        ]},
    },
    "parallel": {
        "name": "Parallel execution", "description": "Two independent delays feed a join task.",
        "expected": "The two root task intervals overlap and the join starts after both finish.",
        "requires": "PostgreSQL with at least two workers for overlap verification.",
        "definition": {"name": "lab-parallel", "tasks": [
            {"key": "branch_a", "type": "demo.delay", "input": {"seconds": 0.6, "value": "a"}},
            {"key": "branch_b", "type": "demo.delay", "input": {"seconds": 0.6, "value": "b"}},
            {"key": "join", "type": "demo.echo", "input": {"joined": True},
             "depends_on": ["branch_a", "branch_b"]},
        ]},
    },
    "retries": {
        "name": "Automatic retries", "description": "A task fails twice and succeeds on its third attempt.",
        "expected": "Three persisted attempts, two failures, retry events, and a successful run.",
        "definition": {"name": "lab-retries", "tasks": [
            {"key": "flaky", "type": "demo.fail_n_times", "input": {"failures": 2},
             "max_attempts": 3, "retry_base_seconds": 0.1, "max_retry_delay_seconds": 1},
        ]},
    },
    "permanent-failure": {
        "name": "Permanent failure", "description": "A permanent error skips its dependent task.",
        "expected": "Failure is classified permanent; dependent task is skipped; run is FAILED.",
        "definition": {"name": "lab-permanent-failure", "tasks": [
            {"key": "reject", "type": "demo.permanent_failure", "max_attempts": 3},
            {"key": "must_not_run", "type": "demo.echo", "depends_on": ["reject"]},
        ]},
    },
    "idempotency": {
        "name": "Payment idempotency", "description": "Commit ledger then fail once before task completion.",
        "expected": "The retry reuses the persisted payment result; exactly one ledger entry exists.",
        "definition": {"name": "lab-idempotency", "tasks": [
            {"key": "payment", "type": "demo.process_payment",
             "input": {"operation_key": "lab-idempotency-fixed", "amount": 12.5,
                       "crash_after_commit": True},
             "max_attempts": 2, "retry_base_seconds": 0.1, "max_retry_delay_seconds": 1},
        ]},
    },
    "concurrent": {
        "name": "Concurrent workflow runs", "description": "Launch two runs from one saved definition.",
        "expected": "Both runs succeed with distinct task and event identities.",
        "definition": {"name": "lab-concurrent", "tasks": [
            {"key": "work", "type": "demo.delay", "input": {"seconds": 0.2, "value": "isolated"}},
        ]},
    },
    "cancellation": {
        "name": "Cooperative cancellation", "description": "Cancel a long running development delay.",
        "expected": "Cancel the created run from its run inspector; scheduler records terminal cancellation.",
        "manual": True,
        "definition": {"name": "lab-cancellation", "tasks": [
            {"key": "long_work", "type": "demo.delay", "input": {"seconds": 20},
             "timeout_seconds": 30, "max_attempts": 1},
        ]},
    },
    "worker-recovery": {
        "name": "Worker crash recovery", "description": "Verify lease expiry after a worker stops.",
        "expected": "Worker lease expires, recovery event is persisted, and another worker reclaims the task.",
        "manual": True, "requires": "PostgreSQL and worker process access; do not use SQLite.",
        "definition": {"name": "lab-worker-recovery", "tasks": [
            {"key": "long_work", "type": "demo.delay", "input": {"seconds": 25},
             "timeout_seconds": 30, "max_attempts": 2},
        ]},
    },
    "restart-recovery": {
        "name": "Service restart recovery", "description": "Inspect persisted work after a scheduler restart.",
        "expected": "Existing run snapshot and completed tasks persist; eligible work resumes.",
        "manual": True, "requires": "PostgreSQL/Docker process access.",
        "definition": {"name": "lab-restart-recovery", "tasks": [
            {"key": "long_work", "type": "demo.delay", "input": {"seconds": 20},
             "timeout_seconds": 30, "max_attempts": 2},
        ]},
    },
}


def scenario_definition(scenario_id: str):
    return deepcopy(SCENARIOS[scenario_id]["definition"])
