"""Typed, bounded, allowlisted task handlers. JSON input never selects code to execute."""
import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import select

from .db import Ledger


class PermanentTaskError(ValueError):
    permanent = True


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="allow")


class DelayInput(StrictInput):
    seconds: float = Field(default=0.1, ge=0, le=30)
    value: Any = None


class GenerateInput(StrictInput):
    count: int = Field(default=10, ge=1, le=10000)
    start: int = 0


class DataInput(StrictInput):
    data: list[int | float] | None = None
    values: list[int | float] | None = None
    multiplier: float = Field(default=2, ge=-10000, le=10000)


class SummaryInput(StrictInput):
    data: list[int | float] | None = None
    values: list[int | float] | None = None


class FailureInput(StrictInput):
    failures: int = Field(default=1, ge=1, le=20)
    message: str = Field(default="Configured retryable failure", max_length=500)


class PaymentInput(StrictInput):
    operation_key: str | None = Field(default=None, max_length=200)
    amount: float = Field(default=42, ge=0, le=1_000_000)
    fail_first: int = Field(default=0, ge=0, le=20)
    crash_after_commit: bool = False


class ValidateOrderInput(StrictInput):
    order_id: str = Field(default="demo-order", max_length=200)
    valid: bool = True
    delay: float = Field(default=0.05, ge=0, le=30)


class InventoryInput(StrictInput):
    sku: str = Field(default="demo-sku", max_length=200)
    delay: float = Field(default=0.1, ge=0, le=30)


class ConfirmationInput(StrictInput):
    recipient: str = Field(default="customer@example.test", max_length=320)


class HttpSimulationInput(StrictInput):
    url: str = Field(default="https://example.test/", max_length=2048)
    status_code: int = Field(default=200, ge=100, le=599)
    latency_seconds: float = Field(default=0.05, ge=0, le=30)


@dataclass(frozen=True)
class HandlerSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    function: Callable


REGISTRY: dict[str, HandlerSpec] = {}


def register(name: str, description: str, input_model: type[BaseModel]):
    def decorator(function):
        if name in REGISTRY:
            raise RuntimeError(f"Handler already registered: {name}")
        REGISTRY[name] = HandlerSpec(name, description, input_model, function)
        return function
    return decorator


def validate_handler_input(name: str, value: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 256_000:
            raise ValueError("input exceeds the 256 KB size limit")
        return TypeAdapter(REGISTRY[name].input_model).validate_python(value).model_dump(mode="json")
    except ValidationError as exc:
        raise ValueError(f"Invalid input for {name}: {exc.errors(include_url=False)}") from exc
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid input for {name}: {exc}") from exc


def catalog() -> list[dict[str, Any]]:
    items = []
    for spec in REGISTRY.values():
        output_schema = dict(OUTPUT_SCHEMAS[spec.name])
        if OUTPUT_REQUIRED.get(spec.name):
            output_schema["required"] = OUTPUT_REQUIRED[spec.name]
        items.append({"type": spec.name, "description": spec.description,
                      "input_schema": spec.input_model.model_json_schema(), "output_schema": output_schema})
    return items


def validate_output(name: str, value: Any) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PermanentTaskError("Task handler returned a non-JSON result") from exc
    if len(encoded.encode("utf-8")) > 1_000_000:
        raise PermanentTaskError("Task output exceeds the 1 MB limit")
    if not isinstance(value, dict):
        raise PermanentTaskError(f"Handler {name} must return an object matching its output schema")
    schema = OUTPUT_SCHEMAS[name]
    for field in OUTPUT_REQUIRED.get(name, []):
        if field not in value:
            raise PermanentTaskError(f"Handler {name} output is missing required field '{field}'")
    for field, spec in schema.get("properties", {}).items():
        if field not in value:
            continue
        expected = spec.get("type")
        actual = value[field]
        valid = (expected == "string" and isinstance(actual, str)) or (
            expected == "boolean" and isinstance(actual, bool)) or (
            expected == "integer" and isinstance(actual, int) and not isinstance(actual, bool)) or (
            expected == "number" and isinstance(actual, (int, float)) and not isinstance(actual, bool)) or (
            expected == "array" and isinstance(actual, list)) or (
            expected == "object" and isinstance(actual, dict)) or expected is None
        if not valid:
            raise PermanentTaskError(f"Handler {name} output field '{field}' must be {expected}")
    return value


def dependency_payload(context: dict[str, Any], explicit: list | None = None) -> list:
    if explicit is not None:
        return explicit
    found = []
    for output in context.get("dependencies", {}).values():
        if isinstance(output, dict):
            found.append(output.get("data", output.get("values", output)))
        else:
            found.append(output)
    if not found:
        return []
    return found[0] if len(found) == 1 and isinstance(found[0], list) else found


@register("demo.echo", "Return the provided JSON object unchanged.", EchoInput)
async def echo(data: dict[str, Any], context: dict) -> dict:
    return data


@register("demo.delay", "Wait for a bounded interval and return the configured value.", DelayInput)
async def delay(data: dict[str, Any], context: dict) -> dict:
    await asyncio.sleep(data.get("seconds", 0.1))
    return {"value": data.get("value"), "completed": True}


@register("demo.generate_data", "Generate a bounded sequence of integer data.", GenerateInput)
async def generate_data(data: dict[str, Any], context: dict) -> dict:
    start = data.get("start", 0)
    return {"data": list(range(start, start + data.get("count", 10)))}


@register("demo.validate_data", "Validate a list from input or dependency outputs.", DataInput)
async def validate_data(data: dict[str, Any], context: dict) -> dict:
    values = dependency_payload(context, data.get("data") or data.get("values"))
    if not isinstance(values, list) or not values:
        raise PermanentTaskError("No data was provided by input or dependencies")
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in values):
        raise PermanentTaskError("Dataset must contain only numbers")
    return {"valid": True, "count": len(values), "data": values}


@register("demo.transform_data", "Multiply numeric data from a dependency or input.", DataInput)
async def transform_data(data: dict[str, Any], context: dict) -> dict:
    values = dependency_payload(context, data.get("data") or data.get("values"))
    if not isinstance(values, list):
        raise PermanentTaskError("No numeric data was provided")
    return {"data": [value * data.get("multiplier", 2) for value in values]}


@register("demo.summarize_data", "Calculate count, sum, average, minimum, and maximum.", SummaryInput)
async def summarize_data(data: dict[str, Any], context: dict) -> dict:
    values = dependency_payload(context, data.get("data") or data.get("values"))
    if not isinstance(values, list) or not values:
        raise PermanentTaskError("No data was provided to summarize")
    return {"count": len(values), "sum": sum(values), "average": sum(values) / len(values),
            "min": min(values), "max": max(values)}


@register("demo.http_simulation", "Simulate a bounded HTTP request without network access.", HttpSimulationInput)
async def http_simulation(data: dict[str, Any], context: dict) -> dict:
    await asyncio.sleep(data.get("latency_seconds", 0.05))
    status = data.get("status_code", 200)
    if status >= 500:
        raise RuntimeError(f"Simulated HTTP {status}")
    if status >= 400:
        raise PermanentTaskError(f"Simulated HTTP {status}")
    return {"url": data.get("url"), "status_code": status, "ok": True}


@register("demo.fail_once", "Raise a retryable error on the first attempt only.", StrictInput)
async def fail_once(data: dict[str, Any], context: dict) -> dict:
    if context["attempt"] == 1:
        raise RuntimeError("Configured one-time failure")
    return {"succeeded_after": context["attempt"]}


@register("demo.fail_n_times", "Fail retryably for N attempts, then return success.", FailureInput)
async def fail_n_times(data: dict[str, Any], context: dict) -> dict:
    if context["attempt"] <= data.get("failures", 1):
        raise RuntimeError(data.get("message", "Configured retryable failure"))
    return {"succeeded_after": context["attempt"]}


@register("demo.permanent_failure", "Raise a permanent error that bypasses retries.", StrictInput)
async def permanent_failure(data: dict[str, Any], context: dict) -> dict:
    raise PermanentTaskError(str(data.get("message", "Configured permanent failure")))


@register("demo.validate_order", "Validate a sample order.", ValidateOrderInput)
async def validate_order(data: dict[str, Any], context: dict) -> dict:
    await asyncio.sleep(data.get("delay", 0.05))
    if data.get("valid", True) is False:
        raise PermanentTaskError("Order validation failed")
    return {"valid": True, "order_id": data.get("order_id", "demo-order")}


@register("demo.check_inventory", "Simulate an inventory availability check.", InventoryInput)
async def check_inventory(data: dict[str, Any], context: dict) -> dict:
    await asyncio.sleep(data.get("delay", 0.1))
    return {"available": True, "sku": data.get("sku", "demo-sku")}


@register("demo.process_payment", "Create an idempotent demo payment ledger entry.", PaymentInput)
async def process_payment(data: dict[str, Any], context: dict) -> dict:
    session = context["session"]
    key = str(data.get("operation_key", context["task_id"]))
    existing = (await session.execute(select(Ledger).where(Ledger.operation_key == key))).scalar_one_or_none()
    if existing:
        return existing.result
    failures = int(data.get("fail_first", 0))
    if failures and os.getenv("ENVIRONMENT", "development").lower() != "development":
        raise PermanentTaskError("Failure injection is disabled outside development")
    if context["attempt"] <= failures:
        raise RuntimeError(f"Injected payment failure {context['attempt']}/{failures}")
    value = {"payment_id": f"pay-{key[:12]}", "charged": float(data.get("amount", 42.0))}
    dialect = session.bind.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
        statement = insert(Ledger).values(operation_key=key, result=value).on_conflict_do_nothing(
            index_elements=[Ledger.operation_key]
        )
        await session.execute(statement)
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        statement = insert(Ledger).values(operation_key=key, result=value).on_conflict_do_nothing(
            index_elements=[Ledger.operation_key]
        )
        await session.execute(statement)
    else:
        session.add(Ledger(operation_key=key, result=value))
    await session.commit()
    result = (await session.execute(select(Ledger).where(Ledger.operation_key == key))).scalar_one()
    if data.get("crash_after_commit") and context["attempt"] == 1:
        raise RuntimeError("Simulated crash after idempotent payment commit")
    return result.result


@register("demo.send_confirmation", "Record a simulated confirmation result.", ConfirmationInput)
async def send_confirmation(data: dict[str, Any], context: dict) -> dict:
    await asyncio.sleep(0.02)
    return {"sent": True, "recipient": data.get("recipient", "customer@example.test")}


# Compatibility mapping for existing call sites; registry remains authoritative.
HANDLERS = {name: spec.function for name, spec in REGISTRY.items()}

OUTPUT_SCHEMAS = {
    "demo.echo": {"type": "object", "description": "The submitted JSON object."},
    "demo.delay": {"type": "object", "properties": {"value": {}, "completed": {"type": "boolean"}}},
    "demo.generate_data": {"type": "object", "properties": {"data": {"type": "array", "items": {"type": "integer"}}}},
    "demo.validate_data": {"type": "object", "properties": {"valid": {"type": "boolean"}, "count": {"type": "integer"}, "data": {"type": "array"}}},
    "demo.transform_data": {"type": "object", "properties": {"data": {"type": "array", "items": {"type": "number"}}}},
    "demo.summarize_data": {"type": "object", "properties": {"count": {"type": "integer"}, "sum": {"type": "number"}, "average": {"type": "number"}, "min": {"type": "number"}, "max": {"type": "number"}}},
    "demo.http_simulation": {"type": "object", "properties": {"url": {"type": "string"}, "status_code": {"type": "integer"}, "ok": {"type": "boolean"}}},
    "demo.fail_once": {"type": "object", "properties": {"succeeded_after": {"type": "integer"}}},
    "demo.fail_n_times": {"type": "object", "properties": {"succeeded_after": {"type": "integer"}}},
    "demo.permanent_failure": {"type": "never", "description": "Raises PermanentTaskError on every attempt."},
    "demo.validate_order": {"type": "object", "properties": {"valid": {"type": "boolean"}, "order_id": {"type": "string"}}},
    "demo.check_inventory": {"type": "object", "properties": {"available": {"type": "boolean"}, "sku": {"type": "string"}}},
    "demo.process_payment": {"type": "object", "properties": {"payment_id": {"type": "string"}, "charged": {"type": "number"}}},
    "demo.send_confirmation": {"type": "object", "properties": {"sent": {"type": "boolean"}, "recipient": {"type": "string"}}},
}
OUTPUT_REQUIRED = {
    "demo.delay": ["completed"], "demo.generate_data": ["data"],
    "demo.validate_data": ["valid", "count", "data"], "demo.transform_data": ["data"],
    "demo.summarize_data": ["count", "sum", "average", "min", "max"],
    "demo.http_simulation": ["url", "status_code", "ok"],
    "demo.fail_once": ["succeeded_after"], "demo.fail_n_times": ["succeeded_after"],
    "demo.validate_order": ["valid", "order_id"],
    "demo.check_inventory": ["available", "sku"],
    "demo.process_payment": ["payment_id", "charged"],
    "demo.send_confirmation": ["sent", "recipient"],
}
