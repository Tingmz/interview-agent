# -*- coding: utf-8 -*-
"""
模块 2：本地 MediaCrawler 调用封装（crawler_runner）

职责边界（本文件只做这 5 件事）：
  1) 把 CrawlRequest 翻译成 MediaCrawler 命令行，并注入"CLI 传不进去"的配置
  2) 以独立子进程隔离运行爬虫，支持超时强杀（含子进程树）
  3) 判定运行状态：ok / empty / timeout / login_required / blocked / error / throttled
  4) 读取并解析爬虫产出的 JSONL，返回结构化数据
  5) 输出统一 CrawlResult，并落盘 crawl_result.json（便于审计/回放）

明确不做：内容清洗去重摘要（模块3）、牛客/看准降级检索（模块2b）、任何爬虫逻辑。

合规声明（硬性）：
  * MediaCrawler 为「非商用学习研究」许可，本封装仅用于个人学习研究，严禁商用。
  * 本文件内置数量上限与限频护栏，请勿放宽（高频抓取会导致账号/IP 封禁）。
  * 关键词与结果仅用于个人面试备考，不得二次分发或用于任何商业用途。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ============================================================
# 0. 配置区（唯一需要按本机情况调整的地方）
# ============================================================
# MediaCrawler 的本地路径：优先读 MC_DIR 环境变量，否则按"与本项目同级目录"推断。
# 不要把绝对路径写死进代码 —— 换台机器就得改代码，也没法公开到 GitHub。
MC_DIR = Path(os.getenv("MC_DIR") or (Path(__file__).resolve().parent.parent / "MediaCrawler"))
RUNS_ROOT = Path(__file__).resolve().parent / "data" / "runs"


def _resolve_python() -> str:
    """
    解释器选择顺序：MC_PYTHON 环境变量 > MediaCrawler 自己的 .venv > 当前解释器。
    必须优先用爬虫的 .venv：本机默认 python 是 3.8，而 MediaCrawler 要求 >=3.11
    （见 pyproject.toml requires-python 与 .python-version），用错解释器会直接崩。
    """
    env_py = os.getenv("MC_PYTHON")
    if env_py:
        return env_py
    for cand in (MC_DIR / ".venv" / "Scripts" / "python.exe",   # Windows
                 MC_DIR / ".venv" / "bin" / "python"):          # macOS / Linux
        if cand.exists():
            return str(cand)
    return sys.executable


PYTHON_EXE = _resolve_python()

# ---- 合规护栏（硬上限，不要放宽）----
# 说明：你目前只有 1 个真实小红书账号（无小号可牺牲），故护栏按"最保守"设定。
MAX_NOTES_HARD_LIMIT = 30   # 单次任务最多抓取的帖子数
MAX_KEYWORDS = 5            # 单次任务最多关键词数
MIN_INTERVAL_SEC = 120      # 两次爬虫任务之间的最小间隔（秒）

# ---- 状态判定关键词（等你贴真实终端日志后，我们再一起校准这里）----
LOGIN_PATTERNS = (
    "扫码", "请登录", "未登录", "登录失败",
    "login_expired", "login failed", "qr code", "please login",
)
BLOCKED_PATTERNS = (
    "captcha appeared", "verifytype",
    "300012", "network connection error, please check network settings",
    "验证码", "滑动验证", "访问频繁", "风控", "操作过于频繁",
)

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_TIMEOUT = "timeout"
STATUS_LOGIN = "login_required"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"
STATUS_THROTTLED = "throttled"
STATUS_DRY_RUN = "dry_run"

# 触发上层降级（切牛客/看准）的状态
FALLBACK_STATUSES = {STATUS_EMPTY, STATUS_TIMEOUT, STATUS_LOGIN, STATUS_BLOCKED, STATUS_ERROR}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ============================================================
# 1. 数据结构（= 模块间数据契约，见阶段一的定义）
# ============================================================
@dataclass
class CrawlRequest:
    task_id: str
    keywords: List[str]
    max_notes: int = 20
    need_comments: bool = False
    max_comments: int = 0
    timeout_sec: int = 300
    start_page: int = 1
    headless: bool = False

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CrawlRequest":
        return CrawlRequest(
            task_id=str(d.get("task_id", "")),
            keywords=[str(k) for k in (d.get("keywords") or [])],
            max_notes=int(d.get("max_notes", 20)),
            need_comments=bool(d.get("need_comments", False)),
            max_comments=int(d.get("max_comments", 0)),
            timeout_sec=int(d.get("timeout_sec", 300)),
            start_page=int(d.get("start_page", 1)),
            headless=bool(d.get("headless", False)),
        )


@dataclass
class CrawlResult:
    task_id: str
    status: str
    run_dir: str = ""
    contents_file: str = ""
    comments_file: str = ""
    count: int = 0
    raw_count: int = 0                                   # 去重前条数（多关键词重叠时远大于 count）
    duplicate_count: int = 0                             # 被去重掉的条数
    missing_url_count: int = 0                           # 缺 note_url 的条数（无法溯源，下游必须警惕）
    keyword_hits: Dict[str, int] = field(default_factory=dict)   # 每个关键词命中的帖子数
    returncode: Optional[int] = None
    error: str = ""
    cmd: List[str] = field(default_factory=list)
    stdout_tail: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_sec: float = 0.0
    items: List[Dict[str, Any]] = field(default_factory=list)
    comments: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self, include_items: bool = False) -> Dict[str, Any]:
        d = asdict(self)
        if not include_items:
            d.pop("items", None)
            d.pop("comments", None)
        return d


# ============================================================
# 2. 命令拼装（源码零改动的关键：用 -c 引导脚本注入 config）
# ============================================================
# ---- 诊断注入（--diag）：源码零改动前提下，把 selfinfo 的真实 HTTP 响应打出来 ----
# 为什么必须加它：pong() 会把「非 200」和「200 但 success=false」两种情况都折叠成一个 False，
# 日志里只剩 "Login state result: False"，根本分不清是签名被拒、风控 461、还是 cookie 没带上。
# 这里只临时替换 query_self 增加打印，返回值与原实现完全一致（不改判定逻辑），定位完即可删。
_DIAG_SRC = '''\
import media_platform.xhs.client as _xc
from tools.httpx_util import make_async_client as _mac
from media_platform.xhs.playwright_sign import get_b1_from_localstorage as _b1

async def _qs(self):
    uri = "/api/sns/web/v1/user/selfinfo"
    hd = await self._pre_headers(uri, params={})
    async with _mac(proxy=self.proxy) as _c:
        r = await _c.get(f"{self._host}{uri}", headers=hd)
    ck = self.headers.get("Cookie") or ""
    print("[DIAG] selfinfo HTTP", r.status_code)
    print("[DIAG] cookie_len", len(ck), "| has_web_session", "web_session" in ck, "| has_a1", "a1" in ck)
    print("[DIAG] x-s", (hd.get("X-S") or "")[:24], "| x-s-common_len", len(hd.get("x-S-Common") or ""))
    try:
        print("[DIAG] b1_len", len(await _b1(self.playwright_page) or ""))
    except Exception as _e:
        print("[DIAG] b1_len err", _e)
    print("[DIAG] body", (r.text or "")[:400])
    return r.json() if r.status_code == 200 else None

_xc.XiaoHongShuClient.query_self = _qs
'''

# ---- 跳过登录自检（--skip-login-check）----
# 实测：profile 已登录时，selfinfo 接口仍返回 HTTP 406 + {"code":-1,"success":false}，
# pong() 因此误判为"未登录"，转去做扫码登录，但页面已登录根本没有二维码，最后 30s×2 超时崩掉。
# 既然浏览器里登录态是好的，就直接跳过这个不可靠的接口自检（源码零改动，只在 wrapper 注入）。
_SKIP_LOGIN_SRC = '''\
import media_platform.xhs.client as _xc

async def _pong(self):
    print("[WRAP] 已跳过 pong() 登录自检（--skip-login-check），直接进入搜索")
    return True

_xc.XiaoHongShuClient.pong = _pong
'''

# ---- 跳过逐条详情请求（--skip-detail）：只发 1 次搜索，不碰详情 ----
# 为什么这么做：
#   (a) 上次整轮崩溃就出在详情页：__INITIAL_STATE__ 解析出的 JSONDecodeError 经 tenacity
#       重试 3 次变成 RetryError，而 core.py 的外层 try 只捕获 NoteNotFoundError /
#       DataFetchError / KeyError，于是 RetryError 逃逸出 asyncio.gather，整个进程崩掉；
#   (b) 详情是请求量最大的部分（1 个关键词 = 1 次搜索 vs 20 次详情），也是最容易触发风控的部分；
#   (c) 我们只需要标题/作者/互动数/发布时间，这些在**搜索响应**里就全有了。
# 因此把详情任务直接短路成 None：搜索响应照常打印到 stdout，由 notes_from_search_log 兜底解析。
_SKIP_DETAIL_SRC = '''\
import media_platform.xhs.core as _core

async def _no_detail(self, *a, **k):
    return None

_core.XiaoHongShuCrawler.get_note_detail_async_task = _no_detail
print("[WRAP] 已跳过逐条详情请求（--skip-detail），本轮只发 1 次搜索请求")
'''

# ---- 安全详情抓取（默认路径）：用已登录的浏览器打开笔记页，直接读 window.__INITIAL_STATE__ ----
# 三条路都试过了，记录一下结论，免得以后重走弯路：
#   1) feed API：POST /api/sns/web/v1/feed 对本账号一律返回 {"code":-1,"success":false}
#      （DataFetchError），xsec_source 补成 pc_search 也没用 —— 接口不可用。
#   2) httpx 抓笔记页 HTML 再 json.loads：__INITIAL_STATE__ 里是 **JS 字面量而不是严格 JSON**
#      （实测 3 个帖子都卡在同一处 Expecting value），靠字符串替换去兼容补不完，此路不通。
#   3) 浏览器求值（现在用的）：爬虫自己那个已登录的浏览器上下文里新开一个标签页 goto 笔记页，
#      再 page.evaluate("window.__INITIAL_STATE__") —— 由浏览器的 JS 引擎把对象求值后直接交给
#      Python，**没有任何解析/转义问题**；取完立刻关掉标签页，不干扰用于签名的主页面。
# 另外：
#   - 源码里那个 detail 任务会 API→HTML 兜底，且 HTML 解析抛的 RetryError 会逃出
#     asyncio.gather 把整个进程搞崩（前面"疑似风控"的真因）；这里异常全吞，只返回 None。
#   - 用 _LIMIT 限制取正文的帖子数：只对最相关的前 N 条取正文，其余保留标题，控制请求量。
_SAFE_DETAIL_SRC = '''\
import asyncio as _aio
import re as _re
import config as _cfg
import media_platform.xhs.core as _core

_LIMIT = __LIMIT__
_state = {"n": 0}


def _snake(obj):
    """把 __INITIAL_STATE__ 的 camelCase 键转成 snake_case（等价源码里的 humps.decamelize）。"""
    def conv(k):
        return _re.sub(r"([a-z0-9])([A-Z])", r"\\1_\\2", str(k)).lower()
    if isinstance(obj, dict):
        return {conv(k): _snake(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_snake(v) for v in obj]
    return obj


async def _safe_detail(self, note_id, xsec_source, xsec_token, semaphore):
    async with semaphore:
        if _LIMIT > 0 and _state["n"] >= _LIMIT:
            return None
        _state["n"] += 1
        src = xsec_source or "pc_search"
        url = ("https://www.xiaohongshu.com/explore/" + str(note_id)
               + "?xsec_token=" + str(xsec_token or "") + "&xsec_source=" + str(src))
        # 只在 state 里真的出现 noteDetailMap 时才算拿到；没有就继续等（页面是异步渲染的）
        js = ("() => (window.__INITIAL_STATE__ && window.__INITIAL_STATE__.note "
              "&& window.__INITIAL_STATE__.note.noteDetailMap) "
              "? window.__INITIAL_STATE__ : null")
        page = None
        st = None
        try:
            # 新开标签页，不碰用于签名的主页面（context_page）
            page = await self.browser_context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            for _ in range(16):
                st = await page.evaluate(js)
                if st:
                    break
                await _aio.sleep(0.5)
        except Exception as e:
            print("[WRAP] detail browser failed", note_id, type(e).__name__, str(e)[:160])
            return None
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
        if not st:
            print("[WRAP] detail no state", note_id, "(页面未给出 noteDetailMap)")
            return None
        try:
            m = (st.get("note") or {}).get("noteDetailMap") or {}
            key = str(note_id) if str(note_id) in m else (list(m.keys())[0] if m else None)
            note = (m.get(key) or {}).get("note") if key else None
        except Exception as e:
            print("[WRAP] detail state shape error", note_id, repr(e)[:120])
            return None
        if not note:
            print("[WRAP] detail empty note", note_id, "keys=", list(m.keys())[:5])
            return None
        note = _snake(note)          # store 层要 note_id / desc / interact_info / image_list 这套键名
        note["note_id"] = str(note_id)
        note.update({"xsec_token": xsec_token, "xsec_source": src})
        print("[WRAP] detail ok", note_id, "desc_len=", len(str(note.get("desc") or "")))
        await _aio.sleep(_cfg.CRAWLER_MAX_SLEEP_SEC)
        return note


_core.XiaoHongShuCrawler.get_note_detail_async_task = _safe_detail
print("[WRAP] 安全详情抓取已启用（浏览器读 __INITIAL_STATE__，异常全吞），详情上限", _LIMIT)
'''


def _safe_detail_src(limit: int) -> str:
    return _SAFE_DETAIL_SRC.replace("__LIMIT__", str(int(limit)))


def build_command(req: CrawlRequest, run_dir: Path, diag: bool = False,
                  skip_login_check: bool = False, skip_detail: bool = False,
                  detail_limit: int = 0) -> List[str]:
    """
    为什么不用「修改 config/base_config.py」？
      你要求源码零改动。而 CRAWLER_MAX_NOTES_COUNT / ENABLE_GET_MEIDAS
      并没有 CLI 参数（见 cmd_arg/arg.py），config 也不读环境变量。
      因此用 python -c 先 import config 覆写，再 runpy 执行 main 模块，
      既保证源码零改动，又能真正限制抓取数量。

    必须显式传参的原因（base_config.py 的默认值会坑人）：
      PLATFORM 默认 "wb"、SAVE_DATA_OPTION 默认 "csv"、START_PAGE 默认 9。
    """
    max_notes = min(int(req.max_notes), MAX_NOTES_HARD_LIMIT)

    argv = [
        "main.py",
        "--platform", "xhs",
        "--type", "search",
        "--lt", "qrcode",
        "--keywords", ",".join(req.keywords),
        "--start", str(req.start_page),
        "--save_data_option", "jsonl",
        "--save_data_path", run_dir.as_posix(),   # 正斜杠：AsyncFileWriter 用 f"{path}/{platform}/..." 拼接
        "--get_comment", "true" if req.need_comments else "false",
        "--max_comments_count_singlenotes", str(int(req.max_comments)),
        "--max_concurrency_num", "1",             # 合规：强制串行，禁止并发
        "--headless", "true" if req.headless else "false",
    ]

    prelude = ""
    if skip_login_check:
        prelude += f"exec({_SKIP_LOGIN_SRC!r});"
    if skip_detail:
        prelude += f"exec({_SKIP_DETAIL_SRC!r});"
    else:
        prelude += f"exec({_safe_detail_src(detail_limit)!r});"
    if diag:
        prelude += f"exec({_DIAG_SRC!r});"

    bootstrap = (
        "import sys, runpy, config;"
        f"config.CRAWLER_MAX_NOTES_COUNT={max_notes};"
        "config.ENABLE_GET_MEIDAS=False;"
        + prelude
        + f"sys.argv={argv!r};"
        "runpy.run_module('main', run_name='__main__')"
    )
    return [PYTHON_EXE, "-c", bootstrap]


def _build_env() -> Dict[str, str]:
    """
    子进程环境。**必须剔除 PYTHONHOME/PYTHONPATH**：
    实测发现，当 Agent 用的 3.12 venv 启动爬虫用的 3.11 venv 时，
    父进程环境里的 PYTHONHOME（指向 uv 的 cpython-3.12）会被子进程继承，
    导致 3.11 解释器去加载 3.12 的标准库，直接崩在 threading 上：
        AttributeError: module '_thread' has no attribute 'daemon_threads_allowed'
    这是"两个 venv 版本不同"时的隐蔽坑，靠日志很难定位，所以在这里强制隔离。
    """
    env = dict(os.environ)
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        env.pop(k, None)
    # main.py 会把 stdout 强制成 utf-8，这里保证子进程整体按 utf-8 走
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


# ============================================================
# 3. 进程控制
# ============================================================
def _kill_tree(proc: subprocess.Popen) -> None:
    """超时/中断时必须连浏览器子进程一起杀，否则 Chrome 会残留。"""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, check=False,
            )
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def _check_interval(force: bool) -> Optional[float]:
    """两次爬虫任务的最小间隔护栏，返回还需等待的秒数（None = 可以跑）。"""
    if force:
        return None
    stamp = RUNS_ROOT / ".last_crawl_ts"
    if not stamp.exists():
        return None
    try:
        last = float(stamp.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    waited = time.time() - last
    if waited < MIN_INTERVAL_SEC:
        return round(MIN_INTERVAL_SEC - waited, 1)
    return None


def _touch_interval() -> None:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    (RUNS_ROOT / ".last_crawl_ts").write_text(str(time.time()), encoding="utf-8")


# ============================================================
# 4. 结果解析与状态判定
# ============================================================
def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """读 JSONL；单行损坏不影响整体（爬虫被打断时最后一行可能不完整）。"""
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("note_id"):
                rows.append(obj)
    return rows


def _to_epoch_seconds(v: Any) -> Optional[int]:
    """
    时间戳单位不保证：media_platform/xhs/field.py 里 time 声明为 int 且原样透传，
    没有做单位转换。这里按量级兜底：> 1e12 视为毫秒，否则视为秒。
    """
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n // 1000 if n > 10 ** 12 else n


def normalize_note(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    给爬虫原始记录补上编排层必需的确定性字段（不改动爬虫原有的 20 个字段）。
    以 "_" 开头 = 编排层附加字段：
      _publish_dt / _age_days → 模块3 的"过滤超过2年面经"依赖它
      _matched_keywords       → 关键词有效性分析依赖它
    """
    note = dict(item)
    ts = _to_epoch_seconds(note.get("time"))
    if ts:
        dt = datetime.fromtimestamp(ts)
        note["_publish_dt"] = dt.isoformat(timespec="seconds")
        note["_age_days"] = (datetime.now() - dt).days
    else:
        note["_publish_dt"] = ""
        note["_age_days"] = None   # 时效未知：下游必须标"信息存疑"，不可当作新帖使用
    return note


_SEARCH_MARKER = "Search notes response:"
_KW_MARKER = "Current search keyword:"


def _ts_from_note_id(note_id: str) -> Optional[int]:
    """
    小红书 note_id 的前 8 位十六进制就是创建时间戳（秒）。
    搜索响应里**不带发布时间**，所以用这个反推；解不出来就返回 None（下游按"时效未知"处理）。
    """
    try:
        n = int(str(note_id)[:8], 16)
    except (ValueError, TypeError):
        return None
    return n if n > 10 ** 9 else None


def notes_from_search_log(stdout: str) -> List[Dict[str, Any]]:
    """
    从爬虫 stdout 兜底解析「Search notes response:」里的搜索结果，转成 note 记录。

    为什么需要兜底（配合 --skip-detail 使用）：
      搜索响应本身已包含 display_title / user.nickname / interact_info.*_count，
      而我们只用到这些字段；详情接口既是我们崩溃的来源，也是风控最敏感的部分，
      所以详情一律跳过，直接吃搜索响应 —— 每个关键词只发 1 次请求。
    解析失败（日志被截断/格式变化）时静默跳过该行，绝不让解析问题变成崩溃。
    """
    rows: List[Dict[str, Any]] = []
    cur_kw = ""
    for line in (stdout or "").splitlines():
        if _KW_MARKER in line:
            cur_kw = line.split(_KW_MARKER, 1)[1].strip()
            continue
        if _SEARCH_MARKER not in line:
            continue
        seg = line.split(_SEARCH_MARKER, 1)[1].strip()
        try:
            obj = ast.literal_eval(seg)
        except (ValueError, SyntaxError):
            continue
        if not isinstance(obj, dict):
            continue
        for it in (obj.get("items") or []):
            if not isinstance(it, dict) or it.get("model_type") != "note":
                continue
            nc = it.get("note_card") or {}
            nid = str(it.get("id") or "")
            if not nid:
                continue
            inter = nc.get("interact_info") or {}
            token = str(it.get("xsec_token") or "")
            rows.append({
                "note_id": nid,
                "title": str(nc.get("display_title") or "").replace("\n", " ").strip(),
                "nickname": (nc.get("user") or {}).get("nickname", ""),
                "liked_count": inter.get("liked_count"),
                "collected_count": inter.get("collected_count"),
                "comment_count": inter.get("comment_count"),
                "share_count": inter.get("shared_count"),
                "time": _ts_from_note_id(nid),
                "note_url": f"https://www.xiaohongshu.com/explore/{nid}"
                            f"?xsec_token={token}&xsec_source=pc_search",
                "source_keyword": cur_kw,
            })
    return rows


def dedupe_notes(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    5 个关键词的结果必然大量重叠，同一篇帖子会被多次写入 JSONL，必须按 note_id 去重。
    策略：保留首次出现的顺序（搜索结果按关键词顺序依次追加，靠前的通常更相关），
    并把命中的全部关键词合并进 _matched_keywords。
    """
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        nid = str(it.get("note_id") or "")
        if not nid:
            continue
        kw = str(it.get("source_keyword") or "").strip()
        if nid not in merged:
            note = normalize_note(it)
            note["_matched_keywords"] = [kw] if kw else []
            merged[nid] = note
            order.append(nid)
        elif kw and kw not in merged[nid]["_matched_keywords"]:
            merged[nid]["_matched_keywords"].append(kw)
    return [merged[n] for n in order]


def keyword_hit_stats(items: List[Dict[str, Any]]) -> Dict[str, int]:
    """每个关键词命中的（去重后）帖子数 → 用于迭代关键词组合。"""
    stats: Dict[str, int] = {}
    for it in items:
        for kw in (it.get("_matched_keywords") or []):
            stats[kw] = stats.get(kw, 0) + 1
    return dict(sorted(stats.items(), key=lambda kv: -kv[1]))


def _pick_latest(run_dir: Path, item_type: str) -> Optional[Path]:
    """文件名形如 search_contents_2026-10-17.jsonl，同目录可能多天累积，取最新。"""
    files = sorted(run_dir.glob(f"*_{item_type}_*.jsonl"))
    return files[-1] if files else None


def load_crawl_items(crawl_dir: Path, stdout: str = "") -> List[Dict[str, Any]]:
    """
    从一次爬虫任务的产物目录还原帖子列表（返回**未去重**记录，调用方自行 dedupe_notes）。

    优先级：JSONL > 搜索日志。
      开了详情时 JSONL 里是"打通了详情的 N 条"、**含正文 desc**；搜索日志里只有标题。
      所以必须以 JSONL 为主，再用搜索日志把没取到详情的帖子（标题/元数据）补回来，
      让详情记录覆盖到对应 note_id 上 —— 结果 = 全量标题 + 部分正文。
      没开详情时不写 JSONL，只能吃搜索响应（每关键词 1 次请求那版）。

    为什么抽成公共函数：run_crawl() 抓完当场要解析一份，pipeline 的 --crawl-reuse
    要把上次的产物从磁盘再读一份，两处逻辑必须完全一致 —— 早先 --crawl-reuse 只解析
    crawl_stdout.log，刚花请求抓到的正文会被整批丢掉。
    """
    contents_file = _pick_latest(crawl_dir / "xhs" / "jsonl", "contents")
    raw_items = _read_jsonl(contents_file) if contents_file else []
    if not raw_items:
        return notes_from_search_log(stdout) if stdout else []
    if stdout:
        search_items = notes_from_search_log(stdout)
        if search_items:
            by_id: Dict[str, Dict[str, Any]] = {str(it["note_id"]): it for it in search_items}
            for d in raw_items:
                nid = str(d.get("note_id") or "")
                if not nid:
                    continue
                base = by_id.get(nid) or {"note_id": nid}
                base.update({k: v for k, v in d.items() if v not in (None, "", [])})
                by_id[nid] = base
            raw_items = list(by_id.values())
    return raw_items


def _detect_status(
    stdout: str, returncode: Optional[int], count: int, timed_out: bool
) -> tuple[str, str]:
    low = (stdout or "").lower()

    if timed_out:
        return STATUS_TIMEOUT, f"爬虫超时未结束（>{_TIMEOUT_FOR_MSG}s），已强制中断"

    # 有数据就优先算成功：部分失败但已拿到可用数据，不该阻塞下游
    if count > 0:
        if returncode not in (0, None):
            return STATUS_OK, f"已获取 {count} 条，但进程退出码={returncode}（部分失败，数据可用）"
        return STATUS_OK, ""

    if any(p.lower() in low for p in BLOCKED_PATTERNS):
        return STATUS_BLOCKED, "疑似触发风控/验证码/限流，已停止，建议切换数据源或延迟重试"

    if any(p.lower() in low for p in LOGIN_PATTERNS):
        return STATUS_LOGIN, "需要人工扫码登录（XHS 首登人工断点），无法自动完成"

    if returncode == 0:
        return STATUS_EMPTY, "爬虫正常结束但无结果，可能是关键词过窄或搜索结果为空"

    return STATUS_ERROR, f"爬虫异常退出，returncode={returncode}"


_TIMEOUT_FOR_MSG = 0  # 仅用于状态文案，运行时被 run_crawl 覆盖


def _tail(text: str, lines: int = 30) -> str:
    if not text:
        return ""
    parts = text.strip().splitlines()
    return "\n".join(parts[-lines:])


# ============================================================
# 5. 主入口
# ============================================================
def run_crawl(req: CrawlRequest, dry_run: bool = False, force: bool = False,
              diag: bool = False, skip_login_check: bool = False,
              skip_detail: bool = False, detail_limit: int = 0) -> CrawlResult:
    global _TIMEOUT_FOR_MSG
    _TIMEOUT_FOR_MSG = req.timeout_sec

    started = _now_iso()
    t0 = time.time()

    # ---- 参数校验（合规护栏）----
    if not req.keywords:
        return CrawlResult(task_id=req.task_id, status=STATUS_ERROR, error="关键词为空",
                           started_at=started, finished_at=_now_iso())
    if len(req.keywords) > MAX_KEYWORDS:
        return CrawlResult(task_id=req.task_id, status=STATUS_ERROR,
                           error=f"关键词数量 {len(req.keywords)} 超过上限 {MAX_KEYWORDS}",
                           started_at=started, finished_at=_now_iso())
    if req.max_notes > MAX_NOTES_HARD_LIMIT:
        return CrawlResult(task_id=req.task_id, status=STATUS_ERROR,
                           error=f"max_notes {req.max_notes} 超过合规上限 {MAX_NOTES_HARD_LIMIT}",
                           started_at=started, finished_at=_now_iso())
    if not MC_DIR.exists():
        return CrawlResult(task_id=req.task_id, status=STATUS_ERROR,
                           error=f"爬虫目录不存在：{MC_DIR}（可用环境变量 MC_DIR 覆盖）",
                           started_at=started, finished_at=_now_iso())
    if not Path(PYTHON_EXE).exists():
        return CrawlResult(task_id=req.task_id, status=STATUS_ERROR,
                           error=f"解释器不存在：{PYTHON_EXE}（可用环境变量 MC_PYTHON 覆盖）",
                           started_at=started, finished_at=_now_iso())

    run_dir = RUNS_ROOT / req.task_id / "crawl"
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_command(req, run_dir, diag=diag, skip_login_check=skip_login_check,
                        skip_detail=skip_detail, detail_limit=detail_limit)

    if dry_run:
        return CrawlResult(task_id=req.task_id, status=STATUS_DRY_RUN, run_dir=str(run_dir),
                           cmd=cmd, error="dry_run：仅打印命令，未真正执行",
                           started_at=started, finished_at=_now_iso())

    wait = _check_interval(force)
    if wait is not None:
        return CrawlResult(task_id=req.task_id, status=STATUS_THROTTLED,
                           error=f"距上次爬取不足 {MIN_INTERVAL_SEC}s，请等待 {wait}s（合规限频）",
                           started_at=started, finished_at=_now_iso())

    timed_out = False
    stdout = ""
    returncode: Optional[int] = None
    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(MC_DIR), env=_build_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        try:
            stdout, _ = proc.communicate(timeout=req.timeout_sec)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                stdout, _ = proc.communicate(timeout=20)
            except Exception:
                stdout = ""
            returncode = proc.returncode
    except Exception as e:  # 启动失败（解释器路径错误、权限等）
        return CrawlResult(task_id=req.task_id, status=STATUS_ERROR,
                           run_dir=str(run_dir), cmd=cmd, error=f"子进程启动失败：{e}",
                           started_at=started, finished_at=_now_iso())
    finally:
        _touch_interval()

    # 完整日志落盘：状态判定校准、事后审计都靠它（Result 里的 stdout_tail 只留 30 行，不够用）
    try:
        (run_dir / "crawl_stdout.log").write_text(stdout or "", encoding="utf-8")
    except Exception:
        pass

    contents_file = _pick_latest(run_dir / "xhs" / "jsonl", "contents")
    comments_file = _pick_latest(run_dir / "xhs" / "jsonl", "comments")
    raw_items = load_crawl_items(run_dir, stdout or "")
    comments = _read_jsonl(comments_file) if comments_file else []
    items = dedupe_notes(raw_items)          # 多关键词重叠 → 按 note_id 去重 + 时间归一

    status, err = _detect_status(stdout or "", returncode, len(items), timed_out)

    result = CrawlResult(
        task_id=req.task_id,
        status=status,
        run_dir=str(run_dir),
        contents_file=str(contents_file) if contents_file else "",
        comments_file=str(comments_file) if comments_file else "",
        count=len(items),
        raw_count=len(raw_items),
        duplicate_count=max(0, len(raw_items) - len(items)),
        missing_url_count=sum(1 for it in items if not it.get("note_url")),
        keyword_hits=keyword_hit_stats(items),
        returncode=returncode,
        error=err,
        cmd=cmd,
        stdout_tail=_tail(stdout or ""),
        started_at=started,
        finished_at=_now_iso(),
        duration_sec=round(time.time() - t0, 1),
        items=items,
        comments=comments,
    )
    (run_dir / "crawl_result.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def should_fallback(result: CrawlResult) -> bool:
    """模块 2b（牛客/看准网页搜索备选源）的触发条件，由编排层调用。"""
    return result.status in FALLBACK_STATUSES


def crawl_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph 节点适配器（纯函数，不依赖 langgraph 包）。
    state 约定：{"task_id": str, "crawl_request": {...}}
    返回：{"crawl_result": {...}, "raw_notes": [...], "need_fallback": bool}
    """
    req_dict = dict(state.get("crawl_request") or {})
    req_dict.setdefault("task_id", state.get("task_id", ""))
    req = CrawlRequest.from_dict(req_dict)
    result = run_crawl(req)
    return {
        "crawl_result": result.to_dict(),
        "raw_notes": result.items,
        "need_fallback": should_fallback(result),
    }


# ============================================================
# 6. CLI（用于模块单独测试）
# ============================================================
def _main() -> int:
    p = argparse.ArgumentParser(description="MediaCrawler 调用封装（模块2）")
    p.add_argument("--task-id", required=True)
    p.add_argument("--keywords", required=True, help="多个关键词用 | 分隔")
    p.add_argument("--max-notes", type=int, default=20)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--need-comments", action="store_true")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")
    p.add_argument("--force", action="store_true", help="忽略最小间隔护栏（仅测试用）")
    p.add_argument("--diag", action="store_true",
                   help="诊断模式：打印 selfinfo 的真实 HTTP 状态/cookie/签名长度（排查登录态用）")
    p.add_argument("--skip-login-check", action="store_true",
                   help="跳过 pong() 登录自检，直接爬（profile 已登录、但 selfinfo 接口被风控返回 406 时用）")
    p.add_argument("--skip-detail", action="store_true",
                   help="只发搜索请求、不抓逐条详情（最省请求、最不易触发风控；标题/作者/互动数从搜索响应解析）")
    p.add_argument("--detail-limit", type=int, default=0,
                   help="不开 --skip-detail 时，最多对前 N 条帖子取正文（0=不限制）。"
                        "详情是最容易触发风控的部分，建议只取最相关的前若干条")
    args = p.parse_args()

    req = CrawlRequest(
        task_id=args.task_id,
        keywords=[k.strip() for k in args.keywords.split("|") if k.strip()],
        max_notes=args.max_notes,
        need_comments=args.need_comments,
        max_comments=20 if args.need_comments else 0,
        timeout_sec=args.timeout,
        headless=args.headless,
    )
    result = run_crawl(req, dry_run=args.dry_run, force=args.force, diag=args.diag,
                       skip_login_check=args.skip_login_check, skip_detail=args.skip_detail,
                       detail_limit=args.detail_limit)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    print(f"\n[result] status={result.status} count={result.count} "
          f"(raw={result.raw_count} dup={result.duplicate_count} no_url={result.missing_url_count}) "
          f"run_dir={result.run_dir}", file=sys.stderr)
    if result.keyword_hits:
        print("[keyword_hits]", file=sys.stderr)
        for kw, n in result.keyword_hits.items():
            print(f"  {n:>3}  {kw}", file=sys.stderr)
    if result.stdout_tail:
        print("\n[stdout_tail]\n" + result.stdout_tail, file=sys.stderr)
    return 0 if result.status in (STATUS_OK, STATUS_DRY_RUN) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
