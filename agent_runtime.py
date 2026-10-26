"""Planner-agnostic Agent runtime over the HTTP tool adapter. No AI/model calls."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class AgentClientError(Exception):
    pass


class PlannerError(Exception):
    pass


@dataclass(frozen=True)
class Action:
    tool: str
    arguments: dict
    request_id: str | None = None
    confirmation_id: str | None = None
    approve: bool = False
    label: str = ""


@dataclass
class Step:
    index: int
    action: Action
    ok: bool
    status: int
    data: dict | None
    error: dict | None
    duplicate: bool = False


@dataclass
class RunReport:
    run_id: str
    ok: bool
    steps: list[Step] = field(default_factory=list)
    error: dict | None = None


class Planner(Protocol):
    def next(self, history: list[Step]) -> Action | None:
        ...


class Approver(Protocol):
    def __call__(self, action: Action, confirmation: dict) -> bool:
        ...


class AgentClient:
    def __init__(self, base_url: str, timeout: float = 15, session_file: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session_file = Path(session_file) if session_file else None
        self.session_token = None
        self._load_session()

    def login(self, role: str = "customer") -> dict:
        if self.session_token:
            status, body = self._request("/api/session")
            if status == 200 and body.get("authenticated") and body.get("role") == role:
                return body
            self.session_token = None
        status, body = self._request("/api/session", "POST", {"role": role})
        if status != 200 or body.get("role") != role:
            raise AgentClientError(body.get("error", {}).get("message") or "身份选择失败")
        self._save_session()
        return body

    def tools(self) -> dict[str, dict]:
        status, body = self._request("/api/agent/tools")
        if status != 200 or not body.get("ok"):
            raise AgentClientError(body.get("error", {}).get("message") or "工具列表读取失败")
        return {item["name"]: item for item in body["data"]["tools"]}

    def confirmation(self, confirmation_id: str) -> tuple[int, dict]:
        return self._request(f"/api/agent/confirmations/{quote(confirmation_id, safe='')}")

    def approve(self, confirmation_id: str) -> tuple[int, dict]:
        return self._request(f"/api/agent/confirmations/{quote(confirmation_id, safe='')}/approve", "POST")

    def call(self, tool: str, arguments: dict, request_id: str | None = None,
             confirmation_id: str | None = None) -> tuple[int, dict]:
        payload = {"arguments": arguments}
        if request_id is not None:
            payload["request_id"] = request_id
        if confirmation_id is not None:
            payload["confirmation_id"] = confirmation_id
        return self._request(f"/api/agent/tools/{quote(tool, safe='.')}", "POST", payload)

    def _request(self, path: str, method: str = "GET", payload=None) -> tuple[int, dict]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        if self.session_token:
            headers["Cookie"] = f"shop_session={self.session_token}"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status, raw, response_headers = response.status, response.read(), response.headers
        except HTTPError as error:
            status, raw, response_headers = error.code, error.read(), error.headers
        except URLError as error:
            raise AgentClientError(f"无法连接 {self.base_url}：{error.reason}") from error
        self._capture_session(response_headers)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {"ok": False, "data": None,
                    "error": {"code": "INVALID_RESPONSE", "message": "服务端返回非 JSON", "status": status}}
        if not isinstance(body, dict):
            body = {"ok": False, "data": None,
                    "error": {"code": "INVALID_RESPONSE", "message": "服务端返回格式无效", "status": status}}
        return status, body

    def _capture_session(self, headers) -> None:
        if headers is None:
            return
        for raw in headers.get_all("Set-Cookie", []):
            cookie = SimpleCookie()
            try:
                cookie.load(raw)
            except Exception:
                continue
            morsel = cookie.get("shop_session")
            if morsel is not None:
                self.session_token = morsel.value or None
                self._save_session()

    def _load_session(self) -> None:
        if self.session_file is None or not self.session_file.is_file():
            return
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if data.get("base_url") == self.base_url and data.get("token"):
            self.session_token = data["token"]

    def _save_session(self) -> None:
        if self.session_file is None or not self.session_token:
            return
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(json.dumps({
            "base_url": self.base_url, "token": self.session_token,
        }), encoding="utf-8")


class Runtime:
    def __init__(self, client: AgentClient, planner: Planner, approver: Approver | None = None,
                 run_id: str = "run-001", max_steps: int = 20, stop_on_error: bool = True):
        self.client = client
        self.planner = planner
        self.approver = approver
        self.run_id = run_id
        self.max_steps = max_steps
        self.stop_on_error = stop_on_error

    def run(self) -> RunReport:
        tools = self.client.tools()
        history: list[Step] = []
        finished = False
        error = None
        while len(history) < self.max_steps:
            try:
                action = self.planner.next(history)
            except PlannerError as exc:
                error = {"code": "PLANNER_ERROR", "message": str(exc), "status": 0}
                break
            if action is None:
                finished = True
                break
            descriptor = tools.get(action.tool)
            if descriptor is None:
                history.append(Step(len(history) + 1, action, False, 0, None,
                                    {"code": "TOOL_NOT_AVAILABLE",
                                     "message": f"当前身份不可用工具：{action.tool}", "status": 0}))
                break
            if action.approve:
                if not self._approve(action, history):
                    break
            request_id = action.request_id
            if request_id is None and descriptor.get("side_effect"):
                request_id = self._request_id(len(history) + 1)
            status, body = self.client.call(action.tool, action.arguments, request_id, action.confirmation_id)
            step = Step(len(history) + 1, action, bool(body.get("ok")), status,
                        body.get("data"), body.get("error"), bool(body.get("duplicate")))
            history.append(step)
            if not step.ok and self.stop_on_error:
                break
        else:
            error = {"code": "MAX_STEPS", "message": f"超过最大步骤数 {self.max_steps}", "status": 0}
        if error is None and not finished:
            failed = next((step for step in reversed(history) if not step.ok), None)
            error = failed.error if failed and failed.error else {
                "code": "RUN_INCOMPLETE", "message": "编排未正常结束", "status": 0}
        if not history and error is None:
            error = {"code": "EMPTY_RUN", "message": "planner 没有产生任何动作", "status": 0}
        ok = finished and error is None and all(step.ok for step in history)
        return RunReport(self.run_id, ok, history, error)

    def _approve(self, action: Action, history: list[Step]) -> bool:
        confirmation_id = action.confirmation_id
        if not confirmation_id:
            history.append(Step(len(history) + 1, action, False, 0, None,
                                {"code": "CONFIRMATION_REQUIRED", "message": "缺少确认编号", "status": 0}))
            return False
        if self.approver is None:
            history.append(Step(len(history) + 1, action, False, 0, None,
                                {"code": "APPROVAL_REQUIRED", "message": "需要审批策略", "status": 0}))
            return False
        status, body = self.client.confirmation(confirmation_id)
        if not body.get("ok"):
            history.append(Step(len(history) + 1, action, False, status, None, body.get("error")))
            return False
        confirmation = body.get("data") or {}
        if not self.approver(action, confirmation):
            history.append(Step(len(history) + 1, action, False, status, confirmation,
                                {"code": "APPROVAL_DENIED", "message": "审批被拒绝", "status": 0}))
            return False
        status, body = self.client.approve(confirmation_id)
        approval = Action("confirmation.approve", {}, confirmation_id=confirmation_id, label="批准确认")
        history.append(Step(len(history) + 1, approval, bool(body.get("ok")), status,
                            body.get("data"), body.get("error")))
        return bool(body.get("ok"))

    def _request_id(self, step: int) -> str:
        suffix = f"-{step:02d}"
        prefix = self.run_id[:100 - len(suffix)] if len(self.run_id) + len(suffix) > 100 else self.run_id
        return prefix + suffix
