# -*- coding: utf-8 -*-
"""
统一 LLM 入口（模块 0 编排层基建）

为什么要有这一层（不直接在业务代码里调 ChatOpenAI）：
  1) **省钱可测量**：每次调用都记录估算 token 与花费，落到 data/llm_usage.jsonl，
     既有单次上限也有累计上限，超了直接抛错，不会"不知不觉烧钱"。
  2) **开发期零成本**：mock 模式完全不联网；cache 模式把真实响应录到磁盘后回放，
     反复调试同一条 prompt 不再重复付费。
  3) **换模型只改 .env**：DeepSeek / 豆包都是 OpenAI 兼容接口，只差 base_url 与 model。

三种模式（环境变量 LLM_MODE）：
  mock   ：不联网，返回占位内容。开发/单测用，0 成本。
  auto   ：联网调用；命中磁盘缓存则直接回放（默认）。
  replay ：只读缓存，绝不联网；缓存未命中就报错。适合反复调试已跑通的流程。

⚠️ 价格默认值仅为**保守上限**，DeepSeek 会调价，请以官网为准并改 .env。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "llm_cache"
USAGE_FILE = DATA_DIR / "llm_usage.jsonl"

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"   # 豆包改为火山方舟的兼容地址
DEFAULT_MODEL = "deepseek-chat"                    # 别用 reasoner 跑批量任务，贵且慢


# ============================================================
# 0. 极简 .env 加载（不引入 python-dotenv 依赖）
# ============================================================
def load_env(path: Optional[Path] = None) -> None:
    p = path or (BASE_DIR / ".env")
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ============================================================
# 1. token 估算（用作预算护栏；不是精确计费口径）
# ============================================================
def estimate_tokens(text: str) -> int:
    """
    优先用 tiktoken 估算；DeepSeek 的 tokenizer 与 GPT 不完全一致，所以这只是**估算**，
    真实账单以平台后台为准。护栏的意义是"防意外爆量"，不是精确对账。
    """
    if not text:
        return 0
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text, disallowed_special=()))
    except Exception:
        # 兜底经验值：中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token
        return int(len(text) / 1.6) + 1


# ============================================================
# 2. 成本护栏
# ============================================================
class BudgetExceeded(RuntimeError):
    """超出预算：宁可报错停下，也不要静默烧钱。"""


@dataclass
class BudgetGuard:
    max_total_tokens: int = 200_000          # 累计上限（估算值），超过就停
    price_per_m_in: float = 2.0              # 元/百万 token（保守上限，以官网为准）
    price_per_m_out: float = 8.0             # 元/百万 token（保守上限，以官网为准）
    used_in: int = 0
    used_out: int = 0
    used_cost: float = 0.0
    calls: int = 0
    history: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "BudgetGuard":
        return cls(
            max_total_tokens=int(os.getenv("LLM_TOKEN_BUDGET", "200000")),
            price_per_m_in=float(os.getenv("LLM_PRICE_IN", "2.0")),
            price_per_m_out=float(os.getenv("LLM_PRICE_OUT", "8.0")),
        )

    def _cost(self, tin: int, tout: int) -> float:
        return tin / 1_000_000 * self.price_per_m_in + tout / 1_000_000 * self.price_per_m_out

    def add(self, tin: int, tout: int, meta: Optional[Dict[str, Any]] = None) -> float:
        if self.used_in + self.used_out + tin + tout > self.max_total_tokens:
            raise BudgetExceeded(
                f"本次任务累计 token 估算将超过上限 {self.max_total_tokens}"
                f"（已用 {self.used_in + self.used_out}）。"
                "请缩小输入（截断正文/减少帖子数）或调大 LLM_TOKEN_BUDGET。"
            )
        cost = self._cost(tin, tout)
        self.used_in += tin
        self.used_out += tout
        self.used_cost += cost
        self.calls += 1
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "est_in_tokens": tin,
            "est_out_tokens": tout,
            "est_cost_cny": round(cost, 6),
            "calls": self.calls,
            **(meta or {}),
        }
        self.history.append(rec)
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return cost

    def summary(self) -> str:
        return (f"调用 {self.calls} 次 | 估算 token in={self.used_in} out={self.used_out} "
                f"| 估算花费 ￥{self.used_cost:.4f} | 上限 {self.max_total_tokens}")


# ============================================================
# 3. Provider
# ============================================================
class LLMProvider(ABC):
    name = "base"
    model = ""
    # 上一次 chat 是否命中磁盘缓存。命中 = 没有真实联网，计费方必须跳过记账，
    # 否则重复记账会让 token 预算提前耗尽、成本报告失真。
    last_cache_hit = False

    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        ...

    def chat_json(self, messages, temperature=0.2, max_tokens=1024) -> Dict[str, Any]:
        """要求模型返回 JSON 并解析。解析失败会降级为 {"_raw": 原文}，避免整条流程崩掉。"""
        text = self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        obj = loads_lenient(text)
        if obj is None:
            print("[warn] 模型输出里捞不到合法 JSON（最常见原因：max_tokens 太小、"
                  "输出被截断在半截），本次结果降级为 _raw 原文")
            return {"_raw": text}
        return obj


def loads_lenient(text: str) -> Optional[Dict[str, Any]]:
    """
    从模型输出里"捞出"JSON 对象。

    为什么需要：模型经常在 JSON 前后多写一句人话（"好的，以下是……"），直接
    json.loads 就失败、整节退化成原始文本。这里① 先剥掉 ``` 代码围栏；
    ② 从候选的 "{" 开始试 raw_decode，前后噪音自动忽略。

    但**必须要求"这一个 JSON 基本吃掉了整段文本"**（尾巴不超过 60 字）：否则输出被
    max_tokens 截断时，会从残骸里捞出一个完整的内部小对象（比如某一个 question）
    当成整份结果返回，那是静默的错数据，比直接失败危险得多。
    """
    t = (text or "").strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) > 1:
            t = parts[1]
            if t.lstrip().lower().startswith("json"):
                t = t.lstrip()[4:]
    dec = json.JSONDecoder()
    starts = [0] + [i for i, ch in enumerate(t) if ch == "{"]
    for i in starts[:200]:              # 只看开头的若干候选，避免大文本上白跑
        chunk = t[i:].lstrip()
        if not chunk.startswith("{"):
            continue
        try:
            obj, end = dec.raw_decode(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict) and len(chunk[end:].strip()) <= 60:
            return obj
    return None


class MockProvider(LLMProvider):
    """不联网。开发期跑通流程用，0 成本。"""

    name = "mock"
    model = "mock"

    def chat(self, messages, temperature=0.2, max_tokens=1024) -> str:
        last = messages[-1]["content"] if messages else ""
        return json.dumps(
            {"_mock": True, "echo_head": last[:80], "echo_len": len(last)},
            ensure_ascii=False,
        )


class DeepSeekProvider(LLMProvider):
    """OpenAI 兼容接口；换豆包只需改 .env 的 base_url 与 model。"""

    name = "deepseek"

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None,
                 api_key: Optional[str] = None):
        self.model = model or os.getenv("LLM_MODEL", DEFAULT_MODEL)
        self._api_key = api_key or os.getenv("DEEPSEEK_API_KEY") or ""
        if not self._api_key:
            raise RuntimeError("未找到 DEEPSEEK_API_KEY，请在 interview_agent/.env 里填写")
        self._base_url = base_url or os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL)

    def chat(self, messages, temperature=0.2, max_tokens=1024) -> str:
        """
        直接打 OpenAI 兼容的 HTTP 接口（只用标准库 urllib），刻意不依赖
        langchain-openai / openai SDK：少一个装不上的依赖，也少一层封装，
        服务端返回的错误原文能原样看到（欠费、key 失效、参数错一眼就能分辨）。
        """
        import urllib.error
        import urllib.request

        url = self._base_url.rstrip("/") + "/chat/completions"
        data = json.dumps(
            {"model": self.model, "messages": messages,
             "temperature": temperature, "max_tokens": max_tokens},
            ensure_ascii=False,
        ).encode("utf-8")

        last_err = ""
        for _ in range(3):        # 含首次共 3 次尝试：网络抖动重试，但别把预算耗光
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self._api_key}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    body = json.loads(r.read().decode("utf-8"))
                choice = body["choices"][0]
                if choice.get("finish_reason") == "length":
                    # 必须显式告警：截断的 JSON 一定解析失败，但肉眼很难发现，
                    # 否则会误以为是"模型不听话"，其实是 max_tokens 给小了。
                    print("[warn] 输出达到 max_tokens 上限被截断，JSON 会残缺；"
                          "请调大 max_tokens 或把这次调用拆成多次")
                content = choice["message"]["content"]
                if isinstance(content, list):   # 兼容多模态返回格式
                    content = "".join(str(c) for c in content)
                return str(content)
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
                if e.code in (400, 401, 402, 403, 404):
                    break           # key 错 / 欠费 / 参数错，重试没有意义
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
        raise RuntimeError(f"LLM 调用失败（model={self.model} url={url}）：{last_err}")


class CachedProvider(LLMProvider):
    """
    给任意 provider 套一层磁盘缓存：key = hash(model + messages + 参数)。
    调试时反复跑同一条 prompt 不会重复付费（这也是开发期最有效的省钱手段）。
    """

    def __init__(self, inner: LLMProvider, mode: str = "auto", cache_dir: Path = CACHE_DIR):
        self.inner = inner
        self.mode = mode
        self.cache_dir = cache_dir
        self.name = f"cached({inner.name})"
        self.model = getattr(inner, "model", "")
        self.hits = 0
        self.misses = 0
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, messages, temperature, max_tokens) -> str:
        blob = json.dumps(
            {"m": self.model, "msg": messages, "t": temperature, "mt": max_tokens},
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    def chat(self, messages, temperature=0.2, max_tokens=1024) -> str:
        f = self.cache_dir / f"{self._key(messages, temperature, max_tokens)}.json"
        if f.exists():
            self.hits += 1
            self.last_cache_hit = True
            return json.loads(f.read_text(encoding="utf-8"))["text"]
        self.misses += 1
        self.last_cache_hit = False
        if self.mode == "replay":
            raise RuntimeError(f"replay 模式下缓存未命中：{f.name}（禁止联网，避免产生费用）")
        text = self.inner.chat(messages, temperature=temperature, max_tokens=max_tokens)
        f.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
        return text


def get_provider(mode: Optional[str] = None, budget: Optional[BudgetGuard] = None) -> LLMProvider:
    """按 .env 的 LLM_MODE 组装 provider（默认 auto，即"联网 + 缓存"）。"""
    load_env()
    mode = (mode or os.getenv("LLM_MODE", "auto")).lower()
    if mode == "mock":
        return MockProvider()
    inner: LLMProvider = DeepSeekProvider()
    return CachedProvider(inner, mode=mode)


# ============================================================
# 4. 自检：--mock 0 成本；--live 只发 1 次极短请求（成本可忽略）
# ============================================================
if __name__ == "__main__":
    import sys

    load_env()
    arg = sys.argv[1] if len(sys.argv) > 1 else "--mock"
    g = BudgetGuard.from_env()

    if arg == "--mock":
        p = get_provider(mode="mock")
        out = p.chat([{"role": "user", "content": "给我一句面试自我介绍的开头"}])
        print("[mock 输出]", out)
        print("[结论] mock 模式不联网，0 成本")
    elif arg == "--live":
        # 自检必须真联网：故意绕过缓存层。若走缓存，第二次自检就会直接返回旧结果，
        # 让你误以为 key 仍然有效，其实一个请求都没发出去。
        p = DeepSeekProvider()
        q = "只回复两个字：收到"
        t0 = time.time()
        out = p.chat([{"role": "user", "content": q}], max_tokens=16)
        cost = g.add(estimate_tokens(q), estimate_tokens(out), meta={"tag": "selftest"})
        print(f"[真实输出] {out!r}")
        print(f"[耗时] {time.time() - t0:.1f}s | provider={p.name} model={p.model}")
        print(f"[本次估算花费] ￥{cost:.6f}（真实账单以 DeepSeek 后台为准）")
        print(f"[注意] 这次调用已写入 {USAGE_FILE.name}")
    elif arg == "--report":
        if not USAGE_FILE.exists():
            print("暂无调用记录")
        else:
            rows = [json.loads(l) for l in USAGE_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
            total = sum(r.get("est_cost_cny", 0) for r in rows)
            print(f"累计调用 {len(rows)} 次 | 估算累计花费 ￥{total:.4f}")
            for r in rows[-10:]:
                print("  ", r)
    else:
        print("用法：python llm_provider.py [--mock|--live|--report]")
