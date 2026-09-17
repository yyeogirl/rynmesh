# 本地推理 API 与双节点 P2P 验收说明

日期：2026-09-18

## 结论与交付范围

验收结论：本次本地推理 API 与双节点 P2P 核心需求验收通过，可以提交代码评审。
依据是服务器关闭前已完成的真实模型、SDK、严格 P2P 实测，以及当前代码回归结果。
用户于 2026-09-18 确认服务器由其主动关闭，并确认按此前跑通的记录完成本次功能验收。
此结论不表示服务器当前在线，也不表示已完成安装包发布或主分支整合。

2026-09-18 现场复验：本地 API 可访问，但 `qwen` 请求返回 HTTP 503
`model_alias_target_unavailable`；服务器 SSH 端口 23 连接超时，节点日志也记录
注册服务访问超时。随后用户确认测试服务器已被主动关闭，因此此次离线复测未能
执行推理检查，不作为功能验收失败或回归缺陷；保留原始观察结果，不将其改写为
本轮调用成功。重新启用服务时可按下方命令进行上线检查。

本次实现让项目和 Agent 通过本机 Ryn 的 OpenAI 兼容 API 调用本机模型或远端
Ryn 服务节点。远端推理固定使用严格 ICE/UDP P2P，打洞失败明确报错，不回退
HTTP、TURN 或密文中继。注册服务与 STUN 负责发现、信令及连接协调，不承载
该路径的提示词与回答正文；仍会保留协调、任务状态和结算元数据，不能宣称无痕。

本次实际服务模型为测试服务器上的 Qwen3-14B-Q4；用户已确认继续使用该模型。
另一台 Compshare 机器的 27B Uncensored 模型未接入本次链路。

## 已验证

| 项目 | 结果与证据 |
| --- | --- |
| 本地／远端模型统一 API | 已验证本地执行与远端执行；本地执行实测在 GPU 服务节点进行，Windows 未安装本地 GPU 模型 |
| OpenAI Chat、Responses、Anthropic Messages | 支持文档列明的文本和客户端函数工具子集；历史三条测试路径共 27 项 SDK 检查通过 |
| Agent 使用短名称 | 本机页面保存 qwen 到节点／模型的映射，持久化、目标离线报错、旧 ID 兼容均有测试 |
| 配置页面 | Services → Manage → API access；别名、目标、密钥、预算、调用示例；页面交互自动化测试通过 |
| 严格 P2P | 同一实际本地 API 普通与流式调用均记录 ice_udp_direct、relay_used=false，启用公网打洞及不同出口校验 |
| 密钥与权限 | 独立推理密钥、哈希保存、撤销、预算、并发、loopback 限制、管理权限隔离均有测试 |
| 长输出及 Agent 参数 | 支持 enable_thinking、128000 请求预算自动收敛；真实 76 工具请求回放成功 |
| 长上下文 | 实测 40933 输入 tokens 加 1 输出；32768 输出参数接受，实际 2304 tokens 长回答完成 |
| 取消请求 | 历史 HTTP/P2P SDK 断流取消与余额释放验证通过 |
| 当前代码回归 | 2026-09-18：120 项后端测试、12 项 Services 页面测试通过，Ruff 与前端生产构建通过 |
| 当前现场 SDK 复验 | 用户已主动关闭服务器，本轮未完成推理检查；观察到 503 与 SSH 超时，不影响关闭前实测的验收结论；见 final-audit.json |

使用说明见 [LOCAL_INFERENCE_API.md](LOCAL_INFERENCE_API.md)。详细证据见
[验收报告](../artifacts/inference-api-acceptance/README.md)及其 JSON 文件。

## 交付状态与验证边界

- 测试服务器由用户主动关闭；当前离线不列为本次功能验收阻塞项。
- 用户要求的实际页面标注截图尚未交付：浏览器接口故障，备用截图工具因无法
  可靠确定浏览器 URL 停止。旧报告中的视觉检查发生在后续别名改动之前，不能
  代替这项截图验收。
- 本次未打包或发布新的桌面安装程序。当前电脑的后台启动、登录启动及退出重启
  是现场运维配置，位于忽略目录 `.rynmesh`，不是已发布的安装器功能。
- 未连续生成完整 32768 tokens；跨所有 NAT／防火墙环境、长期稳定性和所有
  Agent 客户端兼容性未验证。实际提示词、工具和输出共同占用上下文。
- 本分支源自 c6d15f9，检查时比 upstream/main 落后 155 个提交；尚未验证与最新
  主分支的合并结果。推送独立评审分支，不直接合并主分支。

## 复测方式

```text
python -m pytest tests/test_inference_api.py tests/test_llm_package.py tests/test_llm_hardening.py tests/test_llm_runtime_server.py tests/test_peer_http_auth.py tests/test_video_package.py -q
python scripts/inference_api_acceptance.py --base http://127.0.0.1:18796 --model qwen --output report.json
cd webapp
npm run build
npm test -- --run src/screens/Services.llm.test.tsx
```

实际部署端口、密钥和网络密钥存于本机私有配置，不提交 Git。评审分支仅包含
本次代码、测试、使用说明与脱敏证据，不包含其他产品规划、PPT 或临时文件。
