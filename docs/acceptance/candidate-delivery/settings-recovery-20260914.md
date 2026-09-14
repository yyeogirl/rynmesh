# 设置首次加载失败恢复

2026-09-14，生产修复提交 `858cda5`，Windows / Chrome，安装 wheel 后的实际页面。本次对应 G03/G07 的明确缺口，不宣称全部通用门槛完成。

## 复现与修复

首次进入 Settings 时 `getSettings()` 拒绝，旧组件没有 catch，永久留在 Loading from local Ryn node。回归在旧实现失败，报告缺少 alert 和一个未处理 rejection。

现在显示固定的连接恢复说明及 Retry settings；错误获得键盘焦点，可 Tab → Enter 重试。离开页面后的迟到响应不再更新旧页面，不展示底层异常私有文本。

## 安装包页面验证

两个节点升级至带 `index-CX3z-8Lo.js` 的 wheel。另开仅监听回环的 18992 故障代理，转发候选 18990 的页面和 GET 请求，仅按模式对 `/api/local/settings` 返回真实 503。没有修改产品配置、存储或浏览器内部状态。

1. 首次连同应用外层也故障时，应用显示 Cannot reach local Ryn node 和 Retry；这不是设置组件本身的证据。先恢复请求，经该按钮加载正常应用。
2. 从 Home 再进入 Settings，设置请求返回 503。页面显示 Settings could not be loaded，alert 为实际焦点，存在 Retry settings，没有永久加载。
3. 恢复代理转发，不刷新页面。Tab 将焦点移到 Retry settings，Enter 后显示 Node policy 和 Identity & storage，错误及重试按钮消失。

代理仅用于本次检查，验证后停止。两个正式试用节点继续运行在 18990/18991。

## 检查与交付

- 修复后 Settings 两个相关文件 4 项通过；随后完整前端 **252 passed / 45 files，31.93 秒**。两组有重叠，不叠加。
- TypeScript、Vite 构建、diff 空白检查通过；已有大于 500 kB 提示保留。
- 后端产品代码未改变，复用已记录的 1482 passed / 29 skipped；另两个新增验收分支各 1 项通过，未重跑完整后端。
- wheel 含 18 个前端资源，SHA-256 `644e86a40ba96661a4eefe4f48c149aa83ea59fc2b824a91a958269a8af968eb`；独立环境重新安装成功，`pip check` 通过。
- 两个已安装节点状态接口均为 200，首页引用新资源。两端收藏、身份、会话、好友、设备和导入副本共 18 个关键文件的摘要在停止升级、重启后保持一致。
- 最初关闭构建隔离失败，因为开发虚拟环境没有 setuptools；启用标准构建隔离后构建成功。没有因此改项目依赖或替换运行包来源。

当前入口及范围见[试用说明](../../product-briefs/TRY_CANDIDATE.md)。macOS 和公网按用户要求跳过，远端 CI 未执行。
