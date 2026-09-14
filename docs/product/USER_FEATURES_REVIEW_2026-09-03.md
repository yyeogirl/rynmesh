# Review: user-facing feature proposals (2026-09-03)

Reviewed: the five work plans in [`docs/product/user/`](user/) submitted by
@yyeogirl on 2026-09-03 — First Success, Local AI Setup, Unified Assistant,
My Content, Simple Pair & Share.

Review lens (maintainer's goal): **every user feature must give someone a
compelling reason to download Ryn and keep using it, so the network grows.**
System/infra work exists to make those features work seamlessly and robustly.

## TL;DR (English)

- **Accept all five plans.** They are grounded in the real code (every "current
  problem" they cite was verified), security-aware, and correctly scoped away
  from infrastructure. Quality is high; this review is about *ordering and
  sharpening*, not rework.
- **Main gap:** the plans are written as "make the installed app usable", not as
  "why install it". Four of five describe things a single node could do as well
  as Perplexity, Feedly, LM Studio or Pocket. The only feature no competitor can
  offer is *the mesh itself* — your friends' nodes, their content, their AI.
- **Change the order:** Pair & Share is the growth loop (every invite is a new
  install) and must move from "last, P1" to the second P0, started in parallel
  with First Success. Local AI Setup drops to third because its consumer path
  is blocked on infrastructure (see below).
- **Infrastructure we (system track) commit to** so the plans can ship:
  bundled inference runtime (the current "managed" runtime is a Docker image),
  cross-NAT invite acceptance for pairing, a shared atomic-store helper and
  route-package scaffolding, two-node acceptance in CI, and public showcase
  content so the first screen proves a network exists.

---

## 1. 总体结论

五份需求都可以接受。优点很明确：

- 每一条“当前问题”都能在代码里找到（`SearchAsk.tsx` 的硬编码 seed 会话、
  `LocalModelPicker.tsx` 让用户去 ollama.com 并执行 `ollama pull`、consumption /
  watcher 接口分散在 For You 与 Settings、Peers 页无法解释“朋友怎么出现”）；
- 每份都有状态机、稳定错误码、原子持久化、导出/擦除范围、验收脚本，
  并且坚持“不伪造成功”；
- 隐私规则与仓库既有规则一致：Prompt、正文、密钥、私有路径不进 Registry、
  日志、URL、通知。

需要修正的是**顺序和“下载理由”**，不是内容。

## 2. 核心反馈：用户为什么要下载？

按“能否成为下载理由”给五份需求打分：

| 需求 | 单机就能做到？ | 竞品 | 独有价值 | 结论 |
|---|---|---|---|---|
| 首次成功（3 分钟） | 是 | 任何阅读器 | 无（但是所有其他价值的前提） | P0，第一个做 |
| 两台设备配对分享 | **否** | 无（Signal 不共享内容/AI，Syncthing 无身份与推荐） | **有：这就是 mesh** | **升为 P0，与首次成功并行** |
| 统一助手 Ask Ryn | 是 | Perplexity / NotebookLM / Open WebUI | 只有“用好友的模型”是独有的 | P0，但先做小切片 |
| 一键本地 AI | 是 | LM Studio / Ollama App / Jan | 无；是 Ask Ryn 和“把我的 AI 分享给朋友”的前提 | P0 → 第三，受基础设施阻塞 |
| 我的内容 | 是 | Pocket / Readwise | 无；是内容卡片分享的选择器 | P1，先做 Slice 1–3 |

结论：**能让人下载 Ryn 的只有网络效应**，而网络效应从第二台设备开始。
所以“扫码连接朋友 → 马上分享一条内容/一个文件 → 之后用朋友的 AI”
必须是首次体验的一部分，而不是四个单机闭环之后的事。

要求每份需求补一节 **“下载理由与成功指标”**：

1. 一句用户能在 rynmesh.ai 首页读到的话（不是内部目标）；
2. 一个本地可测的指标（例如：首次成功用时、邀请→安装转化、配对后 24 小时内
   是否发生第一次分享）。不做集中遥测，指标来自验收脚本和用户主动的
   “分享诊断”。

## 3. 建议顺序

```text
第 1 批（并行）
  A. 首次成功 Slice 1–3          （吸收 #16 来源健康、#17 解释信号与撤销）
  B. 两台设备配对 Slice 1–3       （局域网 / 已可达端点；系统组同时交付跨 NAT 接受）
第 2 批
  C. 统一助手 Slice 1–2           （小 PR：去 seed、改名 Ask Ryn、统一会话存储；吸收 #24 #25）
  D. 配对 Slice 4–5 + 内容卡片    （需要“我的内容”只作为选择器：已保存列表）
第 3 批
  E. 一键本地 AI Slice 1–4        （等系统组交付内置运行时与签名模型清单）
  F. 我的内容 Slice 1–3           （Slice 4 本地文件、Slice 5 批量操作后置）
  G. 统一助手 Slice 3–4，Private AI friends-only ACL（#30 剩余部分）
```

首次成功一旦有真实内容页，配对入口就应出现在“完成”页的三个非阻塞下一步里
（把“启用本地 AI”换成“连接一位朋友”排第一）。

## 4. 逐份反馈

### 4.1 首次成功（P0，同意第一个做）

- **首批内容要证明“有网络”。** 如果默认来源只是 RSS / YouTube / subreddit，
  首屏和任何阅读器没有区别。建议首批 3–5 条里至少 1 条来自 Registry 发现的
  其他 Ryn 节点发布的内容，并标注“来自 Ryn 网络”。这需要系统组维护几台公开
  展示节点（见 §5）。
- **聚合状态接口** `ryn.first-success.v1` 很好。请放在新的路由包
  （例如 `rynmesh/first_run/` + `install_first_run_routes(app, ...)`），
  不要再往 `peer_http.py`（已 2437 行）里加端点。仓库规则是单文件硬上限
  10K 行、接近 8K 强制拆分；`llm_package/routes.py` 是现成的样板。
- **吸收 #16 和 #17。** 需求里的 `healthy_sources/source_count` 就是 #16，
  “已根据你的选择调整，你随时可以撤销”就是 #17。两个 issue 转到
  `track:user-facing` 并由本需求关闭，避免两套实现。
- **“三分钟”要可测。** 验收脚本请记录 macOS 和 Windows 打包应用冷启动到
  `ready` 的耗时（不是 dev server），并写入 `docs/acceptance/first-success/`。
- 引导关闭后的“完成首次设置”轻量入口：同意；请同时删掉现在的
  `OnboardingTour` 功能介绍式引导，而不是并存。

### 4.2 两台设备简单配对与分享（建议升为 P0）

- **这是增长回路。** 邀请必须能发给**还没有安装 Ryn 的人**：一段可复制的
  文字“我在用 Ryn，安装 rynmesh.ai 后粘贴这个邀请码 XXXX（15 分钟内有效）”。
  邀请码本身就是一次性短期秘密，符合需求中“不放 network key / local token”
  的规则。请把“邀请→安装→配对”写进验收脚本。
- **v1 可达边界（需求要求维护者拍板）：** 建议批准 **“局域网 + 双方端点已
  可达”** 作为编码起点，UI 显示可达范围；**跨 NAT 的邀请接受由系统组交付**
  （复用 Registry 托管的 relay 邮箱，目前 `llm.relay-poll` 已在用同一机制），
  用户组不要自己实现打洞。在系统组交付前，UI 不宣传“任何网络扫码即连”。
- **QR 本地生成**：webapp 目前没有 QR 依赖；请选一个无网络请求的纯前端库
  并在 PR 中说明许可证。
- **关系凭证与 `trusted_roots` 分离**、一次性并发消费测试、撤销不复活：
  全部同意，这些是安全评审的必检项。
- **与 #30 的关系**：把 #30 拆成 “#30a 简单配对与分享（本需求）” 和
  “#30b Private AI friends-only ACL 与深链”，后者在本需求稳定后立即开始——
  “用朋友的 GPU”是配对之后最有说服力的独有价值。
- 内容卡片 `ryn.shared-content-card.v1` 只需要“我的内容”的**已保存列表**
  作为选择器，所以不必等“我的内容”整体完成。

### 4.3 统一 AI 助手入口（P0，先做小切片）

- **先出一个小 PR：** 删除 `SearchAsk.tsx` 的 seed 会话、把导航项改为
  Ask Ryn、把 Search & Ask 与 Private AI 的会话读写统一到一个存储。这一步就能
  关闭 #25（问当前条目）和 #24（Provider 切换）的大部分，不要等 Slice 1–2 的
  完整重构。
- **会话存储一步到位。** 需求说“更推荐逐步迁移到 node 管理的统一会话存储”。
  建议 Slice 1 就以 node 端加密存储为唯一真相，webapp 只做缓存；“我的内容”
  需求自己也写了“不要复制多个相互漂移的真相源”。
- **云端 Provider 默认不存在。** “云端：Claude”只在用户自己填入 API Key 后
  出现；默认安装没有任何云选项。请在 §6.1 明确。
- 流式（#23）后置：同意。
- 证据当作不可信、确定性截断、无证据就说不知道：同意，验收里加一条
  Prompt 注入用例（文章正文里包含“忽略以上指令”）。

### 4.4 一键启用本地 AI（P0 → 第三批，受基础设施阻塞）

- **最大的问题在系统侧：现在的“受管运行时”是 Docker 镜像**
  （`lifecycle.py` 拉取 `ghcr.io/ggml-org/llama.cpp:server@sha256:…`）。
  普通用户机器没有 Docker Desktop，所以“无 Ollama、无 Python、无终端即可完成”
  这条验收标准在当前架构下不可能通过。系统组负责把 llama.cpp server 作为
  Tauri sidecar 打包（macOS/Windows 签名与公证），并保留 Docker 路径给服务器
  节点。用户组的向导只依赖统一的“运行时能力”接口，不感知是 sidecar 还是容器。
- **不要再写一套后台任务系统。** Slice 2 “可恢复后台任务”请建在
  `rynmesh/background_workers.py`（`BackgroundWorkerRegistry`）之上：任务状态
  文件 + 一个 worker spec；注册表已经提供监督、重启、有界停止和状态接口。
- **三个档位需要一份签名的模型清单**（名称、URL、SHA-256、内存需求、许可证），
  由系统组托管并随发布更新，而不是硬编码在 webapp 里。
- 复用已运行的 Ollama、导入 GGUF 永不删除、删除受管模型高风险确认：同意。
- 完成页的下一步除了“问一个问题”，加上**“把这个 AI 分享给一位朋友”**——
  这是本地 AI 唯一的独有价值，也把用户导向配对。
- #34 delivered: native runtime + profiles + resumable download; the wizard can now target `/api/local/llm/setup/async` with `{mode: "managed", profile}`.

### 4.5 “我的内容”统一工作台（P1）

- 设计完整，但对“下载理由”的贡献最小，且切片最多（5 个）。建议：
  Slice 1（统一查询与状态）→ Slice 2（已保存与继续阅读）→ Slice 3（来源与监控，
  同时关闭 #16）。Slice 4 本地文件与 Slice 5 批量操作放到配对与 Ask Ryn 之后。
- **本地文件解析是攻击面。** PDF/文档解析请在子进程中做，带大小、超时和
  内存上限；系统组会提供一个受限的提取辅助模块，用户组不要在 node 主进程内
  直接调用解析库。
- 推荐信号与保存状态分离、“移除记录”和“删除原文件”分离：同意。
- LibraryItem 投影不要复制 consumption 记录：同意；请写明迁移版本与去重键
  （content_id 优先，规范化 URL 兜底）。

## 5. 系统组承诺（我们负责，供用户功能依赖）

| # | 交付 | 解锁的用户需求 |
|---|---|---|
| S1 | 内置推理运行时：llama.cpp server 作为桌面 sidecar，统一“运行时能力”接口，签名模型清单 | 一键本地 AI |
| S2 | 跨 NAT 邀请接受：把 `llm.relay-poll` 用的 Registry relay 邮箱泛化为通用 peer 邮箱，供配对与撤销通知使用 | 配对分享 |
| S3 | 共享基础件：原子写入辅助模块（目前 `os.replace` 模式在 4 处重复）、路由包脚手架 `install_*_routes`、版本化记录迁移约定 | 全部五项 |
| S4 | 两节点验收进 CI：基于 `RYNNET_TESTBED.md` 的自动化双节点作业 | 配对分享、统一助手 Friend Provider |
| S5 | 公开展示节点：几台持续发布内容的 Ryn 节点，首屏可发现 | 首次成功 |
| S6 | 受限文档提取子进程 | 我的内容 Slice 4、Ask Ryn 文件上下文 |

每项在 GitHub 上单独立 issue（`track:system`），用户需求 issue 在“依赖”里引用。

## 6. 横切要求

- **先定最终导航，再动五个页面。** 建议：Home / For You / Ask Ryn /
  My Content / Friends / Services / Settings。五份需求各自改导航会互相冲突；
  先提交一页 IA 决定文档（可以很短），再开始 Slice 2 类的 UI 工作。
- **模块边界。** 新端点进新路由包；新 webapp 页面一文件一屏，共享 hook 放在
  `webapp/src/domain/`（与 #26 的服务体验框架一致）。
- **安全规则不变**：Prompt、模型输出、密钥、私有路径、原始邀请秘密不进 Git、
  Registry、控制面日志、普通节点日志、URL、通知、截图证据。
- **PR 粒度**：每个 Slice 一个 PR，先后端契约后 UI；每个 PR 附验收脚本结果
  （耗时与错误码，不含正文）。
- **认领流程**：本文和五份需求已入库；对应 issue 在
  [rynmesh.ai/contribute](https://rynmesh.ai/contribute) 展示，任何人可用
  `/claim` 认领切片。

## 7. 维护者决定（2026-09-03，全部批准）

1. 配对 v1 可达边界：局域网 + 已可达端点，跨 NAT 由 S2（#35）交付。
2. 配对分享升为 P0，与首次成功并行；本地 AI 降为第三批，等 #34。
3. 最终导航：Home / For You / Ask Ryn / My Content / Friends / Services / Settings。
4. #16、#17 转为用户功能，由“首次成功”关闭。

#30 已改为“简单配对与分享 v1”；好友专属 Private AI 授权拆到后续 issue。
系统组承诺对应 issue：S1 #34 · S2 #35 · S3 #36 · S4 #37 · S5 #38 · S6 #39。
