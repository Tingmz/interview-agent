# -*- coding: utf-8 -*-
"""
端到端流水线（模块 1 → 2 → 3 → 4 → 6 → 5）

本轮定位：**一次性把链路跑通**。两个外部数据源都以"冻结"的离线 fixture 形式接入：
  - data/fixtures/web_findings.json  ← 模块1：公开信息检索
    （当前没有搜索 API key，先固化人工检索结果；再接上 Serper/Bing 时只换数据源，字段结构不变）
  - data/fixtures/xhs_notes.json     ← 模块2：小红书帖子
    （已冻结：只有标题+元数据，正文未采集，且不再访问小红书）

为什么这么切：外部源天然不稳定（搜索要 key、小红书有风控），而"加工链路"必须能反复验证。
把「采集」和「加工」解耦，换真实数据源时不用改加工逻辑。

数据总线：每一阶段的产物都落盘到 data/runs/{task_id}/，可离线审计、可复跑、可对账。

用法：
  python pipeline.py --input inputs/task_01.json          # 完整跑（会调 LLM、写飞书）
  python pipeline.py --input inputs/task_01.json --dry-run # 0 成本：不调 LLM、不写飞书，只验串联
  python pipeline.py --input inputs/task_01.json --no-feishu
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import prompts
from llm_provider import BudgetGuard, estimate_tokens, get_provider, load_env

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
FIXTURE_DIR = DATA_DIR / "fixtures"
RUNS_DIR = DATA_DIR / "runs"
XHS_FIXTURE = FIXTURE_DIR / "xhs_notes.json"
WEB_FIXTURE = FIXTURE_DIR / "web_findings.json"

CRED_WEB = "中（公开网页，非官方招聘口径，需交叉验证）"
CRED_XHS = "低（仅标题与互动数据，正文未采集）"


# ============================================================
# 控制台安全输出：Windows 控制台是 GBK，帖子里有 emoji 会直接抛 UnicodeEncodeError
# ============================================================
def p(*args: Any) -> None:
    text = " ".join(str(a) for a in args)
    enc = sys.stdout.encoding or "gbk"
    sys.stdout.write(text.encode(enc, errors="replace").decode(enc, errors="replace") + "\n")
    sys.stdout.flush()


# ============================================================
# 简历读取（只用标准库，避免为读一个文件装一堆依赖）
# ============================================================
def read_resume(path: str) -> str:
    f = Path(path)
    if not f.exists():
        raise FileNotFoundError(f"简历文件不存在：{f}")
    suf = f.suffix.lower()

    if suf in (".txt", ".md", ".markdown"):
        return f.read_text(encoding="utf-8", errors="ignore")

    if suf in (".docx", ".docm"):
        # .docx 本质是 zip，正文在 word/document.xml。用标准库解，不装 python-docx。
        with zipfile.ZipFile(f) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
        # ⚠️ 只能取 <w:t> 里的内容。早先"删掉所有标签"的写法会把
        # 域代码(w:instrText)、批注、mc:Fallback 兼容层里的字节也带进来，
        # 结果是简历里凭空多出一串数字，直接污染后面的 LLM 分析。
        lines = []
        for para in xml.split("</w:p>"):
            runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, flags=re.S)
            line = html.unescape("".join(runs)).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)

    if suf == ".pdf":
        try:
            from pypdf import PdfReader          # 装了就用
        except ImportError:
            raise RuntimeError(
                f"读 PDF 需要 pypdf（当前未安装）。两个选择：\n"
                f"  1) 装：.venv\\Scripts\\pip.exe install pypdf\n"
                f"  2) 更省事：把简历另存为 .docx 或 .txt 再跑\n"
                f"文件：{f}"
            )
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(f)).pages)

    if suf == ".doc":
        raise RuntimeError("旧版 .doc 无法解析，请用 Word 另存为 .docx 后再跑")

    raise RuntimeError(f"不支持的简历格式：{suf}（支持 .docx / .txt / .md / .pdf）")


_PII_RULES = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "【邮箱】"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "【手机号】"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "【身份证】"),
    (re.compile(r"\b\d{16,19}\b"), "【银行卡号】"),
]


def desensitize(text: str) -> str:
    """
    上大模型前必须做的脱敏。真实简历不上传到在线模型，是项目的硬约束。
    只做机器能可靠识别的部分（邮箱/手机/身份证/卡号）；
    姓名、学校这类靠上下文判断的，交给模块4 的 resume_profile 提示词回报 pii_left 再人工确认。
    """
    for pat, rep in _PII_RULES:
        text = pat.sub(rep, text)
    return text


# ============================================================
# 数据源（模块1 / 模块2）
# ============================================================
def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"缺少数据文件：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def stage_web() -> List[Dict[str, Any]]:
    """模块1 输出：公开信息。每条必须带 url，否则无法复核。"""
    data = load_json(WEB_FIXTURE)
    out = []
    for f in data.get("findings", []):
        out.append({
            "id": f.get("id"),
            "source_type": "web",
            "site": f.get("site", ""),
            "title": f.get("title", ""),
            "url": f.get("url", ""),
            "fetched_at": f.get("fetched_at", ""),
            "points": f.get("points", []),
            "credibility": CRED_WEB,
        })
    return out


def stage_notes() -> List[Dict[str, Any]]:
    """模块2 输出（冻结态）：小红书帖子。"""
    data = load_json(XHS_FIXTURE)
    out = []
    for n in data.get("notes", []):
        out.append({
            "id": n.get("note_id"),
            "source_type": "community",
            "platform": "xiaohongshu",
            "title": (n.get("title") or "").replace("\n", " ").strip(),
            "author": n.get("author", ""),
            "publish_time": n.get("publish_time", ""),
            "engagement": {
                "liked": n.get("liked"), "collected": n.get("collected"),
                "comment": n.get("comment"), "shared": n.get("shared"),
            },
            "url": n.get("url", ""),
            "text": n.get("body", ""),
            "credibility": CRED_XHS,
        })
    return out


def stage_notes_from_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """模块2 输出（实时抓取态）：把爬虫记录转成统一 note 结构（与冻结 fixture 同构）。"""
    out = []
    for n in items:
        out.append({
            "id": n.get("note_id"),
            "source_type": "community",
            "platform": "xiaohongshu",
            "title": (n.get("title") or "").replace("\n", " ").strip(),
            "author": n.get("nickname", ""),
            "publish_time": n.get("_publish_dt", ""),
            "engagement": {
                "liked": n.get("liked_count"), "collected": n.get("collected_count"),
                "comment": n.get("comment_count"), "shared": n.get("share_count"),
            },
            "url": n.get("note_url", ""),
            "text": n.get("desc", "") or "",   # 只有抓了详情的帖子才有正文；只搜标题时为空
            "credibility": CRED_XHS,
        })
    return out


# ============================================================
# 模块3：预处理 —— 去重 / 排序 / 打可信度标签
# ============================================================
def _is_int(v: Any) -> bool:
    s = str(v or "").strip()
    return s.isdigit()


def preprocess(web: List[Dict[str, Any]], notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    证据统一化。这里刻意**不做内容摘要**：没采到正文的帖子只有标题，
    对标题做摘要只会把"猜测"包装成"事实"，违背可信度分级的意义；
    采到正文的帖子则把正文原样带下去，交给模型依据正文下结论。
    """
    seen, evidence = set(), []

    for f in web:
        evidence.append(f)

    for n in notes:
        if n["id"] in seen:
            continue
        seen.add(n["id"])
        evidence.append(n)

    # 帖子按互动量（赞）降序，让高关注度的线索排前面；非数字的排最后
    def sort_key(e: Dict[str, Any]):
        if e.get("source_type") != "community":
            return (-1, 0)
        liked = e["engagement"].get("liked")
        return (0, -int(liked)) if _is_int(liked) else (1, 0)

    web_part = [e for e in evidence if e.get("source_type") == "web"]
    note_part = sorted([e for e in evidence if e.get("source_type") == "community"], key=sort_key)
    return web_part + note_part


# ============================================================
# 模块4：LLM 分析
# ============================================================
def _parse_json(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    if t.startswith("```"):
        parts = t.split("```")
        t = parts[1] if len(parts) > 1 else t
        if t.lower().startswith("json"):
            t = t[4:]
    try:
        obj = json.loads(t.strip())
        return obj if isinstance(obj, dict) else {"_raw": obj}
    except json.JSONDecodeError:
        return {"_raw": t}


def _merge_qb(*parts: Dict[str, Any]) -> Dict[str, Any]:
    """
    把多次 question_bank 调用的结果拼成一个题库。

    业务面是按"取材方向"拆成多次调用的，所以**同名场景必须合并成一个 section**，
    否则文档里会出现两个"业务一面"标题。顺序按场景首次出现的先后保持。
    """
    order: List[str] = []
    by_stage: Dict[str, Dict[str, Any]] = {}
    raws: List[str] = []
    ask_back: Any = None
    for part in parts:
        if part.get("_raw"):
            raws.append(str(part["_raw"]))
            continue
        for s in (part.get("sections") or []):
            if not isinstance(s, dict):
                continue
            stage = str(s.get("stage") or "未命名场景")
            if stage not in by_stage:
                by_stage[stage] = {"stage": stage, "questions": []}
                order.append(stage)
            by_stage[stage]["questions"].extend(
                q for q in (s.get("questions") or []) if isinstance(q, dict))
        ask_back = part.get("ask_back") or ask_back
    out: Dict[str, Any] = {"sections": [by_stage[s] for s in order]}
    if ask_back:
        out["ask_back"] = ask_back
    if raws:
        # 有场景没解析出来：原始文本挂在最后，至少不丢内容
        out["_raw"] = "\n\n".join(raws)
    return out


def ask_json(provider, msgs: List[Dict[str, str]], tag: str,
             budget: BudgetGuard, max_tokens: int = 2048) -> Dict[str, Any]:
    _ = msgs
    p(f"  [LLM] {tag} ...")
    t0 = time.time()
    out = provider.chat(msgs, temperature=0.2, max_tokens=max_tokens)
    if not getattr(provider, "last_cache_hit", False):
        budget.add(
            estimate_tokens(json.dumps(msgs, ensure_ascii=False)),
            estimate_tokens(out),
            meta={"tag": tag},
        )
    else:
        p(f"        （命中磁盘缓存，未产生费用）")
    obj = _parse_json(out)
    if "_raw" in obj:
        p(f"        [warn] {tag} 返回的不是合法 JSON，已原样保留到 _raw")
    p(f"        完成，用时 {time.time() - t0:.1f}s")
    return obj


# ============================================================
# 模块6：面试备考包 Markdown
# ============================================================
def _bullets(items: Any, empty: str = "（无）") -> List[str]:
    if not items:
        return [f"- {empty}"]
    lines = []
    for it in items:
        if isinstance(it, dict):
            it = "；".join(f"{k}：{v}" for k, v in it.items())
        lines.append(f"- {it}")
    return lines


def build_markdown(state: Dict[str, Any]) -> str:
    c = state["input"]
    web = state["web_findings"]
    notes = state["raw_notes"]
    notes_with_body = sum(1 for n in notes if (n.get("text") or "").strip())
    jd = state.get("jd_profile") or {}
    res = state.get("resume_profile") or {}
    gap = state.get("gap_analysis") or {}
    star = state.get("star_questions") or {}
    qb = state.get("question_bank") or {}
    dg = state.get("notes_digest") or {}

    # 正文里只留 [来源 N] 编号，真实链接统一收进文末"参考来源"，
    # 这样正文读起来是连续的，不会被一串 URL 打断。
    ref_entries: List[str] = []
    ref_no: Dict[str, int] = {}

    def _ref(key: str, label: str, url: str) -> str:
        if key not in ref_no:
            ref_entries.append(
                f"{len(ref_entries) + 1}. [{label}]({url})" if url
                else f"{len(ref_entries) + 1}. {label}"
            )
            ref_no[key] = len(ref_entries)
        return f"[来源 {ref_no[key]}]"

    L: List[str] = []
    L.append(f"# {c['company']} · {c['position']} · 面试备考包")
    L.append("")
    L.append(f"> 生成时间：{state['generated_at']}　｜　任务 ID：`{state['task_id']}`")
    if notes_with_body:
        body_desc = f"其中 {notes_with_body} 条含正文，其余仅标题与元数据"
    else:
        body_desc = "**仅标题与元数据，正文未采集**"
    L.append(f"> 数据来源：公开网页 {len(web)} 条 ＋ 小红书帖子 {len(notes)} 条（{body_desc}）")
    L.append("")
    L.append("> **本文档重点：业务面（业务一面 / 业务二面）** —— 这部分才是决定录不录的环节，"
             "题量最大、最细；简历拆解只作辅助，不做展开。")
    L.append("")

    # ---- 〇 阅读说明 ----
    L.append("## 〇、怎么读这份文档（信息可信度分级）")
    L.append("")
    L.append("本项目的每一条结论都带来源标签，**请务必区分对待**，不要把它们当成同等可信：")
    L.append("- 【网络搜集信息】：来自公开网页，附原文链接，你可以自己点开复核")
    if notes_with_body:
        L.append(f"- 【社区帖子线索】：来自小红书的**标题、互动数据，以及其中 {notes_with_body} 条已采集的正文**。"
                 "有正文的帖子可以据此下结论；只有标题的帖子，任何\"考了什么题\"的推断都不可靠，按【信息存疑】处理")
    else:
        L.append("- 【社区帖子线索】：来自小红书帖子的**标题与互动数据**。正文没有采集，"
                 "所以任何\"考了什么题\"的推断都不可靠，一律按【信息存疑】处理")
    L.append("- 【AI 推理分析】：模型基于以上材料做的推断，**不是公司官方口径**，仅供参考")
    L.append("")
    L.append("⚠️ **红线提醒**：本材料的用途是帮你把真实经历讲清楚，"
             "**不是**给你一套可以背下来的假经历。面试中说出一段自己没做过的事，"
             "是最容易被追问穿、后果最严重的失误。")
    L.append("")
    L.append("---")
    L.append("")

    # ---- 一 公司岗位 ----
    L.append("## 一、公司与岗位速览")
    L.append("")
    L.append("### 1.1 公司公开信息【网络搜集信息】")
    L.append("")
    for f in web:
        tag = _ref(str(f.get("id")), f"{f['title']}（{f['site']}）", f.get("url", ""))
        L.append(f"- **{f['title']}**（{f['site']}）　{tag}")
        for pt in f.get("points", []):
            L.append(f"  - {pt}")
    L.append("")
    L.append("### 1.2 JD 结构化【AI 推理分析】")
    L.append("")
    if jd.get("_raw"):
        L.append("> 模型未返回结构化结果，以下为原始输出：")
        L.append("")
        L.append(jd["_raw"])
    else:
        L.append(f"- **岗位一句话定位**：{jd.get('job_summary', '（未提取到）')}")
        L.append("")
        L.append("**主要职责**")
        L.extend(_bullets(jd.get("responsibilities")))
        L.append("")
        L.append("**硬性要求**")
        L.extend(_bullets(jd.get("hard_requirements")))
        L.append("")
        L.append("**加分项**")
        L.extend(_bullets(jd.get("nice_to_have")))
        L.append("")
        L.append("**核心能力要求**")
        for it in jd.get("core_competencies") or []:
            if isinstance(it, dict):
                L.append(f"- **{it.get('name', '')}**：{it.get('why', '')}")
            else:
                L.append(f"- {it}")
        L.append("")
        kw = jd.get("keywords") or []
        if kw:
            L.append(f"**高频关键词**：{'、'.join(str(k) for k in kw)}")
            L.append("")
    L.append("---")
    L.append("")

    # ---- 二 社区线索 ----
    L.append("## 二、社区面经线索【社区帖子线索】")
    L.append("")
    if dg.get("_raw"):
        L.append("> 模型未返回结构化结果，以下为原始输出：")
        L.append("")
        L.append(dg["_raw"])
    else:
        L.append("### 2.1 面试环节线索")
        L.append("")
        for s in dg.get("stage_hints") or []:
            if isinstance(s, dict):
                L.append(f"- **{s.get('stage', '')}**（置信度：{s.get('confidence', '')}）")
                for t in s.get("evidence_titles") or []:
                    L.append(f"  - 依据帖：《{t}》")
                if s.get("basis"):
                    L.append(f"  - 依据类型：{s['basis']}")
        L.append("")
        L.append("### 2.2 面试流程（仅正文可确认的部分）")
        L.extend(_bullets(dg.get("interview_flow")))
        L.append("")
        L.append("### 2.3 主题聚类")
        L.append("")
        for t in dg.get("topic_clusters") or []:
            if isinstance(t, dict):
                L.append(f"- **{t.get('topic', '')}**：{t.get('note', '')}")
        L.append("")
        L.append("### 2.4 必须标【信息存疑】的点")
        L.extend(_bullets(dg.get("uncertain_items")))
        L.append("")
        L.append("### 2.5 建议你人工补看的内容")
        L.extend(_bullets(dg.get("followup_suggestions")))
        L.append("")
    body_hint = "标题 + 已采集正文" if notes_with_body else "仅标题"
    L.append(f"### 2.6 帖子清单（按互动量降序，{body_hint}）")
    L.append("")
    for i, n in enumerate(notes, 1):
        e = n["engagement"]
        tag = _ref(str(n.get("id")), f"{n['title']}（{n['author']}）", n.get("url", ""))
        L.append(f"- {i}. **{n['title']}**　｜　作者：{n['author']}　｜　{n['publish_time']}　"
                 f"｜　赞{e.get('liked')}/藏{e.get('collected')}/评{e.get('comment')}　{tag}")
        body = (n.get("text") or "").strip()
        if body:
            snippet = body if len(body) <= 600 else body[:600] + "…"
            for line in snippet.splitlines():
                line = line.strip()
                if line:
                    L.append(f"  > {line}")
    L.append("")
    L.append("---")
    L.append("")

    # ---- 三 匹配分析 ----
    L.append("## 三、简历 × JD 匹配分析【AI 推理分析】")
    L.append("")
    if not res:
        L.append("> 未提供简历，本节跳过。")
    elif gap.get("_raw"):
        L.append("> 模型未返回结构化结果，以下为原始输出：")
        L.append("")
        L.append(gap["_raw"])
    else:
        L.append(f"**匹配度：{gap.get('match_score', '?')} / 100**　—— {gap.get('score_reason', '')}")
        L.append("")
        L.append("### 3.1 逐条对照")
        L.append("")
        for row in gap.get("line_by_line") or []:
            if not isinstance(row, dict):
                continue
            L.append(f"- **要求**：{row.get('requirement', '')}")
            L.append(f"  - 简历证据：{row.get('resume_evidence', '')}")
            L.append(f"  - 结论：**{row.get('verdict', '')}**　｜　应对：{row.get('advice', '')}")
        L.append("")
        L.append("### 3.2 能力缺口与应对策略")
        L.append("")
        for g in gap.get("gaps") or []:
            if isinstance(g, dict):
                L.append(f"- **[{g.get('severity', '')}] {g.get('gap', '')}**")
                L.append(f"  - 面试中怎么回应：{g.get('cover_strategy', '')}")
        L.append("")
        L.append("### 3.3 简历改写建议（只能说清已有事实）")
        L.append("")
        for t in gap.get("resume_rewrite_tips") or []:
            if isinstance(t, dict):
                L.append(f"- 原句：{t.get('original', '')}")
                L.append(f"  - 建议：{t.get('suggest', '')}")
        L.append("")
        L.append("### 3.4 红线")
        L.extend(_bullets(gap.get("red_lines")))
        L.append("")
    L.append("---")
    L.append("")

    # ---- 四 STAR ----
    L.append("## 四、STAR 深挖追问脚本【AI 推理分析】")
    L.append("")
    if star.get("_raw"):
        L.append("> 模型未返回结构化结果，以下为原始输出：")
        L.append("")
        L.append(star["_raw"])
    else:
        for i, s in enumerate(star.get("scripts") or [], 1):
            if not isinstance(s, dict):
                continue
            L.append(f"### 4.{i} 挂在：{s.get('anchor', '')}")
            L.append(f"（验证能力：{s.get('target_competency', '')}）")
            L.append("")
            for q in s.get("questions") or []:
                if isinstance(q, dict):
                    L.append(f"- **Q：{q.get('q', '')}**")
                    L.append(f"  - 在验什么：{q.get('intent', '')}")
                    L.append(f"  - 答不上会暴露：{q.get('risk_if_blank', '')}")
            L.append("")
            if s.get("self_prep"):
                L.append(f"**需要你提前准备/回忆**：{s['self_prep']}")
                L.append("")
        L.append("### 最容易问穿的点（按危险程度排序）")
        L.extend(_bullets(star.get("weak_spots")))
        L.append("")
    L.append("---")
    L.append("")

    # ---- 五 分场景问题 ----
    L.append("## 五、分场景高频问题与答题框架【AI 推理分析】")
    L.append("")
    sections = [s for s in (qb.get("sections") or []) if isinstance(s, dict)]
    if not sections:
        L.append("> 模型未返回结构化结果，以下为原始输出：")
        L.append("")
        L.append(qb.get("_raw", "（无）"))
    else:
        for sec in sections:
            L.append(f"### {sec.get('stage', '')}")
            L.append("")
            for q in sec.get("questions") or []:
                if not isinstance(q, dict):
                    continue
                L.append(f"- **Q：{q.get('q', '')}**")
                if q.get("intent"):
                    L.append(f"  - 考察点：{q['intent']}")
                for pt in q.get("frame") or []:
                    L.append(f"  - {pt}")
                kc = q.get("key_concepts") or []
                if kc:
                    L.append(f"  - 业务概念：{'；'.join(str(x) for x in kc)}")
                if q.get("anchor"):
                    L.append(f"  - 结合你的经历：{q['anchor']}")
                L.append(f"  - 依据：{q.get('basis', '')}　｜　易踩坑：{q.get('pitfall', '')}")
            L.append("")
        # 模型自己已经产出"反问环节"时就不再重复补一节
        if not any("反问" in (s.get("stage") or "") for s in sections):
            L.append("### 反问环节")
            L.extend(_bullets(qb.get("ask_back")))
            L.append("")
        # 拆调用后有场景没解析出来时，原文挂在这里，至少不丢内容
        if qb.get("_raw"):
            L.append("> 另有部分场景未返回结构化结果，原始输出如下：")
            L.append("")
            L.append(qb["_raw"])
            L.append("")
    L.append("---")
    L.append("")

    # ---- 六 风险 ----
    L.append("## 六、风险与合规声明")
    L.append("")
    L.append("- 小红书帖子内容仅用于**个人面试准备**，未经作者授权不得转载、不得商用。")
    if notes_with_body:
        L.append(f"- 文档中【社区帖子线索】有 {notes_with_body} 条带正文、{len(notes) - notes_with_body} 条只有标题。"
                 "带正文的结论仍来自用户自述，**不构成对公司待遇、管理风格的定论**；"
                 "只有标题的更不能当事实。")
    else:
        L.append("- 文档中所有【社区帖子线索】均未核实正文，**不构成对公司待遇、管理风格的结论**。")
    L.append("- 简历相关信息已在本地脱敏后再送入模型；原始简历文件未上传。")
    L.append("- AI 生成内容可能存在偏差，关键判断请以公司官方口径与你的真实经历为准。")
    L.append("")
    L.append("## 七、待补齐清单")
    L.append("")
    L.append("- [ ] 招聘官网/官方公众号的 JD 原文（替换当前网络来源的 JD）")
    L.append("- [ ] 上面标【信息存疑】的帖子，需要人工打开正文核实")
    L.append("- [ ] 简历上可量化的结果（数字、比例、规模）尽量补齐")
    L.append("- [ ] 针对第四节被追问出来的空白点，提前想好诚实且有利的说法")
    L.append("")
    L.append("## 八、参考来源")
    L.append("")
    L.append("> 正文中以 `[来源 N]` 标注的内容，对应下面第 N 条链接，点标题可直达原文。")
    L.append("")
    L.extend(ref_entries or ["（无）"])
    L.append("")
    return "\n".join(L)


# ============================================================
# 主流程
# ============================================================
def save_json(run_dir: Path, name: str, obj: Any) -> None:
    (run_dir / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def run(args) -> int:
    load_env()

    # ---- 输入 ----
    cfg: Dict[str, Any] = {}
    if args.input:
        cfg = load_json(Path(args.input))
    company = args.company or cfg.get("company") or "示例公司"
    position = args.position or cfg.get("position") or "示例岗位"
    aliases = cfg.get("aliases") or []
    jd_text = args.jd_text or cfg.get("jd_text") or ""
    if args.jd_file:
        jd_text = Path(args.jd_file).read_text(encoding="utf-8", errors="ignore")
    resume_file = args.resume_file or cfg.get("resume_file") or ""

    if not jd_text.strip():
        p("[错误] 没有 JD 原文。请用 --jd-file 指定文件，或在 --input 的 JSON 里填 jd_text。")
        return 2
    if not resume_file:
        p("[warn] 没有提供简历（--resume-file），第三、四节会跳过。")

    task_id = args.task_id or f"run_{time.strftime('%Y%m%d_%H%M%S')}"
    run_dir = RUNS_DIR / task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    state: Dict[str, Any] = {
        "task_id": task_id,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "input": {
            "company": company, "aliases": aliases, "position": position,
            "jd_text": jd_text, "resume_file": resume_file,
        },
        "errors": [],
    }

    p(f"===== 任务 {task_id} =====")
    alias_desc = f"（{'、'.join(aliases)}）" if aliases else ""
    p(f"公司：{company}{alias_desc}　岗位：{position}")

    # ---- 模块1 / 模块2（冻结数据源）----
    p("\n[模块1] 载入公开信息")
    state["web_findings"] = stage_web()
    p(f"  {len(state['web_findings'])} 条")

    if args.crawl_reuse:
        # 复用某次已抓取的产物：0 次新请求。小红书侧有风控，能复用就不重发。
        p(f"\n[模块2] 复用已抓取任务的产物：{args.crawl_reuse}（0 次新请求）")
        from crawler_runner import dedupe_notes, load_crawl_items

        crawl_dir = RUNS_DIR / args.crawl_reuse / "crawl"
        if not crawl_dir.exists():
            p(f"  [错误] 找不到产物目录 {crawl_dir}")
            return 2
        log_path = crawl_dir / "crawl_stdout.log"
        stdout = log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else ""
        # 以 JSONL 为主（详情记录里**含正文 desc**），搜索日志只用来补齐没取到详情的帖子。
        # 早先只解析 crawl_stdout.log，会把刚抓到的正文整批丢掉。
        items = dedupe_notes(load_crawl_items(crawl_dir, stdout))
        p(f"  从产物还原出 {len(items)} 条帖子")
        if items:
            save_json(run_dir, "02_crawl_items.json", items)
            state["raw_notes"] = stage_notes_from_items(items)
            p(f"  采用复用数据：{len(state['raw_notes'])} 条")
        else:
            p("  产物中无有效帖子 → 回退冻结 fixture")
            state["raw_notes"] = stage_notes()
    elif args.crawl:
        mode_desc = (f"仅搜索（每关键词 1 次请求）" if not args.crawl_detail
                     else f"搜索 + 对前 {args.crawl_detail_limit or '全部'} 条取正文（含详情请求）")
        p(f"\n[模块2] 实时抓取小红书（{mode_desc}）")
        from crawler_runner import CrawlRequest, run_crawl
        from keyword_builder import build_keywords

        if args.crawl_keywords:
            kws = [k.strip() for k in args.crawl_keywords.split("|") if k.strip()]
        else:
            kws = build_keywords(company, position, aliases)
        for k in kws:
            p(f"  - {k}")
        req = CrawlRequest(task_id=task_id, keywords=kws,
                           max_notes=args.crawl_max_notes, timeout_sec=args.crawl_timeout)
        # skip_login_check 必开：profile 已登录时 selfinfo 仍会返回 406，pong() 误判会导致
        # 转去扫码页、页面无二维码、30s×2 超时崩掉（详见 crawler_runner.py 注释）。
        result = run_crawl(req, dry_run=args.dry_run, skip_login_check=True,
                           skip_detail=not args.crawl_detail,
                           detail_limit=args.crawl_detail_limit)
        p(f"  状态={result.status} 命中={result.count} 条"
          f"（raw={result.raw_count} dup={result.duplicate_count} "
          f"no_url={result.missing_url_count} 用时={result.duration_sec}s）")
        if result.error:
            p(f"  说明：{result.error}")
        save_json(run_dir, "02_crawl_result.json", result.to_dict())
        if result.items:
            save_json(run_dir, "02_crawl_items.json", result.items)
            state["raw_notes"] = stage_notes_from_items(result.items)
            p(f"  采用实时数据：{len(state['raw_notes'])} 条")
        else:
            p("  实时抓取无结果 → 回退冻结 fixture")
            state["raw_notes"] = stage_notes()
    else:
        p("\n[模块2] 载入小红书帖子（冻结 fixture）")
        state["raw_notes"] = stage_notes()
    n_body = sum(1 for n in state["raw_notes"] if (n.get("text") or "").strip())
    p(f"  模块2 产出 {len(state['raw_notes'])} 条（含正文 {n_body} 条）")

    # ---- 模块3 ----
    p("\n[模块3] 预处理：去重 / 排序 / 可信度分级")
    evidence = preprocess(state["web_findings"], state["raw_notes"])
    state["evidence"] = evidence
    p(f"  证据条目 {len(evidence)} 条")
    save_json(run_dir, "01_web_findings.json", state["web_findings"])
    save_json(run_dir, "02_xhs_notes.json", state["raw_notes"])
    save_json(run_dir, "03_evidence.json", evidence)

    # ---- 模块4 ----
    if args.dry_run:
        p("\n[模块4] dry-run：跳过所有 LLM 调用（0 成本）")
        state["jd_profile"] = {"job_summary": "[dry-run] 未调用模型"}
        state["resume_profile"] = {}
        state["gap_analysis"] = {}
        state["star_questions"] = {}
        state["question_bank"] = {}
        state["notes_digest"] = {}
    else:
        mode = "mock" if args.mock else None
        provider = get_provider(mode=mode)
        budget = BudgetGuard.from_env()
        p(f"\n[模块4] LLM 分析（provider={provider.name} model={provider.model}）")

        resume_text = ""
        if resume_file:
            raw = read_resume(resume_file)
            resume_text = desensitize(raw)
            (run_dir / "04_resume_sanitized.txt").write_text(resume_text, encoding="utf-8")
            p(f"  简历已读取并脱敏：原文 {len(raw)} 字 → 脱敏后 {len(resume_text)} 字")

        # 线索摘要要逐条点评 68 条帖子（其中 8 条带 800 字正文）。给 4000 仍然被截断过，
        # 所以提示词里加了篇幅纪律（evidence_titles ≤5、每条一句话），额度也给到 8000
        # —— deepseek-chat 的输出上限就是 8192，再高没意义。
        state["notes_digest"] = ask_json(
            provider, prompts.notes_digest(state["raw_notes"], company, position),
            "notes_digest", budget, max_tokens=8000)
        state["jd_profile"] = ask_json(
            provider, prompts.jd_profile(jd_text, company, position),
            "jd_profile", budget)
        if resume_text:
            state["resume_profile"] = ask_json(
                provider, prompts.resume_profile(resume_text, position),
                "resume_profile", budget)
            state["gap_analysis"] = ask_json(
                provider, prompts.gap_analysis(state["jd_profile"], state["resume_profile"],
                                               company, position),
                "gap_analysis", budget)
            state["star_questions"] = ask_json(
                provider, prompts.star_questions(state["resume_profile"], state["jd_profile"],
                                                 state["notes_digest"]),
                "star_questions", budget)
        else:
            state["resume_profile"] = {}
            state["gap_analysis"] = {}
            state["star_questions"] = {}
        # 题库拆成多次调用：业务面是重点、每题都要展开分步框架，实测"一个业务面一次生成"
        # 输出就到 17000+ 字、直接撞上模型 max_tokens 上限被截断成残缺 JSON（整节全废）。
        # 所以同一个业务面再按**取材方向**拆两半，每次只出 5 题；合并时按场景名归并成
        # 一个"业务一面"标题。业务面排在文档最前。
        def _qb(stage, dirs, count, tag):
            return ask_json(
                provider, prompts.question_bank(state["jd_profile"], state["notes_digest"],
                                                state["web_findings"], stages=(stage,),
                                                directions=dirs, count=count),
                tag, budget, max_tokens=4000)

        qb_biz1a = _qb("业务一面",
                       ["推广费花出去但没转化", "大促目标如何拆解", "直播间流量承接",
                        "单品起量", "价格体系与促销机制"], 5, "question_bank#业务一面-a")
        qb_biz1b = _qb("业务一面",
                       ["竞品对冲", "售后与评价", "店铺日常经营数据复盘",
                        "跨部门协同排期", "给自己定一个新品的月度销量目标并拆解打法"],
                       5, "question_bank#业务一面-b")
        qb_biz2a = _qb("业务二面",
                       ["库存压货与补货", "商品结构与选品", "费率/费比与盈利模型",
                        "店铺月度经营目标拆解到周", "老客复购与用户分层"],
                       5, "question_bank#业务二面-a")
        qb_biz2b = _qb("业务二面",
                       ["新渠道/内容场拓展", "供应商与采购谈判", "价格体系与促销机制复盘",
                        "客服与售后体验对复购的影响", "团队协作与向上汇报"],
                       5, "question_bank#业务二面-b")
        qb_rest = ask_json(
            provider, prompts.question_bank(state["jd_profile"], state["notes_digest"],
                                            state["web_findings"],
                                            stages=("AI面", "群面", "HR面"), with_ask_back=True),
            "question_bank#其余场景", budget, max_tokens=6000)
        state["question_bank"] = _merge_qb(qb_biz1a, qb_biz1b, qb_biz2a, qb_biz2b, qb_rest)

        save_json(run_dir, "04_notes_digest.json", state["notes_digest"])
        save_json(run_dir, "04_jd_profile.json", state["jd_profile"])
        save_json(run_dir, "04_resume_profile.json", state["resume_profile"])
        save_json(run_dir, "04_gap_analysis.json", state["gap_analysis"])
        save_json(run_dir, "04_star_questions.json", state["star_questions"])
        save_json(run_dir, "04_question_bank.json", state["question_bank"])
        p(f"\n[成本] {budget.summary()}")

    # ---- 模块6 ----
    p("\n[模块6] 生成面试备考包")
    md = build_markdown(state)
    (run_dir / "06_package.md").write_text(md, encoding="utf-8")
    p(f"  Markdown 已落盘：{(run_dir / '06_package.md').relative_to(BASE_DIR)}（{len(md)} 字，"
      f"约 {md.count(chr(10)) + 1} 行）")

    # ---- 模块5 ----
    doc_url = ""
    if args.dry_run or args.no_feishu:
        p("\n[模块5] 跳过飞书写入（--dry-run / --no-feishu）")
    else:
        p("\n[模块5] 写入飞书云文档")
        from feishu_client import FeishuClient

        fc = FeishuClient()
        title = f"{company} · {position} · 面试备考包（{time.strftime('%Y-%m-%d')}）"
        doc_url = fc.publish_markdown(title, md)
        p(f"  完成：{doc_url}")

    state["feishu_doc_url"] = doc_url
    save_json(run_dir, "07_result.json", {
        "task_id": task_id, "feishu_doc_url": doc_url,
        "web_count": len(state["web_findings"]), "note_count": len(state["raw_notes"]),
        "errors": state["errors"],
    })

    p("\n===== 结束 =====")
    if doc_url:
        p(f"飞书文档：{doc_url}")
    else:
        p(f"本地产物：{(run_dir / '06_package.md')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="面试备考包端到端流水线")
    ap.add_argument("--input", help="任务输入 JSON（含 company/position/aliases/jd_text/resume_file）")
    ap.add_argument("--company")
    ap.add_argument("--position")
    ap.add_argument("--jd-file", help="JD 原文文件路径（.txt/.md），优先级高于 --input 里的 jd_text")
    ap.add_argument("--jd-text", help="直接传 JD 原文（短文本用）")
    ap.add_argument("--resume-file", help="简历文件路径（.docx/.txt/.md/.pdf）")
    ap.add_argument("--task-id")
    ap.add_argument("--dry-run", action="store_true", help="不调 LLM、不写飞书，只验证串联（0 成本）")
    ap.add_argument("--no-feishu", action="store_true", help="调 LLM 但不写飞书")
    ap.add_argument("--mock", action="store_true", help="LLM 用 mock（不联网、不花钱）")
    ap.add_argument("--crawl", action="store_true",
                    help="模块2 走实时抓取；默认 --skip-detail（只发搜索，最省请求）；"
                         "加 --crawl-detail 才会抓正文；失败/空则自动回退冻结 fixture")
    ap.add_argument("--crawl-detail", action="store_true",
                    help="实时抓取时抓取帖子正文（每个帖子多 1 次详情请求，风控风险更高）")
    ap.add_argument("--crawl-detail-limit", type=int, default=8,
                    help="最多对前 N 条帖子取正文（默认 8，0=全部）。详情是最敏感的部分，建议只取最相关的前若干条")
    ap.add_argument("--crawl-keywords", help="覆盖抓取关键词（多个用 | 分隔）；不传则由公司/岗位/别名自动生成")
    ap.add_argument("--crawl-max-notes", type=int, default=20, help="单次抓取帖子数上限（默认 20，合规硬上限 30）")
    ap.add_argument("--crawl-timeout", type=int, default=300, help="抓取超时秒数（默认 300）")
    ap.add_argument("--crawl-reuse",
                    help="复用某次已抓取任务的日志解析帖子（0 次新请求），传那个任务的 task-id，如 live_02")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
