"""Minimal MCP server (stdio, JSON-RPC 2.0, newline-delimited) that exposes the app's tools to Claude Code. It only talks to the local app
over HTTP with a one-run token; when Claude is off the app refuses every call."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from .tools import TOOL_SPECS

API = os.environ.get("TT_API", "http://127.0.0.1:8080")
TOKEN = os.environ.get("TT_TOKEN", "")


def call_app(name: str, args: dict):
    req = urllib.request.Request(f"{API}/api/claude/tool/{name}", method="POST", data=json.dumps(args or {}).encode(), headers={"Content-Type": "application/json", "X-TT-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}"), False
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read()).get("detail", f"HTTP {e.code}")}, True
        except Exception:
            return {"error": f"HTTP {e.code}"}, True
    except (urllib.error.URLError, OSError) as e:
        return {"error": f"The app is not reachable: {e}"}, True


def handle(msg: dict):
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        pv = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": pv, "capabilities": {"tools": {}}, "serverInfo": {"name": "tt", "version": "1.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["schema"]} for t in TOOL_SPECS]}}
    if method == "tools/call":
        p = msg.get("params") or {}
        data, is_err = call_app(p.get("name", ""), p.get("arguments") or {})
        if isinstance(data, dict) and data.get("error"):
            is_err = True
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)[:30000]}], "isError": is_err}}
    if mid is None:
        return None                                     # notification (e.g. notifications/initialized)
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except Exception as e:  # noqa
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
