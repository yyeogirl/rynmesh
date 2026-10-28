# 空安装入口与页面状态复核

2026-09-14，已安装 `f486fb7` wheel，前端 `index-DhKuxa6a.js`。另建此前不存在的 `dist/fresh-f486fb7/user-data`，从独立安装环境启动回环 18993；沿用试用公共来源配置，没有预置好友、模型、会话、收藏或下载。浏览器为 Codex in-app browser，实际读取安装包网页。

首次实际显示 Loading from local Ryn node，随后出现 Home 和可关闭的首次引导。按 Enter 选择 Continue later 后，沿应用链接进入以下页面，不直接写数据库或手工填写路由。下面只是入口与空状态验收，不冒充全流程仅键盘完成。

| 入口 | 实际空状态与起步操作 |
|---|---|
| Home／阅读 | 无推荐时解释来源恢复且不要求模型；My reading 无记录，提供 Find something to read、Manage offline copies、My devices |
| Friends | No friends yet、No shared content；可创建邀请、粘贴后审核；明确 AI 权限默认关闭 |
| Ask Ryn | No conversations yet、无可用模型；草稿输入与保存可用，提供本地模型入口 |
| Search | Index ready、0 records；空关键词不自动列举内容，说明仅搜索本地 |
| Friend updates | 无好友、无关注、无发布或草稿；提供邀请入口，缺内容/受众时禁用发布草稿 |
| Offline reading | 0 B、无下载；无内容时禁用下载与清理，不把收藏当作离线正文 |
| My devices | 无配对、无冲突；三种同步范围均未勾选，提供邀请/审核入口，说明不复制模型和权限 |
| Local AI | not configured；可选安装方式，明确安装后不发布；无可用 Provider、任务历史为空，不允许无 Provider 下单 |

观察期间没有无限转圈或未处理错误。一次 Friends 链接同时出现在导航和正文导致自动化严格定位拒绝；改为导航内的对应链接后继续，这是定位歧义，不记为产品失败。

**G01 通过**：本次补齐干净数据下八入口的实际页面证据；全新虚拟环境安装和首次真实阅读耗时复用先前安装候选记录。

**G03 通过**：按已有组件与实际页面记录复核加载、空、错误、有内容四类状态，而不是重跑全部故障：

- Home/Reading：`Home.test.tsx` 独立请求等待、内容保留、失败重试；`Reading.test.tsx` 首次失败与刷新保留；实际来源恢复见首次阅读证据。
- Friends/updates：`Friends.test.tsx`、`FriendFeed.test.tsx` 首次/后台请求失败恢复、未确认投递和受众保存错误；真实主流程见两节点和受众恢复记录。
- Ask/AI：`AskRyn.test.tsx` 无模型/历史/草稿及 Provider 变化；`Services.llm.test.tsx` 模型、依赖和配置错误持续显示、聚焦和重试。当前 34 项通过。
- Search：`Search.test.tsx` 空查询、部分索引、重建失败与恢复、旧响应、内容不可用；有内容与返回位置已有实际页面证据。
- Offline：`OfflineReading.test.tsx` 排队不冒充可读、失败重试、旧副本保留及清理恢复；真实 Windows 文件失败和重启记录沿用。
- Devices：`Devices.test.tsx` 邀请审核、失败批准、待确认与冲突分别显示；实际双端和范围恢复记录沿用。
- Settings：此前已复现并修复永久 Loading，安装包真实 503 → 聚焦错误 → Tab/Enter 重试见 [settings-recovery-20260914.md](settings-recovery-20260914.md)。

除本轮 Services 提示外，这些前端实现未改变，复用此前 252 项全量结果；没有重新累加测试总数。G07 全流程键盘、G08 数据维护与远端 CI 仍单独保留，macOS 与公网已跳过。
