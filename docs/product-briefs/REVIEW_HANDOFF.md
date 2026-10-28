# 上游审核交接

2026-09-15。本次提交八项已完成的用户功能，关联产品方案 #40 及 #30、#24、#25、#41。保留原始提交历史；提交审核时已确认上游 `main` 的 `094f03a` 是本分支祖先。

- [八项需求及验收标准](README.md)
- [95 条用例的逐项结果](../acceptance/candidate-delivery/requirement-review-20260914.md)
- [最终产品实现的远程 CI](../acceptance/candidate-delivery/remote-ci-final-20260914.md)
- [安装包与试用入口](TRY_CANDIDATE.md)
- [数据导出、清理和保留边界](../acceptance/candidate-delivery/data-scope-final-20260914.md)

已验证 Windows 安装 wheel、浏览器主流程、独立本机节点、真实 CPU 模型，以及相应失败恢复。部分异常分支通过真实存储/加密/HTTP 自动化与合成夹具验证，不将其描述成逐条人工实机测试。macOS 实机与不同公网出口按用户要求跳过，未进行 V100/CUDA 硬件验收。

产品实现 `04d94f2` 与 CI 提交 `25aa0b8` 的产品目录一致；该 CI 9/9 成功，后端 1521 passed / 3 skipped，前端 256 passed。`624f64f` 只补充验收记录和已执行的隔离验收脚本，当时跳过了重复 CI。

本次审核交接提交不含产品代码修改，提交消息不跳过 CI，使上游 PR 可以正常运行自己的检查。此前通过记录不冒充本次上游检查的结果。上游合并和正式发布由仓库维护者审核后决定。
