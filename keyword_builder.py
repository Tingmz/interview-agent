# -*- coding: utf-8 -*-
"""
关键词构造（模块 2 的输入准备，归属编排层）

职责：把「公司名 + 岗位 + 公司别名/品牌名 + 关注点」翻译成可直接喂给爬虫的搜索关键词。

额度只有 5 个（MAX_KEYWORDS），怎么花才是最优的：
  1) **差异化优先**：5 个额度要覆盖"不同意图"，而不是同一意图换同义词。
     "一面面经"、"二面面经" 在小红书里返回结果高度重叠，等于把一个额度花了两遍；
     所以把"一面/二面"合并成 "面经" 一档，腾出额度给 "AI面" 这种独立的意图。
  2) **别名补召回**：公司在小红书上常以品牌名出现（例：某集团 ↔ 其品牌名），
     别名只分配给最核心的"面经"档，避免别名铺开挤占精准意图。
  3) **岗位词不进搜索词**：小红书是多词模糊匹配，搜索词里加"电商运营"会把结果
     直接砍到接近 0（几乎没人会写"公司名+岗位名+一面"这种长查询）；
     岗位相关性筛选放到**模块 3（预处理）**做：按标题/正文是否含"电商/运营"过滤。
     只有额度 >5 时，才会自动追加"公司+岗位+面经"这类精准组合。

合规：默认只产出 MAX_KEYWORDS 个关键词，不要放宽。
"""

from __future__ import annotations

from typing import Iterable, List, Sequence

from crawler_runner import MAX_KEYWORDS

# 关注点：顺序 = 优先级，"面经"覆盖一面/二面/整体流程
DEFAULT_ASPECTS = ("面经", "AI面", "避雷", "待遇")


def _dedupe(seq: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for s in seq:
        s = " ".join(str(s).split())
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def build_keywords(
    company: str,
    position: str,
    aliases: Sequence[str] = (),
    aspects: Sequence[str] = DEFAULT_ASPECTS,
    limit: int = MAX_KEYWORDS,
) -> List[str]:
    """生成搜索关键词列表（差异化优先，别名补量，岗位词最后兜底）。"""
    company = (company or "").strip()
    if not company:
        return []
    aliases = [a.strip() for a in aliases if a and a.strip()]
    aspects = [a.strip() for a in aspects if a and a.strip()] or list(DEFAULT_ASPECTS)
    cap = max(1, min(limit, MAX_KEYWORDS))

    candidates: List[str] = []

    # 第一轮：公司名 × 各关注点（差异化最大，最可能出结果）
    for a in aspects:
        candidates.append(f"{company} {a}")

    # 第二轮：别名 × 核心关注点（补召回；品牌名帖子通常更多）
    for n in aliases:
        candidates.append(f"{n} {aspects[0]}")

    # 第三轮：公司 + 岗位 + 关注点（最精准但最可能为空，额度有剩才用）
    if position:
        for a in aspects[:2]:
            candidates.append(f"{company} {position} {a}")

    return _dedupe(candidates)[:cap]


def build_crawl_request(
    task_id: str,
    company: str,
    position: str,
    aliases: Sequence[str] = (),
    aspects: Sequence[str] = DEFAULT_ASPECTS,
    max_notes: int = 20,
    timeout_sec: int = 300,
) -> dict:
    """产出 CrawlRequest 字典（可直接喂给 crawler_runner.CrawlRequest.from_dict）。"""
    return {
        "task_id": task_id,
        "keywords": build_keywords(company, position, aliases, aspects),
        "max_notes": max_notes,
        "need_comments": False,
        "max_comments": 0,
        "timeout_sec": timeout_sec,
    }


if __name__ == "__main__":
    kw = build_keywords("示例公司", "示例岗位", aliases=["示例品牌"])
    print(f"关键词数量：{len(kw)}")
    for i, k in enumerate(kw, 1):
        print(f"{i}. {k}")
