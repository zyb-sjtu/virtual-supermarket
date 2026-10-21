# 鲜邻 · 超市管理系统 v1.4

传统超市交易与库存管理 Demo。初始提供苹果、香蕉，支持自由上架、编辑价格、上下架、补货、购买、取消订单和库存调整。本轮对应十二周计划第一阶段的业务完善，不含 AI；暂不区分商家与客户。

**项目进度：[TASKS.md](TASKS.md)　版本变化：[CHANGELOG.md](CHANGELOG.md)　后续维护规范：[AGENTS.md](AGENTS.md)。** 每次版本更新必须同步更新任务状态、验收证据与变更记录。

仓库：[zyb-sjtu/virtual-supermarket](https://github.com/zyb-sjtu/virtual-supermarket)（当前可匿名克隆；如需私有请在 GitHub 调整可见性）。源代码、最终报告和验证记录已入库，本机密码、数据库与运行环境按忽略规则排除；新克隆请按下文安装环境。

身份入口：访问 `http://127.0.0.1:8000/` 会先进入 `/login`，手动选择“商户”或“顾客”后进入对应页面，顶部提供退出登录。**该入口不需要账号密码，是演示级角色模拟，不是真实认证。** 权限矩阵与安全边界见 [权限与角色设计](docs/权限与角色设计.md)。

开发、提交、版本发布与自动进度检查见 [Git 仓库与维护说明](docs/Git仓库与维护说明.md)。

## 快速运行（PostgreSQL）

当前默认使用 **PostgreSQL 17 独立服务**，Python 3.9+，驱动固定为 Psycopg 3.2.13。Windows 项目内运行：

```powershell
cd E:\shop
& .\scripts\Start-Postgres.ps1
& .\scripts\Start-Shop.ps1
```

打开 <http://127.0.0.1:8000>，页面会先跳转到身份选择页 `/login`。网页/API 使用 8000 端口，数据库独立使用 5433 端口。按 `Ctrl+C` 仅停止网页后端；数据库继续运行。需要停止数据库时运行 `scripts\Stop-Postgres.ps1`。

> **Windows 中文路径说明：** PostgreSQL 的 `initdb` 在包含中文的仓库路径下可能失败，建议将项目放在纯 ASCII 路径。仓库内 PowerShell 脚本已按 UTF-8 BOM 保存；本机使用的一键启停入口仅作为本地运行辅助，不随仓库上传。

服务启动后可双击项目根目录的 `打开超市.url`。不要把 `static/index.html` 当成独立网页运行；直接双击它会自动跳转到默认后端地址。若自定义网页端口，请使用该端口对应的 HTTP 地址。

本机环境已准备好；新电脑先运行 `scripts\Setup-Local.ps1` 安装项目虚拟环境、驱动和 PostgreSQL 二进制包。新空库首次启动提供苹果、香蕉；迁移原数据时先运行迁移工具，再启动网页。原有 SQLite 数据本轮已迁移，保留原库作备份。

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `SHOP_HOST` / `SHOP_PORT` | `127.0.0.1` / `8000` | HTTP 监听地址/端口 |
| `SHOP_DB_BACKEND` | `postgresql` | 当前正式数据库；`sqlite` 为兼容旧库模式 |
| `PGHOST` / `PGPORT` | `127.0.0.1` / `5433` | 独立数据库地址/端口 |
| `PGDATABASE` / `PGUSER` | `shop` / `shop_app` | 数据库名称与业务账号 |
| `PGPASSFILE` | `.local/pgpass.conf` | 本机凭据文件，不纳入版本控制 |
| `SHOP_DB_PATH` | `data/shop.sqlite3` | 仅 SQLite 模式使用 |

**已实现数据库服务分离：** 浏览器 → HTTP 后端 → PostgreSQL TCP 连接。独立数据库进程与数据目录，不再由后端直接读写 SQLite。将 PostgreSQL 部署到另一台主机后可配置 `PGHOST`、端口、账号等连接信息，无需再次适配业务代码。

本轮同机模拟了 `8000 → 5433` 正式系统，以及 `8001 → 5434` 备份搬迁验证系统。不同端口不是物理主机隔离；跨主机云部署尚未执行。完整安装、迁移、备份和验证命令见 [PostgreSQL 运行与隔离验证](docs/PostgreSQL运行与隔离验证.md)。

## 网页功能

网页首先进入 `/login` 身份选择页。无需账号密码：选择“商户”进入管理后台 `/merchant`，选择“顾客”进入选购页 `/customer`；顶部均有退出登录。选择身份只用于演示页面和接口权限隔离，不构成真实身份认证。

1. **商品管理：** 自定义名称、SKU、分类、货架分区、单价、单位、简介、初始库存及图片。SKU 可自动生成且唯一；已有 SKU 和单位暂不支持修改。
2. **展示：** 每页 6 个商品，按上架顺序分页；可搜索名称/SKU、筛选分类、切换在售和已下架商品。无图使用中性购物袋占位图。
3. **编辑：** 修改名称、价格、分类、分区、简介和图片，支持移除图片。版本冲突提示刷新，避免覆盖另一人刚保存的商品资料。
4. **上下架：** 下架保留库存、图片和历史记录，下架后禁止新购买，允许补货与重新上架。
5. **交易：** 单商品购买立即扣库存并生成订单；补货增加库存。订单按 10 条分页展示，可填写原因取消整单，取消后一次性恢复库存。
6. **库存管理：** 填写损耗、盘盈等原因，按正负整数调整库存；按商品查看分页台账，包含期初、上架入库、购买、补货、调整和取消返还。
7. **批量导入：** 上传 JSON 一次导入 1–100 个商品，要求明确 SKU；任一数据错误或 SKU 冲突时整批回滚。页面提供示例文件下载，批量图片请导入后逐个编辑。
8. **数据维护：** 页面下载商品、订单、库存台账、审计 CSV；命令行提供一致性检查、在线备份及停机恢复。
9. **顾客选购：** 顾客页只展示在售商品，可搜索/分类筛选、按数量购买、查看当前会话订单并取消自己的订单；看不到销售额、后台流水、台账、审计和导出。

商户后台每 5 秒刷新数据；顾客页在购买、取消后主动刷新商品和订单。金额指标使用“累计净销售额”：原购买金额减已取消订单金额。取消只是本 Demo 的库存与订单冲销，尚未连接实际支付、退款或物流系统。

### 输入约束

金额用整数分存储，单价 ¥0.01–¥1,000,000.00，最多两位小数；数量只接受整数。初始库存 0–9999，单次购买/补货 1–9999，单次调整为 ±1–9999，总库存不超过 10 亿。取消与调整原因必填，最多 200 字。

名称最多 40 字，单位 8 字，简介 100 字，分区 40 字；SKU 最多 32 字，只接受字母、数字、下划线、连字符并转为大写。分类固定为水果、蔬菜、肉禽水产、乳品饮料、粮油食品、日用百货、其他。图片支持 JPG/PNG/WebP，最大 1 MB；图片字节存于数据库，分页 API 只返回图片地址。

## 项目架构

```mermaid
flowchart LR
    B[浏览器 HTML / CSS / JS] -->|HTTP 8000| H[server.py 请求处理]
    H --- AU[auth.py 内存角色会话]
    H --> D[db.py 业务规则与事务]
    D --> A[storage.py PostgreSQL 驱动与事务]
    A -->|TCP 5433| P[(独立 PostgreSQL 服务)]
    M[postgres_maintenance.py 迁移 / 备份 / 恢复 / 检查] --> P
```

`server.py` 负责页面路由和服务端角色校验：匿名访问进入 `/login`，商户进入 `/merchant`，顾客进入 `/customer`；所有受保护 API 都从服务端会话读取角色。`auth.py` 只保存演示用内存会话，不保存账号或密码。

| 文件 | 职责 |
| --- | --- |
| `server.py` | 静态页面、身份路由、JSON API、Agent 工具路由、权限拦截、输入解析、CSV 导出、HTTP 错误响应 |
| `auth.py` | 演示级角色会话、Cookie 令牌、角色常量；不持久化、不保存密码 |
| `agent_registry.py` | Agent 工具注册表、角色过滤、受限 JSON Schema、错误映射和确认校验 |
| `agent_confirmations.py` | 内存确认令牌、状态机、参数指纹和 TTL |
| `agent_tools.py` | P1 Agent 工具：11 个只读工具 + 顾客 `prepare_order`/`commit_order` |
| `db.py` | 数据模型、迁移、商品管理、订单、库存台账、审计与查询；顾客订单按 `buyer_key` 隔离；订单支持预期价格复验 |
| `storage.py` / `postgres_schema.py` | 数据库驱动、配置、事务边界及 PostgreSQL 表结构 |
| `runtime.py` | SQLite 兼容模式的服务/恢复进程互斥 |
| `postgres_maintenance.py` / `maintenance.py` | PostgreSQL 迁移与归档、旧 SQLite 备份兼容入口 |
| `static/` | 商户后台、身份选择页、顾客选购页、响应式样式、交互、缺省图片、批量导入模板 |
| `tests/test_shop.py` | 原交易、上架、上下架、分页、图片的回归测试 |
| `tests/test_business.py` | 订单、版本冲突、台账、导入、迁移、维护与 HTTP 测试 |
| `tests/test_permissions.py` | 角色页面、API 权限矩阵、顾客订单归属、退出登录测试 |
| `tests/test_agent_tools.py` | Agent 工具列表、只读工具、订单隔离、prepare/approve/commit、幂等和确认失败测试 |
| `tests/test_postgres.py` | PostgreSQL 并发、幂等、事务、图片与权限验证 |
| `.github/workflows/tests.yml` | Windows/Linux 的单元测试与语法检查配置 |
| `data/` | 默认数据文件与备份，不纳入版本控制 |

详细模型与阶段成果见 [后端迭代说明](docs/后端迭代说明.md)。

### 可靠更新机制

- PostgreSQL 业务写事务使用数据库内的事务级 advisory lock，SQLite 兼容模式使用 `BEGIN IMMEDIATE`；库存、订单、明细、台账、审计与幂等结果一起提交，失败一起回滚。
- 库存通过条件更新和非负约束保护；多商品订单先检查全部商品，任一不足则整单不扣库存。PostgreSQL 提交启用同步持久化；SQLite 兼容模式保留 WAL 与完整同步。
- 写请求携带唯一 `request_id`。同一接口作用域内，同一编号与内容重试返回已保存结果；同一编号换内容返回 409。客户端需为一次操作保留编号，为新操作生成新编号。
- 商品资料用 `version` 防止并发覆盖；订单明细保存成交时名称、SKU、单位和单价快照，改价不影响旧订单。
- 取消整单与返还库存在同一事务完成；并发重复取消只返还一次。商品下架仍允许退回库存。
- `postgres_maintenance.py check` 检查库存累计与连续台账、订单金额、取消返还数量和约束状态；SQLite 模式另执行物理完整性检查。
- PostgreSQL 写事务锁跨应用进程生效，保留当前串行写入语义；数据库故障时返回 503，无 SQLite 自动回退。SQLite 模式继续持有本机服务进程锁。

旧库升级一次性补充 SKU、订单、期初与历史库存台账。旧购买保留原成交单价；旧版本没有保存的历史名称与单位只能用迁移时商品信息补齐，无法恢复不存在的历史快照。

## API

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/health` | 公开 | 服务及数据库连接检查，版本 1.4，附数据库引擎与服务端口 |
| GET | `/api/session` | 公开 | 当前角色会话，不返回令牌 |
| POST | `/api/session` | 公开 | 选择身份，传 `{"role":"merchant"}` 或 `{"role":"customer"}`，无密码 |
| POST | `/api/logout` | 公开 | 删除当前会话并清除 Cookie |
| GET | `/api/state?page=1&status=active&search=&category=` | 商户/顾客 | 固定每页 6 个；顾客只能看 `active`，且响应不含销售额、流水、下架统计等后台字段 |
| GET | `/api/categories` | 商户/顾客 | 固定分类列表 |
| GET | `/api/products/{id}/image` | 商户/顾客 | 商品图片 |
| POST | `/api/products` | 商户 | 创建商品与初始库存 |
| PATCH | `/api/products/{id}` | 商户 | 编辑资料，必须提供 `version` 和 `request_id` |
| POST | `/api/product-status` | 商户 | 上下架，传 `product_id`、布尔 `active`、`request_id` |
| POST | `/api/products/import` | 商户 | 事务批量导入，传 `products` 数组和 `request_id` |
| POST | `/api/movements` | 顾客（购买）/商户（补货） | 兼容单商品接口；按 `action` 区分权限 |
| POST | `/api/orders` | 顾客 | 创建多商品订单，1–50 种商品，单价由后端读取；订单绑定顾客会话 |
| GET | `/api/orders?page=1&status=` | 商户/顾客 | 每页 10 个；顾客只返回当前会话订单，商户返回全部 |
| GET | `/api/orders/{id}` | 商户/顾客 | 订单与成交快照；顾客只能访问自己的订单，其他订单返回 404 |
| POST | `/api/orders/{id}/cancel` | 商户/顾客 | 整单取消；顾客只能取消自己的订单，商户可取消全部 |
| POST | `/api/inventory/adjust` | 商户 | 库存调整，传 `product_id`、`delta`、`reason`、`request_id` |
| GET | `/api/inventory?product_id=apple&page=1` | 商户 | 每页 20 条台账，省略商品 ID 则查全部 |
| GET | `/api/audit?page=1` | 商户 | 每页 20 条审计事件及前后值 |
| GET | `/api/export?kind=products` | 商户 | CSV，可选 `products`/`orders`/`inventory`/`audit` |
| GET | `/api/agent/tools` | 顾客/商户 | 列出当前角色可执行的 Agent 工具 |
| POST | `/api/agent/tools/{name}` | 顾客/商户 | 调用工具；写操作要求 `request_id`，`commit_*` 要求 `confirmation_id` |
| GET | `/api/agent/confirmations/{id}` | 顾客/商户 | 查看确认预览与状态（UI 使用） |
| POST | `/api/agent/confirmations/{id}/approve` | 顾客/商户 | 批准确认（UI 使用，不是 Agent 工具） |

写请求使用 `Content-Type: application/json`。新建通常返回 201，重复返回 200、`duplicate: true`；PATCH 返回 200；库存不足、版本冲突、重复 SKU 或编号内容冲突返回 409。页码为正整数，超出末页自动返回末页。未登录访问受保护 API 返回 401，角色不匹配返回 403；页面访问错误角色会重定向到该角色自己的页面。完整矩阵见 [权限与角色设计](docs/权限与角色设计.md)。

### Agent 工具（P1）

`GET /api/agent/tools` 只返回当前角色可见的工具；`POST /api/agent/tools/{name}` 使用统一 envelope 调用。P1 已实现：

- 顾客 4 个只读工具：分类、商品搜索、自己的订单列表/详情；
- 商户 7 个只读工具：分类、商品列表、订单列表/详情、台账、审计、销售汇总；
- 顾客 2 个下单工具：`customer.prepare_order` 预览并生成确认令牌，`customer.commit_order` 在 UI 批准后提交；提交时复验商品在售、库存和价格，变化返回 `CONFIRMATION_STALE`。

工具设计、56 个规划工具、Python Registry/HTTP Adapter 契约和 P2+ 路线见 [Agent工具封装设计](docs/Agent工具接口设计.md)。AI 工具未实现、未注册；当前确认令牌为内存存储，服务重启失效。

创建商品示例：

```json
{
  "sku": "VEG-001",
  "name": "上海青",
  "category": "蔬菜",
  "zone": "蔬菜区 A-01",
  "unit": "袋",
  "description": "当天鲜选",
  "price_cents": 350,
  "stock": 8,
  "request_id": "listing-example-001"
}
```

多商品订单示例：

```json
{
  "request_id": "order-example-001",
  "items": [
    {"product_id": "apple", "quantity": 2},
    {"product_id": "banana", "quantity": 1}
  ]
}
```

网页当前逐个商品购买；多商品原子下单已提供后端接口，购物车界面尚未实现。`product_id` 使用查询结果中的 ID，不是 SKU。

## 备份、迁移与检查

```powershell
. .\scripts\Environment.ps1
$env:PGPORT = '5433'
& $ShopPython postgres_maintenance.py check
& $ShopPython postgres_maintenance.py backup data\backups\transfer.dump
```

备份使用 `pg_dump` 一致快照；恢复用 `pg_restore` 单事务，只允许恢复到新的空目标数据库，拒绝覆盖已有业务库。SQLite 到 PostgreSQL 的迁移工具逐字段校验全部业务表与图片，完整保留订单、台账、审计与去重数据。旧 SQLite 维护使用 `maintenance.py --database data\shop.sqlite3 ...`。

详细命令及跨端口恢复步骤见 [数据库隔离与迁移](docs/数据库隔离与迁移.md)。CSV 是报表，不是完整数据库备份。

## 验证与边界

```powershell
. .\scripts\Environment.ps1
$env:SHOP_TEST_POSTGRES = '1'
& $ShopPython -m unittest discover -s tests -v
& $ShopPython -m py_compile auth.py agent_registry.py agent_confirmations.py agent_tools.py db.py server.py storage.py postgres_schema.py postgres_maintenance.py
node --check static/app.js
node --check static/login.js
node --check static/customer.js
```

本机已通过 37 项 SQLite 回归测试（含 4 项角色权限测试、5 项 Agent 工具测试）与 15 项 PostgreSQL 测试，共 52 项，覆盖并发购买/取消、幂等、整单回滚、100 商品导入、快照、维护、校验、角色页面/API 权限、顾客订单隔离，以及 Agent 工具注册/角色过滤、只读不写库、prepare/approve/commit 和确认失败。PostgreSQL 测试强制使用专用 `shop_test`，每个用例会清空测试库；正式 `shop` 不受影响。未设置测试开关时 PostgreSQL 用例跳过。历史远端基线为 43 项测试与五项 CI；**本次本地改动未提交、未推送，远端 CI 尚未重新运行。**

网页上架、改价、图片上传/移除、购买、取消、调整、台账、筛选、导入和 CSV 下载已在独立 PostgreSQL 测试库验证；本次新增的身份选择、商户/顾客页面、权限隔离和退出登录已通过本机 SQLite/PostgreSQL 自动化测试，桌面与移动端布局待人工复验。Node/Playwright 仅为验证依赖。

隔离验证完成：备份从 5433 搬到 5434，全部业务表一致；修改验证库不影响正式库；停库后 HTTP 静态页面继续运行、数据/写入返回 503；重启后持久化数据不变。结果见 [验证报告](docs/postgresql-validation.json)。

当前提供**演示级角色选择**（无账号密码），商户/顾客页面分流和 API 权限由服务端校验；这不是真实认证，也没有真实用户、密码、统一登录、审计操作人或跨会话订单归属。顾客订单仅在当前匿名会话内可见。P1 Agent 工具已实现 11 个只读工具和顾客下单确认；商户写工具、商品详情/筛选、购物车和 AI 工具仍未实现。真实支付、配送、多门店隔离仍未完成。数据库 TCP 分离已验证，尚未部署到云主机或做生产吞吐压测；公网部署前需完成真实身份与权限、HTTPS 和部署运维。当前按业务事务串行写入，连接池与细粒度并发控制可继续迭代。
