# MIR v0.1架构

独立根目录 `Cheetah_Market_Intelligence_Research`；SQLite与raw文件均在自身data下。没有从quant_v4_6、Cheetah_UI、Legacy导入代码。只读取人工明确提供的文件，不调用生产流程。

四层链路：本地文件/bytes → 内容寻址Raw Archive → 带UTC时间/来源/稳定ID的规范记录 → NetworkX知识网络与确定性研究导出。CLI贯通来源、文件、RSS、市场、观点、事件、图、校验和报告。

为避免多个topic造互不一致表，规范事实存于不可变`records`，SQLite JSON载荷加source/document外键与审计表。行情、人物、企业、事件领域通过有条件的只读VIEW投影；不是多个相互同步的数据库。MarketBar的常用字段由行为API验证；关系通过原始Evidence和实体ID关联。

Raw原件不被解析/摘要替换。解析结果进入normalized，引用原document；失败仍保留原件与错误。修订生成新记录并引用previous_version_id，原记录禁止UPDATE/DELETE。重复执行不改采集时间，不添加时间噪声。

应用路径守卫使用resolve和MIR标记，拒绝进入旧项目目录及路径越界。它不等同OS权限隔离；拥有任意Python执行权的人仍有系统权限。测试证明本项目工作流不会写入受保护工程，而不宣称任意研究代码绝对无法写盘。

网络接口保持DISABLED，AKShare仅归一化人工DataFrame，RSS只接bytes，Docling采用无模型native PDF。未注册服务、RSSHub、FreshRSS、浏览器或模型训练进程。
