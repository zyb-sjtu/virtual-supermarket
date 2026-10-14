# Git 仓库与版本维护

## 仓库与文件边界

- 仓库：<https://github.com/zyb-sjtu/virtual-supermarket>。
- 可见性：私有；浏览器需要登录有权限的 `zyb-sjtu` 账号或已获授权账号，未登录可能显示 404。
- 默认分支：`main`。本机项目目录 `E:\shop` 已配置 `origin`。
- 提交内容：43 个源代码、测试、配置、任务规划、运行文档及最终报告文件；本维护文档加入后为 44 个文件。
- 不提交：实际密码、`.local/`、数据库与备份、`.venv/`、`.tools/`、`.tmp/`、报告中间制作文件。
- `.github/workflows/tests.yml` 中的固定密码只用于每次运行新建的临时测试服务，与本机随机密码无关。

## 获取与运行

```powershell
git clone https://github.com/zyb-sjtu/virtual-supermarket.git
cd virtual-supermarket
& .\scripts\Setup-Local.ps1
& .\scripts\Start-Postgres.ps1
& .\scripts\Start-Shop.ps1
```

克隆需要 GitHub Git 身份验证。新克隆没有本机数据库和凭据，需要安装并启动自己的环境；首次空库提供苹果、香蕉。原数据库需要使用维护工具迁移，不随 Git 复制。

## 持续任务维护

根目录 [TASKS.md](../TASKS.md) 是当前进度的唯一维护入口；原 Word 企划与早期周报为历史材料。任务分为“已完成、部分完成、待实施、暂缓”，每项写明验收证据和剩余范围。

每次版本更新：

1. 从 TASKS 定位任务 ID，例如 `P2-03`；新增需求先登记任务，不依赖聊天记录追踪。
2. 在开发分支完成变更与针对性验证；不改动正式数据库做破坏性测试。
3. 同一提交/PR 更新 TASKS 的状态、证据、剩余范围、日期、当前版本和下一轮优先级。
4. 同步更新 [CHANGELOG.md](../CHANGELOG.md)；接口、配置、数据库或运行方式变化同步更新 README 与对应文档。
5. 运行进度/链接检查、相关测试和 `git diff --check`；CI 全部通过后合入 `main`。
6. 发布时把 Unreleased 实际变化归档，维护迁移/回退说明并建立版本标签。不要用“脚本已写”代替“部署/测试已完成”。

示例开发命令：

```powershell
git pull --ff-only
git switch -c feat/role-permissions
# 完成代码、TASKS.md、CHANGELOG.md 和必要运行文档更新
. .\scripts\Environment.ps1
& $ShopPython scripts\check_progress.py
$env:SHOP_TEST_POSTGRES = '1'
& $ShopPython -m unittest discover -s tests -v
git diff --check
git add .
git commit -m "feat: add role permissions and update task progress"
git push -u origin feat/role-permissions
```

角色分支名仅为示例，不代表该功能已实现。

## 自动检查

- 本机 `scripts/check_progress.py` 检查任务 ID 唯一、状态有效、各状态计数与台账一致，以及 Markdown 文档链接。
- CI 使用本次 push/PR 的起始提交比较范围。运行代码、测试或配置变化时，范围内必须包含 `TASKS.md` 与 `CHANGELOG.md`，否则检查失败。
- 初次空仓库推送只有文档结构检查，后续提交启用差异检查。纯文档更新不强制改动其他文档。
- CI 包含 Windows/Linux × Python 3.9/3.12 的回归与语法检查，以及真实 PostgreSQL 17 的业务测试。
- 该检查能防止遗漏进度文件，不能自动判断文字真实性；维护者仍需按实际验收结果修改状态。
- [AGENTS.md](../AGENTS.md) 把上述规则写入后续编码代理的项目指令。

## 首次交付证据

- 基线提交 `1da1f5d` 已从私有远程重新克隆，43 个文件的 Git blob 全部匹配。
- 重新克隆的代码使用独立 `shop_test` 库，28 项 SQLite + 15 项 PostgreSQL 共 43 项测试通过，语法与进度检查通过。
- 首次 CI 暴露 PostgreSQL 引导超级用户不能降级，提交 `d510ac8` 修复为单独创建普通业务账号。
- 修复提交的 [五项 CI](https://github.com/zyb-sjtu/virtual-supermarket/actions/runs/38540057987) 已全部成功，包含真实 PostgreSQL 17 Linux 服务和 Windows/Linux Python 回归。
- 最新远程 CI 结果和后续发布基线见 [TASKS.md](../TASKS.md)，GitHub Actions 页面可查看每次更新的运行证据。

本机源码与 Git 远程将沿版本演进；数据库、凭据、备份和业务数据仍由各部署环境独立维护。
