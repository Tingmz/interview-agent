# -*- coding: utf-8 -*-
"""
模块 4 提示词（子 Agent 提示词资产）

统一约束（每条提示词都继承 SYSTEM_BASE）：
  - **只依据给定材料**做判断，材料里没有的不要补；
  - **不编造**：任何"候选人做过 X"的结论，必须能在简历原文里找到出处；
  - **不确定就标【信息存疑】**，绝不靠语气强硬来伪装确定性；
  - 输出必须是**合法 JSON**（不要 Markdown 代码围栏），键名严格按提示词给的来。

为什么把"禁止编造"写进系统提示词而不是靠事后检查：
  这是整个项目里**唯一可能直接害到用户**的地方 —— 面试时说出一段自己没做过
  的经历，是最致命的事故。必须在生成端就掐掉，而不是生成后再筛。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

SYSTEM_BASE = (
    "你是一名严谨的校招面试辅导专家，服务对象是正在准备秋招的应届生。\n"
    "硬性规则（违反即视为任务失败）：\n"
    "1) 只依据我给你的材料作答；材料里没有的信息，写\"材料未提供\"或标【信息存疑】，"
    "绝对不要用常识猜测来填空。\n"
    "2) 严禁编造候选人的经历、数据、成果。凡是说\"候选人具备/做过\"的，"
    "必须能在简历原文里找到对应表述，否则一律标【信息存疑】。\n"
    "3) 所有输出必须是**合法 JSON**，不要加 ```json 围栏，不要加解释性前后缀。\n"
    "4) 用中文输出。语言要具体、可执行，不要写\"建议多准备\"这类空话。\n"
)


def _msgs(user: str) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_BASE},
        {"role": "user", "content": user},
    ]


def jd_profile(jd_text: str, company: str, position: str) -> List[Dict[str, str]]:
    return _msgs(
        f"下面是【{company} · {position}】的岗位 JD 原文。请把它结构化。\n\n"
        "输出 JSON，键名固定如下：\n"
        "{\n"
        '  "job_summary": "一句话概括这个岗位到底在做什么（不超过60字）",\n'
        '  "responsibilities": ["职责，逐条拆开，原文用词尽量保留"],\n'
        '  "hard_requirements": ["硬性要求（学历/专业/毕业时间/必备技能等，不满足基本会被筛）"],\n'
        '  "nice_to_have": ["加分项"],\n'
        '  "keywords": ["HR/面试官高频关键词，用于简历对齐"],\n'
        '  "core_competencies": [\n'
        '    {"name":"能力名","why":"JD里哪句话体现了它"}\n'
        "  ]\n"
        "}\n\n"
        f"=== JD 原文 ===\n{jd_text}"
    )


def resume_profile(resume_text: str, position: str) -> List[Dict[str, str]]:
    return _msgs(
        f"下面是候选人简历（已做隐私脱敏，占位符如【手机号】无需处理）。"
        f"请针对【{position}】这个方向，把它结构化。\n\n"
        "输出 JSON，键名固定如下：\n"
        "{\n"
        '  "education": "学历信息概述",\n'
        '  "experiences": [\n'
        '    {"title":"项目/实习名称","org":"公司或组织","time":"时间段",'
        '"what":"做了什么（只复述简历原文有的内容）",'
        '"quantified":["简历里出现的可量化结果，没有就给空数组"],'
        '"skills_demonstrated":["体现的能力"]}\n'
        "  ],\n"
        '  "skills": ["技能标签"],\n'
        '  "highlights": ["简历里最亮眼的 3 条，必须能在原文找到出处"],\n'
        '  "weaknesses": ["简历上的明显短板（如无量化、无相关实习、经历零散等）"],\n'
        '  "pii_left": ["如果还残留姓名/学校/电话等可识别信息，列出来；没有就空数组"]\n'
        "}\n\n"
        f"=== 简历原文 ===\n{resume_text}"
    )


def notes_digest(notes_brief: List[Dict[str, Any]], company: str, position: str) -> List[Dict[str, str]]:
    lines = []
    with_body = 0
    for n in notes_brief:
        e = n.get("engagement") or {}
        # 统一 note 结构里互动数是嵌套在 engagement 下的（early 版本直接读 n['liked'] 会永远为空）
        head = (f"- 标题：{n.get('title','')} | 作者：{n.get('author','')} | "
                f"发布时间：{n.get('publish_time','')} | "
                f"互动：赞{e.get('liked','')}/藏{e.get('collected','')}/评{e.get('comment','')}")
        body = (n.get("text") or "").strip()
        if body:
            with_body += 1
            lines.append(head + " | 正文：" + body[:800].replace("\n", " "))
        else:
            lines.append(head + " | 正文：（未采集，只有标题）")
    joined = "\n".join(lines)
    only_head = len(notes_brief) - with_body
    return _msgs(
        f"下面是从小红书抓到的【{company} · {position}】相关帖子。"
        f"共 {len(notes_brief)} 条，其中 {with_body} 条**带正文**、{only_head} 条**只有标题**"
        "（正文是否采到，每条后面都标了）。\n\n"
        "请做线索聚类，规则分两种情况：\n"
        f"- 带正文的 {with_body} 条：可以依据正文内容下结论，并明确指出「依据哪条帖子的正文」；\n"
        f"- 只有标题的 {only_head} 条：**严禁根据标题脑补正文里才有的细节**"
        "（标题写\"AI面\"，只能说\"有帖子提到 AI 面环节\"，不能说\"AI 面考了什么问题\"）。\n\n"
        "输出 JSON，键名固定如下：\n"
        "{\n"
        '  "stage_hints": [{"stage":"面试环节名（如 AI面/群面/一面/二面/HR面）",'
        '"evidence_titles":["支撑该结论的帖子标题"],"confidence":"高|中|低",'
        '"basis":"依据来自正文还是标题"}],\n'
        '  "topic_clusters": [{"topic":"主题（如 待遇/避雷/流程/岗位方向）",'
        '"evidence_titles":["..."],"note":"能确定什么、不能确定什么"}],\n'
        '  "interview_flow": ["从正文里能确认的面试流程/环节顺序；没有正文支撑就写：无正文支撑"],\n'
        '  "uncertain_items": ["只能靠猜、必须标【信息存疑】的点"],\n'
        '  "followup_suggestions": ["建议用户去补哪些信息（如某帖正文未采到，需要人工打开看）"]\n'
        "}\n\n"
        "篇幅纪律（必须遵守，否则输出会被硬性截断、整节作废）：\n"
        f"- stage_hints 最多 6 条，topic_clusters 最多 6 条；\n"
        f"- 每条的 evidence_titles **最多列 5 个标题**，不要把所有相关帖子都罗列进来；\n"
        "- note / basis / uncertain_items / followup_suggestions 每条**一句话**，不要展开成段落；\n"
        "- 不要复述帖子标题的原文全文，标题超过 25 字就截断。\n\n"
        f"=== 帖子清单（共 {len(notes_brief)} 条）===\n{joined}"
    )


def gap_analysis(jd: Dict[str, Any], resume: Dict[str, Any], company: str,
                 position: str) -> List[Dict[str, str]]:
    import json as _json

    return _msgs(
        f"下面是【{company} · {position}】的结构化 JD 与结构化简历。请做匹配分析。\n\n"
        "篇幅纪律（必须遵守）：简历拆解**只占整份备考材料的一小块**，是配菜不是主菜，"
        "**不要面面俱到、不要逐句点评**，只挑真正影响面试结果的关键点：\n"
        "- line_by_line：只保留最关键、面试官最可能追问的条目，**不超过 6 条**；\n"
        "- gaps：按危险程度排序，**不超过 4 条**；\n"
        "- resume_rewrite_tips：**不超过 4 条**。\n\n"
        "输出 JSON，键名固定如下：\n"
        "{\n"
        '  "match_score": 0到100的整数（保守估计，宁低勿高）,\n'
        '  "score_reason": "打这个分的一句话理由",\n'
        '  "line_by_line": [{"requirement":"JD 的某条要求",'
        '"resume_evidence":"简历里的对应证据（找不到就写\"简历未体现\"）",'
        '"verdict":"匹配|部分匹配|不匹配|存疑","advice":"面试里怎么应对这一条"}],\n'
        '  "gaps": [{"gap":"能力/经历缺口","severity":"高|中|低",'
        '"cover_strategy":"面试中如何诚实且有利地回应"}],\n'
        '  "resume_rewrite_tips": [{"original":"简历原句","suggest":"改写建议（只能说清已有事实，'
        '禁止添加未发生的成果）"}],\n'
        '  "red_lines": ["必须提醒候选人的红线（如不得虚构经历、不得夸大职级）"]\n'
        "}\n\n"
        f"=== 结构化 JD ===\n{_json.dumps(jd, ensure_ascii=False, indent=2)}\n\n"
        f"=== 结构化简历 ===\n{_json.dumps(resume, ensure_ascii=False, indent=2)}"
    )


def star_questions(resume: Dict[str, Any], jd: Dict[str, Any],
                   digest: Dict[str, Any]) -> List[Dict[str, str]]:
    import json as _json

    return _msgs(
        "请为这位候选人设计一套 STAR 深挖追问脚本。目的：帮他把简历上"
        "写得含糊的经历，提前想清楚细节，避免面试被追问时卡壳。\n\n"
        "要求：\n"
        "- **只覆盖最多 3 段最关键的简历经历**（挑对目标岗位最有说服力、也最可能被深挖的），"
        "不要为简历上每段经历都写一套；\n"
        "- 每条追问必须**挂靠在简历里真实存在的经历上**，不要凭空造经历；\n"
        "- 追问要像真实面试官：先问背景，再逼数字，再问个人贡献，最后问反思；"
        "每段经历 **2-3 问**即可，不要堆砌；\n"
        "- 明确标出\"如果答不上来会暴露什么风险\"。\n\n"
        "输出 JSON，键名固定如下：\n"
        "{\n"
        '  "scripts": [\n'
        '    {"anchor":"挂在简历的哪段经历上","target_competency":"在验证哪项 JD 能力",\n'
        '     "questions":[{"q":"追问原话","intent":"这题在验什么","risk_if_blank":"答不上会暴露什么"}],\n'
        '     "self_prep":"候选人需要提前准备/回忆的具体信息（数字、细节、决策理由）"}\n'
        "  ],\n"
        '  "weak_spots": ["最可能被问穿的点，按危险程度排序"]\n'
        "}\n\n"
        f"=== 结构化简历 ===\n{_json.dumps(resume, ensure_ascii=False, indent=2)}\n\n"
        f"=== 结构化 JD ===\n{_json.dumps(jd, ensure_ascii=False, indent=2)}\n\n"
        f"=== 社区线索 ===\n{_json.dumps(digest, ensure_ascii=False, indent=2)}"
    )


# 题库为什么按场景拆成多次调用：
#   加厚业务场景题之后，一次生成的总输出会超过模型 8192 的输出上限，
#   被截断成非法 JSON，整节退化成"原始输出"。按场景分组、每次只覆盖 2-3 个场景，
#   单次长度就回到安全区，最后由编排层合并。
QB_HEAVY_STAGES = ("业务一面", "业务二面")

_QB_SCHEMA = (
    "输出 JSON，键名固定如下：\n"
    "{\n"
    '  "sections": [\n'
    '    {"stage":"场景名",\n'
    '     "questions":[{\n'
    '        "q":"问题（业务场景题要写成一个具体场景，不要只写概念）",\n'
    '        "intent":"这题在考察什么能力、出题动机是什么",\n'
    '        "frame":["答题步骤1（做什么）","答题步骤2","答题步骤3"],\n'
    '        "key_concepts":["涉及的业务概念/指标 + 一句话解释"],\n'
    '        "anchor":"能落到候选人简历里的哪段经历；没有就写：无对应经历，需诚实说明",\n'
    '        "basis":"依据来源（JD第几条 / 公司公开信息 / 社区线索 / 无依据）",\n'
    '        "pitfall":"这题最容易踩的坑"}]\n'
    "  }\n"
    "  ],\n"
    '  "ask_back": ["反问环节可以问的问题（显得专业，且能套到真实信息）"]\n'
    "}\n\n"
)


def question_bank(jd: Dict[str, Any], digest: Dict[str, Any],
                  web_findings: List[Dict[str, Any]],
                  stages: Sequence[str] = ("AI面", "群面", "业务一面", "业务二面", "HR面"),
                  with_ask_back: bool = False,
                  directions: Sequence[str] = (),
                  count: int = 0) -> List[Dict[str, str]]:
    """
    生成指定场景的面试问题清单。

    一次只覆盖少量场景 + 少量取材方向：业务面题量大、每题又要求分步框架，一次生成
    的总输出会超过模型 max_tokens 上限被截断成非法 JSON，所以编排层会把同一个场景
    按**取材方向**拆成多次调用（directions 指定本次负责的方向，count 指定题量），
    合并时再按场景名归并。
    """
    import json as _json

    stages = [s for s in stages if s]
    heavy = [s for s in stages if s in QB_HEAVY_STAGES]
    light = [s for s in stages if s not in QB_HEAVY_STAGES]

    rules = [f"- 本次**只输出这些场景**：{'、'.join(stages)}，不要输出其它场景；"]
    if heavy:
        pool = list(directions) or [
            "推广费花出去但没转化", "大促目标如何拆解", "库存压货与补货",
            "单品起量", "价格体系与促销机制", "竞品对冲", "直播间流量承接",
            "售后与评价", "老客复购与用户分层", "经营报表与数据复盘",
            "跨部门协同排期", "给自己定一个新品的月度销量目标并拆解打法",
        ]
        n = count or 8
        rules.append(
            f"- {'、'.join(heavy)}是**整份材料的重点**（业务面才是真正决定录不录的地方），"
            f"本次出 {n} 道题，且**至少三分之二必须是业务场景题**"
            "（真实工作里会遇到的具体问题，而不是概念题）；"
            "取材方向**只限这几个**（不要跑到别的方向去）："
            + "、".join(pool) + "；"
        )
    if light:
        rules.append(
            f"- {'、'.join(light)}是配角，每个场景 2-3 道题即可，不要展开太长；"
        )
    rules += [
        "- 每题必须给出**分步答题框架**（step by step，能直接照着说），不要只给一句结论；",
        "- **篇幅纪律（很重要）**：每题控制在 6 行以内 —— frame 3-5 步、每步一句话"
        "（不超过 30 字）；key_concepts 最多 3 条；intent / anchor / pitfall 各一句话。"
        "**不要把每题写成小作文**，输出超长会被硬性截断，整节内容就全废了；",
        "- 业务场景题要写成一个**具体场景**（带数字/约束/冲突），并明确点出用到的"
        "**电商业务概念/指标**且附一句话解释"
        "（如 GMV、ROI/ROAS、转化漏斗、费率/费比、客单价、UV价值、售罄率、"
        "库存周转天数、DSR、复购率），让不熟悉的人也能看懂；",
        "- 每题都要说明**在考察什么能力**，以及**能落到候选人哪段真实经历**；"
        "简历里找不到对应经历的，明确写：无对应经历，需诚实说明；",
        "- 答题框架必须基于**岗位 JD 与公司公开信息**定制，不要给放之四海皆准的通用模板；",
        "- 涉及公司具体信息的题，要指明依据来源（网络公开信息 / 社区线索 / 无依据需谨慎）。",
    ]

    schema = _QB_SCHEMA if with_ask_back else _QB_SCHEMA.split('  "ask_back"')[0] + "}\n\n"

    return _msgs(
        "请生成一份面向**电商运营岗校招**的分场景面试问题清单，"
        "并给出可以直接照着讲的答题框架。\n\n"
        "重点要求：\n" + "\n".join(rules) + "\n\n" + schema +
        f"=== 结构化 JD ===\n{_json.dumps(jd, ensure_ascii=False, indent=2)}\n\n"
        f"=== 社区线索 ===\n{_json.dumps(digest, ensure_ascii=False, indent=2)}\n\n"
        f"=== 公司公开信息 ===\n{_json.dumps(web_findings, ensure_ascii=False, indent=2)}"
    )
