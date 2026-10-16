import pytest
from pydantic import ValidationError

from backend.engine import TaskStatus, WorkflowSpec, transition
from backend.handlers import REGISTRY, PermanentTaskError, catalog, transform_data, validate_handler_input, validate_output


def sample():
    return {"name": "test", "tasks": [
        {"key": "a", "type": "demo.echo"},
        {"key": "b", "type": "demo.echo"},
        {"key": "c", "type": "demo.echo", "depends_on": ["a", "b"]},
    ]}


def test_valid_dag_and_dependency_snapshot():
    spec = WorkflowSpec.model_validate(sample())
    assert [t.key for t in spec.tasks] == ["a", "b", "c"]
    assert spec.tasks[2].depends_on == ["a", "b"]


@pytest.mark.parametrize("change, message", [
    (lambda x: x["tasks"].append(x["tasks"][0].copy()), "unique"),
    (lambda x: x["tasks"][0].update(depends_on=["missing"]), "unknown"),
    (lambda x: (x["tasks"][0].update(depends_on=["b"]), x["tasks"][1].update(depends_on=["a"])), "cycle"),
    (lambda x: x["tasks"][0].update(depends_on=["a"]), "itself"),
])
def test_rejects_invalid_dags(change, message):
    import copy
    payload = copy.deepcopy(sample())
    change(payload)
    with pytest.raises(ValidationError, match=message):
        WorkflowSpec.model_validate(payload)


def test_transition_rules_are_centralized():
    transition(TaskStatus.READY, TaskStatus.RUNNING)
    with pytest.raises(ValueError, match="Illegal"):
        transition(TaskStatus.SUCCEEDED, TaskStatus.RUNNING)


def test_size_limit():
    payload = {"name": "large", "tasks": [{"key": str(i), "type": "demo.echo"} for i in range(101)]}
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(payload)


def test_registry_is_allowlisted_and_publishes_input_schemas():
    assert {"demo.generate_data", "demo.transform_data", "demo.summarize_data",
            "demo.fail_n_times", "demo.permanent_failure"} <= set(REGISTRY)
    assert any(item["type"] == "demo.generate_data" and "properties" in item["input_schema"]
               for item in catalog())
    with pytest.raises(KeyError):
        validate_handler_input("demo.exec", {})
    with pytest.raises(ValueError, match="Invalid input"):
        validate_handler_input("demo.delay", {"seconds": 100})


@pytest.mark.asyncio
async def test_dependency_outputs_flow_to_transform_handler():
    generated = {"generate": {"data": [1, 2, 3]}}
    result = await transform_data({"multiplier": 4}, {"dependencies": generated})
    assert result == {"data": [4, 8, 12]}


def test_handler_outputs_must_be_json_serializable_and_bounded():
    assert validate_output("demo.echo", {"answer": 42}) == {"answer": 42}
    with pytest.raises(PermanentTaskError, match="non-JSON"):
        validate_output("demo.echo", {"bad": object()})
    with pytest.raises(PermanentTaskError, match="missing required"):
        validate_output("demo.generate_data", {})
