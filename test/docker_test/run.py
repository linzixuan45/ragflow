#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAGFlow 部署后冒烟测试脚本。

用途：探测运行中的 RAGFlow 实例（默认 http://127.0.0.1:9380）对外 HTTP API 的可达性
与基本行为，输出按域分组的 PASS/FAIL 汇总。脚本只读、不修改业务数据。

依赖：仅 Python 3.10+ 标准库，外加 `cryptography`（用于 RSA 加密登录密码）。运行时
需要能从项目根目录读到 `conf/public.pem`，优先复用 `api/utils/crypt.py::crypt()`。

使用：
  python3 test/docker_test/run.py                       # 默认配置：交互式输入密码
  RAGFLOW_PASSWORD=xxx python3 test/docker_test/run.py # 非交互
  RAGFLOW_HOST=http://host:9380 python3 test/docker_test/run.py

退出码：0 = 全部通过；1 = 至少一项失败；2 = 配置/环境错误。
"""

import base64
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

HOST = os.environ.get("RAGFLOW_HOST", "http://127.0.0.1:9380")
EMAIL = os.environ.get("RAGFLOW_EMAIL", "admin@ragflow.io")
PASSWORD = os.environ.get("RAGFLOW_PASSWORD", "")
API_PREFIX = "/api/v1"
REQUEST_TIMEOUT = 10  # 秒

# 项目根目录（脚本位于 <root>/test/docker_test/）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONF_DIR = PROJECT_ROOT / "conf"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _encrypt_password(password: str) -> str:
    """对登录密码做 RAGFlow 服务端期望的加密：base64 → RSA PKCS1_v1_5 → base64。

    优先复用项目内 `api.utils.crypt.crypt`（依赖 Cryptodome）；若不可用，则用
    `cryptography` 内联等价实现，仅依赖 `conf/public.pem`。
    """
    try:
        from api.utils.crypt import crypt as _rsa_encrypt  # type: ignore
        return _rsa_encrypt(password)
    except Exception:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        pub_pem = (CONF_DIR / "public.pem").read_bytes()
        pub_key = serialization.load_pem_public_key(pub_pem)
        encoded = base64.b64encode(password.encode("utf-8"))
        ciphertext = pub_key.encrypt(encoded, padding.PKCS1v15())
        return base64.b64encode(ciphertext).decode("utf-8")


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
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
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
# 登录
# ---------------------------------------------------------------------------

def login(email: str, password: str) -> str:
    encrypted = _encrypt_password(password)
    res = _request("POST", "/auth/login", payload={"email": email, "password": encrypted})
    if res.error:
        raise RuntimeError(f"login network error: {res.error}")
    if res.status != 200 or not isinstance(res.body, dict) or res.body.get("code") != 0:
        msg = (res.body or {}).get("message", res.raw) if isinstance(res.body, dict) else res.raw
        raise RuntimeError(f"login failed [{res.status}]: {msg}")
    token = (res.body.get("data") or {}).get("access_token")
    if not token:
        raise RuntimeError("login response missing access_token")
    return token


# ---------------------------------------------------------------------------
# 探测定义
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
            if isinstance(data, list):
                return f"list[{len(data)}]"
            if isinstance(data, dict):
                return f"keys={list(data.keys())[:5]}"
        return f"code={body.get('code', '?')}"
    if isinstance(body, str):
        return f"text[{len(body)}]"
    return "raw"


# 端点表：(域, 方法, 路径, 是否需要鉴权, 期望状态码, 摘要校验函数)
# expect_status 不为 None 时严格匹配；为 None 时只要求 2xx/3xx
CHECKS: list[tuple[str, str, str, bool, int | None, Any]] = [
    # System
    ("system", "GET", "/system/ping", False, 200, lambda r: r.raw.strip() == "pong"),
    ("system", "GET", "/system/version", False, 200, None),
    ("system", "GET", "/system/healthz", False, 200, None),
    ("system", "GET", "/system/status", True, 200, None),
    # User
    ("user", "GET", "/auth/login/channels", False, 200, None),
    ("user", "GET", "/users/me", True, 200, None),
    # Dataset
    ("dataset", "GET", "/datasets", True, 200, None),
    ("dataset", "GET", "/datasets/tags/aggregation", True, 200, None),
    # File（GET /files 无需路径参数）
    ("file", "GET", "/files", True, 200, None),
    # Memory
    ("memory", "GET", "/memories", True, 200, None),
]


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"[INFO] RAGFlow host: {HOST}")
    print(f"[INFO] Login email:  {EMAIL}")

    # 公开端点探测（无 token）— 在登录前即可确认服务在线
    print("\n[STEP] public probes")
    public_pass = 0
    public_total = 0
    for domain, method, path, needs_auth, expect, _validate in CHECKS:
        if needs_auth:
            continue
        public_total += 1
        res = _request(method, path)
        ok, detail = _ok(res, expect or 200)
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] {method:6s} {path:36s}  {detail}  {_summary(res)}")
        if ok:
            public_pass += 1

    # 登录
    password = PASSWORD
    if not password:
        try:
            password = getpass.getpass("RAGFlow admin password: ")
        except (EOFError, KeyboardInterrupt):
            print("\n[FATAL] 未提供密码", file=sys.stderr)
            return 2
    if not password:
        print("[FATAL] 密码为空", file=sys.stderr)
        return 2

    print("\n[STEP] login")
    t0 = time.perf_counter()
    try:
        token = login(EMAIL, password)
    except RuntimeError as e:
        print(f"  [FAIL] login: {e}")
        return 1
    print(f"  [PASS] login ({time.perf_counter() - t0:.2f}s), token={token[:8]}…")

    # 鉴权端点探测
    print("\n[STEP] authed probes")
    auth_pass = 0
    auth_total = 0
    failures: list[str] = []
    for domain, method, path, needs_auth, expect, validate in CHECKS:
        if not needs_auth:
            continue
        auth_total += 1
        res = _request(method, path, token=token)
        ok, detail = _ok(res, expect or 200)
        if ok and validate is not None:
            ok, detail = (validate(res), "ok" if validate(res) else "validation failed")
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] {method:6s} {path:36s}  {detail}  {_summary(res)}")
        if ok:
            auth_pass += 1
        else:
            failures.append(f"{method} {path}: {detail}")

    # 汇总
    total = public_total + auth_total + 1  # +1 for login
    passed = public_pass + auth_pass + 1
    print("\n[SUMMARY]")
    print(f"  public: {public_pass}/{public_total}")
    print(f"  login:  1/1")
    print(f"  authed: {auth_pass}/{auth_total}")
    print(f"  total:  {passed}/{total}")

    if failures:
        print("\n[FAILURES]")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
