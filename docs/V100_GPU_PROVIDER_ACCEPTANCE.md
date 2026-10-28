# Rynmesh V100 GPU Provider 验收报告

验收状态：**通过（指定测试部署范围）**
首次验收日期：2026-09-07
冷启动复验日期：2026-09-08
验收环境：Windows Consumer + 公网 Ubuntu V100 32GB Provider

## 1. 验收结论

用户提出的服务器节点、Qwen 约 13B 模型、LLM package、公网 P2P 打洞、视频生成
包、V100 视频模型、本地 Consumer 调用服务器、截图留存和持续验证要求均已完成。

最终结论：

- Provider 节点可通过公网发现并调用；
- Qwen3-14B 在 V100 32GB 上真实推理；
- Windows 与 Provider 完成严格公网 ICE/UDP candidate nomination；
- `transport=ice_udp_direct`，`relay_used=false`；
- Wan2.1 在服务器生成真实 MP4，本地下载哈希一致；
- 云服务器冷启动后关键链路再次通过；
- 三个服务已由 Supervisor 自动启动和异常恢复；
- 相关代码测试 77 项通过，Ruff 无问题；
- 验收材料不包含 SSH 密码、network key 或视频 token。

## 2. 验收矩阵

| 需求 | 验收标准 | 实际结果 | 状态 | 主要证据 |
|---|---|---|---|---|
| RQ-01 | 登录服务器并确认 V100 32GB/CUDA | 硬件、驱动、CUDA、模型盘均确认 | 通过 | `server-hardware.json` |
| RQ-02 | 本地部署 Provider 节点 | Provider online、accepting orders | 通过 | `mesh-status.json` |
| RQ-03 | 约 13B Qwen 模型 | Qwen3-14B NF4 返回真实文本 | 通过 | `llm-public-result.json` |
| RQ-04 | LLM package | 服务发现、加密任务、模型响应、结算元数据完整 | 通过 | LLM task JSON |
| RQ-05 | 公网 P2P 打洞 | 双方公网候选、nominated pair、无 relay | 通过 | `p2p-final-result.json` |
| RQ-06 | 防火墙端口 | TCP/3000 和 UDP/3000 生效 | 通过 | `ui-firewall-proof.png` |
| RQ-07 | 开发视频生成包 | backend、service、CLI、测试齐全 | 通过 | `rynmesh/video_package/` |
| RQ-08 | 适配视频模型 | Wan2.1-T2V-1.3B 在 V100 上运行 | 通过 | `video-health.json` |
| RQ-09 | 本地调用生成视频 | Windows 提交、轮询、下载 MP4、哈希一致 | 通过 | `video-job.json`、MP4 |
| RQ-10 | 截图和证据 | 五类 UI 截图、JSON、CLI、MP4 齐全 | 通过 | 验收目录 |
| RQ-11 | 冷启动与恢复 | Supervisor 自动管理，异常退出可恢复 | 通过 | 冷启动复验 JSON |
| RQ-12 | 工程质量 | 77 tests passed，Ruff clean | 通过 | 测试报告 |

## 3. 关键验收证据

证据根目录：
[`../artifacts/gpu-provider-acceptance/`](../artifacts/gpu-provider-acceptance/README.md)

### 3.1 节点与硬件

- [`server-hardware.json`](../artifacts/gpu-provider-acceptance/server-hardware.json)
- [`mesh-status.json`](../artifacts/gpu-provider-acceptance/mesh-status.json)
- [`ui-overview-proof.png`](../artifacts/gpu-provider-acceptance/ui-overview-proof.png)

证明公网 Provider、Windows Consumer、V100/CUDA、Qwen 和视频 runtime 状态。

### 3.2 Qwen 对话

- [`llm-public-result.json`](../artifacts/gpu-provider-acceptance/llm-public-result.json)
- [`ui-llm-proof.png`](../artifacts/gpu-provider-acceptance/ui-llm-proof.png)

真实响应为 `Rynmesh 公网 Qwen3-14B 对话验收通过`，任务成功，未使用 relay。

### 3.3 严格 P2P

- [`p2p-final-result.json`](../artifacts/gpu-provider-acceptance/p2p-final-result.json)
- [`ui-p2p-proof.png`](../artifacts/gpu-provider-acceptance/ui-p2p-proof.png)

证据包含：

- Windows 和 Provider 公网候选；
- Consumer 和 Provider 两端的 nominated pair 视图；
- `transport=ice_udp_direct`；
- `relay_allowed=false`、`relay_used=false`；
- 987 B 加密请求和 952 B 加密响应；
- 真实 Qwen 响应 `严格 P2P 最终验收通过`。

### 3.4 防火墙

- [`firewall-udp-enabled.png`](../artifacts/gpu-provider-acceptance/firewall-udp-enabled.png)
- [`ui-firewall-proof.png`](../artifacts/gpu-provider-acceptance/ui-firewall-proof.png)

云控制台截图显示 UDP/3000 入方向规则；运行时 P2P nomination 和模型响应独立证明
该规则实际生效，而不是只依赖截图判断。

### 3.5 视频生成

- [`video-health.json`](../artifacts/gpu-provider-acceptance/video-health.json)
- [`video-job.json`](../artifacts/gpu-provider-acceptance/video-job.json)
- [`video-cli-transcript.txt`](../artifacts/gpu-provider-acceptance/video-cli-transcript.txt)
- [`ui-video-proof.png`](../artifacts/gpu-provider-acceptance/ui-video-proof.png)
- [`final-public-video.mp4`](../artifacts/gpu-provider-acceptance/final-public-video.mp4)

基线 MP4 为 480×272、33 帧、8 FPS、4.125 秒，服务器和本地 SHA-256 均为：

```text
261544fc28676c564bad17fea635c851fd9ee91795e6d4053ef375e6aa4ba62f
```

### 3.6 冷启动复验

- [`cold-restart-retest-2026-09-08.json`](../artifacts/gpu-provider-acceptance/cold-restart-retest-2026-09-08.json)
- [`retest-2026-09-08.mp4`](../artifacts/gpu-provider-acceptance/retest-2026-09-08.mp4)
- [`retest-supervisor-2026-09-08.mp4`](../artifacts/gpu-provider-acceptance/retest-supervisor-2026-09-08.mp4)

服务器重启后发现并修复 PID 文件不能自动恢复的问题。迁移到 Supervisor 后：

1. 三个 program 均为 `RUNNING`；
2. 强制终止 Provider 后自动产生新 PID；
3. 严格 P2P Qwen 任务再次成功；
4. 视频 pipeline 再次加载并生成新 MP4；
5. 新 MP4 本地哈希与服务器结果一致。

## 4. 缺陷关闭情况

| 缺陷 | 处理 | 验证 |
|---|---|---|
| 云防火墙缺少 UDP/3000 | 用户新增入方向允许规则 | 严格 ICE nomination 成功 |
| Provider 对 prflx 误判 | 允许公网 `srflx/prflx`，继续禁止 relay | 单元测试和物理 P2P 通过 |
| 重启后 PID 文件残留 | 改用 Supervisor autostart/autorestart | 强制终止和冷启动复验通过 |

当前没有阻塞本次指定范围验收的未关闭缺陷。

## 5. 已知非阻塞限制

1. Qwen 和 Wan 视频 pipeline 的首次冷加载约需 60–81 秒。
2. 当前公网入口用于测试部署；生产发布应增加 TLS 反向代理。
3. UDP/3000 当前允许 `0.0.0.0/0`，生产环境应按实际来源收紧。
4. 尚未进行长时间 soak、高并发、多租户和商业 SLA 验收。
5. Windows Consumer 当前随用户会话启动，不是 Windows 系统服务。

这些限制必须保留在发布说明中；在完成生产加固前，不应将本结果宣传为生产级公网
托管服务认证。

## 6. 安全复核

- [x] 模型和 Provider 控制端口只监听回环地址。
- [x] LLM Prompt/响应通过签名加密任务信封传输。
- [x] Registry 只承载签名信令和必要元数据。
- [x] 严格 P2P 禁止 relay/TURN。
- [x] 视频 API 要求 token。
- [x] 文档、截图和 JSON 未保存认证秘密。
- [x] `.codex-tmp/` 明确排除在提交范围外。

## 7. 测试结论

详见 [`V100_GPU_PROVIDER_TEST_REPORT.md`](V100_GPU_PROVIDER_TEST_REPORT.md)。

最终自动化检查：

```text
pytest: 77 passed
ruff: All checks passed!
```

物理环境验收不是由上述自动化测试替代；公网 Qwen、P2P 和视频任务均已经在真实
Windows + V100 服务器上执行并保留证据。

## 8. 验收签署

| 项目 | 结论 |
|---|---|
| 需求覆盖 | 通过 |
| 开发实现 | 通过 |
| 自动化测试 | 通过 |
| 物理公网 P2P | 通过 |
| 真实 Qwen 对话 | 通过 |
| 真实视频生成 | 通过 |
| 冷启动与异常恢复 | 通过 |
| 证据完整性 | 通过 |
| 指定测试部署最终验收 | **通过** |

本报告可作为本次 Git 提交的验收依据。最终提交前仍须执行敏感信息扫描，并只暂存
本需求文件，避免混入当前工作区的无关改动。
