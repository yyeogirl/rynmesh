# 最终远程 CI

[草稿 PR](https://github.com/yyeogirl/rynmesh/pull/1) 的 [GitHub Actions 运行 34862743090](https://github.com/yyeogirl/rynmesh/actions/runs/34862743090) 已完成，**9/9 项成功**。

被测提交 `25aa0b8`。它相对安装候选 `04d94f2` 仅修改端到端验收编排和测试替身；`rynmesh/`、`webapp/` 完全相同。因此远程产品实现与本机已安装 wheel 一致，无需为测试脚本变化重新打包。此后的验收记录提交单独标记跳过重复 CI，不冒充该记录提交本身有新的测试运行。

| 检查 | 结果 |
|---|---|
| backend | 1521 passed、3 skipped；Ruff 通过 |
| webapp | 256 passed、46 文件；TypeScript 和生产构建通过 |
| friend-product-e2e | 新节点首次分享与默认邮箱配额/恢复两个命令通过 |
| llm-e2e | 严格 P2P、加密中转、邮箱三条流程通过 |
| peer-transit-e2e | 三节点连接、并发及持续运行审计通过 |
| packaged-node | 节点自带页面和路由校验通过 |
| contribution-workflow | 通过 |
| desktop-compile (aarch64) | 侧车、原生运行时启动和桌面编译通过 |
| desktop-compile (x86_64) | 侧车、原生运行时启动和桌面编译通过 |

原 SHARE11 的产品 CI 部分通过。公网双出口实机部分按用户要求跳过；Docker 网络和本机双节点都未当作跨公网证明。macOS CI 编译成功也未替代用户已跳过的 macOS 实机安装操作。

首次运行 `34861850694` 有两个失败检查，未抹去历史：旧 LLM 脚本未进行新增的明确好友授权，收到 409；四个 POSIX 测试的 `resolve_server` 替身不接受 `root` 关键字。修复后脚本通过正常邀请、接受、授权及私有发现接口取得权限，将权限修订绑定到请求；Compose 为邀请设置可验证的字面 IP。未修改生产权限规则、放宽健康要求或跳过失败断言。测试替身与真实函数签名对齐。相关本地回归 132 passed、17 skipped，最终以上远程全量实际通过。

后端保留 6 条依赖/弃用警告，前端保留已有构建体积提示，不影响本轮通过条件。测试数来自最终运行，不累计多轮次数。[结构化证据](remote-ci-final-20260914.json)保存运行与各检查 URL、状态和测试摘要。
