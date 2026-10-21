# -*- coding: utf-8 -*-
"""
模块 5：飞书云文档写入（企业自建应用 + tenant_access_token）

为什么这样设计（而不是直接用官方 SDK）：
  1) **少一个依赖就少一个坑**：只用标准库 urllib（本机 requests 的
     charset_normalizer 在 Python 3.12 下直接 import 崩），HTTP 报文一眼可见。
  2) **token 必须缓存**：tenant_access_token 有效期 2 小时，且换发有频率限制。
     如果不缓存，写 60 个块就会换 60 次 token，直接触发限流。
  3) **文档是"块(block)"结构**：写入 = 往根块（block_id 恰好等于 document_id）
     的 children 里 append。单次最多 50 个块，超了必须分批。
  4) **自建应用建的文档，owner 是应用**：用户在自己飞书里打不开（403）。
     所以要顺手把文档的链接分享权限改成"组织内可编辑"。

安全：app_id / app_secret 只从 .env 读，不写死在代码里、不打印到日志。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = Path(__file__).resolve().parent
OPEN_API = "https://open.feishu.cn/open-apis"


def _http(method: str, url: str, *, headers: Dict[str, str],
          params: Optional[Dict[str, Any]] = None,
          body: Optional[Dict[str, Any]] = None,
          timeout: int = 30) -> Dict[str, Any]:
    """
    发一个 HTTP 请求并把响应解析成 JSON（只用标准库 urllib）。

    刻意不用 requests：本机 Python 3.12 下 requests 依赖的 charset_normalizer
    是旧版编译产物，import 时就会抛
    "TypeError: Metaclasses with custom tp_new are not supported"，整条流水线
    在"写飞书"这一步直接崩掉。urllib 是标准库，没有这类环境坑。
    非 JSON 响应（如网关错误页）返回 {"_http_status":..., "_text":...} 交调用方判断。
    """
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    status = -1
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:      # 4xx/5xx 也会带 JSON body，不能直接扔
        status = e.code
        text = e.read().decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return {"_http_status": status, "_text": text[:400]}

# 单次追加块的数量上限（飞书硬限制）
MAX_BLOCKS_PER_CALL = 50

# docx 块类型枚举（只列本模块用到的）
BT_TEXT = 2
BT_H1 = 3
BT_H2 = 4
BT_H3 = 5
BT_BULLET = 12
BT_ORDERED = 13
BT_CODE = 14
BT_QUOTE = 15
BT_DIVIDER = 22

_KEY_OF_TYPE = {
    BT_TEXT: "text",
    BT_H1: "heading1",
    BT_H2: "heading2",
    BT_H3: "heading3",
    BT_BULLET: "bullet",
    BT_ORDERED: "ordered",
    BT_CODE: "code",
    BT_QUOTE: "quote",
    BT_DIVIDER: "divider",
}


class FeishuError(RuntimeError):
    """飞书接口返回 code != 0。把 path / code / msg 全带上，方便直接定位是权限还是参数问题。"""

    def __init__(self, path: str, code: int, msg: str, raw: Any = None):
        super().__init__(f"[{path}] code={code} msg={msg}")
        self.path, self.code, self.msg, self.raw = path, code, msg, raw


def load_env(path: Optional[Path] = None) -> None:
    """极简 .env 加载（与 llm_provider.load_env 同款，避免互相 import 造成循环）。"""
    import os

    p = path or (BASE_DIR / ".env")
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class FeishuClient:
    def __init__(self, app_id: Optional[str] = None, app_secret: Optional[str] = None,
                 timeout: int = 30):
        import os

        load_env()
        self.app_id = app_id or os.getenv("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.getenv("FEISHU_APP_SECRET", "")
        if not self.app_id or not self.app_secret:
            raise RuntimeError("未找到 FEISHU_APP_ID / FEISHU_APP_SECRET，请检查 interview_agent/.env")
        self.timeout = timeout
        self._token = ""
        self._token_expire_at = 0.0

    # ---------------- 基础设施 ----------------

    def raw(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
            body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """诊断用：把飞书的原始响应原样返回（code != 0 也不抛异常）。"""
        headers = {"Content-Type": "application/json; charset=utf-8",
                   "Authorization": f"Bearer {self.tenant_access_token()}"}
        return _http(method, f"{OPEN_API}{path}", headers=headers,
                     params=params, body=body, timeout=self.timeout)

    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                 body: Optional[Dict[str, Any]] = None, use_token: bool = True) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if use_token:
            headers["Authorization"] = f"Bearer {self.tenant_access_token()}"
        payload = _http(method, f"{OPEN_API}{path}", headers=headers,
                        params=params, body=body, timeout=self.timeout)
        if "code" not in payload:
            raise FeishuError(path, int(payload.get("_http_status") or -1),
                              f"响应不是预期的 JSON：{payload.get('_text', '')[:200]}", payload)
        code = payload.get("code", -1)
        if code != 0:
            raise FeishuError(path, code, payload.get("msg", ""), payload)
        return payload.get("data", {}) or {}

    def tenant_access_token(self) -> str:
        """带内存缓存；留 5 分钟安全边界，避免"刚好过期"的边界抖动。"""
        if self._token and time.time() < self._token_expire_at - 300:
            return self._token
        path = "/auth/v3/tenant_access_token/internal"
        payload = _http(
            "POST", f"{OPEN_API}{path}",
            headers={"Content-Type": "application/json; charset=utf-8"},
            body={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=self.timeout,
        )
        if payload.get("code") != 0:
            raise FeishuError(path, payload.get("code", -1),
                              payload.get("msg", "") or payload.get("_text", ""), payload)
        self._token = payload["tenant_access_token"]
        self._token_expire_at = time.time() + int(payload.get("expire", 7200))
        return self._token

    # ---------------- 文档 ----------------

    def create_document(self, title: str, folder_token: Optional[str] = None) -> str:
        """新建 docx，返回 document_id（它同时也是根块的 block_id）。"""
        body: Dict[str, Any] = {"title": title}
        if folder_token:
            body["folder_token"] = folder_token
        data = self._request("POST", "/docx/v1/documents", body=body)
        return data["document"]["document_id"]

    def add_blocks(self, document_id: str, blocks: List[Dict[str, Any]],
                   index: int = 0) -> int:
        """
        把 blocks 追加到根块下。自动按 50 个/批切分。
        返回下一批应该使用的 index（调用方可忽略）。
        """
        cur = index
        for i in range(0, len(blocks), MAX_BLOCKS_PER_CALL):
            chunk = blocks[i:i + MAX_BLOCKS_PER_CALL]
            self._request(
                "POST",
                f"/docx/v1/documents/{document_id}/blocks/{document_id}/children",
                params={"document_revision_id": -1},
                body={"index": cur, "children": chunk},
            )
            cur += len(chunk)
        return cur

    def set_link_share(self, document_id: str, entity: str = "anyone_readable") -> None:
        """
        默认设为"获得链接的人可阅读"。原因：本应用属于飞书自动创建的默认组织，
        文档 owner 是应用而非用户本人，tenant_editable（仅组织内可读）会导致
        用户用自己的账号打开时报"找不到该文件"。
        entity 可选：tenant_readable / tenant_editable / anyone_readable / anyone_editable / closed
        失败不致命 —— 有些租户不支持这个设置，只打日志继续。
        """
        try:
            self._request(
                "PATCH",
                f"/drive/v1/permissions/{document_id}/public",
                params={"type": "docx"},
                body={"link_share_entity": entity},
            )
        except FeishuError as e:
            print(f"[warn] 设置链接分享权限失败（不影响文档写入）：{e}")

    @staticmethod
    def doc_url(document_id: str) -> str:
        return f"https://feishu.cn/docx/{document_id}"

    # ---------------- 诊断用（定位"文档打不开"类问题）----------------

    def file_meta(self, document_id: str) -> Dict[str, Any]:
        """取文件元信息。with_url=True 会返回飞书**官方**的链接（含正确的租户域名）。"""
        data = self._request(
            "POST", "/drive/v1/metas/batch_query",
            body={"request_docs": [{"doc_token": document_id, "doc_type": "docx"}],
                  "with_url": True},
        )
        metas = data.get("metas") or []
        return metas[0] if metas else {}

    def official_url(self, document_id: str) -> str:
        """优先用官方链接；拿不到再退回手拼。手拼的 https://feishu.cn/docx/xxx 在
        一个账号属于多个租户时会解析到错误租户，正是"找不到该文件"的常见成因。"""
        try:
            meta = self.file_meta(document_id)
            if meta.get("url"):
                return meta["url"]
        except FeishuError:
            pass
        return self.doc_url(document_id)

    def public_permission(self, document_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/drive/v1/permissions/{document_id}/public",
                             params={"type": "docx"})

    def list_members(self, document_id: str) -> List[Dict[str, Any]]:
        data = self._request("GET", f"/drive/v1/permissions/{document_id}/members",
                             params={"type": "docx"})
        return data.get("items") or []

    def add_member(self, document_id: str, member_id: str, member_type: str = "openid",
                   perm: str = "full_access") -> None:
        """把某个用户/群加成协作者。member_type: openid / unionid / email / openchat / opendepartmentid"""
        self._request(
            "POST", f"/drive/v1/permissions/{document_id}/members",
            params={"type": "docx", "need_notification": "false"},
            body={"member_type": member_type, "member_id": member_id, "perm": perm},
        )

    # ---------------- 组装好的入口 ----------------

    def publish_markdown(self, title: str, markdown: str,
                         folder_token: Optional[str] = None) -> str:
        """一行搞定：新建文档 → 写内容 → 开分享 → 返回可打开的 URL。"""
        doc_id = self.create_document(title, folder_token=folder_token)
        blocks = markdown_to_blocks(markdown)
        self.add_blocks(doc_id, blocks, index=0)
        self.set_link_share(doc_id)
        return self.official_url(doc_id)


# ============================================================
# Markdown → docx blocks（只用我们真正需要的那几种块）
# ============================================================
def _text_elements(content: str) -> List[Dict[str, Any]]:
    """
    解析行内 **加粗** 与 [文字](链接)，产出 text_run 数组。

    ⚠️ 链接是硬需求：文档里所有"网络搜集信息"都必须能点回原文，
    否则无法人工复核，可信度分级就成了一句空话。
    飞书要求 link.url 做 percent-encode，中文 URL 不转码会直接 400。
    """
    from urllib.parse import quote

    elements: List[Dict[str, Any]] = []
    buf = ""
    i = 0
    while i < len(content):
        if content.startswith("**", i):
            end = content.find("**", i + 2)
            if end != -1:
                if buf:
                    elements.append({"text_run": {"content": buf}})
                    buf = ""
                elements.append({"text_run": {
                    "content": content[i + 2:end],
                    "text_element_style": {"bold": True},
                }})
                i = end + 2
                continue
        if content.startswith("[", i):
            m = re.match(r"\[([^\]]+)\]\(([^)]+)\)", content[i:])
            if m:
                if buf:
                    elements.append({"text_run": {"content": buf}})
                    buf = ""
                elements.append({"text_run": {
                    "content": m.group(1),
                    "text_element_style": {"link": {"url": quote(m.group(2), safe=":/?&=#%")}},
                }})
                i += m.end()
                continue
        buf += content[i]
        i += 1
    if buf:
        elements.append({"text_run": {"content": buf}})
    if not elements:
        elements.append({"text_run": {"content": ""}})
    return elements


def _make_block(block_type: int, content: str) -> Dict[str, Any]:
    key = _KEY_OF_TYPE[block_type]
    if block_type == BT_DIVIDER:
        return {"block_type": block_type, "divider": {}}
    block: Dict[str, Any] = {"block_type": block_type, key: {"elements": _text_elements(content)}}
    if block_type == BT_CODE:
        block[key]["style"] = {"language": 1, "wrap": True}
    return block


def markdown_to_blocks(markdown: str) -> List[Dict[str, Any]]:
    """
    极简 Markdown → blocks。支持：# / ## / ### 标题、- 或 * 无序、1. 有序、
    > 引用、``` 代码块、--- 分隔线，其余按普通段落。
    每个非代码行独立成块；空行忽略（飞书的块自带段间距，不需要空块占位）。
    """
    blocks: List[Dict[str, Any]] = []
    in_code = False
    code_buf: List[str] = []

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()

        if line.strip().startswith("```"):
            if not in_code:
                in_code, code_buf = True, []
            else:
                in_code = False
                blocks.append(_make_block(BT_CODE, "\n".join(code_buf)))
            continue
        if in_code:
            code_buf.append(raw_line)
            continue

        s = line.strip()
        if not s:
            continue
        if s in ("---", "***", "___"):
            blocks.append(_make_block(BT_DIVIDER, ""))
        elif s.startswith("### "):
            blocks.append(_make_block(BT_H3, s[4:]))
        elif s.startswith("## "):
            blocks.append(_make_block(BT_H2, s[3:]))
        elif s.startswith("# "):
            blocks.append(_make_block(BT_H1, s[2:]))
        elif s.startswith("- ") or s.startswith("* "):
            blocks.append(_make_block(BT_BULLET, s[2:]))
        elif s.startswith("> "):
            blocks.append(_make_block(BT_QUOTE, s[2:]))
        else:
            # 形如 "1. xxx" / "12. xxx" 视为有序列表
            dot = s.find(". ")
            if 0 < dot <= 3 and s[:dot].isdigit():
                blocks.append(_make_block(BT_ORDERED, s[dot + 2:]))
            else:
                blocks.append(_make_block(BT_TEXT, s))

    if in_code and code_buf:      # 代码块没闭合，兜底收尾
        blocks.append(_make_block(BT_CODE, "\n".join(code_buf)))
    return blocks


# ============================================================
# 自检：--check 只验鉴权；--smoke 真建一篇文档（会留下垃圾文档，需手动删）
# ============================================================
if __name__ == "__main__":
    import sys

    arg = sys.argv[1] if len(sys.argv) > 1 else "--check"
    c = FeishuClient()

    if arg == "--check":
        t = c.tenant_access_token()
        print(f"[鉴权] 成功，token 前缀 {t[:12]}...（长度 {len(t)}）")
        print("[结论] app_id / app_secret 有效，应用已发布且能换取 tenant_access_token")
    elif arg == "--smoke":
        md = (
            "# 飞书写入连通性自检\n\n"
            "如果你能看到这段文字，说明**模块5 打通了**。\n\n"
            "## 二级标题\n"
            "- 无序项 1\n"
            "- 无序项 2（**加粗**）\n\n"
            "1. 有序项 1\n"
            "2. 有序项 2\n\n"
            "> 引用块测试\n\n"
            "---\n\n"
            "```\ncode block test\n```\n"
        )
        url = c.publish_markdown("【自检】interview_agent 飞书写入测试", md)
        print(f"[文档] 已创建：{url}")
        print("[请操作] 在飞书里打开上面的链接，确认：1) 能打开 2) 内容完整 3) 自己可编辑")
    elif arg == "--diag":
        target = sys.argv[2] if len(sys.argv) > 2 else ""

        def show(tag: str, payload: Any) -> None:
            # ensure_ascii=True：中文转义成 \uXXXX，绕开 Windows GBK 控制台
            print(f"{tag} = " + json.dumps(payload, ensure_ascii=True))

        show("[0] 租户信息 /tenant/v2/tenant/query", c.raw("GET", "/tenant/v2/tenant/query"))

        if not target:
            created = c.raw("POST", "/docx/v1/documents", body={"title": "【诊断】权限探针（可删）"})
            show("[1] 建文档原始响应 /docx/v1/documents", created)
            target = ((created.get("data") or {}).get("document") or {}).get("document_id", "")

        print(f"[2] document_id = {target}")
        print(f"[3] 手拼 url    = {FeishuClient.doc_url(target)}")
        show("[4] 读文档 /docx/v1/documents/{id}", c.raw("GET", f"/docx/v1/documents/{target}"))
        show("[5] 文件元信息 /drive/v1/metas/batch_query",
             c.raw("POST", "/drive/v1/metas/batch_query",
                   body={"request_docs": [{"doc_token": target, "doc_type": "docx"}],
                         "with_url": True}))
        show("[6] 链接分享设置 /drive/v1/permissions/{id}/public",
             c.raw("GET", f"/drive/v1/permissions/{target}/public", params={"type": "docx"}))
        show("[7] 协作者列表 /drive/v1/permissions/{id}/members",
             c.raw("GET", f"/drive/v1/permissions/{target}/members", params={"type": "docx"}))
        show("[8] 应用根目录 /drive/explorer/v2/root_folder/meta",
             c.raw("GET", "/drive/explorer/v2/root_folder/meta"))
        show("[9] 应用根目录文件列表 /drive/v1/files",
             c.raw("GET", "/drive/v1/files", params={"page_size": 50}))
    else:
        print("用法：python feishu_client.py [--check|--smoke|--diag [document_id]]")
