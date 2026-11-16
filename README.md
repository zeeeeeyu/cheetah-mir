# Cheetah Market Intelligence & Research

Cheetah-MIR v0.1.0 是猎豹的离线市场研究工作台。它为行情、文档、事件、人物观点及其证据记录来源、实际采集时间和修订历史。

## 本地运行

使用 Python 3.12 或以上版本，在仓库目录执行。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m cheetah_mir --help
.\.venv\Scripts\python.exe tools/demo_chain.py reports/mock_workspace
.\.venv\Scripts\python.exe -m cheetah_mir --root reports/mock_workspace provenance verify
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

演示使用合成文章、RSS 和行情，生成物写入本地 `reports/mock_workspace`。PDF 解析可按需安装 `.[documents]`，市场表格接口可按需安装 `.[market]`。存入真实研究材料前先明确数据权利和原始来源。

## 研究记录

[研究状态](research/README.md)列明本仓包含的代码、冻结协议及其验证边界。[数据治理规范](docs/research_protocols/DATA_GOVERNANCE_POLICY.md)说明证据与来源记录方式。原始行情、文档、研究数据库及运行记录保存在授权的本地归档。
