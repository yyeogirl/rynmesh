# Rynmesh V100 GPU Provider 需求说明

状态：已实现并完成物理环境验收
需求确认日期：2026-09-07
最终复验日期：2026-09-08
适用范围：Windows Consumer + 公网 Ubuntu V100 32GB Provider

## 1. 背景与目标

用户需要将一台具有 NVIDIA V100 32GB GPU 的公网服务器作为 Rynmesh
计算提供端，本地 Windows 电脑作为使用端。系统需要提供真实可用的大语言模型和
视频生成能力，并验证两端经公网进行严格 ICE/UDP 打洞通信，而不是只验证端口存活
或使用云端数据中继。

本需求的完成定义不是“进程已启动”，而是本地 Consumer 能通过公网 Provider：

1. 发现并调用真实 Qwen 模型；
2. 通过双方公网候选提名直接 ICE/UDP 路径；
3. 提交视频生成任务并下载真实 MP4；
4. 保存可复核的结构化结果、截图和媒体文件；
5. 在服务器冷启动后再次恢复并通过关键链路复验。

## 2. 角色和部署边界

| 角色 | 环境 | 职责 |
|---|---|---|
| Consumer | Windows 本地电脑 | 发起 LLM、P2P 和视频请求，校验响应及下载文件 |
| Provider | Ubuntu 公网服务器 | 注册节点、运行 GPU 模型、接收加密任务、生成视频 |
| Registry/Rendezvous | Provider 公网 TCP/3000 | 节点发现、容量发布和签名 ICE 信令交换 |
| 模型运行时 | Provider 回环地址 | Qwen 推理和 Provider 控制端口，不直接暴露公网 |

## 3. 功能需求

### RQ-01 服务器接入与硬件确认

- 必须能够通过管理端口登录目标服务器。
- 必须确认 GPU 为 NVIDIA V100 32GB、CUDA 可用、模型盘已挂载。
- 硬件和运行时信息必须保存为脱敏验收证据。

### RQ-02 Rynmesh Provider 节点

- Provider 节点必须在服务器本地部署并运行。
- 节点必须发布到指定 Rynmesh 网络并允许 Consumer 发现。
- 公网入口使用 TCP/3000，Provider 控制端口仅监听回环地址。

### RQ-03 Qwen 大语言模型

- 在 V100 32GB 上部署约 13B 级别的 Qwen 模型。
- 本次选用 Qwen3-14B，并使用 NF4 4-bit 量化降低显存占用。
- 模型必须返回真实生成文本，不能使用 fixture、mock 或固定响应替代。

### RQ-04 LLM 服务包

- Qwen 必须以 Rynmesh LLM package 形式发布。
- Consumer 请求必须包含服务 ID、Provider 绑定、幂等键和签名加密任务信封。
- Provider 不得记录 Prompt 或响应正文。

### RQ-05 公网 P2P 打洞

- Consumer 和 Provider 必须分别获得公网 STUN 候选。
- Registry 只承载签名信令，不承载 Prompt/响应正文。
- 必须提名实际 ICE candidate pair。
- 最终传输必须为 `ice_udp_direct`。
- `relay_allowed` 和 `relay_used` 必须为 `false`。
- 必须在该传输上得到真实 Qwen 响应。

### RQ-06 防火墙端口

- 保留 TCP/3000，用于 Registry、Peer HTTP 和视频 API。
- 放行 UDP/3000，用于 Provider 固定端口的 ICE/STUN 通信。
- SSH 管理端口保持现有设置。
- 测试完成后可按实际 Consumer 出口范围收紧 UDP 来源地址。

### RQ-07 视频生成包

- 代码库必须提供独立的视频生成 backend、异步任务服务和 Consumer CLI。
- API 必须支持提交、轮询、失败状态和下载 MP4。
- 任务必须记录模型、分辨率、帧数、步数、FPS、种子、耗时和输出哈希。

### RQ-08 视频模型选择

- 优先评估 HunyuanVideo-1.5 8B 是否适合 V100 32GB。
- 若完整模型显存预算不安全，必须选择能在该 GPU 上稳定运行的替代方案。
- 本次选择 Wan2.1-T2V-1.3B Diffusers；HunyuanVideo-1.5 的单个 DiT
  权重已接近或超过可用显存预算，未作为本机默认模型。

### RQ-09 本地调用服务器生成视频

- Windows Consumer 必须通过公网认证接口提交真实生成任务。
- Provider 必须在 V100 上执行推理并提供 MP4 下载。
- 本地下载文件的 SHA-256 必须与服务器任务结果一致。

### RQ-10 验收证据

- 节点、LLM、P2P、视频和防火墙必须分别保留 UI 截图。
- 必须保存机器可读 JSON、命令行记录和生成的 MP4。
- 证据不得包含 SSH 密码、network key 或视频 API token。

### RQ-11 自动恢复

- 云服务器或容器重启后，Provider 三个进程必须自动启动。
- 进程异常退出后必须自动拉起。
- 自动恢复后必须再次通过公网 P2P 和视频生成测试。

### RQ-12 工程质量

- 新增代码必须具有 focused tests。
- 相关 pytest 必须全部通过。
- Ruff 检查必须无发现。
- 部署脚本必须支持语法检查和安全的显式配置。

## 4. 非功能需求

### 安全与隐私

- Qwen 和 Provider 控制端口只监听 `127.0.0.1`。
- LLM 任务正文采用签名并端到端加密的任务信封。
- 严格 P2P 模式禁止 TURN/relay 回退。
- 视频 API 使用独立 token 认证；token 只保存在服务器和本地私密配置中。
- 验收报告只记录必要元数据、任务 ID、哈希和用户明确要求的测试响应。

### 可靠性

- Provider 使用固定 UDP/3000，避免云防火墙无法预知临时端口。
- Supervisor 管理 Qwen runtime、Provider gateway 和 Provider node。
- 视频任务采用异步状态机，失败必须返回可诊断状态。

### 性能预期

- 冷启动首次请求允许包含模型加载时间。
- 本次物理复验观察到首次 Qwen/视频请求约 60–81 秒。
- 模型加载后的 Qwen 验收请求曾达到 948–1,882 ms；该数值不是 SLA。

## 5. 验收条件

以下条件必须全部满足：

- [x] 公网 TCP/3000 health 返回成功。
- [x] Windows Consumer 发现 Provider 且 Registry 为 connected。
- [x] Qwen3-14B 返回真实文本。
- [x] 双方公网 ICE candidates 完成交换。
- [x] candidate pair 被提名。
- [x] `transport=ice_udp_direct` 且 `relay_used=false`。
- [x] Wan2.1 生成真实 MP4 并下载至 Windows。
- [x] 本地和服务器视频 SHA-256 一致。
- [x] 服务器冷启动后重新完成 P2P 和视频生成。
- [x] Supervisor 异常恢复测试成功。
- [x] 相关测试 77 项通过，Ruff 无问题。
- [x] 所有敏感值均未写入验收材料。

## 6. 不在本次范围内

- 多租户计费、生产级配额和商业结算。
- 面向公网生产发布所需的域名、TLS 证书和 WAF。
- HunyuanVideo-1.5 在更新架构 GPU 上的性能测试。
- 长时间压力、并发容量和多地域 SLA 测试。
- Windows Consumer 的系统级开机服务安装。

## 7. 需求到文档的追踪

- 开发实现：[`V100_GPU_PROVIDER_DEVELOPMENT.md`](V100_GPU_PROVIDER_DEVELOPMENT.md)
- 测试报告：[`V100_GPU_PROVIDER_TEST_REPORT.md`](V100_GPU_PROVIDER_TEST_REPORT.md)
- 验收结论：[`V100_GPU_PROVIDER_ACCEPTANCE.md`](V100_GPU_PROVIDER_ACCEPTANCE.md)
- 运维手册：[`GPU_PROVIDER_RUNBOOK.md`](GPU_PROVIDER_RUNBOOK.md)
- 原始证据：[`../artifacts/gpu-provider-acceptance/`](../artifacts/gpu-provider-acceptance/README.md)
