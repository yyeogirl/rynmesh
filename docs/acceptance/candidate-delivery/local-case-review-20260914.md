# 本轮范围内的用例复核

用户已跳过 macOS 与公网。单个用例的 `passed` 表示其明确操作和结果在本轮本机范围内有证据；G01–G10 继续独立记录，不把通用门槛未汇总等同于每个已验证操作仍在开发，也不把单项通过等同于全部产品通过。

本次只改动下列已读完实际执行记录的用例，其余状态不作推断。运行时代码为 `1829eb20296cb3d5ceb23e6409d57ae686a3e6a9`；最终全量检查见[结果](final-checks-20260914.md)。历史记录中的“本轮未测”按其当次时间理解，较新结果补足的缺口无需重跑。

| 用例 | 复核结果 | 直接证据 |
|---|---|---|
| READ01 | 本轮通过：空目录无模型获得真实内容、打开并收藏；167.15 秒为节点启动至首次成功，不是安装耗时。安装 wheel 的全新环境另有实际启动与阅读结果。 | [真实来源首次阅读](../first-reading-development/browser-recovery-20260914.md)、[安装候选](../../product-briefs/TRY_CANDIDATE.md) |
| READ02 | 本轮通过：未完成引导可关闭，重启可继续，没有虚假完成。 | [页面执行记录](../first-reading-development/browser-recovery-20260914.md) |
| READ03 | 本轮通过：唯一收藏与阅读位置在节点重启后保留，页面恢复中段。 | [页面与持久结果](../first-reading-development/browser-recovery-20260914.md) |
| READ04 | 本轮通过：单来源真实 HTTP 503 与恢复重试，其他来源记录及阅读不变。 | [来源失败与恢复](../first-reading-development/browser-recovery-20260914.md) |
| READ05 | 本轮通过：全部来源连接失败，空目录有恢复入口；已有缓存可读且重启保留，恢复连接后刷新可用。 | [全来源故障结果](../first-reading-development/all-sources-recovery-20260914.md) |
| READ07 | 本轮通过：三种反馈、指定撤销、替代状态、解释字段与重启持久化。 | [反馈操作及检查点](../first-reading-development/browser-recovery-20260914.md) |
| READ08 | 本轮通过：实际 Windows 文件占用使收藏和反馈写入失败，页面不确认成功；释放后重试无重复，重启保留。 | [写入失败恢复](../first-reading-development/browser-recovery-20260914.md) |
| READ09 | 本轮通过：完成后重启无强制引导，继续阅读及好友入口可用。 | [完成后重启](../first-reading-development/browser-recovery-20260914.md) |
| READ06 | 继续核对：已有正常、失败、恢复和从未成功证据；从未检查及自定义来源对应分支的证据尚未确认完整。 | [来源状态边界](../first-reading-development/all-sources-recovery-20260914.md) |
| READ10 | 继续核对：导出文件落盘缺口已关闭；完整日志与网络隐私核对仍未关闭。 | [实际下载文件](reading-export-file-20260914.md)、[隐私与重置恢复](../first-reading-development/privacy-recovery-20260914.md) |

本轮没有产品代码变更，没有重复运行已完成的全量测试，也没有执行远端 CI 或新增平台适配。
