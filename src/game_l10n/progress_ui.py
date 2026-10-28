from __future__ import annotations

import copy
import hmac
import json
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, TextIO
from urllib.parse import urlparse


PROGRESS_SCHEMA = "parr0t.processing-progress/v1"
UI_SCHEMA = "parr0t.processing-progress-ui/v1"
PROGRESS_STAGES = {
    "starting",
    "context",
    "translation",
    "translation_fallback",
    "review",
    "review_fallback",
    "finalizing",
    "complete",
    "failed",
}
STAT_FIELDS = {
    "total_records",
    "completed_records",
    "translation_completed",
    "review_completed",
    "fallback_pending",
    "failed_records",
    "source_preserved",
    "cached_records",
    "attempts",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProcessingProgress:
    """Thread-safe, text-free progress state for one semantic operation."""

    def __init__(self, *, operation: str, total_records: int):
        if not operation or not operation.replace("_", "").isalnum():
            raise ValueError("Progress operation must be a categorical identifier")
        if not isinstance(total_records, int) or isinstance(total_records, bool) or total_records < 0:
            raise ValueError("Progress total_records must be a non-negative integer")
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._state: dict[str, Any] = {
            "schema": PROGRESS_SCHEMA,
            "status": "running",
            "operation": operation,
            "stage": "starting",
            "started_at_utc": _utc_now(),
            "updated_at_utc": _utc_now(),
            "statistics": {
                "total_records": total_records,
                "completed_records": 0,
                "translation_completed": 0,
                "review_completed": 0,
                "fallback_pending": 0,
                "failed_records": 0,
                "source_preserved": 0,
                "cached_records": 0,
                "attempts": 0,
            },
        }

    def update(self, *, stage: str | None = None, **statistics: int) -> None:
        if stage is not None and stage not in PROGRESS_STAGES:
            raise ValueError("Progress stage is unsupported")
        if set(statistics) - STAT_FIELDS:
            raise ValueError("Progress update contains unsupported statistics")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in statistics.values()
        ):
            raise ValueError("Progress statistics must be non-negative integers")
        with self._lock:
            if stage is not None:
                self._state["stage"] = stage
            self._state["statistics"].update(statistics)
            total = self._state["statistics"]["total_records"]
            completed = min(total, self._state["statistics"]["completed_records"])
            self._state["statistics"]["completed_records"] = completed
            self._state["statistics"]["pending_records"] = max(0, total - completed)
            self._state["statistics"]["completion_percent"] = (
                round(completed * 100.0 / total, 2) if total else 100.0
            )
            self._state["updated_at_utc"] = _utc_now()

    def apply_event(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            raise ValueError("Progress event must be an object")
        stage = event.get("stage")
        statistics = {key: value for key, value in event.items() if key in STAT_FIELDS}
        if set(event) - ({"stage"} | STAT_FIELDS):
            raise ValueError("Progress event contains non-statistical detail")
        self.update(stage=stage, **statistics)

    def finish(self, *, failed: bool = False) -> None:
        with self._lock:
            self._state["status"] = "failed" if failed else "complete"
            self._state["stage"] = "failed" if failed else "complete"
            self._state["updated_at_utc"] = _utc_now()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = copy.deepcopy(self._state)
        payload["elapsed_seconds"] = round(max(0.0, time.monotonic() - self._started), 1)
        statistics = payload["statistics"]
        total = statistics["total_records"]
        completed = min(total, statistics["completed_records"])
        statistics.setdefault("pending_records", max(0, total - completed))
        statistics.setdefault(
            "completion_percent", round(completed * 100.0 / total, 2) if total else 100.0
        )
        return payload


HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Parr0t 처리 현황</title><style>
body{font-family:system-ui,sans-serif;background:#111827;color:#f9fafb;margin:0;padding:2rem}
main{max-width:760px;margin:auto}.card{background:#1f2937;border-radius:16px;padding:1.25rem;margin:1rem 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem}
.value{font-size:1.8rem;font-weight:700}.label{color:#9ca3af}.bar{height:14px;background:#374151;border-radius:9px;overflow:hidden}
.fill{height:100%;background:#22c55e;width:0}small{color:#9ca3af}</style></head><body><main>
<h1>Parr0t 처리 현황</h1><p id="state">연결 중…</p><div class="bar"><div class="fill" id="bar"></div></div>
<div class="grid" id="stats"></div><div class="card"><small>이 화면은 처리 건수와 단계만 표시하며 원문·번역문·대사 식별자는 표시하지 않습니다.</small></div>
</main><script>
const token=location.hash.slice(1);const labels={total_records:'전체',completed_records:'완료',pending_records:'대기',translation_completed:'번역 완료',review_completed:'검수 완료',fallback_pending:'폴백 대기',failed_records:'실패',source_preserved:'원문 유지',cached_records:'캐시 재사용',attempts:'처리 시도'};
async function refresh(){try{const r=await fetch('/api/status',{headers:{'X-Parr0t-Token':token},cache:'no-store'});if(!r.ok)throw Error('status');const p=await r.json();document.getElementById('state').textContent=`${p.operation} · ${p.stage} · ${p.status} · ${p.elapsed_seconds}초`;document.getElementById('bar').style.width=(p.statistics.completion_percent||0)+'%';document.getElementById('stats').innerHTML=Object.entries(labels).map(([k,v])=>`<div class="card"><div class="value">${p.statistics[k]??0}</div><div class="label">${v}</div></div>`).join('');}catch(e){document.getElementById('state').textContent='처리 종료 또는 연결 해제';}}
refresh();setInterval(refresh,750);
</script></body></html>"""


class _ProgressHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, progress: ProcessingProgress, token: str):
        # Port zero delegates random available-port selection to the OS.
        super().__init__(("127.0.0.1", 0), _ProgressHandler)
        self.progress = progress
        self.token = token


class _ProgressHandler(BaseHTTPRequestHandler):
    server: _ProgressHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send(HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path != "/api/status":
            self._send(b'{"error":"not_found"}', "application/json", HTTPStatus.NOT_FOUND)
            return
        supplied = self.headers.get("X-Parr0t-Token", "")
        if not supplied or not hmac.compare_digest(supplied, self.server.token):
            self._send(b'{"error":"unauthorized"}', "application/json", HTTPStatus.UNAUTHORIZED)
            return
        body = json.dumps(self.server.progress.snapshot(), ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8")


@contextmanager
def live_processing_ui(
    *, operation: str, total_records: int, stream: TextIO | None = None
) -> Iterator[ProcessingProgress]:
    """Run a random-port loopback statistics UI for exactly one operation."""

    progress = ProcessingProgress(operation=operation, total_records=total_records)
    token = secrets.token_urlsafe(32)
    server = _ProgressHTTPServer(progress, token)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
    thread.start()
    address, port = server.server_address[:2]
    output = stream if stream is not None else sys.stderr
    print(
        json.dumps(
            {
                "schema": UI_SCHEMA,
                "status": "serving",
                "url": f"http://{address}:{port}/#{token}",
                "content_policy": "aggregate_statistics_only",
            },
            ensure_ascii=False,
        ),
        file=output,
        flush=True,
    )
    failed = False
    try:
        yield progress
    except BaseException:
        failed = True
        raise
    finally:
        progress.finish(failed=failed)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
