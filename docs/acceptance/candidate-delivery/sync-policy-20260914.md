# 同步暂停、范围关闭和设备移除

候选代码 `1829eb2`，两个已安装 wheel 的 Windows 节点，复用上一轮身份和内容。本轮通过后台 in-app browser 操作实际页面，观察脚本只读取 Owner API 的状态、收藏标记和副本数量。

实际结果：

1. A 暂停后，两端分别显示本机暂停/远端暂停，有效同步范围为空。B 在阅读器取消一条收藏，A 保留原收藏，双方独立正文副本均保留。
2. A 恢复后，取消收藏状态传到 A，两端待确认均归零。
3. A 关闭收藏范围，两端显示没有共同允许的类别。B 重新收藏，A 不接收这次变化；重新允许后 A 收到收藏，两端确认归零。
4. A 审阅“立即停止新同步、旧副本不能收回、重新加入需邀请”的说明后移除 B。两端变为 revoked；B 再取消收藏，A 不变。
5. 两端进程均停止并从同一数据目录重启。关系仍 revoked，A/B 的不同收藏状态保持，双方各一份独立正文仍在。

检查点及断言结果见 [sync-policy-result.json](sync-policy-result.json)。全部记录断言通过。暂停与范围测试使用收藏；会话类别的独立页面变更未在这轮重复执行，不由本结果扩张声称。

旧报文/凭证由现有自动用例补充：`test_pause_resume_invalidates_old_packet_and_old_receipt` 拒绝旧批次与确认；`test_remove_during_network_wait_prevents_ack_and_new_pair_resends` 验证移除后的旧报文被拒绝，重新配对必须新身份且重新发送；`test_revocation_stops_local_access_and_retries_notice_after_restart` 检查重启后的旧确认被拒绝、新配对不受旧撤销通知影响。这些用例包含于本轮通过的后端全量检查，未在实机链路上额外录制/重放报文。

本轮未改产品代码。结束时两个候选保留运行；好友和设备关系都已撤销，重新体验需要各自重新邀请。macOS 与公网按用户明确要求跳过，见[范围确认](../../product-briefs/ACCEPTANCE_SCOPE.md)。
