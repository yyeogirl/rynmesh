# 本轮隐私边界收尾

2026-09-14，生产代码 `f486fb7`。仅评定原 READ10、SHARE10、ASK10、SEARCH11、G06；macOS 和公网按已确认范围跳过。不是对未知第三方服务或所有历史文件作无泄漏保证。

复现并修复三处问题：推荐方向进入审计详情；注册服务的异常正文、引导来源路径进入日志；原生运行时把子进程标准输出/错误直接保存。前三个针对性检查在修复前失败，修复后通过。现在审计保留计数，注册诊断保留异常类型和来源类别，运行时日志只保留节点生成的启动状态与退出码。显式 Owner 导出的偏好仍完整保留。历史日志不会因升级自动全部抹除；本轮约束是当前版本不再新增上述明文，已有审计清除入口继续保留。

| 边界 | 核对及证据 | 结论 |
|---|---|---|
| 阅读画像与解释记录 | `peer_http.py` 的审计调用逐项核对；`test_recommendation_privacy.py` 记录真实 DigestService 的模型请求和来源 URL，私有方向、反馈信号和事件 ID 均不外发；[下载文件核对](reading-export-file-20260914.md)与[重置记录](../first-reading-development/privacy-recovery-20260914.md)覆盖 Owner 范围 | READ10 通过 |
| 好友权限和秘密 | `test_friends.py` 从真实签名配对、消息、内容下载、重启到撤销，信任根前后均为空；`test_product_export.py` 对真实关系、卡片、发布受众使用允许字段，排除邀请、关系秘密及未知凭证；好友模块不记录原始消息/邀请；邮箱和消息存储失败日志只输出类型及受限标识 | SHARE10 通过 |
| Ask 内容与模型日志 | [实际下载、删除、重启及密钥排除](../ask-ryn-development/export-privacy-browser-20260914.md)；会话 Owner 投影和清理范围沿用真实加密存储检查；模型适配器将错误正文变为固定错误码；后台任务诊断不复制结果/异常正文；新增真实子进程输出隔离检查及真实 llama.cpp 推理日志核对 | ASK10 通过 |
| 本地搜索 | `test_local_search_sources.py` 用真实收藏、阅读、分享和会话存储；重建/查询/打开时禁止 TCP connect/connect_ex、UDP sendto 和好友请求，仍找到四类内容；POST 查询私有词的路由检查确认日志和状态无原文；已有 [no-store HTTP 记录](../search-development/cache-privacy-20260914.md) | SEARCH11 通过 |
| Owner 与公开诊断 | `test_peer_http_auth.py` 枚举全部控制路由拒绝未授权隧道请求；各产品路由另有拒绝测试。检查 `store.register_node` 固定注册字段、`LLMPackageManifest.public_dict` 允许字段、Provider 状态、工作器诊断；新产品数据存储没有被整体接入这些公开投影。模型别名、节点身份及公开消息公钥按协议保留，私钥、控制令牌、任务正文、画像和关系秘密不加入 | G06 通过 |

测试边界：模型请求记录器、错误正文 canary 和故障消息属于明确的测试输入；原生子进程隔离测试使用 Python 子进程，真实 DLL 缺失/修复及模型自检另见[运行时记录](../local-ai-development/native-dependency-20260914.md)。未把替身请求描述为真实 GPU 或外网抓包。

最终改动相关后端 **138 passed / 17 skipped**（21.79 秒），AI 页面 **34 passed**，TypeScript、Vite、Ruff 通过。此前本轮全后端返回 1486 passed / 29 skipped，但运行期间追加了依赖修复，故不作为最终提交全量通过的证明；最终提交采用上述受影响范围回归。跳过项不是通过项。
