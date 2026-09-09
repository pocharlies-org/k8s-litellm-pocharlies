from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

ACTIVE_FILE = os.environ.get("LITELLM_ACTIVE_FILE", "/shared/tracking/active_requests.json")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "").strip()
PORT = int(os.environ.get("LITELLM_ACTIVE_PORT", "4001"))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


STALE_AFTER_SEC = _env_int("LITELLM_ACTIVE_STALE_AFTER_SEC", 900)


def _parse_started_at(value: Any) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, (int, float)):
        started_sec = float(value) / 1000.0 if value > 1e12 else float(value)
        return started_sec, datetime.fromtimestamp(started_sec, tz=timezone.utc).isoformat()
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None, None
        try:
            numeric = float(raw)
        except ValueError:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None, None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            return dt.timestamp(), dt.isoformat()
        started_sec = numeric / 1000.0 if numeric > 1e12 else numeric
        return started_sec, datetime.fromtimestamp(started_sec, tz=timezone.utc).isoformat()
    return None, None


def _iter_raw_requests(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and "active" in payload:
        payload = payload.get("active")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        rows: list[dict[str, Any]] = []
        for request_id, info in payload.items():
            if isinstance(info, dict):
                row = dict(info)
                row.setdefault("request_id", request_id)
                rows.append(row)
        return rows
    return []


def _normalize_alias(value: Any) -> str:
    alias = str(value or "").strip()
    if not alias:
        return "unknown"
    if alias.startswith("litellm_proxy_master"):
        return "master"
    return alias


def _normalize(payload: Any) -> list[dict[str, Any]]:
    now = time.time()
    items: list[dict[str, Any]] = []
    for raw in _iter_raw_requests(payload):
        request_id = raw.get("request_id") or raw.get("id")
        if request_id is None:
            continue
        started_raw = raw.get("ts")
        if started_raw is None:
            started_raw = raw.get("started_at")
        if started_raw is None:
            started_raw = raw.get("start_time")
        started_sec, started_iso = _parse_started_at(started_raw)
        age_seconds = int(max(0, now - started_sec)) if started_sec is not None else None
        if age_seconds is not None and age_seconds > STALE_AFTER_SEC:
            continue
        alias_raw = raw.get("key_alias")
        if alias_raw is None:
            alias_raw = raw.get("alias")
        if alias_raw is None:
            alias_raw = raw.get("key")
        alias = _normalize_alias(alias_raw)
        items.append(
            {
                "request_id": str(request_id),
                "trace_id": str(raw.get("trace_id") or "").strip() or None,
                "key_alias": alias,
                "alias": alias,
                "model": raw.get("model"),
                "call_type": raw.get("call_type"),
                "server_id": raw.get("server_id"),
                "refusal_lambda": raw.get("refusal_lambda"),
                "refusal_runtime": raw.get("refusal_runtime"),
                "prompt_tokens": raw.get("prompt_tokens"),
                "completion_tokens": raw.get("completion_tokens"),
                "output_speed_tps": raw.get("output_speed_tps"),
                "ttft_ms": raw.get("ttft_ms"),
                "started_at": started_iso,
                "age_seconds": age_seconds,
                "ts": started_sec,
            }
        )
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "LiteLLMActiveRequests/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not MASTER_KEY:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {MASTER_KEY}"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        if path != "/internal/active-requests":
            self._send_json(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        try:
            with open(ACTIVE_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except FileNotFoundError:
            payload = {}
        except Exception as exc:
            self._send_json(
                200,
                {
                    "active": [],
                    "source": "litellm-active-api",
                    "stale_after_sec": STALE_AFTER_SEC,
                    "error": str(exc),
                },
            )
            return

        self._send_json(
            200,
            {
                "active": _normalize(payload),
                "source": "litellm-active-api",
                "stale_after_sec": STALE_AFTER_SEC,
            },
        )


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"active-requests-api listening on :{PORT}", flush=True)
    server.serve_forever()
