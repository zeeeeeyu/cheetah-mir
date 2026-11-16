# 开源选型与实际采用

核查于2026-11-05。包安装限独立MIR环境；市场/新闻源未访问。GitHub滚动页面存在缓存，“观察到的有效维护”不冒称全部项目最新HEAD。

| 项目 | 观察版本/许可证 | 环境与限制 | 决定 |
|---|---|---|---|
| [AKShare](https://github.com/akfamily/akshare) | 1.20.34 / MIT | Python>=3.11，pandas/requests/curl_cffi/mini-racer；具体来源许可另审 | 已安装并离线导入；Adapter规范化本地DataFrame；端点获取BLOCKED |
| [feedparser](https://github.com/kurtmckee/feedparser) | 6.0.14 / BSD-2-Clause | Python>=3.10；直接传本地bytes，无服务 | 已安装并实际解析本地RSS fixture |
| [Docling](https://github.com/docling-project/docling) | slim2.147.0 / MIT | Python>=3.10；默认PDF流程涉及模型；native无需OCR/layout模型 | 已装convert-core/format-pdf，实际解析本地PDF |
| [NetworkX](https://github.com/networkx/networkx) | 3.7 / BSD-3-Clause | 当前版本需Python>=3.12；基础无外部服务 | 已安装并实际构建MultiDiGraph |
| [RSSHub](https://github.com/DIYgod/RSSHub) | AGPL-3.0 | Node24/26、pnpm12，路由需逐个审查 | 暂缓部署，无具体路由可用声明 |
| [FreshRSS](https://github.com/FreshRSS/FreshRSS) | 1.30.4 / AGPL-3.0 | PHP>=8.2及扩展；Docker可选 | 暂缓部署，标准feed不依赖它 |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | 0.9.5 / Apache-2.0 | Playwright/patchright等，安装浏览器与服务维护超出最小链路 | 候选；只共用fail-closed远程接口，未采网页 |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | V5 metadata Apache-2.0 | 多provider/API平台 | 技术储备 |
| [Huginn](https://github.com/huginn/huginn) | MIT | Ruby应用服务 | 技术储备 |
| [GDELT](https://www.gdeltproject.org/) | 外部数据服务 | 数据许可与机器抽取可信性须另审 | 技术储备，未下载数据 |

主要源码/元数据依据：各官方仓库 `pyproject.toml`、LICENSE和releases；RSSHub `package.json`、FreshRSS `composer.json`。观察到AKShare release `db52db2`的字段映射维护和feedparser `14425f6`发布。其他项目最后有效提交未完整核实，不填虚构日期。安装版本/实际运行以 `reports/runtime_capabilities.json`、`requirements.lock.txt` 为准。

Docling官方[无模型PDF说明](https://docling-project.github.io/docling/usage/advanced_options/)支持NativePdfPipeline。实际Windows中文环境触发默认docling-parse资源路径错误；本项目明确使用PDFium backend与内存bytes，不修改第三方源代码，不偷偷切换成摘要。此模式保留页码和bbox，不恢复表格/标题/阅读顺序；扫描PDF需另批OCR方案。

这四个库都无需账号/Docker即可处理本地样本；代码许可证不授予第三方金融数据商业或再分发权限。NOTICE保留包内许可证位置和版本清单。
