# -*- coding: utf-8 -*-
"""
LangGraph 全局 State 契约（模块 0 编排层）

约定：所有节点只读写本文件声明过的字段，禁止往 state 里塞临时变量。
每个字段都对应磁盘上的产物文件，落盘目录为 ./data/runs/{task_id}/，
这样任何一次任务都可以离线回放、审计、复现（也便于排查"到底是哪一步错了"）。
"""

from __future__ import annotations

from typing import Any, Dict, List, TypedDict


class TaskInput(TypedDict, total=False):
    company: str            # 公司名，如 "示例公司"
    aliases: List[str]      # 公司别名/品牌名，如 ["示例品牌"]
    position: str           # 岗位名，如 "电商运营"
    jd_text: str            # JD 原文
    resume_text: str        # 简历文本（注意：落盘前必须脱敏，见模块4）
    resume_file: str        # 原始文件路径（仅本地留痕，不参与上传）


class AgentState(TypedDict, total=False):
    # ---- 任务标识 ----
    task_id: str
    stage: str                      # 当前阶段，便于断点续跑
    errors: List[Dict[str, Any]]    # 各模块异常记录（不抛断，全部收集）

    # ---- 输入 ----
    input: TaskInput

    # ---- 模块2：爬虫 ----
    crawl_request: Dict[str, Any]    # CrawlRequest 字典
    crawl_result: Dict[str, Any]     # CrawlResult 字典（含 status，是降级判断依据）
    raw_notes: List[Dict[str, Any]]  # 爬虫原始 NoteItem 列表（20 字段，未清洗）
    need_fallback: bool              # 是否需要走模块2b 备选数据源

    # ---- 模块1 / 2b：外部信息 ----
    web_findings: List[Dict[str, Any]]   # 每条必须带 url / fetched_at / source
    fallback_findings: List[Dict[str, Any]]

    # ---- 模块3：预处理 ----
    evidence: List[Dict[str, Any]]  # EvidenceItem：统一后的证据（含可信度分级）

    # ---- 模块4：简历挖掘 ----
    jd_profile: Dict[str, Any]      # JD 结构化：硬性要求 / 加分项 / 关键词
    resume_profile: Dict[str, Any]  # 简历结构化（脱敏后）
    gap_analysis: Dict[str, Any]    # 匹配度 + 能力缺口 + 简历优化提示
    star_questions: List[Dict[str, Any]]  # STAR 深度挖掘追问脚本

    # ---- 模块5 / 6：飞书 ----
    feishu_doc_url: str
    feishu_bitable_url: str
    interview_package: Dict[str, Any]     # 面试备考包结构化内容
