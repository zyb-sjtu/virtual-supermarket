"""P1 Agent tools: read-only business queries and customer checkout confirmation."""
from __future__ import annotations

import agent_confirmations
import db
from agent_registry import ToolContext, ToolError, ToolRegistry, ToolSpec

CUSTOMER = frozenset(("customer",))
MERCHANT = frozenset(("merchant",))
ANY_ROLE = frozenset(("customer", "merchant"))
CATEGORIES = tuple(db.CATEGORIES)


def _require_customer(context: ToolContext) -> None:
    if context.role != "customer" or not context.buyer_key:
        raise ToolError("FORBIDDEN", "仅顾客身份可调用该工具", 403)


def _require_merchant(context: ToolContext) -> None:
    if context.role != "merchant":
        raise ToolError("FORBIDDEN", "仅商户身份可调用该工具", 403)


def _customer_product(product: dict) -> dict:
    return {
        key: product[key] for key in
        ("id", "sku", "name", "price_cents", "stock", "category", "unit",
         "description", "zone", "active", "image_url")
    }


def _page(arguments: dict) -> int:
    return arguments.get("page", 1)


def _list_categories(call) -> dict:
    return {"categories": list(CATEGORIES)}


def _customer_search(call) -> dict:
    _require_customer(call.context)
    state = db.snapshot(page=_page(call.arguments), status="active",
                        search=call.arguments.get("query", ""), category=call.arguments.get("category", ""))
    return {
        "products": [_customer_product(product) for product in state["products"]],
        "page": state["page"],
        "pages": state["pages"],
        "product_count": state["product_count"],
        "page_size": state["page_size"],
    }


def _customer_orders(call) -> dict:
    _require_customer(call.context)
    return db.list_orders(_page(call.arguments), call.arguments.get("status", ""),
                          buyer_key=call.context.buyer_key)


def _customer_order(call) -> dict:
    _require_customer(call.context)
    return {"order": db.get_order(call.arguments["order_id"], buyer_key=call.context.buyer_key)}


def _merchant_products(call) -> dict:
    _require_merchant(call.context)
    state = db.snapshot(page=_page(call.arguments), status=call.arguments.get("status", "active"),
                        search=call.arguments.get("query", ""), category=call.arguments.get("category", ""))
    return {
        "products": state["products"],
        "page": state["page"],
        "pages": state["pages"],
        "product_count": state["product_count"],
        "page_size": state["page_size"],
        "active_count": state["active_count"],
        "inactive_count": state["inactive_count"],
        "stock_total": state["stock_total"],
    }


def _merchant_orders(call) -> dict:
    _require_merchant(call.context)
    return db.list_orders(_page(call.arguments), call.arguments.get("status", ""), buyer_key=None)


def _merchant_order(call) -> dict:
    _require_merchant(call.context)
    return {"order": db.get_order(call.arguments["order_id"], buyer_key=None)}


def _merchant_inventory(call) -> dict:
    _require_merchant(call.context)
    return db.inventory_history(call.arguments.get("product_id", ""), _page(call.arguments))


def _merchant_audit(call) -> dict:
    _require_merchant(call.context)
    return db.audit_history(_page(call.arguments))


def _merchant_sales(call) -> dict:
    _require_merchant(call.context)
    state = db.snapshot(page=1, status="active")
    return {key: state[key] for key in
            ("product_count", "active_count", "inactive_count", "stock_total", "movement_count",
             "sales_cents", "gross_sales_cents", "refunded_cents")}


def _preview_order(items: list[dict]) -> dict:
    preview_items = []
    total_cents = 0
    for item in items:
        product = db.product_by_id(item["product_id"])
        if not product["active"]:
            raise ToolError("CONFLICT", f"{product['name']}已下架", 409)
        if product["stock"] < item["quantity"]:
            raise ToolError("STOCK_SHORTAGE", f"{product['name']}库存不足", 409)
        line_total = product["price_cents"] * item["quantity"]
        total_cents += line_total
        preview_items.append({
            "product_id": product["id"],
            "name": product["name"],
            "sku": product["sku"],
            "unit": product["unit"],
            "quantity": item["quantity"],
            "unit_price_cents": product["price_cents"],
            "line_total_cents": line_total,
            "stock": product["stock"],
        })
    return {"items": preview_items, "total_cents": total_cents}


def _prepare_order(call) -> dict:
    _require_customer(call.context)
    items = db._order_items(call.arguments.get("items"))
    preview = _preview_order(items)
    confirmation = agent_confirmations.store.create(
        "customer.commit_order", call.context.role, call.context.buyer_key,
        {"items": call.arguments.get("items")}, preview)
    return {"preview": preview, "confirmation": confirmation}


def _commit_order(call) -> dict:
    _require_customer(call.context)
    confirmation = call.confirmation
    if confirmation is None:
        raise ToolError("CONFIRMATION_REQUIRED", "该工具需要已批准的确认", 409)
    preview = confirmation["preview"]
    items = [{"product_id": item["product_id"], "quantity": item["quantity"]} for item in preview["items"]]
    expected_prices = {item["product_id"]: item["unit_price_cents"] for item in preview["items"]}
    try:
        return db.create_order({"items": items, "request_id": call.request_id},
                               buyer_key=call.context.buyer_key, expected_prices=expected_prices)
    except db.InventoryError as error:
        if error.status == 409:
            if "请求编号" in str(error):
                raise ToolError("IDEMPOTENCY_CONFLICT", str(error), 409) from error
            raise ToolError("CONFIRMATION_STALE", str(error), 409) from error
        raise


def _object_schema(properties: dict, required=None) -> dict:
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


PAGE = {"type": "integer", "minimum": 1, "maximum": 1000000}
TEXT = {"type": "string", "maxLength": 100}
CATEGORY = {"type": "string", "maxLength": 20}
ORDER_STATUS = {"type": "string", "enum": ["", "completed", "cancelled"]}
ORDER_ID = {"type": "string", "minLength": 1, "maxLength": 100}
ITEM_SCHEMA = _object_schema({
    "product_id": {"type": "string", "minLength": 1, "maxLength": 100},
    "quantity": {"type": "integer", "minimum": 1, "maximum": 9999},
}, ["product_id", "quantity"])
ORDER_ITEMS = _object_schema({
    "items": {"type": "array", "minItems": 1, "maxItems": 50, "items": ITEM_SCHEMA},
}, ["items"])

SPECS = [
    ToolSpec("customer.list_categories", "catalog", "列出商品分类。",
             _object_schema({}), CUSTOMER, False, False, False, _list_categories),
    ToolSpec("customer.search_products", "catalog", "搜索在售商品并分页。",
             _object_schema({"query": TEXT, "category": CATEGORY, "page": PAGE}),
             CUSTOMER, False, False, False, _customer_search),
    ToolSpec("customer.get_my_orders", "orders", "查看当前顾客会话的订单。",
             _object_schema({"page": PAGE, "status": ORDER_STATUS}),
             CUSTOMER, False, False, False, _customer_orders),
    ToolSpec("customer.get_my_order", "orders", "查看当前顾客会话的一笔订单。",
             _object_schema({"order_id": ORDER_ID}, ["order_id"]),
             CUSTOMER, False, False, False, _customer_order),
    ToolSpec("customer.prepare_order", "orders", "预览顾客订单并生成确认令牌。",
             ORDER_ITEMS, CUSTOMER, False, False, True, _prepare_order),
    ToolSpec("customer.commit_order", "orders", "提交已批准的顾客订单。",
             ORDER_ITEMS, CUSTOMER, True, True, False, _commit_order),
    ToolSpec("merchant.list_categories", "catalog", "列出商品分类。",
             _object_schema({}), MERCHANT, False, False, False, _list_categories),
    ToolSpec("merchant.list_products", "catalog", "查看商品列表，可按状态/名称/分类筛选。",
             _object_schema({"status": {"type": "string", "enum": ["active", "inactive"]},
                             "query": TEXT, "category": CATEGORY, "page": PAGE}),
             MERCHANT, False, False, False, _merchant_products),
    ToolSpec("merchant.list_orders", "orders", "查看全部订单。",
             _object_schema({"page": PAGE, "status": ORDER_STATUS}),
             MERCHANT, False, False, False, _merchant_orders),
    ToolSpec("merchant.get_order", "orders", "查看一笔订单及明细。",
             _object_schema({"order_id": ORDER_ID}, ["order_id"]),
             MERCHANT, False, False, False, _merchant_order),
    ToolSpec("merchant.get_inventory_history", "inventory", "查看库存台账。",
             _object_schema({"product_id": {"type": "string", "maxLength": 100}, "page": PAGE}),
             MERCHANT, False, False, False, _merchant_inventory),
    ToolSpec("merchant.get_audit_events", "analytics", "查看操作审计事件。",
             _object_schema({"page": PAGE}),
             MERCHANT, False, False, False, _merchant_audit),
    ToolSpec("merchant.get_sales_summary", "analytics", "查看销售额和商品/库存概览。",
             _object_schema({}),
             MERCHANT, False, False, False, _merchant_sales),
]

registry = ToolRegistry(agent_confirmations.store)
for spec in SPECS:
    registry.register(spec)
