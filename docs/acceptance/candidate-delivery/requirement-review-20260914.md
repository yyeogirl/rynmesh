# 原始 95 条用例：最终范围复核

2026-09-14。**95 条在用户确认的本轮范围内通过，剩余必测项 0 条。** 原始需求及通过条件保留；macOS 实机和公网双出口部分按用户明确指令跳过，不记成实测通过。

安装实现 `04d94f2`，远程被测提交 `25aa0b8`（产品代码相同）。最终远程 **9/9 项 CI 成功**：后端 **1521 passed / 3 skipped**，前端 **256 passed / 46 文件**；类型、构建、Ruff、产品双节点、LLM 和中转端到端、两种 macOS 架构编译均通过。[远程结论与原始运行](remote-ci-final-20260914.md)。macOS 编译未替代实机验收。

判定按逐条页面、真实存储/加密/HTTP 和组件证据，不按数量推算完成。最后三项已闭合：[键盘主流程](keyboard-final-20260914.md)、[数据维护与升级](data-scope-final-20260914.md)、[产品 CI](remote-ci-final-20260914.md)。历史阶段的待验说明由本文更新，失败记录保留。

本轮未合并到上游 main、未发布正式版本。可用安装包及两个本机试用节点见[试用说明](../../product-briefs/TRY_CANDIDATE.md)。

## 逐项结果

| 编号 | 本轮结果 | 核对依据与边界 |
|---|---|---|
| READ01 | 通过 | 空安装环境与空数据节点取得真实推荐、打开并收藏；167.15 秒为启动至首次完成，安装耗时另列。 [local-case-review-20260914.md](local-case-review-20260914.md)、[TRY_CANDIDATE.md](../../product-briefs/TRY_CANDIDATE.md) |
| READ02 | 通过 | 关闭未完成引导后重启仍可继续，未错误完成。 [ACCEPTANCE_SCOPE.md](../../product-briefs/ACCEPTANCE_SCOPE.md)、[local-case-review-20260914.md](local-case-review-20260914.md) |
| READ03 | 通过 | 唯一收藏、完整历史摘要及约 16% 阅读位置在重启后保留。 [ACCEPTANCE_SCOPE.md](../../product-briefs/ACCEPTANCE_SCOPE.md)、[local-case-review-20260914.md](local-case-review-20260914.md) |
| READ04 | 通过 | 单来源 503、单独重试和恢复 200，其他 14 项健康记录不变。 [ACCEPTANCE_SCOPE.md](../../product-briefs/ACCEPTANCE_SCOPE.md)、[local-case-review-20260914.md](local-case-review-20260914.md) |
| READ05 | 通过 | 14 个来源连接全部失败分别验证空目录恢复、缓存阅读与重启。 [ACCEPTANCE_SCOPE.md](../../product-briefs/ACCEPTANCE_SCOPE.md)、[local-case-review-20260914.md](local-case-review-20260914.md) |
| READ06 | 通过 | 实际默认及自定义来源健康字段已有页面/接口核对；新增组件检查覆盖从未检查与首次失败，真实后端状态由 source_recovery 验证。 [test_source_recovery.py](../../../tests/test_source_recovery.py)、[SourceHealthPanel.test.tsx](../../../webapp/src/components/SourceHealthPanel.test.tsx) |
| READ07 | 通过 | 三种反馈、单项撤销、替代状态、信号解释及重启保留已验证。 [ACCEPTANCE_SCOPE.md](../../product-briefs/ACCEPTANCE_SCOPE.md)、[local-case-review-20260914.md](local-case-review-20260914.md) |
| READ08 | 通过 | 真实文件占用使收藏/反馈写入失败；释放后原操作重试，记录不重复。 [ACCEPTANCE_SCOPE.md](../../product-briefs/ACCEPTANCE_SCOPE.md)、[local-case-review-20260914.md](local-case-review-20260914.md) |
| READ09 | 通过 | 首次完成后重启不强制引导，继续阅读与好友入口可见。 [ACCEPTANCE_SCOPE.md](../../product-briefs/ACCEPTANCE_SCOPE.md)、[local-case-review-20260914.md](local-case-review-20260914.md) |
| READ10 | 通过 | 导出、擦除、画像外发与当前日志边界核对完成；审计详情改为计数，历史日志不自动抹除。 [reading-export-file-20260914.json](reading-export-file-20260914.json)、[privacy-boundaries-20260914.md](privacy-boundaries-20260914.md) |
| SHARE01 | 通过 | 已安装 wheel 双节点从邀请文本审核、接受至首次消息和正文分享，macOS 安装分支跳过。 [browser-checkpoints-20260914.json](../friends-development/browser-checkpoints-20260914.json)、[two-node-20260914.md](two-node-20260914.md) |
| SHARE02 | 通过 | 邀请方离线时本地审核身份/权限，确认前观察到零好友出站请求、零关系。 [browser-first-sharing-20260914.md](../friends-development/browser-first-sharing-20260914.md)、[browser-checkpoints-20260914.json](../friends-development/browser-checkpoints-20260914.json) |
| SHARE03 | 通过 | 三节点两次接受请求实际重叠，只建立一条关系；另一方明确提示邀请已用。 [invite-attachments-browser-20260914.md](../friends-development/invite-attachments-browser-20260914.md)、[invite-attachment-checkpoints-20260914.json](../friends-development/invite-attachment-checkpoints-20260914.json) |
| SHARE04 | 通过 | 自然 15 分钟过期、取消、同成功请求重试及新邀请恢复均有页面记录。 [invite-attachments-browser-20260914.md](../friends-development/invite-attachments-browser-20260914.md)、[invite-attachment-checkpoints-20260914.json](../friends-development/invite-attachment-checkpoints-20260914.json) |
| SHARE05 | 通过 | 双向消息和文章、副本下载同摘要；三份附件在 Chrome 实际落盘，5 MiB+1 发送前阻止。Codex 内置浏览器保存差异不列为通过。 [invite-attachment-checkpoints-20260914.json](../friends-development/invite-attachment-checkpoints-20260914.json)、[two-node-20260914.md](two-node-20260914.md) |
| SHARE06 | 通过 | 接收方停机先显示邮箱未确认；仅邮箱收取后回执更新为送达，重试无重复。 [browser-first-sharing-20260914.md](../friends-development/browser-first-sharing-20260914.md)、[browser-checkpoints-20260914.json](../friends-development/browser-checkpoints-20260914.json) |
| SHARE07 | 通过 | 真实默认配额第 17 条被拒绝且重启保留；收取后同身份恢复。期限及迟到回执用受控时钟和真实文件邮箱验证。 [browser-first-sharing-20260914.md](../friends-development/browser-first-sharing-20260914.md)、[browser-checkpoints-20260914.json](../friends-development/browser-checkpoints-20260914.json) |
| SHARE08 | 通过 | 撤销前受保护请求 200，离线撤销后及重启后均 403，旧关系不复活。 [browser-checkpoints-20260914.json](../friends-development/browser-checkpoints-20260914.json)、[two-node-20260914.md](two-node-20260914.md) |
| SHARE09 | 通过 | 撤销确认解释不能收回独立副本，新获取拒绝；保存副本重启后仍可读。 [browser-checkpoints-20260914.json](../friends-development/browser-checkpoints-20260914.json)、[two-node-20260914.md](two-node-20260914.md) |
| SHARE10 | 通过 | 默认分享权限、根信任不扩展、邀请/关系秘密在日志、诊断和允许字段导出中的边界核对完成。 [product-http-20260914-d.json](../friends-development/product-http-20260914-d.json)、[privacy-boundaries-20260914.md](privacy-boundaries-20260914.md) |
| SHARE11 | 通过（本轮范围） | 最终GitHub Actions 9项全部通过，含产品双节点分享与配额恢复；公网双出口实机按用户明确要求跳过，未声称完整跨公网实测。 [remote-ci-final-20260914.md](remote-ci-final-20260914.md) |
| ASK01 | 通过 | 空节点无示例会话、无模型有管理入口；真实原生模型从 Ask 入口完成问答。 [runtime-recovery-20260914.md](../local-ai-development/runtime-recovery-20260914.md)、[TRY_CANDIDATE.md](../../product-briefs/TRY_CANDIDATE.md) |
| ASK02 | 通过 | 侧栏与主页相同会话身份、任务和原服务绑定，真实问答重启后可读。 [article-browser-20260914.md](../ask-ryn-development/article-browser-20260914.md)、[browser-checkpoints-20260914.json](../ask-ryn-development/browser-checkpoints-20260914.json) |
| ASK03 | 通过 | 实际加密 IndexedDB 迁移遇原子写失败保留原件；重试、重复导入及删除后再导入不丢绑定、不重复。 [legacy-migration-browser-20260914.md](../ask-ryn-development/legacy-migration-browser-20260914.md)、[migration-checkpoints-20260914.json](../ask-ryn-development/migration-checkpoints-20260914.json) |
| ASK04 | 通过 | 真实短文问答预览与归档来源材料一致，历史可展开并返回；不保证模型主动生成行内引用标号。 [article-browser-20260914.md](../ask-ryn-development/article-browser-20260914.md)、[browser-checkpoints-20260914.json](../ask-ryn-development/browser-checkpoints-20260914.json) |
| ASK05 | 通过 | 65404 字节材料及真实长历史，预览提示截断/省略，实际归档摘要与 4096 总预算对应。 [article-browser-20260914.md](../ask-ryn-development/article-browser-20260914.md)、[browser-checkpoints-20260914.json](../ask-ryn-development/browser-checkpoints-20260914.json) |
| ASK06 | 通过 | 真实两 Provider 页面切换先确认，新会话无旧内容；旧历史绑定和摘要保持。 [model-switch-checkpoints-20260914.json](../ask-ryn-development/model-switch-checkpoints-20260914.json)、[running-recovery-20260914.json](../ask-ryn-development/running-recovery-20260914.json) |
| ASK07 | 通过 | 页面验证 busy、not_ready、unreachable 和实际固定端口容量不足，释放会话后恢复推理。 [peer-failure-checkpoints-20260914.json](../ask-ryn-development/peer-failure-checkpoints-20260914.json)、[peer-failure-assertions-20260914.json](../ask-ryn-development/peer-failure-assertions-20260914.json) |
| ASK08 | 通过 | 真实 CPU 取消/中断与非零开发额度去重；页面实际取消及 120 秒超时、迟到响应、原任务核对与重启。超时页面用零费合成服务，非 GPU 停算证明。 [full-timeout-observation-20260914.json](../ask-ryn-development/full-timeout-observation-20260914.json)、[cancel-timeout-assertions-20260914.json](../ask-ryn-development/cancel-timeout-assertions-20260914.json) |
| ASK09 | 通过 | 恶意引用作为 JSON 数据/用户材料，结构化角色及接收方保持；实际模型订单仍发原本机，结果作为文本归档。 [test_ask_context.py](../../../tests/test_ask_context.py)、[runs.py](../../../rynmesh/ask_ryn/runs.py) |
| ASK10 | 通过 | 真实导出/删除/重启与最终推理日志隔离、诊断和恢复副本范围已对齐。 [export-privacy-checkpoints-20260914.json](../ask-ryn-development/export-privacy-checkpoints-20260914.json)、[privacy-boundaries-20260914.md](privacy-boundaries-20260914.md) |
| AI01 | 通过 | Windows 原生 CPU 模型经产品页面安装恢复与真实问答，不使用 Docker；macOS 桌面跳过。 [README.md](../local-ai-development/README.md)、[runtime-recovery-20260914.md](../local-ai-development/runtime-recovery-20260914.md) |
| AI02 | 通过 | 来源、许可、资源需求可见，重试必须重新确认，真实下载安装未绕过固定摘要。 [README.md](../local-ai-development/README.md)、[runtime-recovery-20260914.md](../local-ai-development/runtime-recovery-20260914.md) |
| AI03 | 通过 | 实际取消保留 5 MiB、节点中断保留 16 MiB，206 从检查点续传，最终正常校验并完成真实问答。 [windows-install-recovery.json](../local-ai-development/windows-install-recovery.json)、[runtime-recovery-20260914.md](../local-ai-development/runtime-recovery-20260914.md) |
| AI04 | 通过 | 真实 Windows CPU DLL 缺失返回明确错误且不假健康；Update runtime 实际重新下载修复，自检成功，发布保持关闭。 [lifecycle-browser-checkpoints-20260914.json](../local-ai-development/lifecycle-browser-checkpoints-20260914.json)、[native-dependency-20260914.md](../local-ai-development/native-dependency-20260914.md) |
| AI05 | 通过 | 真实节点重启保留配置与会话，页面启动模型重新检查健康，原会话可继续真实问答。 [README.md](../local-ai-development/README.md)、[runtime-recovery-20260914.md](../local-ai-development/runtime-recovery-20260914.md) |
| AI06 | 通过 | 真实签名 Provider 请求在未授权时拒绝且模型零调用；真实双节点页面另证好友关系本身不开放 AI。 [windows-two-node-native.json](../ai-permissions-development/windows-two-node-native.json)、[test_ai_access_provider.py](../../../tests/test_ai_access_provider.py) |
| AI07 | 通过 | 真实模型获准 B 调用成功；签名加密三身份集成验证 C 与 B 请求模型 Y 被拒绝。 [windows-two-node-native.json](../ai-permissions-development/windows-two-node-native.json)、[test_ai_access_provider.py](../../../tests/test_ai_access_provider.py) |
| AI08 | 通过 | 真实任务 running 后撤销，新旧权限拒绝；取消任务释放一次，完成任务各结算一次，不声称立即停算。 [README.md](../ai-permissions-development/README.md)、[windows-two-node-native.json](../ai-permissions-development/windows-two-node-native.json) |
| AI09 | 通过 | 重启、旧版本、重新授权和重新好友关系的权限验证均拒绝继承旧授权。 [windows-two-node-native.json](../ai-permissions-development/windows-two-node-native.json)、[test_ai_access.py](../../../tests/test_ai_access.py) |
| AI10 | 通过 | 真实模型停止、占用导致删除失败、释放后删除成功及重启，实际空间与 10 段历史/16 笔订单对应。 [lifecycle-browser-20260914.md](../local-ai-development/lifecycle-browser-20260914.md)、[lifecycle-browser-checkpoints-20260914.json](../local-ai-development/lifecycle-browser-checkpoints-20260914.json) |
| SEARCH01 | 通过 | 真实四类存储分别查询并从浏览器打开对应文章、好友消息和 Ask 命中。 [README.md](../search-development/README.md)、[windows-browser-node.json](../search-development/windows-browser-node.json) |
| SEARCH02 | 通过 | 中文无空格、英文大小写及中英混合查询和 Unicode 高亮由实际页面及核心检查覆盖。 [README.md](../search-development/README.md)、[windows-browser-node.json](../search-development/windows-browser-node.json) |
| SEARCH03 | 通过 | 日期/好友/类型/来源组合、清除、打开与返回均已验证，返回恢复 2375px 和筛选值。 [filter-combinations-20260914.md](../search-development/filter-combinations-20260914.md)、[filter-combinations-checkpoints-20260914.json](../search-development/filter-combinations-checkpoints-20260914.json) |
| SEARCH04 | 通过 | 文章收藏/历史/分享合并为一个结果；独立消息身份保留，实际双节点下载前后均核对。 [revocation-browser-20260914.md](../search-development/revocation-browser-20260914.md)、[revocation-checkpoints-20260914.json](../search-development/revocation-checkpoints-20260914.json) |
| SEARCH05 | 通过 | 新增/改名实测更新在建议 5 秒内；旧片段立即过滤，删除直接打开 409，损坏来源隔离。 [cache-privacy-20260914.md](../search-development/cache-privacy-20260914.md)、[cache-privacy-http-20260914.json](../search-development/cache-privacy-http-20260914.json) |
| SEARCH06 | 通过 | 保持旧加密索引不变进行真实撤权，受保护结果与直接打开均拒绝；独立副本保留。 [revocation-browser-20260914.md](../search-development/revocation-browser-20260914.md)、[revocation-checkpoints-20260914.json](../search-development/revocation-checkpoints-20260914.json) |
| SEARCH07 | 通过 | 下载前仅卡片元数据，来源/好友离线后合法本地内容可搜可读；缺失和撤权有明确原因。 [filter-combinations-20260914.md](../search-development/filter-combinations-20260914.md)、[filter-combinations-checkpoints-20260914.json](../search-development/filter-combinations-checkpoints-20260914.json) |
| SEARCH08 | 通过 | 真实延迟首屏及分页响应晚于新查询，页面仍保留新结果；清空和快速输入不被旧响应覆盖。 [response-order-browser-20260914.md](../search-development/response-order-browser-20260914.md)、[response-order-http-20260914.json](../search-development/response-order-http-20260914.json) |
| SEARCH09 | 通过 | 实际进程在损坏索引重建中终止后恢复 10000 条，1004 个原始文件摘要不变。 [windows-browser-node.json](../search-development/windows-browser-node.json)、[process-recovery.json](../search-development/process-recovery.json) |
| SEARCH10 | 通过 | 声明环境下真实存储 TCP 首屏查询 p95 1.627 秒；完整分页 10000 唯一身份。该计时不含浏览器绘制。 [real-store-scale-current.json](../search-development/real-store-scale-current.json)、[tcp-current.json](../search-development/tcp-current.json) |
| SEARCH11 | 通过 | 真实四类来源在禁止 TCP/UDP 与好友请求时完成本地搜索，POST 私有查询不进入日志/诊断。 [cache-privacy-http-20260914.json](../search-development/cache-privacy-http-20260914.json)、[privacy-boundaries-20260914.md](privacy-boundaries-20260914.md) |
| FOLLOW01 | 通过 | 默认未关注及明确关注有页面证据；真实存储/加密通信 23 项分页验证 20+3，无丢失。 [windows-three-node.json](../friend-feed-development/windows-three-node.json)、[test_friend_feed.py](../../../tests/test_friend_feed.py) |
| FOLLOW02 | 通过 | 真实三节点仅 B 受众，C 列表为空且直接获取拒绝。 [README.md](../friend-feed-development/README.md)、[windows-three-node.json](../friend-feed-development/windows-three-node.json) |
| FOLLOW03 | 通过 | 页面组件确认包含现在和以后好友；四个真实密钥身份集成中新加 D 仅能读取全部好友发布。 [test_friend_feed.py](../../../tests/test_friend_feed.py)、[FriendFeed.test.tsx](../../../webapp/src/screens/FriendFeed.test.tsx) |
| FOLLOW04 | 通过 | 保存文档及草稿不发布，真实节点其他好友列表保持空；发布另行确认。 [README.md](../friend-feed-development/README.md)、[windows-three-node.json](../friend-feed-development/windows-three-node.json) |
| FOLLOW05 | 通过 | 实际页面发布后 51.572 秒内观察到自动更新；手动刷新及后续修订获取已有记录。仅为所记录本机环境。 [README.md](../friend-feed-development/README.md)、[windows-three-node.json](../friend-feed-development/windows-three-node.json) |
| FOLLOW06 | 通过 | 重复刷新去重、修订变未读、真实三进程重启保留关注/版本/已读；后续页撤权另有集成覆盖。 [audience-recovery-browser-20260914.md](../friend-feed-development/audience-recovery-browser-20260914.md)、[audience-recovery-checkpoints-20260914.json](../friend-feed-development/audience-recovery-checkpoints-20260914.json) |
| FOLLOW07 | 通过 | 实际取消关注保留副本；补充在取消后发布新条目，工作器零获取、列表为空、关系和副本不变。 [cleanup-browser.json](../friend-feed-development/cleanup-browser.json)、[test_friend_feed.py](../../../tests/test_friend_feed.py) |
| FOLLOW08 | 通过 | 实际页面收窄受众与停止分享，旧新版本获取被拒绝；解析中关系撤销另由真实加密集成验证。 [audience-recovery-browser-20260914.md](../friend-feed-development/audience-recovery-browser-20260914.md)、[audience-recovery-checkpoints-20260914.json](../friend-feed-development/audience-recovery-checkpoints-20260914.json) |
| FOLLOW09 | 通过 | Bob 离线跨越收窄，最后检查时间与旧版本保留；重连移除失权条目，独立副本可读。 [audience-recovery-browser-20260914.md](../friend-feed-development/audience-recovery-browser-20260914.md)、[audience-recovery-checkpoints-20260914.json](../friend-feed-development/audience-recovery-checkpoints-20260914.json) |
| FOLLOW10 | 通过 | 真实文件占用导致发布失败，草稿重启保留并重试同身份成功；离线/空状态不伪造已读。 [audience-recovery-browser-20260914.md](../friend-feed-development/audience-recovery-browser-20260914.md)、[audience-recovery-checkpoints-20260914.json](../friend-feed-development/audience-recovery-checkpoints-20260914.json) |
| OFFLINE01 | 通过 | 页面明确下载，来源进程关闭、节点重启后正文和 PNG 可读，时间与摘要一致。 [README.md](../offline-reading-development/README.md)、[windows-browser.json](../offline-reading-development/windows-browser.json) |
| OFFLINE02 | 通过 | 普通收藏初始无下载，读取未提交副本返回未下载；页面不提供离线可读按钮、不自动创建下载。 [test_offline_reading.py](../../../tests/test_offline_reading.py)、[OfflineReading.test.tsx](../../../webapp/src/screens/OfflineReading.test.tsx) |
| OFFLINE03 | 通过 | 真实浏览器正文与本地 PNG 可读，缺图有标记；不宣称全部资源完整。 [README.md](../offline-reading-development/README.md)、[windows-browser.json](../offline-reading-development/windows-browser.json) |
| OFFLINE04 | 通过 | 真实进程终止恢复、加密检查点重核、取消与迟到写入隔离及页面取消请求状态有对应自动证据。 [test_offline_reading.py](../../../tests/test_offline_reading.py)、[OfflineReading.test.tsx](../../../webapp/src/screens/OfflineReading.test.tsx) |
| OFFLINE05 | 通过 | 实际页面来源不可达保留旧正文/时间；集成检查新版本校验后替换，并拒绝旧图片版本；密码页回归保持旧副本。 [password-gate-fix-20260914.md](../offline-reading-development/password-gate-fix-20260914.md)、[test_offline_reading.py](../../../tests/test_offline_reading.py) |
| OFFLINE06 | 通过 | 真实存储单项/总量与模拟可用空间不足拒绝且旧版可读，页面展示限制及清理动作；未填满实际磁盘。 [windows-browser.json](../offline-reading-development/windows-browser.json)、[test_offline_reading.py](../../../tests/test_offline_reading.py) |
| OFFLINE07 | 通过 | 实际页面清理/文件占用失败/重启/原操作恢复，释放已审核字节且保留新副本、收藏和进度，旧全文搜索清除。 [cleanup-retry-browser.json](../offline-reading-development/cleanup-retry-browser.json)、[cleanup-retry-http.json](../offline-reading-development/cleanup-retry-http.json) |
| OFFLINE08 | 通过 | 空正文、非支持内容、HTTP 拒绝及密码页不产生可读成功记录；真实加密好友版本获取按现权限拒绝。 [test_offline_reading.py](../../../tests/test_offline_reading.py)、[test_offline_material.py](../../../tests/test_offline_material.py) |
| OFFLINE09 | 通过 | 真实加密授权链覆盖保存、更新、撤权后拒绝新旧远程版本、源不可达重启读独立副本；撤销页面说明不远程收回。 [README.md](../offline-reading-development/README.md)、[test_offline_material.py](../../../tests/test_offline_material.py) |
| OFFLINE10 | 通过 | 实际延迟图片后恢复阅读区域；组件验证保存进度、关闭返回焦点、无自动外站回退、外链仅点击访问。 [windows-browser.json](../offline-reading-development/windows-browser.json)、[OfflineReading.test.tsx](../../../webapp/src/screens/OfflineReading.test.tsx) |
| SYNC01 | 通过 | 已安装双节点页面双端审核；真实密钥协议检查未批准、过期、重复邀请和第二设备拒绝。 [two-node-20260914.md](two-node-20260914.md)、[test_device_sync_pairing.py](../../../tests/test_device_sync_pairing.py) |
| SYNC02 | 通过 | 双端范围交集；实际收藏范围同步，集成断言未选会话/进度不读不传，字段投影排除正文和凭证。 [test_device_sync_transfer.py](../../../tests/test_device_sync_transfer.py)、[test_device_sync_records.py](../../../tests/test_device_sync_records.py) |
| SYNC03 | 通过 | 真实三范围源合并与回执，保留双方原数据和原服务键；重复记录/消息与重启另有集成覆盖。 [README.md](../device-sync-development/README.md)、[test_device_sync_transfer.py](../../../tests/test_device_sync_transfer.py) |
| SYNC04 | 通过 | 已声明回环路由/真实源环境下万条基线 100 次更改及确认 56.831 秒、200 回执、164476 加密 JSON 字节；首次合并另计，不含完整节点后台负载。 [scale-v4-10000-resumed.json](../device-sync-development/scale-v4-10000-resumed.json)、[scale-v4-10000-cached.json](../device-sync-development/scale-v4-10000-cached.json) |
| SYNC05 | 通过 | 丢响应、重启、乱序、重复和本地新写入组合保留原数据，来源未落盘不确认；实际双节点状态对应。 [test_device_sync_transfer.py](../../../tests/test_device_sync_transfer.py)、[test_device_sync_records.py](../../../tests/test_device_sync_records.py) |
| SYNC06 | 通过 | 并发取消优先、旧快照不复活，观察结果后明确再收藏恢复；真实因果存储与来源检查覆盖。 [README.md](../device-sync-development/README.md)、[test_device_sync_records.py](../../../tests/test_device_sync_records.py) |
| SYNC07 | 通过 | 因果算法不使用机器时间选择，向前重读有效；页面明确选 20% 后双端收敛并重启保留，不默认选最大值。 [README.md](../device-sync-development/README.md)、[test_device_sync_records.py](../../../tests/test_device_sync_records.py) |
| SYNC08 | 通过 | 实际页面完整展开分支，删除冲突独立恢复；保留、再次保留与丢弃确认/取消/重启都有对应证据。 [recovery-replacement-browser.json](../device-sync-development/recovery-replacement-browser.json)、[recovery-discard-browser.json](../device-sync-development/recovery-discard-browser.json) |
| SYNC09 | 通过 | 真实加密传输中源/副本分别写入失败不发回执；保留待确认并原请求重试成功。 [README.md](../device-sync-development/README.md)、[test_device_sync_transfer.py](../../../tests/test_device_sync_transfer.py) |
| SYNC10 | 通过 | 安装候选实际暂停/恢复/关闭收藏范围；新增会话范围集成覆盖停传、旧报文拒绝、旧历史保留、恢复补齐无重复。 [sync-policy-20260914.md](sync-policy-20260914.md)、[test_device_sync_transfer.py](../../../tests/test_device_sync_transfer.py) |
| SYNC11 | 通过 | 安装候选移除后双端重启仍撤销，后续修改不传；集成拒绝旧认证批次并要求重新配对。 [README.md](../device-sync-development/README.md)、[sync-policy-20260914.md](sync-policy-20260914.md) |
| SYNC12 | 通过 | 同步历史在未安装原模型时可读，绑定和材料保留；页面提示不可用，不自行换 Provider。 [README.md](../device-sync-development/README.md) |
| SYNC13 | 通过 | 设置页本地范围清理及重启、真实加密冲突/旧投递屏障已对齐；远端明确未确认，不声明全局擦除。 [reading-cleanup-http.json](../device-sync-development/reading-cleanup-http.json)、[mainflow-and-erasure-review-20260914.md](mainflow-and-erasure-review-20260914.md) |
| G01 | 通过 | 当前安装 wheel 另起全新空数据节点，实际页面逐一核对八入口及无模型/好友/历史的空状态。 [实际入口和状态证据](fresh-entry-states-20260914.md) |
| G02 | 通过 | 八项任务均有真实页面入口和完成动作；复用现有主流程记录，不要求用户手工改配置或数据库。 [mainflow-and-erasure-review-20260914.md](mainflow-and-erasure-review-20260914.md) |
| G03 | 通过 | 当前空状态、加载与既有八页面故障恢复/有内容证据逐项对齐，设置永久转圈已修复，未新增测试矩阵。 [实际入口和状态证据](fresh-entry-states-20260914.md) |
| G04 | 通过 | 各功能专属重启用例与真实进程记录覆盖保留、恢复或明确失败，原任务身份不自动重发。 [sync-policy-20260914.md](sync-policy-20260914.md)、[model-switch-recovery-20260914.md](../ask-ryn-development/model-switch-recovery-20260914.md) |
| G05 | 通过 | 各功能真实存储/加密与路由重试检查覆盖并发、原身份、重复投递与结算；已有页面错误不伪造成功。 [mailbox-recovery-20260914.md](../friends-development/mailbox-recovery-20260914.md)、[README.md](../ai-permissions-development/README.md) |
| G06 | 通过 | 节点权限拒绝、固定公开投影、模型错误与后台诊断核对完成；三个日志问题修复并回归。 [cache-privacy-http-20260914.json](../search-development/cache-privacy-http-20260914.json)、[privacy-boundaries-20260914.md](privacy-boundaries-20260914.md) |
| G07 | 通过（本轮范围） | 主要流程的键盘操作、弹窗取消/确认和焦点恢复已闭合；状态有明确文字。使用实际安装页面与已有键盘证据，不宣称屏幕阅读器或全站逐控件认证。 [keyboard-final-20260914.md](keyboard-final-20260914.md) |
| G08 | 通过（本轮范围） | 九范围导出及各本地清理边界已明确；补齐卡片历史与已知旧备份清理，文件失败重启恢复通过，秘密不默认导出，升级保留18个关键数据文件。保留凭证/删除标记和未确认远端范围明确列出。 [data-scope-final-20260914.md](data-scope-final-20260914.md) |
| G09 | 通过 | 损坏搜索源、坏同步副本、模型忙碌/不可达、来源和好友失败均有独立恢复记录；阅读及 HTTP 健康保持可用。 [test_device_sync_reading_bridge.py](../../../tests/test_device_sync_reading_bridge.py)、[cancel-timeout-browser-20260914.md](../ask-ryn-development/cancel-timeout-browser-20260914.md) |
| G10 | 通过 | 每条原用例保留条件、实际结果与证据；提交/平台/计时边界明确，本地结果不冒充远端 CI，跳过不冒充通过。 [two-node-20260914.md](two-node-20260914.md)、[ACCEPTANCE_SCOPE.md](../../product-briefs/ACCEPTANCE_SCOPE.md) |
