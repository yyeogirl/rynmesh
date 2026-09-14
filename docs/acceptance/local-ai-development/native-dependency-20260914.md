# Windows 原生依赖缺失与恢复

生产代码 `f486fb7`；Windows CPU、真实 llama.cpp b10774、真实 Qwen2.5-0.5B GGUF，沿用隔离测试数据目录。使用实际节点 Owner 路由和 TestClient，模型运行及推理为真实子进程/回环 HTTP；此次分支不是鼠标浏览器操作、GPU 或公网验收。

1. 确认原模型停止，临时移走受管理目录内的 `ggml-base.dll`。启动返回 409，运行时退出码 3221225781；就绪、上线和发布均为 false。
2. 首次检查发现原 Update runtime 只检查主程序是否存在，不能修复丢失的 DLL；页面还错误建议换小模型。修复后缺失依赖有明确说明，指向 Update runtime。
3. 保持 DLL 缺失，调用页面对应的 **Update runtime** Owner 操作。它停止本包运行时，从原已固定 SHA256 的 HTTPS 发布包重新下载和安装受管理运行时，再启动并自检。没有手工恢复 DLL。已显式配置的外部/捆绑运行时不被改写；其他已记录的活跃模型会阻止共享文件修复。
4. 更新返回 200，缺失 DLL 恢复且 SHA256 与原文件相同；模型 ready=true，发布仍关闭。真实自检成功，正文仅记摘要和 token 数；日志只有启动状态。结束时停止模型，保存的数据和模型保留。

原 AI04 的校验失败、模型/可执行文件缺失证据沿用 [lifecycle-browser-20260914.md](lifecycle-browser-20260914.md) 和 [runtime-recovery-20260914.md](runtime-recovery-20260914.md)。本次补齐受支持 CPU 配置下的依赖缺失与恢复，**AI04 在本轮范围内通过**；CPU 不要求 CUDA，macOS 跳过。

新增受管理缺失依赖修复、活跃兄弟模型拒绝覆盖的测试使用实际归档提取和模型文件；首次测试夹具缺少 manifest 必填字段而失败，补齐后通过，没有放宽断言。最终后端相关 138 passed / 17 skipped，页面含依赖提示/聚焦/重试共 34 passed，类型检查与构建通过。机器记录：[native-dependency-20260914.json](native-dependency-20260914.json)；重现入口 `scripts/accept_native_dependency.py`。
