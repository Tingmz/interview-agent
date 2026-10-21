# Interview Agent —— 面试备考包自动生成流水线

输入**公司 + 岗位**（可选附加 JD 原文、简历），自动产出一份结构化的**面试备考包**：抓取小红书面经线索 → LLM 结构化分析 → 生成 Markdown 备考包 → 一键写入飞书云文档。

> ⚠️ 本项目仅用于**个人秋招面试准备**。小红书内容与 MediaCrawler 均受版权 / 许可约束，仅供学习研究，请勿商用、勿二次分发。

---

## 一、它能做什么

给到一家目标公司、一个投递岗位，它会自动生成包含以下内容的面经备考包：

- 公司与岗位速览（公开信息 + JD 结构化）
- 社区面经线索（小红书帖子，按互动量排序，含可信度分级）
- 简历 × JD 匹配分析（匹配度、逐条对照、能力缺口与应对）
- STAR 深挖追问脚本（面试官会怎么追着简历问、答不上会暴露什么）
- 分场景高频问题与答题框架（业务一面 / 业务二面 / AI面 / 群面 / HR面）
- 风险与合规声明、待补齐清单、参考来源

关键设计：**每一条结论都带来源标签**，区分「网络搜集信息 / 社区帖子线索 / AI 推理分析」，并强制标注【信息存疑】，防止把社区猜测当成事实背进面试。

---

## 二、架构与模块

采用「采集」与「加工」解耦的流水线，每一阶段产物都落盘到 `data/runs/{task_id}/`，可离线审计、可复跑、可对账。

| 模块 | 职责 | 文件 |
| --- | --- | --- |
| 模块 0 | 编排基座：统一 LLM 入口（mock/auto/replay 三模式 + 预算护栏 + 磁盘缓存）、State 契约 | `llm_provider.py`、`state.py` |
| 模块 1 | 公开信息检索（冻结 fixture） | `data/fixtures/web_findings.json` |
| 模块 2 | **小红书爬虫**（封装 MediaCrawler）、关键词构造 | `crawler_runner.py`、`keyword_builder.py` |
| 模块 3 | 预处理：去重 / 排序 / 可信度分级 | `pipeline.py` |
| 模块 4 | LLM 分析（JD 结构化、简历挖掘、缺口分析、STAR、题库） | `prompts.py` |
| 模块 5 | 飞书云文档写入 | `feishu_client.py` |
| 模块 6 | 面试备考包 Markdown 生成 | `pipeline.py` |

端到端入口只有一个：`pipeline.py`。

---

## 三、前置依赖

| 依赖 | 说明 | 是否必装 |
| --- | --- | --- |
| **Python 3.11+** | MediaCrawler 要求 ≥3.11；本机 3.8 会崩 | 必须 |
| **MediaCrawler** | 开源项目 [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)，提供小红书爬取能力 | 实时抓取时必装；仅跑冻结 fixture 可跳过 |
| **DeepSeek API Key**（或任意 OpenAI 兼容接口） | 默认 `https://api.deepseek.com/v1`，换豆包只需改 `LLM_BASE_URL` | 完整跑时必装；`--dry-run` / `--mock` 可跳过 |
| **飞书企业自建应用** | `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，用于写飞书云文档 | 可选；不写飞书可跳过 |
| `pypdf` | 读取 PDF 格式简历时用到 | 可选 |
| `tiktoken` | token 估算（用于预算护栏） | 可选，缺失时用经验值兜底 |

核心链路只依赖标准库（`urllib` / `zipfile`），不引入 `python-dotenv`、`openai` SDK 等重依赖，减少环境坑。

---

## 四、安装

### 1. 克隆本项目，并准备好同级 MediaCrawler

`crawler_runner.py` 默认按「与本项目同级的 `MediaCrawler` 目录」推断爬虫路径：

```
D:\Github code\
├── interview_agent\     ← 本项目
└── MediaCrawler\        ← NanmiCoder/MediaCrawler
```

如果你把 MediaCrawler 放在别处，用环境变量 `MC_DIR` 指定路径（见下文配置）。

### 2. 创建虚拟环境

```powershell
cd D:\Github code\interview_agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. 安装依赖（按需）

```powershell
pip install pypdf      # 读 PDF 简历
pip install tiktoken   # 更准的 token 估算（可选）
```

### 4. 配置 `.env`

复制 `.env`（已含全部配置项与注释），填入你的密钥：

```powershell
Copy-Item .env .env.bak   # 也可以先备份
# 然后编辑 .env，填入 DEEPSEEK_API_KEY 等
```

---

## 五、环境变量（`.env`）

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（**必填**，完整跑时） | 无 |
| `LLM_BASE_URL` | OpenAI 兼容接口地址，换豆包填火山方舟兼容地址 | `https://api.deepseek.com/v1` |
| `LLM_MODEL` | 模型名（批量任务别用 reasoner，贵且慢） | `deepseek-chat` |
| `LLM_MODE` | `auto`=联网+磁盘缓存(默认) / `mock`=不联网不花钱 / `replay`=只回放缓存绝不联网 | `auto` |
| `LLM_TOKEN_BUDGET` | 单任务累计 token 估算上限，超出直接报错停 | `200000` |
| `LLM_PRICE_IN` / `LLM_PRICE_OUT` | 单价（元/百万 token），**保守上限，以 DeepSeek 官网为准** | `2.0` / `8.0` |
| `FEISHU_APP_ID` | 飞书自建应用 App ID（写飞书时必填） | 空 |
| `FEISHU_APP_SECRET` | 飞书自建应用 App Secret（写飞书时必填） | 空 |
| `MC_DIR` | MediaCrawler 目录路径（不设则用同级目录推断） | 同级 `MediaCrawler` |
| `MC_PYTHON` | MediaCrawler 的 Python 解释器路径（不设则优先用其自带 `.venv`） | 自动探测 |

> 🔒 **密钥安全**：`.env` 与本地备份副本均已加入 `.gitignore`，**不会被提交到 GitHub**。请勿把密钥写进代码或提交任何 `.env` 文件。

---

## 六、小红书爬虫（MediaCrawler）使用要点

`crawler_runner.py` 用**子进程隔离**方式调用 MediaCrawler，源码零改动（通过 `python -c` 注入配置），并把「需要人工处理」的情况判定成明确状态：

| 状态 | 含义 | 处理方式 |
| --- | --- | --- |
| `ok` | 已抓到帖子 | 继续下游 |
| `empty` | 关键词过窄或搜索无结果 | 自动回退冻结 fixture |
| `timeout` | 超时被强杀 | 自动回退 / 延迟重试 |
| `login_required` | 需要人工扫码登录 | 首次使用必须人工扫码（小红书首登为人工断点） |
| `blocked` | 疑似触发风控 / 验证码 / 限流 | 停止，延迟重试或切换数据源 |
| `throttled` | 距上次爬取不足最小间隔 | 等待后重试 |

**首次使用请先人工扫码登录小红书**（浏览器会弹出登录二维码），登录态会保存在 MediaCrawler 的浏览器数据中，之后可自动抓取。

**合规护栏（硬上限，请勿放宽）**：

- 单次任务最多抓取 **30** 篇帖子（`MAX_NOTES_HARD_LIMIT`）
- 单次任务最多 **5** 个关键词（`MAX_KEYWORDS`）
- 两次爬虫任务最小间隔 **120** 秒（`MIN_INTERVAL_SEC`），强制串行、禁止并发

高频抓取会导致账号 / IP 封禁，请务必克制。

---

## 七、使用

统一入口为 `pipeline.py`，参数分层如下。

### 1. 最省事：验证链路（0 成本）

不调 LLM、不写飞书、不联网，只验证整条流水线串联是否通畅：

```powershell
python pipeline.py --input inputs/task_01.json --dry-run
```

### 2. 完整跑：读本地冻结数据 → LLM 分析 → 写飞书

```powershell
python pipeline.py --input inputs/task_01.json
```

- 需先在 `.env` 配置 `DEEPSEEK_API_KEY`（及飞书 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`）
- 会调用 LLM 并写入飞书云文档（末尾打印文档链接）

### 3. 实时抓取小红书

默认只发搜索请求（最省请求、最不易触发风控），失败 / 空结果会自动回退到冻结 fixture：

```powershell
python pipeline.py --input inputs/task_01.json --crawl
```

抓取正文（每个帖子多一次详情请求，风控风险更高），并限制只对前 8 条取正文：

```powershell
python pipeline.py --input inputs/task_01.json --crawl --crawl-detail --crawl-detail-limit 8
```

指定关键词（多个用 `|` 分隔；不传则由公司/岗位/别名自动生成）：

```powershell
python pipeline.py --input inputs/task_01.json --crawl --crawl-keywords "示例公司 面经|示例品牌 避雷"
```

### 4. 复用已抓取的数据（0 次新请求）

小红书有风控，能复用就不重发。传之前某次任务的 task-id：

```powershell
python pipeline.py --input inputs/task_01.json --crawl-reuse live_01
```

### 5. 只生成本地 Markdown，不写飞书

```powershell
python pipeline.py --input inputs/task_01.json --no-feishu
```

产物在 `data/runs/{task_id}/06_package.md`。

### 6. 直接传 JD / 简历（不经输入 JSON）

```powershell
python pipeline.py --company "示例公司" --position "示例岗位" `
  --jd-file inputs/jd.txt --resume-file "inputs/resume.docx" --no-feishu
```

### 常用参数速查

| 参数 | 作用 |
| --- | --- |
| `--input <json>` | 任务输入 JSON（含 `company/position/aliases/jd_text/resume_file`） |
| `--company / --position` | 直接指定公司 / 岗位 |
| `--jd-file / --jd-text` | JD 原文来源（文件优先于 input 里的 `jd_text`） |
| `--resume-file` | 简历路径（`.docx/.txt/.md/.pdf`） |
| `--dry-run` | 不调 LLM、不写飞书，只验证串联（0 成本） |
| `--no-feishu` | 调 LLM 但写本地，不写飞书 |
| `--mock` | LLM 用 mock（不联网、不花钱） |
| `--crawl` | 模块 2 走实时抓取（默认只搜索） |
| `--crawl-detail` | 实时抓取时抓正文（详情请求更多、风控更高） |
| `--crawl-detail-limit N` | 最多对前 N 条取正文（默认 8，0=全部） |
| `--crawl-keywords` | 覆盖抓取关键词（`|` 分隔） |
| `--crawl-max-notes N` | 单次抓取帖子数上限（默认 20，硬上限 30） |
| `--crawl-timeout N` | 抓取超时秒数（默认 300） |
| `--crawl-reuse <task-id>` | 复用某次已抓取任务的产物（0 次新请求） |

### 输入 JSON 格式（`inputs/task_01.json`）

```json
{
  "company": "示例公司",
  "aliases": ["示例品牌"],
  "position": "示例岗位",
  "jd_text": "",
  "resume_file": ""
}
```

> 示例值（公司 / 岗位 / 品牌）请替换为你实际的目标公司。`jd_text` 与 `resume_file` 留空时可用命令行参数补充。

---

## 八、离线 fixture（模块 1 / 模块 2 的冻结数据）

`data/` 已被 `.gitignore` 排除，**不会随仓库提交**。克隆到新机器后，需要自行准备（或用 `--crawl` 实时抓取）：

- `data/fixtures/web_findings.json` —— 公开信息（每条必须带 `url` 以便复核）
- `data/fixtures/xhs_notes.json` —— 小红书帖子（冻结态，仅标题 + 元数据）

`data/runs/{task_id}/` 下保存每次任务的中间产物（爬虫原始数据、脱敏简历、LLM 各阶段结果、最终 Markdown、飞书链接），可用于审计与复现。

---

## 九、目录结构

```
interview_agent/
├── pipeline.py            # 端到端入口（模块1→2→3→4→6→5）
├── crawler_runner.py      # 模块2：MediaCrawler 调用封装
├── keyword_builder.py     # 模块2：搜索关键词构造
├── prompts.py             # 模块4：LLM 提示词资产
├── llm_provider.py        # 模块0：统一 LLM 入口 + 预算护栏 + 磁盘缓存
├── feishu_client.py       # 模块5：飞书云文档写入
├── state.py               # 模块0：LangGraph State 契约
├── .env                   # 密钥配置（已被 .gitignore 排除）
├── .gitignore
├── inputs/                # 任务输入（company/position/JD/简历）
│   ├── task_01.json
│   ├── jd.txt
│   └── resume.docx        # 你的简历（本地文件，已被 .gitignore 排除）
└── data/                  # 运行产物（已被 .gitignore 排除）
    ├── fixtures/          #   冻结数据源
    ├── llm_cache/         #   LLM 磁盘缓存
    ├── llm_usage.jsonl    #   LLM 调用成本记录
    └── runs/              #   各任务产物
```

---

## 十、合规与免责声明

- **MediaCrawler** 为「非商用学习研究」许可，本项目封装仅用于个人学习研究，**严禁商用**。
- 内置的抓取数量上限与限频护栏是**硬约束，请勿放宽**——高频抓取会导致账号 / IP 封禁。
- 小红书帖子内容仅用于**个人面试准备**，未经作者授权不得转载、不得商用。
- 简历相关信息已在本地脱敏后再送入模型；原始简历文件不会上传。
- AI 生成内容可能存在偏差，关键判断请以公司官方口径与你的真实经历为准。
