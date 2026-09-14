# Rynmesh V100 GPU Provider 测试报告

测试结论：通过
首次完整测试：2026-09-07
冷启动复验：2026-09-08
关联需求：[`V100_GPU_PROVIDER_REQUIREMENTS.md`](V100_GPU_PROVIDER_REQUIREMENTS.md)

## 1. 测试范围

本报告覆盖：服务器硬件、Provider 部署、公网入口、Windows Consumer、Qwen3-14B、
LLM package、严格 ICE/UDP P2P、Wan2.1 视频生成、文件完整性、进程自动恢复、
单元/集成测试和静态检查。

本报告明确区分：

- **代码测试**：本地 pytest 和 Ruff；
- **物理环境测试**：真实 Windows Consumer 与公网 V100 Provider；
- **证据复核**：JSON、截图、命令记录和 MP4 哈希。

## 2. 测试环境

### Provider

| 项目 | 值 |
|---|---|
| 公网地址 | `117.50.189.73` |
| 系统 | Ubuntu 22.04.5 LTS |
| GPU | NVIDIA V100 32GB |
| 驱动 | 580.159.04 |
| PyTorch | 2.7.1+cu126 |
| Compute capability | 7.0 |
| Transformers | 4.57.1 |
| Diffusers | 0.35.2 |
| Qwen | Qwen3-14B NF4 4-bit |
| 视频模型 | Wan2.1-T2V-1.3B-Diffusers |

初次验收记录的设备字符串为 `Tesla V100-SXM2-32GB`；2026-09-08 云实例重启后
`nvidia-smi` 报告为 `Tesla V100S-PCIE-32GB`。两次均为 32GB V100、Compute
Capability 7.0，驱动版本相同，不影响需求结论；云平台可能在重启时重新分配同等级
GPU 型号。

### Consumer

| 项目 | 值 |
|---|---|
| 系统 | Windows |
| 本地 API | `127.0.0.1:18792` |
| 网络 | `rynmesh-gpu-e2e` |
| Registry | `http://117.50.189.73:3000` |
| STUN | `stun.chat.bilibili.com:3478` |

## 3. 测试命令

### 代码测试

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\test_video_package.py `
  tests\test_llm_runtime_server.py `
  tests\test_llm_package.py `
  tests\test_llm_hardening.py `
  tests\test_transport.py -q

.\.venv\Scripts\ruff.exe check `
  rynmesh\provider_gateway.py `
  rynmesh\video_package `
  rynmesh\llm_runtime_server.py `
  rynmesh\llm_package\p2p.py `
  tests\test_video_package.py `
  tests\test_llm_runtime_server.py `
  tests\test_llm_package.py
```

结果：`77 passed`；Ruff：`All checks passed!`。唯一 pytest warning 来自
Starlette 对 `httpx` TestClient 适配层的弃用提示，不影响本次功能。

### 公网存活检查

```powershell
Invoke-RestMethod http://117.50.189.73:3000/health
```

期望和实际结果：`status=ok`。

### 严格 P2P

通过 Windows Consumer 向本地 LLM order API 提交 `transport=p2p` 的指定 Provider
订单。测试脚本从私密文件读取 network key，不在输出中记录原值。

### 视频

通过 `rynmesh.video_package.cli` 调用公网 `/video` API，轮询状态并把 MP4 下载到
Windows。token 只从本地私密环境读取，不进入命令记录和报告。

## 4. 测试矩阵

| ID | 测试 | 关键断言 | 结果 | 证据 |
|---|---|---|---|---|
| T-01 | SSH 和硬件 | 管理端口可达、V100 32GB、CUDA 可用 | 通过 | `server-hardware.json` |
| T-02 | 公网 Gateway | TCP/3000、`GET /health` 返回 ok | 通过 | `public-health.json` |
| T-03 | 节点发现 | Consumer running、Registry connected、peer count 2 | 通过 | `mesh-status.json` |
| T-04 | Qwen 真实对话 | 正确模型响应、任务成功、无 relay | 通过 | `llm-public-result.json` |
| T-05 | 双方公网候选 | Consumer 和 Provider 均有 STUN 公网候选 | 通过 | `p2p-final-result.json` |
| T-06 | candidate pair | nominated pair 可审计，Provider 公网端口为 UDP/3000 | 通过 | `p2p-final-result.json` |
| T-07 | 严格直连 | `ice_udp_direct`、`relay_used=false` | 通过 | `p2p-final-result.json` |
| T-08 | P2P 模型载荷 | 987 B 请求、952 B 响应、真实 Qwen 文本 | 通过 | `p2p-final-result.json` |
| T-09 | 视频 health | 模型存在、CUDA 可用、pipeline 可加载 | 通过 | `video-health.json` |
| T-10 | 完整视频生成 | 480×272、33 帧、20 步、MP4 下载 | 通过 | `video-job.json`、MP4 |
| T-11 | 视频哈希 | 本地文件 SHA-256 等于服务器结果 | 通过 | `video-job.json`、MP4 |
| T-12 | 云服务器冷启动 | 重启后恢复公网、P2P 和视频生成 | 通过 | `cold-restart-retest-2026-09-08.json` |
| T-13 | 进程异常恢复 | 终止 Provider 后 Supervisor 自动产生新 PID | 通过 | 同上 |
| T-14 | Supervisor 后 P2P | 重新提名公网 pair 并返回 Qwen 响应 | 通过 | 同上 |
| T-15 | Supervisor 后视频 | 新 Gateway 重新加载 pipeline 并生成 MP4 | 通过 | 同上、复验 MP4 |
| T-16 | 代码测试 | focused pytest 全通过 | 通过 | 77 passed |
| T-17 | 静态检查 | Ruff 无发现 | 通过 | All checks passed |
| T-18 | 敏感信息检查 | 报告不包含密码、network key、video token | 通过 | 文档和证据抽查 |

## 5. 关键实测结果

### 5.1 Qwen 公网调用

- Task：`task_2a3302a61e4143ef96837e5f76b88c06`
- State：`succeeded`
- Model alias：`qwen3-14b-v100`
- Duration：1,882 ms
- Transport：`peer_http_direct`
- Relay used：`false`
- 返回文本：`Rynmesh 公网 Qwen3-14B 对话验收通过`

### 5.2 首次严格公网 P2P

- Task：`task_90dd301a0b26488a9b52913e53bb20f1`
- State：`succeeded`
- Provider public candidate：`117.50.189.73:3000` / srflx
- Consumer public candidate：`14.154.222.55:60707` / srflx
- Provider 观察到 Consumer nominated candidate：prflx
- Transport：`ice_udp_direct`
- Relay used：`false`
- Duration：948 ms（模型已加载）
- 模型响应：`严格 P2P 最终验收通过`

### 5.3 冷启动后的严格 P2P

第一次服务器冷启动复验：

- Task：`task_158ff74a99bc4047afa72ad9be2a49a8`
- State：`succeeded`
- Duration：79,034 ms
- Transport：`ice_udp_direct`
- Relay used：`false`

迁移到 Supervisor 并强制恢复 Provider 后：

- Task：`task_c32ac6c2a35444fe8cd3def4c071fe01`
- State：`succeeded`
- Duration：80,693 ms
- Provider remote nominated candidate：`117.50.189.73:3000` / srflx
- Request/response：987 B / 952 B
- Relay used：`false`

两次约 80 秒的耗时包含 Qwen 冷加载，不代表稳定态延迟。

### 5.4 视频基线任务

- Job：`vid_943f641617fa4973bce9bf7491db4b4e`
- Model：Wan2.1-T2V-1.3B
- State：`succeeded`
- Resolution：480×272
- Frames / steps / FPS：33 / 20 / 8
- Video duration：4.125 s
- Generation：47.693 s
- Bytes：40,536
- SHA-256：`261544fc28676c564bad17fea635c851fd9ee91795e6d4053ef375e6aa4ba62f`

### 5.5 冷启动完整视频任务

- Job：`vid_7dbff0240bed442a926cdb94e09bec2c`
- State：`succeeded`
- Resolution：480×272
- Frames / steps：33 / 20
- Generation：75.37 s
- Bytes：20,615
- SHA-256：`17741918e1122c75384bc95072a676bdc20d5fca8f0717b97da0490032f14481`
- 文件：`retest-2026-09-08.mp4`

### 5.6 Supervisor 接管后视频任务

- Job：`vid_15d5b0ff4a524b90881d67c9c44cea1f`
- State：`succeeded`
- Resolution：480×272
- Frames / steps：5 / 2（功能恢复短测）
- Generation：58.962 s，包含 pipeline 冷加载
- SHA-256：`54e60e561ea0afcf89d56888cf1349d3080ec096a5fb63242cce1dd2676c3e9b`
- 文件：`retest-supervisor-2026-09-08.mp4`

## 6. 缺陷发现与修复记录

### D-01 云防火墙缺少 UDP/3000

- 现象：双方 STUN 信令成功，但 ICE nomination 超时。
- 证据：独立 UDP 探针无入站包，云防火墙只有 TCP/3000。
- 修复：新增入方向 UDP/3000 允许规则。
- 回归：candidate pair 成功提名，P2P 真实 Qwen 任务通过。

### D-02 合法 prflx 候选被严格校验拒绝

- 现象：防火墙放行后 ICE 已连接，但 Provider 关闭连接。
- 原因：严格模式只接受 remote `srflx`，未接受 ICE 产生的合法 `prflx`。
- 修复：公网严格模式允许 `srflx` 或 `prflx`，继续拒绝 relay。
- 回归：新增单元测试并完成物理公网验证。

### D-03 重启后 PID 文件残留

- 现象：云服务器重启后 TCP/3000 未恢复，旧 PID 文件仍存在。
- 原因：`nohup + PID file` 不是容器级开机管理机制。
- 修复：新增 Supervisor wrapper 和三个 autostart/autorestart program。
- 回归：强制终止 Provider，PID 从 354 自动恢复为 379；随后 P2P 和视频通过。

## 7. 已知限制与风险

- 首次请求需要加载模型，冷启动约 60–81 秒；应在产品 UI 显示 loading 状态。
- 当前测试入口为 HTTP。签名加密 LLM 任务正文具有端到端保护，但生产视频 API
  仍应放在 TLS 反向代理之后，避免 bearer token 暴露风险。
- 当前 UDP/3000 来源为 `0.0.0.0/0`，生产环境应根据网络方案和客户端范围收紧。
- 本轮未进行高并发、长时间 soak、多租户隔离和 SLA 测试。
- Windows Consumer 当前由用户会话启动，未安装成 Windows 系统服务。

以上限制不阻塞本次指定测试部署验收，但阻塞将其直接声明为生产级公共服务。

## 8. 证据目录

完整证据位于：
[`../artifacts/gpu-provider-acceptance/`](../artifacts/gpu-provider-acceptance/README.md)

其中包括：

- 五张证据型 UI 截图；
- 公网、硬件、节点、LLM、P2P、视频和冷启动 JSON；
- 视频 CLI 记录；
- 三个最终/复验 MP4 及 SHA-256；
- 可交互 HTML 验收控制台。
