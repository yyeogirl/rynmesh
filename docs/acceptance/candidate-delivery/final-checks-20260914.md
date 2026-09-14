# 本机全量检查

2026-09-14，源码 HEAD `45745ec`，运行时代码与安装候选 `1829eb2` 相同；此时未提交变更仅为验收范围文档。Windows / Python 3.12 / Node.js。

| 命令 | 实际结果 |
|---|---|
| `python -m pytest tests/ -q` | 1482 passed，29 skipped，5 warnings；303.47 秒 |
| `node node_modules/vitest/vitest.mjs run`（webapp） | 44 个文件、250 项通过；35.88 秒 |
| `python -m ruff check rynmesh tests` | 通过 |
| `node webapp/node_modules/typescript/bin/tsc -b webapp/tsconfig.json --pretty false` | 通过 |
| `node node_modules/vite/bin/vite.js build`（webapp） | 通过；资源仍为 `index-BgiJBxgZ.js` 和 `index-BiXHFfIh.css`，与候选内一致 |

后端警告为已有 Starlette TestClient 与 FastAPI 生命周期弃用提示；前端保留既有大于 500 kB 的构建提示。29 项跳过未计为通过。没有因为数字全绿而宣称全部产品验收完成；各原始用例仍须依其具体证据判断。

macOS 与公网验收已由用户明确豁免，见[本轮范围确认](../../product-briefs/ACCEPTANCE_SCOPE.md)。本结果是本机执行结果，不冒充 GitHub Actions 运行结果。
