"""Tool registry, schema validation and execution pipeline for Agent tools."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal

import agent_confirmations
from db import InventoryError

ROLES = frozenset(("customer", "merchant"))
RESERVED_ARGUMENTS = frozenset(("role", "buyer_key", "session", "session_token", "token", "confirmed"))


class ToolError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or []


@dataclass(frozen=True)
class ToolContext:
    role: Literal["merchant", "customer"]
    buyer_key: str | None = None


@dataclass(frozen=True)
class ToolCall:
    context: ToolContext
    arguments: dict
    request_id: str | None = None
    confirmation_id: str | None = None
    confirmation: dict | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    domain: Literal["catalog", "orders", "merchant_catalog", "inventory", "analytics", "ai_system"]
    description: str
    input_schema: dict
    allowed_roles: frozenset[str]
    side_effect: bool
    requires_confirmation: bool
    produces_confirmation: bool
    handler: Callable[[ToolCall], dict]


class ToolRegistry:
    def __init__(self, confirmations=None):
        self._tools: dict[str, ToolSpec] = {}
        self._confirmations = confirmations or agent_confirmations.store

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"重复工具名：{spec.name}")
        if not spec.allowed_roles or not spec.allowed_roles <= ROLES:
            raise ValueError(f"工具 {spec.name} 的角色无效")
        if spec.requires_confirmation and not spec.side_effect:
            raise ValueError(f"工具 {spec.name} 需要确认但不是写操作")
        if spec.produces_confirmation and spec.side_effect:
            raise ValueError(f"工具 {spec.name} 的确认生成工具不能有副作用")
        if not isinstance(spec.input_schema, dict) or spec.input_schema.get("type") != "object":
            raise ValueError(f"工具 {spec.name} 的 schema 必须是 object")
        if spec.input_schema.get("additionalProperties") is not False:
            raise ValueError(f"工具 {spec.name} 的 schema 必须禁止未知字段")
        for name in _schema_property_names(spec.input_schema):
            if name in RESERVED_ARGUMENTS:
                raise ValueError(f"工具 {spec.name} 使用了保留参数：{name}")
        self._tools[spec.name] = spec

    def list_for(self, context: ToolContext) -> list[dict]:
        return [_descriptor(spec) for spec in self._tools.values()
                if context.role in spec.allowed_roles]

    def resolve(self, name: str, context: ToolContext) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolError("TOOL_NOT_FOUND", "工具不存在", 404)
        if context.role not in spec.allowed_roles:
            raise ToolError("FORBIDDEN", "当前身份无权调用该工具", 403)
        return spec

    def invoke(self, name: str, arguments: dict, context: ToolContext,
               request_id: str | None = None, confirmation_id: str | None = None) -> dict:
        spec = self.resolve(name, context)
        validate_arguments(spec.input_schema, arguments)
        if spec.side_effect:
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
                raise ToolError("INVALID_ARGUMENT", "写工具需要 1–100 字符的 request_id")
        confirmation = None
        if spec.requires_confirmation:
            if not isinstance(confirmation_id, str) or not confirmation_id:
                raise ToolError("CONFIRMATION_REQUIRED", "该工具需要 confirmation_id", 409)
            try:
                confirmation = self._confirmations.validate_for_commit(
                    confirmation_id, context.role, context.buyer_key, spec.name, arguments, request_id)
            except agent_confirmations.ConfirmationError as error:
                raise ToolError(error.code, str(error), error.status) from error
        call = ToolCall(context, arguments, request_id, confirmation_id, confirmation)
        try:
            result = spec.handler(call)
        except InventoryError as error:
            raise _inventory_tool_error(error) from error
        except agent_confirmations.ConfirmationError as error:
            raise ToolError(error.code, str(error), error.status) from error
        if spec.requires_confirmation:
            try:
                self._confirmations.consume(confirmation_id, context.role, context.buyer_key, request_id)
            except agent_confirmations.ConfirmationError as error:
                raise ToolError(error.code, str(error), error.status) from error
        return result


def validate_arguments(schema: dict, value) -> None:
    _validate(schema, value, "arguments")


def _validate(schema: dict, value, path: str) -> None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ToolError("INVALID_ARGUMENT", f"{path} 必须是对象")
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                raise ToolError("INVALID_ARGUMENT", f"{path}.{name} 为必填项")
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise ToolError("INVALID_ARGUMENT", f"{path} 包含未知字段：{', '.join(sorted(unknown))}")
        for name, child_schema in properties.items():
            if name in value:
                _validate(child_schema, value[name], f"{path}.{name}")
        return
    if expected == "array":
        if not isinstance(value, list):
            raise ToolError("INVALID_ARGUMENT", f"{path} 必须是数组")
        if len(value) < schema.get("minItems", 0):
            raise ToolError("INVALID_ARGUMENT", f"{path} 至少需要 {schema['minItems']} 项")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolError("INVALID_ARGUMENT", f"{path} 最多允许 {schema['maxItems']} 项")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _validate(item_schema, item, f"{path}[{index}]")
        return
    if expected == "string":
        if not isinstance(value, str):
            raise ToolError("INVALID_ARGUMENT", f"{path} 必须是字符串")
        if len(value) < schema.get("minLength", 0):
            raise ToolError("INVALID_ARGUMENT", f"{path} 太短")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ToolError("INVALID_ARGUMENT", f"{path} 太长")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ToolError("INVALID_ARGUMENT", f"{path} 格式无效")
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError("INVALID_ARGUMENT", f"{path} 必须是整数")
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError("INVALID_ARGUMENT", f"{path} 小于最小值")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError("INVALID_ARGUMENT", f"{path} 大于最大值")
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise ToolError("INVALID_ARGUMENT", f"{path} 必须是布尔值")
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError("INVALID_ARGUMENT", f"{path} 必须是数字")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError("INVALID_ARGUMENT", f"{path} 不在允许值范围内")


def _schema_property_names(schema: dict) -> set[str]:
    names = set()
    if schema.get("type") == "object":
        for name, child in schema.get("properties", {}).items():
            names.add(name)
            names |= _schema_property_names(child)
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        names |= _schema_property_names(schema["items"])
    return names


def _descriptor(spec: ToolSpec) -> dict:
    return {
        "name": spec.name,
        "domain": spec.domain,
        "description": spec.description,
        "side_effect": spec.side_effect,
        "requires_confirmation": spec.requires_confirmation,
        "produces_confirmation": spec.produces_confirmation,
        "input_schema": spec.input_schema,
    }


def _inventory_tool_error(error: InventoryError) -> ToolError:
    message = str(error)
    if error.status == 404:
        return ToolError("NOT_FOUND", message, 404)
    if error.status == 409:
        if "请求编号" in message:
            return ToolError("IDEMPOTENCY_CONFLICT", message, 409)
        if "版本" in message:
            return ToolError("VERSION_CONFLICT", message, 409)
        if "SKU" in message:
            return ToolError("SKU_CONFLICT", message, 409)
        if "库存" in message or "下架" in message:
            return ToolError("CONFIRMATION_STALE", message, 409)
        return ToolError("CONFLICT", message, 409)
    if error.status == 403:
        return ToolError("FORBIDDEN", message, 403)
    if error.status == 401:
        return ToolError("UNAUTHENTICATED", message, 401)
    return ToolError("INVALID_ARGUMENT", message, error.status)
