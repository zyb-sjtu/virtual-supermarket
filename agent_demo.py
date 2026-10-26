"""Deterministic customer shopping demo. This is a scripted planner, not an LLM."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import db
from agent_runtime import Action, AgentClient, AgentClientError, PlannerError, Runtime, Step


def _last_data(history: list[Step], tool: str) -> dict:
    for step in reversed(history):
        if step.ok and step.action.tool == tool and isinstance(step.data, dict):
            return step.data
    return {}


class BuyCancelPlanner:
    def __init__(self, query="", category="", min_price_cents=None, max_price_cents=None,
                 sort="price_asc", quantity=1, reason="Agent 演示取消"):
        self.query = query
        self.category = category
        self.min_price_cents = min_price_cents
        self.max_price_cents = max_price_cents
        self.sort = sort
        self.quantity = quantity
        self.reason = reason
        self.stage = 0
        self.product_id = None
        self.order_id = None

    def next(self, history: list[Step]) -> Action | None:
        if self.stage == 0:
            self.stage = 1
            arguments = {"page": 1, "sort": self.sort, "in_stock": True}
            if self.query:
                arguments["query"] = self.query
            if self.category:
                arguments["category"] = self.category
            if self.min_price_cents is not None:
                arguments["min_price_cents"] = self.min_price_cents
            if self.max_price_cents is not None:
                arguments["max_price_cents"] = self.max_price_cents
            return Action("customer.search_products", arguments, label="搜索可售商品")

        if self.stage == 1:
            products = _last_data(history, "customer.search_products").get("products", [])
            candidates = [product for product in products if product.get("stock", 0) >= self.quantity]
            if not candidates:
                raise PlannerError("没有找到库存足够的在售商品")
            self.product_id = candidates[0]["id"]
            self.stage = 2
            return Action("customer.get_product", {"product_id": self.product_id}, label="读取商品详情")

        if self.stage == 2:
            self.stage = 3
            return Action("customer.prepare_buy_now",
                          {"product_id": self.product_id, "quantity": self.quantity}, label="预览立即购买")

        if self.stage == 3:
            prepared = _last_data(history, "customer.prepare_buy_now")
            confirmation_id = (prepared.get("confirmation") or {}).get("id")
            if not confirmation_id:
                raise PlannerError("购买预览没有返回确认编号")
            self.stage = 4
            return Action("customer.commit_buy_now",
                          {"product_id": self.product_id, "quantity": self.quantity},
                          confirmation_id=confirmation_id, approve=True, label="批准并提交购买")

        if self.stage == 4:
            order = _last_data(history, "customer.commit_buy_now").get("order") or {}
            self.order_id = order.get("id")
            if not self.order_id:
                raise PlannerError("购买结果没有返回订单号")
            self.stage = 5
            return Action("customer.get_my_orders", {"page": 1}, label="查看我的订单")

        if self.stage == 5:
            self.stage = 6
            return Action("customer.prepare_order_cancel",
                          {"order_id": self.order_id, "reason": self.reason}, label="预览取消订单")

        if self.stage == 6:
            prepared = _last_data(history, "customer.prepare_order_cancel")
            confirmation_id = (prepared.get("confirmation") or {}).get("id")
            if not confirmation_id:
                raise PlannerError("取消预览没有返回确认编号")
            self.stage = 7
            return Action("customer.commit_order_cancel",
                          {"order_id": self.order_id, "reason": self.reason},
                          confirmation_id=confirmation_id, approve=True, label="批准并提交取消")

        if self.stage == 7:
            self.stage = 8
            return Action("customer.get_my_order", {"order_id": self.order_id}, label="核对最终订单")

        return None


class ScriptedApprover:
    def __init__(self, allow=True, interactive=False):
        self.allow = allow
        self.interactive = interactive

    def __call__(self, action: Action, confirmation: dict) -> bool:
        print(json.dumps({
            "approval_request": {
                "label": action.label,
                "tool": action.tool,
                "preview": confirmation.get("preview"),
            }
        }, ensure_ascii=False), flush=True)
        if self.interactive:
            answer = input("批准该操作? [y/N] ").strip().lower()
            return answer in ("y", "yes", "是")
        return self.allow


def _step_line(step: Step) -> dict:
    return {
        "step": step.index,
        "tool": step.action.tool,
        "label": step.action.label,
        "ok": step.ok,
        "status": step.status,
        "duplicate": step.duplicate,
        "data": step.data,
        "error": step.error,
    }


def _final_status(report):
    data = _last_data(report.steps, "customer.get_my_order")
    status = (data.get("order") or {}).get("status")
    if status:
        return status
    data = _last_data(report.steps, "customer.commit_order_cancel")
    return (data.get("order") or {}).get("status")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="确定性 Agent 购物/取消演示（非 LLM）")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--query", default="苹果")
    parser.add_argument("--category", default="")
    parser.add_argument("--min-price-cents", type=int)
    parser.add_argument("--max-price-cents", type=int)
    parser.add_argument("--sort", default="price_asc", choices=db.SORTS)
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--reason", default="Agent 演示取消")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--session-file", default="",
                        help="复用同一演示会话，便于用相同 run-id 验证幂等重放")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--interactive", action="store_true", help="改为终端人工批准")
    args = parser.parse_args(argv)

    run_id = args.run_id or "demo-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    client = AgentClient(args.base, session_file=args.session_file or None)
    try:
        client.login("customer")
    except AgentClientError as error:
        print(json.dumps({"ok": False, "error": {"code": "CONNECTION_ERROR", "message": str(error)}},
                         ensure_ascii=False))
        return 1

    planner = BuyCancelPlanner(query=args.query, category=args.category,
                               min_price_cents=args.min_price_cents,
                               max_price_cents=args.max_price_cents, sort=args.sort,
                               quantity=args.quantity, reason=args.reason)
    approver = ScriptedApprover(interactive=args.interactive)
    report = Runtime(client, planner, approver, run_id=run_id, max_steps=args.max_steps).run()
    for step in report.steps:
        print(json.dumps(_step_line(step), ensure_ascii=False), flush=True)
    print(json.dumps({
        "summary": {
            "run_id": run_id,
            "ok": report.ok,
            "steps": len(report.steps),
            "order_id": planner.order_id,
            "final_status": _final_status(report),
            "error": report.error,
        }
    }, ensure_ascii=False), flush=True)
    codes = {step.error.get("code") for step in report.steps if step.error}
    if not report.ok and "TOOL_NOT_AVAILABLE" in codes:
        return 2
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
