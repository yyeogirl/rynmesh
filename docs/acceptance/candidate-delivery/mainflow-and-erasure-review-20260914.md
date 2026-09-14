# 页面主流程与会话擦除收尾

2026-09-14。复核原 G02、SYNC13，不新增功能，不重复已有主流程。故障注入与合成测试数据用于建立分支条件，不能把它们当作用户日常需手工改配置或数据库的步骤。

| 用户任务 | 已有实际页面证据 |
|---|---|
| 第一次阅读、收藏、找回 | [新用户浏览器流程](../first-reading-development/browser-recovery-20260914.md)、[安装候选](../../product-briefs/TRY_CANDIDATE.md) |
| 邀请、接受、首次内容分享、撤销 | [安装 wheel 双节点页面流程](two-node-20260914.md) |
| Ask 历史、文章提问、切换模型 | [文章入口](../ask-ryn-development/article-browser-20260914.md)、[模型切换与恢复](../ask-ryn-development/model-switch-recovery-20260914.md) |
| 本地 AI 安装、恢复、好友许可 | [生命周期页面](../local-ai-development/lifecycle-browser-20260914.md)、[真实双节点权限与模型](../ai-permissions-development/windows-two-node-native.json) |
| 本地搜索、筛选、打开、返回 | [搜索与返回](../search-development/keyboard-return-20260914.md)、[筛选](../search-development/filter-combinations-20260914.md) |
| 发布受众、关注和更新 | [受众与恢复](../friend-feed-development/audience-recovery-browser-20260914.md) |
| 离线保存、断网打开、空间清理 | [离线页面及后续实测](../offline-reading-development/README.md) |
| 设备配对、范围、暂停、恢复与移除 | [安装候选双端审核](two-node-20260914.md)、[同步策略页面](sync-policy-20260914.md)，会话与冲突沿用各 SYNC 原用例证据 |

八条主流程均有实际页面完成记录，**G02 在本轮 Windows 浏览器/独立本机节点范围内通过**。各分支另按对应原用例判定；不是全应用键盘完成、macOS 或跨公网声明。

**SYNC13 通过**：将已经完成的以下证据对齐原条件，不要求实现“保证远端已删除”这一额外目标：

- [设置页清理记录](../device-sync-development/cleanup-settings-browser.json)：审核会话、三个恢复分支、备份和草稿；键盘取消/确认、进度聚焦、节点重启和浏览器重载均已执行。节点与浏览器分别显示完成范围，其他设备明确未确认。
- `tests/test_ask_privacy.py::test_erasure_removes_all_local_branches_and_old_delivery_cannot_restore_them`：真实加密来源制造冲突，擦除普通会话、恢复分支和草稿；旧消息重新投递、重启、旧迁移均不能恢复被删除内容，remote_confirmed=false。
- `tests/test_conversation_cleanup.py`：来源→副本→审核备份→搜索→订单结果的协调清理；响应丢失、忙碌索引、文件失败与重启重试保留原身份，也保留审核后新数据；已结束订单只清除结果正文，身份和结算保留。

远端未确认始终属于“未确认”，没有被转换成全局擦除成功。完整所有产品数据维护仍由 G08 独立跟踪，不因 SYNC13 通过而一并宣称完成。
