#!/usr/bin/env python3
"""AIOps Advisor - mock recommendation service for Lab 2 (CHANGE-4471).

Stdlib only. Listens on :8600 by default. Endpoints:

  GET  /healthz   -> {"status": "ok", "changes": [...], "calls": N}
  POST /analyze   -> {"recommendations": [{id, category, claim, confidence}, ...]}
                     request body (JSON):
                       {
                         "change":        "CHANGE-4471",           # optional, default CHANGE-4471
                         "plan_b64":      "<base64 of the plan file bytes>",
                         "plan_sha256":   "<hex sha256 of the same bytes>",   # optional, verified if present
                         "plan_filename": "app-tier.plan",          # optional, informational
                         "plan_summary":  {"add": 5, "change": 0, "destroy": 0}   # optional
                       }
  GET  /history   -> {"calls": [{request_id, served_at, change, plan_sha256,
                                 plan_bytes, plan_recognized, recommendation_ids}, ...]}

Every successful /analyze is appended to a JSONL audit log (env AIOPS_ADVISOR_LOG).
evaluate-lab2.sh reads /history to confirm the student's recommendations.json traces
to a real call whose plan hash matches their saved plan/app-tier.plan.

This service is deliberately NOT an oracle: it returns a fixed recommendation set
per change and never reveals which recommendations are correct. Telling them apart
is the assessed task.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 5 * 1024 * 1024
DEFAULT_CHANGE = "CHANGE-4471"
SERVED_FIELDS = ("id", "category", "claim", "confidence")

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.environ.get("AIOPS_ADVISOR_FIXTURES", os.path.join(HERE, "fixtures"))
ADDR = os.environ.get("AIOPS_ADVISOR_ADDR", "0.0.0.0")
PORT = int(os.environ.get("AIOPS_ADVISOR_PORT", "8600"))
LOG_PATH = os.environ.get("AIOPS_ADVISOR_LOG", "/var/log/aiops-advisor/requests.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_fixtures(path: str) -> dict:
    """Read every *.json fixture in `path`, keyed by its "change" field."""
    fixtures: dict = {}
    if not os.path.isdir(path):
        raise SystemExit(f"fixtures dir not found: {path}")
    for name in sorted(os.listdir(path)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(path, name), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        change = data.get("change")
        if not change:
            raise SystemExit(f"fixture {name} has no 'change' key")
        fixtures[change] = data.get("recommendations", [])
    if not fixtures:
        raise SystemExit(f"no fixtures loaded from {path}")
    return fixtures


def resolve_log_path(preferred: str) -> str:
    """Use `preferred` if its directory is writable, else fall back under $HOME."""
    for candidate in (preferred, os.path.expanduser("~/.aiops-advisor/requests.jsonl")):
        d = os.path.dirname(candidate) or "."
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write-probe")
            with open(probe, "w") as fh:
                fh.write("")
            os.remove(probe)
            return candidate
        except OSError:
            continue
    # last resort: cwd
    return os.path.abspath("aiops-advisor-requests.jsonl")


def build_recommendations(raw_list: list, summary: dict | None) -> list:
    """Turn fixture entries into served {id, category, claim, confidence} objects."""
    summary = summary or {}
    add = summary.get("add", 5)
    change = summary.get("change", 0)
    destroy = summary.get("destroy", 0)
    out = []
    for entry in raw_list:
        claim = entry.get("claim")
        if claim is None and "claim_template" in entry:
            claim = entry["claim_template"].format(add=add, change=change, destroy=destroy)
        out.append(
            {
                "id": entry["id"],
                "category": entry["category"],
                "claim": claim,
                "confidence": entry["confidence"],
            }
        )
    return out


class Advisor:
    def __init__(self, fixtures: dict, log_path: str):
        self.fixtures = fixtures
        self.log_path = log_path
        self.calls: list = []
        self._load_history()

    def _load_history(self) -> None:
        if not os.path.exists(self.log_path):
            return
        try:
            with open(self.log_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self.calls.append(json.loads(line))
        except (OSError, ValueError):
            pass

    def record(self, rec: dict) -> None:
        self.calls.append(rec)
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError as exc:  # pragma: no cover - disk trouble
            print(f"[advisor] WARNING: could not write audit log: {exc}", file=sys.stderr)

    def analyze(self, body: dict) -> tuple[int, dict]:
        change = body.get("change") or DEFAULT_CHANGE
        if change not in self.fixtures:
            return 404, {"error": f"no recommendation set for change {change!r}"}

        plan_b64 = body.get("plan_b64")
        raw = b""
        if plan_b64:
            try:
                raw = base64.b64decode(plan_b64, validate=True)
            except (ValueError, TypeError):
                return 400, {"error": "plan_b64 is not valid base64"}

        computed_sha = hashlib.sha256(raw).hexdigest() if raw else None
        claimed_sha = body.get("plan_sha256")
        if raw and claimed_sha and claimed_sha != computed_sha:
            return 400, {"error": "plan_sha256 does not match plan_b64 contents"}

        plan_recognized = raw[:4] == b"PK\x03\x04"  # terraform plan files are zip archives

        recs = build_recommendations(self.fixtures[change], body.get("plan_summary"))
        request_id = uuid.uuid4().hex
        served_at = _now()

        self.record(
            {
                "request_id": request_id,
                "served_at": served_at,
                "change": change,
                "plan_filename": body.get("plan_filename"),
                "plan_sha256": computed_sha or claimed_sha,
                "plan_bytes": len(raw),
                "plan_recognized": plan_recognized,
                "plan_summary": body.get("plan_summary"),
                "recommendation_ids": [r["id"] for r in recs],
            }
        )

        return 200, {
            "recommendations": recs,
            "meta": {
                "request_id": request_id,
                "served_at": served_at,
                "change": change,
                "plan_sha256": computed_sha,
                "note": "meta is advisory; the canonical audit trail is the service's /history",
            },
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "AIOpsAdvisor/1.0"
    advisor: Advisor = None  # set in main()

    def _send(self, code: int, obj: dict) -> None:
        payload = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[advisor] %s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/healthz":
            self._send(200, {
                "status": "ok",
                "changes": sorted(self.advisor.fixtures),
                "calls": len(self.advisor.calls),
            })
        elif path == "/history":
            self._send(200, {"calls": self.advisor.calls})
        else:
            self._send(404, {"error": f"no such path: {path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/analyze":
            self._send(404, {"error": f"no such path: {path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"error": "bad Content-Length"})
            return
        if length <= 0:
            self._send(400, {"error": "empty request body"})
            return
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": "request body too large"})
            return
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "request body is not valid JSON"})
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "request body must be a JSON object"})
            return
        code, obj = self.advisor.analyze(body)
        self._send(code, obj)


def main() -> None:
    fixtures = load_fixtures(FIXTURES_DIR)
    log_path = resolve_log_path(LOG_PATH)
    Handler.advisor = Advisor(fixtures, log_path)
    httpd = ThreadingHTTPServer((ADDR, PORT), Handler)
    print(
        f"[advisor] listening on {ADDR}:{PORT} | changes={sorted(fixtures)} | "
        f"audit log={log_path}",
        file=sys.stderr,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
