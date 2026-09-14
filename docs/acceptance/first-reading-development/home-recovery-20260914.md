# 首页局部失败修复与真实来源核验

日期：2026-09-14。开发分支：`codex/user-product-development`。

## 已修复的问题

首页原本等待活动、推荐与内容列表全部成功后才显示内容。活动请求一直等待时，
可用文章被整体加载页挡住；任一请求失败时，其他成功结果也被丢弃。
新增组件用例在旧代码上 4 项失败，并出现 3 个未处理的请求异常。

现在三个区域独立加载，失败区域有自己的重试按钮，刷新失败保留现有文章。
来源发现轮询只有成功更新推荐后才记录已处理的内容数量，因此临时失败后，
即使来源数量没变，后续轮询也能重试。

验证：6 个首页交互用例通过，包含通过实际阅读器组件打开正文；全量前端
227 个用例、TypeScript 检查和生产构建通过。这里的交互用例使用 jsdom 和
模拟 API，不能当作浏览器或桌面实机证据。

## 真实公共来源与节点重启

使用 `scripts/accept_first_reading_node.py --home <新的专用目录>` 启动生产节点，
目录必须以 `rynmesh-first-reading-acceptance-` 开头。开启桌面默认公共来源，
不预置内容、好友或模型；关闭公开注册和节点发现，HTTP 只监听本机 18930。
停止该进程后，用同一命令追加 `--resume` 重启。

本次实际目录为 `D:\code\rynmesh-first-reading-acceptance-20260914`。
执行生产接口 `GET /api/local/discovery/status`、`POST /api/local/recommendations`
（limit=5）、`GET /api/local/reader?url=...`，只有正文非空才提交
`POST /api/local/consumption` 的 opened、bookmark、bookmark、progress=0.37。
重启后再次读取 consumption、first-success 和同一 reader，比较记录及正文摘要。

- 全新节点获取 30 条真实内容，14 个来源中 11 个成功、3 个失败；未安装模型。
- 首个 Reddit 文档请求返回空正文，没有记录已读或冒充首次成功。
- 随后的 BBC 文档返回 18 段正文，阅读与收藏接口成功。
- 重复收藏仅保留一条记录。更换节点进程后，完整记录和 37% 进度保留，正文缓存摘要一致。
- 对一个实际失败的 YouTube 来源单独重试，失败次数由 2 增至 3；其他 13 个来源
  的健康记录完全不变，仍有 30 条内容可用。该来源尚未恢复，不能算成功恢复验收。

原始结果和时间见 [HTTP 执行记录](public-source-http-20260914.json)。记录只保留
正文长度和摘要，不提交正文。耗时包含人工检查停顿，不作为打包应用启动性能证据。

## 未完成的边界

浏览器工具返回 `Unable to load browser request-header policy`，本轮没有页面实测。
真实桌面安装、页面错误说明和原文入口、滚动位置恢复、断网后来源恢复仍须验收。
READ01、READ03、READ04 保持 in_progress，不因接口或组件检查通过就整体标记通过。
