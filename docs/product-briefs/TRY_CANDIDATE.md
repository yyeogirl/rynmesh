# 当前候选怎么试用

2026-09-14。八项需求的原始 95 条用例在[本轮确认范围](ACCEPTANCE_SCOPE.md)内验收完成，远程 CI 9/9 成功。macOS 实机与公网跨 NAT 按用户要求跳过。[完整验收报告](../acceptance/candidate-delivery/requirement-review-20260914.md)。

## 本机打开

- 主节点：<http://127.0.0.1:18990/>。
- 第二个独立节点：<http://127.0.0.1:18991/>。
- 启动目录：`D:/code/rynmesh-product-development/dist/candidate-04f6d28/`。双击 `Start-Ryn.cmd` 或 `Start-Second-Ryn.cmd`；后台运行时使用对应的 `Stop-Ryn.cmd` / `Stop-Second-Ryn.cmd` 退出。先等待下载、同步和模型任务结束。
- 数据分别在该目录的 `user-data/` 和 `second-user-data/`。两个节点独立身份、独立数据，均只监听本机；不会公开注册。已有真实文章、收藏及明确的验收记录保留。当前好友、设备配对及关注均已撤销，需要通过页面重新邀请才能继续传输；已保存的副本仍在。

## 安装包

当前 wheel：`D:/code/rynmesh-product-development/dist/candidate-04d94f2/rynmesh-0.6.2-py3-none-any.whl`。

SHA-256：`9e746cfbacae0e7dc8f96355c9e0756a5b05f5ccb52eef9758a60e41bc077f6c`。

已安装实现提交 `04d94f21fb702ffe0ac9ed596ccb56e681e578f4`，页面资源 `index-DaWWiomT.js`，包含 18 个前端资源文件。启动目录沿用第一版名称；其中 `installed-candidate.json` 才是当前安装记录。新包直接升级同一环境，18 个关键数据文件在升级前后摘要一致，依赖检查和两个健康/状态接口均通过。[安装证据](../acceptance/candidate-delivery/installed-04d94f2.json)。

运行环境不能直接搬到另一台机器。另机使用 Python 3.10+ 创建虚拟环境，安装此 wheel 的 `documents` 可选依赖；网页已经包含在 wheel 内，不需要 Node.js 或 Vite。这是 Windows Python 安装包与浏览器交付，不是 Windows 原生桌面安装器。

## 八项入口

| 需求 | 入口与操作 |
|---|---|
| 首次阅读 | Home / For You → 打开正文 → 收藏 → My reading；不安装模型也可用 |
| 两人分享 | Friends → 创建邀请 → 对方审核接受 → 消息/内容卡片 → 查看送达 → 移除好友 |
| Ask Ryn | Ask Ryn；文章内 Ask about this content。历史可独立阅读，提问前说明接收 Provider 和引用内容 |
| AI 设置与好友权限 | **Services → Manage** 安装、启动、自检或恢复模型；Friends → AI with friends 明确允许/撤销。Settings 的 AI curator 是既有推荐设置，不是原生模型安装入口 |
| 搜索与找回 | Search → 关键词、来源和类型筛选 → 打开原内容；新节点需先产生内容 |
| 好友内容更新 | Friend updates → 选择内容与受众 → 审核发布；对方主动 Follow；可停止分享或取消关注 |
| 离线阅读 | 正文内 Download for offline → Offline reading；状态明确显示正文是否可离线读，提供已用空间和清理 |
| 自己的多设备同步 | My reading / Settings → My devices → 双端审核自己的设备 → 选择范围 → 同步状态、冲突与移除 |

两个试用节点未配置模型。真实本地模型安装、问答、恢复、授权和键盘操作在独立的已有 Qwen 验收环境完成；该临时节点与模型已停止，文件和历史保留，不冒充两个试用节点开箱即有模型。

## 数据与验收边界

Settings → Privacy & data 提供按范围导出及阅读/会话清理；Friends 的卡片历史、已保存文档、Friend updates 和 Offline reading 各有明确清理范围。撤销关系不等于收回别人已保存的副本，远端未确认不会显示全局擦除成功。[数据范围和失败恢复](../acceptance/candidate-delivery/data-scope-final-20260914.md)。

[草稿 PR #1](https://github.com/yyeogirl/rynmesh/pull/1) 已在自己的 fork 创建；没有合并上游 main，也没有发布正式版本。[最终 CI](https://github.com/yyeogirl/rynmesh/actions/runs/34862743090) 对 `25aa0b8` 执行，产品目录与已安装 `04d94f2` 完全相同。后续验收文档提交跳过重复 CI，未声称新一轮运行。
