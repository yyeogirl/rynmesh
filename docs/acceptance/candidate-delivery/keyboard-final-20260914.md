# 主要流程键盘验收

原 G07 在本轮 Windows 浏览器范围通过。操作使用浏览器键盘事件 Enter、Space、方向键、Tab 和 Escape；文本框使用工具的文本输入能力。没有用脚本直接改页面状态、发业务请求来代替这些页面操作。工具对命名控件发送按键会先定位该控件，因此这不是逐个 Tab 穷举全站，也不是屏幕阅读器、操作系统剪贴板或多尺寸认证。

本次使用已安装 `04d94f2` wheel、资源 `index-DaWWiomT.js`，两个独立试用节点 18990/18991；AI 复用已有真实 Qwen2.5-0.5B 模型，由同一安装环境启动 18846。没有重新下载模型或重跑已通过的性能/网络矩阵。

| 主要任务 | 键盘证据和结果 |
|---|---|
| 阅读与收藏 | 复用首次阅读和阅读窗口焦点证据；本次 Enter 打开真实文章，分享弹窗关闭后焦点回到 Share with a friend，阅读窗 Escape 关闭后回到 Open reading view |
| 邀请、消息、分享与撤销 | Enter 创建和审核邀请、接受；文本输入后从消息框 Tab 经附件到发送，Enter 后出现 Delivered · confirmed by your friend。文章分享使用接收人原生选择框方向键/Enter，发送后明确确认卡片已收到；B Enter 下载并打开文档。撤销弹窗初始聚焦 Cancel，Tab/Enter 确认后双方关系已撤销 |
| Ask Ryn | 复用[文章提问](../ask-ryn-development/article-browser-20260914.md)和[运行时恢复](../local-ai-development/runtime-recovery-20260914.md)中的实际 Tab/Return 接收方确认与真实整段回答；[搜索返回](../search-development/keyboard-return-20260914.md)实测 Enter 打开并定位归档消息，无模型可读 |
| 本地 AI 与好友权限 | Enter 从 Services → Manage 启动已有模型，页面从 not ready 变为 ready on this device，Enter 自检后恢复可用。好友授权弹窗 Cancel 初始聚焦，Tab/Enter 允许后显示 Allowed，再同样撤销显示 Not allowed。模型删除弹窗 Escape 取消，焦点回到 Delete managed model；Enter 停止运行时后确认为 not ready，模型文件保留 |
| 搜索与找回 | 复用[完整键盘查询、筛选和分页](../search-development/keyboard-return-20260914.md)：Tab 到搜索框、筛选、分页，Enter 加载后焦点进入第一条新增结果，打开正确消息；返回位置由浏览器 back API 核对，不冒充 OS 快捷键 |
| 好友更新 | 方向键选择内容、Space 选择唯一受众、Enter 保存草稿；审核弹窗 Cancel → Tab → Enter 发布。B Enter 关注/刷新并看到 Version 2，Enter 保存并打开副本。A 键盘停止分享；B 键盘取消关注，最终双方时间线为空。状态用 Published、Unread 和实际受众文本说明，没有依赖颜色 |
| 离线阅读 | 本次 Enter 请求下载、进入 Offline downloads；显示 Body available offline 和已核验字节后，Enter 打开正文并显示 Offline copy。清理的 Tab/Enter、失败焦点、重启继续已由[真实文件占用实测](../offline-reading-development/cleanup-retry-browser.json)覆盖；本次没有重复整机断网 |
| 自己的设备同步 | 双端使用 Space 选择 Saved content、Enter 创建/审核邀请、勾选“自己的设备”、请求及批准；两端核对码一致。页面先显示 Pairing confirmed，再显示对端确认且 0 local changes waiting。移除弹窗 Cancel/Enter 取消后焦点恢复，第二次 Tab/Enter 确认移除；最终两端状态均 revoked |

新卡片清理入口另实测键盘审核、取消、文件失败、重启继续与完成聚焦，见[卡片清理检查点](../friends-development/card-cleanup-browser-20260914.json)。通用确认框的焦点约束及错误/成功文本由已有组件回归共同验证。G07 要求的弹窗焦点和非颜色状态有以上实际页面证据，不据此宣称所有辅助技术兼容。

[最终只读复核](keyboard-final-checkpoints-20260914.json)确认：两试用节点没有活动好友、没有活动设备配对和关注；模型已停止且没有对外发布。原本已保存副本和新产生的明确测试记录保留，未用整目录清除整理测试现场。
