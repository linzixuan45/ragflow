#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAGFlow 检索（query）冒烟测试脚本。

用途：探测运行中的 RAGFlow 实例（默认 http://127.0.0.1:9380）检索接口的可用性。
覆盖 `POST /api/v1/retrieval` 与 `POST /api/v1/datasets/<dataset_id>/search` 两个端点，
断言业务返回 `code=0` 且 `data.chunks` 非空。脚本只读，不修改业务数据。

依赖：仅 Python 3.10+ 标准库。鉴权使用 RAGFlow 颁发的 API key（ragflow- 前缀），
通过环境变量 `RAGFLOW_API_KEY` 注入；调用 `GET /api/v1/users/me` 验证 key 有效。

使用：
  RAGFLOW_API_KEY=ragflow-xxx python3 test/docker_test/test_query.py
  RAGFLOW_DATASET_ID=<id> RAGFLOW_API_KEY=ragflow-xxx python3 test/docker_test/test_query.py
  RAGFLOW_QUERY='VPN 申请' RAGFLOW_API_KEY=ragflow-xxx python3 test/docker_test/test_query.py

环境变量：
  RAGFLOW_API_KEY       必填；RAGFlow 颁发的 API key（ragflow- 前缀）
  RAGFLOW_HOST          默认 http://127.0.0.1:9380
  RAGFLOW_DATASET_ID    数据集 ID（可选；未设置时取 GET /datasets 首项）
  RAGFLOW_QUERY         检索 query（可选；默认 "VPN 申请"）

退出码：0 = 全部通过；1 = 至少一项失败；2 = 配置/环境错误。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

HOST = os.environ.get("RAGFLOW_HOST", "http://127.0.0.1:9380")
API_KEY = os.environ.get("RAGFLOW_API_KEY", "").strip()
HINT_DATASET_ID = os.environ.get("RAGFLOW_DATASET_ID", "").strip()
QUERY = os.environ.get("RAGFLOW_QUERY", "VPN 申请").strip() or "VPN 申请"
API_PREFIX = "/api/v1"
REQUEST_TIMEOUT = 15  # 检索比冒烟端点慢，给足超时


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------

class HttpResult:
    __slots__ = ("status", "body", "raw", "error")

    def __init__(self, status: int, body: Any, raw: str, error: str | None = None):
        self.status = status
        self.body = body
        self.raw = raw
        self.error = error


def _request(method: str, path: str, *, token: str | None = None, payload: dict | None = None,
             timeout: int = REQUEST_TIMEOUT) -> HttpResult:
    url = f"{HOST}{API_PREFIX}{path}"
    headers = {"Accept": "application/json"}
    data: bytes | None = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        # ragflow- 前缀的 API key 直接作为 Authorization 值
        headers["Authorization"] = token if token.startswith("ragflow-") else f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                body = None
            return HttpResult(resp.status, body, raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = None
        return HttpResult(e.code, body, raw)
    except urllib.error.URLError as e:
        return HttpResult(0, None, "", error=str(e.reason))
    except Exception as e:  # noqa: BLE001
        return HttpResult(0, None, "", error=f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# API key 校验
# ---------------------------------------------------------------------------

def validate_api_key() -> None:
    """调 GET /users/me 验证 API key 有效；失败抛 RuntimeError。"""
    res = _request("GET", "/users/me", token=API_KEY)
    if res.error:
        raise RuntimeError(f"api_key network error: {res.error}")
    if res.status != 200 or not isinstance(res.body, dict) or res.body.get("code") != 0:
        msg = (res.body or {}).get("message", res.raw) if isinstance(res.body, dict) else res.raw
        raise RuntimeError(f"api_key invalid [{res.status}]: {msg}")


# ---------------------------------------------------------------------------
# 响应校验 / 摘要
# ---------------------------------------------------------------------------

def _ok(res: HttpResult, expect_status: int = 200) -> tuple[bool, str]:
    if res.error:
        return False, f"network: {res.error}"
    if res.status != expect_status:
        return False, f"http={res.status}"
    body = res.body
    if isinstance(body, dict) and "code" in body and body["code"] != 0:
        return False, f"code={body['code']} msg={body.get('message', '')}"
    return True, "ok"


def _summary(res: HttpResult) -> str:
    body = res.body
    if isinstance(body, dict):
        if "data" in body and body["data"] is not None:
            data = body["data"]
            if isinstance(data, dict):
                chunks = data.get("chunks")
                if isinstance(chunks, list):
                    total = data.get("total")
                    if isinstance(total, int):
                        return f"chunks={len(chunks)} total={total}"
                    return f"chunks={len(chunks)}"
                return f"keys={list(data.keys())[:5]}"
            if isinstance(data, list):
                return f"list[{len(data)}]"
        return f"code={body.get('code', '?')}"
    if isinstance(body, str):
        return f"text[{len(body)}]"
    return "raw"


# ---------------------------------------------------------------------------
# 数据集解析
# ---------------------------------------------------------------------------

def resolve_dataset_id(token: str, hint: str) -> tuple[str, str]:
    """确定检索目标 dataset_id。返回 (dataset_id, source_label)。

    优先级：env 显式指定 > GET /datasets 首项。失败时 dataset_id 为空串。
    """
    if hint:
        return hint, "env"

    res = _request("GET", "/datasets?page=1&page_size=1", token=token)
    ok, detail = _ok(res)
    if not ok:
        return "", f"list-failed: {detail}"
    body = res.body
    if not isinstance(body, dict):
        return "", "list-bad-shape"
    data = body.get("data")
    if not isinstance(data, list) or not data:
        return "", "list-empty"
    first = data[0]
    if not isinstance(first, dict) or not first.get("id"):
        return "", "list-no-id"
    return str(first["id"]), "list-first"


# ---------------------------------------------------------------------------
# 检索用例
# ---------------------------------------------------------------------------

def _extract_chunks(body: Any) -> tuple[list | None, int | None]:
    """从 /retrieval 或 /datasets/<id>/search 响应中取出 chunks 与 total。"""
    if not isinstance(body, dict):
        return None, None
    data = body.get("data")
    if not isinstance(data, dict):
        return None, None
    chunks = data.get("chunks")
    total = data.get("total")
    if isinstance(chunks, list):
        return chunks, total if isinstance(total, int) else None
    return None, None


def case_retrieval(token: str, dataset_ids: list[str], question: str) -> tuple[bool, str]:
    res = _request(
        "POST",
        "/retrieval",
        token=token,
        payload={
            "question": question,
            "dataset_ids": dataset_ids,
            "top_k": 8,
            "similarity_threshold": 0.2,
        },
    )
    ok, detail = _ok(res)
    if not ok:
        return False, detail
    chunks, total = _extract_chunks(res.body)
    if chunks is None:
        return False, "data.chunks missing"
    if len(chunks) == 0:
        return False, f"chunks=0 total={total}"
    return True, f"chunks={len(chunks)} total={total}"


def case_dataset_search(token: str, dataset_id: str, question: str) -> tuple[bool, str]:
    res = _request(
        "POST",
        f"/datasets/{dataset_id}/search",
        token=token,
        payload={
            "question": question,
            "top_k": 8,
            "similarity_threshold": 0.2,
        },
    )
    ok, detail = _ok(res)
    if not ok:
        return False, detail
    chunks, total = _extract_chunks(res.body)
    if chunks is None:
        return False, "data.chunks missing"
    if len(chunks) == 0:
        return False, f"chunks=0 total={total}"
    return True, f"chunks={len(chunks)} total={total}"


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def main() -> int:
    if not API_KEY:
        print("[FATAL] RAGFLOW_API_KEY 未设置（需 ragflow- 前缀的 API key）", file=sys.stderr)
        return 2

    print(f"[INFO] RAGFlow host:    {HOST}")
    print(f"[INFO] API key prefix:  {API_KEY[:8]}…")
    print(f"[INFO] Query:           {QUERY!r}")
    if HINT_DATASET_ID:
        print(f"[INFO] Dataset id hint: {HINT_DATASET_ID} (from RAGFLOW_DATASET_ID)")
    else:
        print("[INFO] Dataset id:      <auto: first from GET /datasets>")

    # 验证 API key
    print("\n[STEP] api_key validation")
    t0 = time.perf_counter()
    try:
        validate_api_key()
    except RuntimeError as e:
        print(f"  [FAIL] api_key: {e}")
        return 1
    print(f"  [PASS] api_key ({time.perf_counter() - t0:.2f}s)")

    # 解析 dataset_id
    dataset_id, src = resolve_dataset_id(API_KEY, HINT_DATASET_ID)
    if not dataset_id:
        print(f"\n[FATAL] 无法确定 dataset_id: {src}", file=sys.stderr)
        return 2
    print(f"\n[INFO] Resolved dataset_id={dataset_id} via {src}")

    # 检索用例
    print("\n[STEP] retrieval probes")
    cases = [
        ("retrieval", "POST", f"/retrieval dataset_ids=[{dataset_id}] q={QUERY!r}",
         lambda: case_retrieval(API_KEY, [dataset_id], QUERY)),
        ("dataset_search", "POST", f"/datasets/{dataset_id}/search q={QUERY!r}",
         lambda: case_dataset_search(API_KEY, dataset_id, QUERY)),
    ]

    passed = 1  # api_key 验证已通过
    total = 1
    failures: list[str] = []
    for name, method, label, runner in cases:
        total += 1
        t0 = time.perf_counter()
        try:
            ok, detail = runner()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"exception: {type(e).__name__}: {e}"
        dt = time.perf_counter() - t0
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] {name:14s} {method:6s} {label}  ({dt:.2f}s)  {detail}")
        if ok:
            passed += 1
        else:
            failures.append(f"{name}: {detail}")

    # 汇总
    print("\n[SUMMARY]")
    print(f"  api_key: 1/1")
    print(f"  search:  {passed - 1}/{total - 1}")
    print(f"  total:   {passed}/{total}")

    if failures:
        print("\n[FAILURES]")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
