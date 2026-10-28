# Rynmesh V100 GPU Provider 开发说明

状态：实现完成并已通过验收
实现分支：`feature/p2p-peer-transit`
实现基线：`7a6d6af`
关联需求：[`V100_GPU_PROVIDER_REQUIREMENTS.md`](V100_GPU_PROVIDER_REQUIREMENTS.md)

## 1. 实现概览

本次实现把公网 V100 32GB 服务器组成一个可被 Windows Consumer 调用的
Rynmesh Provider。实现由四条链路构成：

```text
Windows Consumer
  ├─ TCP/3000 → Registry / Provider gateway
  ├─ UDP/ephemeral ⇄ UDP/3000 → strict ICE direct LLM task
  └─ TCP/3000/video → async Wan2.1 job → MP4 download

V100 Provider
  ├─ 127.0.0.1:8080 → Qwen3-14B Transformers runtime
  ├─ 127.0.0.1:8791 → Rynmesh Provider node
  └─ 0.0.0.0:3000 → public gateway + UDP ICE bind
```

## 2. 代码组成

### 2.1 Qwen Transformers runtime

文件：[`../rynmesh/llm_runtime_server.py`](../rynmesh/llm_runtime_server.py)

- 提供 OpenAI-compatible 模型列表和 Chat Completions 接口。
- 从服务器本地模型目录加载 Qwen3-14B。
- 使用 bitsandbytes NF4 4-bit 配置适配 V100 32GB。
- 服务只监听回环地址，公网请求必须经过 Provider。
- 对模型加载和推理错误返回明确 HTTP 状态。

### 2.2 Provider gateway

文件：[`../rynmesh/provider_gateway.py`](../rynmesh/provider_gateway.py)

- 在同一公网 TCP/3000 入口组合 Registry、加密 Peer 路由和视频 API。
- `/health` 提供不含敏感信息的公网存活检查。
- `/peer` 将签名加密 LLM 任务代理到回环 Provider node。
- `/video` 挂载带独立 token 认证的视频服务。
- 内部 Peer 请求使用由 network key 派生的认证值，不在报告中输出原值。

### 2.3 严格 ICE/UDP P2P

文件：[`../rynmesh/llm_package/p2p.py`](../rynmesh/llm_package/p2p.py)

主要开发内容：

- 增加 `RYNMESH_P2P_BIND_PORT`，允许 Provider 固定绑定 UDP/3000。
- 固定端口连接仍通过 STUN 生成 server-reflexive 公网候选。
- 严格模式只交换 `host`、`srflx`、`prflx` UDP 候选，拒绝 relay。
- 要求双方具有不同公网出口并保存 nominated pair 证据。
- 为 JSON 任务载荷增加分片、摘要、ACK、重传和有界缓冲。
- 允许合法的 peer-reflexive nominated candidate；这解决了云防火墙放行后
  Provider 将实际 `prflx` 直连候选误判失败的问题。
- 输出 `transport`、`relay_used`、候选类型、端点和请求/响应字节数。

数据面不经过 Registry：Registry 只交换签名的 ICE offer/answer，Prompt 和模型
响应通过 nominated UDP pair 传输，并继续使用现有端到端任务加密。

### 2.4 视频生成包

目录：[`../rynmesh/video_package/`](../rynmesh/video_package/)

| 文件 | 职责 |
|---|---|
| `backend.py` | Wan Diffusers 模型发现、加载、CUDA/CPU offload、MP4 输出 |
| `service.py` | 认证、参数校验、异步任务状态机、文件下载和健康检查 |
| `cli.py` | Windows Consumer 提交、轮询、下载和结果打印 |
| `__init__.py` | 包级公共接口 |

服务参数有明确边界：分辨率必须为 16 的倍数，帧数必须为 `4k+1`，步数、FPS、
种子和 Prompt 长度均有限制。输出文件由服务器计算 SHA-256，Consumer 下载后再次
校验。

### 2.5 部署与进程管理

目录：[`../deploy/gpu-provider/`](../deploy/gpu-provider/)

| 文件 | 用途 |
|---|---|
| `start.sh` | 手动启动三个服务并写入 PID 文件 |
| `stop.sh` | 安全停止手动启动的进程 |
| `supervisor-wrapper.sh` | 从私密配置加载环境后以前台进程方式启动单个组件 |
| `rynmesh-gpu-provider.conf` | Supervisor 自动启动、异常重启和日志配置 |

冷启动复验发现 PID 文件会在云服务器重启后残留，因此正式运行方式改为 Supervisor。
Supervisor 已经是当前容器 PID 1 管理链的一部分，配置 `autostart=true`、
`autorestart=true`，避免依赖 systemd。

## 3. 模型选型

### Qwen

- 模型：Qwen3-14B。
- 原因：满足约 13B 级别需求，中文能力和通用对话能力合适。
- 运行方式：Transformers + bitsandbytes NF4 4-bit。
- 模型目录：`/model/ModelScope/Qwen/Qwen3-14B`。

### 视频

- 最终模型：Wan2.1-T2V-1.3B-Diffusers。
- HunyuanVideo-1.5 未采用：单个 DiT 权重约 33.3GB，尚未包含 VAE、文本编码器、
  activations 和框架开销，在 V100 32GB 上没有安全运行余量。
- Wan2.1 1.3B 能使用 Diffusers FP16 和 CPU offload 在 V100 上完成真实生成。

## 4. 配置与端口

| 配置 | 值或来源 | 是否公网 |
|---|---|---|
| Registry/Gateway | TCP/3000 | 是 |
| ICE Provider bind | UDP/3000 | 是 |
| Qwen runtime | 127.0.0.1:8080 | 否 |
| Provider node | 127.0.0.1:8791 | 否 |
| network key | `/opt/rynmesh/config/network_key` | 私密 |
| video token | `/opt/rynmesh/config/video_api_token` | 私密 |
| LLM manifest | `/opt/rynmesh/config/llm/packages/qwen3-14b/manifest.json` | 私密配置 |

代码、日志和验收材料不得包含密钥原值。仓库只提供环境变量名称和配置路径。

## 5. 关键设计决定

1. Provider 固定 UDP/3000，Consumer 使用临时 UDP 端口。
2. 严格 P2P 不配置 TURN，也不允许失败后回退到 relay。
3. `prflx` 是 ICE 标准中的直接候选，严格公网模式允许其被提名，但仍禁止 relay。
4. LLM 和视频模型运行时不直接暴露公网。
5. 视频使用异步 job API，避免长时间 HTTP 提交请求阻塞。
6. 服务器使用 Supervisor，而不是当前容器中不可用的 systemd。
7. 验收证据保存哈希和必要元数据，不保存认证秘密。

## 6. 测试实现

新增或更新：

- [`../tests/test_llm_runtime_server.py`](../tests/test_llm_runtime_server.py)
- [`../tests/test_video_package.py`](../tests/test_video_package.py)
- [`../tests/test_llm_package.py`](../tests/test_llm_package.py)

覆盖模型 API、视频参数/认证/任务状态、P2P 分片、固定端口、候选校验、候选提名、
无 relay 和 peer-reflexive 直连场景。物理环境结果见
[`V100_GPU_PROVIDER_TEST_REPORT.md`](V100_GPU_PROVIDER_TEST_REPORT.md)。

## 7. 部署步骤摘要

完整命令见 [`GPU_PROVIDER_RUNBOOK.md`](GPU_PROVIDER_RUNBOOK.md)。部署顺序为：

1. 安装 Python/CUDA 依赖并准备本地模型目录；
2. 写入服务器私密配置文件；
3. 部署应用代码和 LLM manifest；
4. 安装 Supervisor wrapper 和配置；
5. `supervisorctl reread && supervisorctl update`；
6. 检查三个程序均为 `RUNNING`；
7. 从 Windows 运行公网 health、P2P、Qwen 和视频测试。

## 8. 回滚方式

1. `supervisorctl stop rynmesh-provider-node rynmesh-provider-gateway rynmesh-llm-runtime`；
2. 从 `/etc/supervisor/conf.d/` 移除本项目配置并执行 `reread/update`；
3. 恢复前一版 `/opt/rynmesh/app`；
4. 如需临时回退，可用 `start.sh` 手动启动；
5. 不删除模型目录、生成视频或私密配置，除非操作者明确要求。

## 9. Git 提交边界

建议本次提交只包含：

```text
pyproject.toml
rynmesh/llm_runtime_server.py
rynmesh/provider_gateway.py
rynmesh/video_package/
rynmesh/llm_package/p2p.py
tests/test_llm_runtime_server.py
tests/test_video_package.py
tests/test_llm_package.py
deploy/gpu-provider/
docs/GPU_PROVIDER_RUNBOOK.md
docs/V100_GPU_PROVIDER_*.md
artifacts/gpu-provider-acceptance/
```

不得执行 `git add .`，因为当前工作区还包含与本需求无关的既有文档和规划资产。
`.codex-tmp/` 是运行时目录，不应提交。提交前应再次检查 `git status`、敏感信息和
大文件策略。
