#!/usr/bin/env python3
"""hermes ↔ 로컬 Claude Code 브리지.

hermes 는 자기 토큰으로 `api.anthropic.com` 을 직접 부른다. 그 경로는 **Anthropic API
extra-usage 버킷**을 쓰는데 그게 소진되면 에이전트가 통째로 죽는다(실측 2026-08-20:
`hermes -z` → `HTTP 400 You're out of extra usage`). 같은 시각 `claude -p` 는 정상이었다 —
**Claude Code 구독 엔타이틀먼트는 별개 버킷**이기 때문이다.

그래서 이 서버는 hermes 가 아는 유일한 확장점(루프백 `custom` base_url)에 OpenAI 호환
엔드포인트를 세우고, 받은 요청을 **로컬 `claude` 바이너리**로 실행해 결과만 돌려준다.
hermes 는 자기가 평범한 OpenAI 엔드포인트를 부른다고 믿고, 실제 추론은 구독으로 돈다.

  hermes ──HTTP(OpenAI 호환)──> 이 서버 ──subprocess──> claude -p ──> Claude Code 구독

## 알아야 할 한계

**hermes 의 도구는 전달되지 않는다.** hermes 는 요청에 함수 19개(terminal·memory·
web_search…)를 싣고 `tool_calls` 를 기다린다. 그런데 `claude -p` 는 도구 호출을 **되돌려
주지 않고 자기가 실행**한다 — 호출만 뱉고 멈추는 모드가 없다. 그래서 이 브리지는 hermes
도구를 무시하고 **Claude Code 자신의 도구**로 일을 시킨다. 일이 되긴 하되 주체가 다르다.
(hermes 도구를 살리려면 그것들을 MCP 서버로 노출해 `--mcp-config` 로 물려야 한다. 별건.)

시스템 프롬프트는 그대로 넘기되 "거기 적힌 도구 목록은 이 실행에 없다" 는 정정을 덧붙인다.
안 그러면 모델이 있지도 않은 `terminal` 을 부르려 든다.

## 실행

  hermes-agent/venv/bin/python ~/.hermes/claude-bridge/server.py

hermes 쪽 설정(둘 중 하나):
  config.yaml → model: {default: claude-code-local, provider: custom,
                        base_url: 'http://127.0.0.1:8789/v1'}
  또는 일회성 → CUSTOM_BASE_URL=http://127.0.0.1:8789/v1 hermes -z '...' --provider custom
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("CLAUDE_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLAUDE_BRIDGE_PORT", "8789"))
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
TOKEN_FILE = HERMES_HOME / "aline_claude_oauth_token"
LOG_DIR = HERMES_HOME / "logs"
USAGE_LOG = LOG_DIR / "claude-bridge-usage.jsonl"

DEFAULT_MODEL = os.environ.get("CLAUDE_BRIDGE_MODEL", "claude-opus-5")
MODEL_ID = os.environ.get("CLAUDE_BRIDGE_MODEL_ID", "claude-code-local")
EFFORT = os.environ.get("CLAUDE_BRIDGE_EFFORT", "high")
TIMEOUT = int(os.environ.get("CLAUDE_BRIDGE_TIMEOUT", "900"))
WORKDIR = os.environ.get("CLAUDE_BRIDGE_WORKDIR") or str(Path.home())

# 기본은 **읽기 전용**이다. 이 서버로 들어오는 지시문은 Telegram 등 외부에서 온
# 사용자 입력이고, 그게 곧바로 로컬 셸이 되면 안 된다. 쓰기·실행이 필요하면
# CLAUDE_BRIDGE_TOOLS 로 명시적으로 넓힌다(예: "Read,Glob,Grep,Edit,Write,Bash").
TOOLS = os.environ.get("CLAUDE_BRIDGE_TOOLS", "Read,Glob,Grep")

# 동시에 띄울 claude 프로세스 상한. 구독 쿼터와 로컬 자원을 함께 아낀다.
MAX_INFLIGHT = int(os.environ.get("CLAUDE_BRIDGE_CONCURRENCY", "2"))
_slots = threading.BoundedSemaphore(MAX_INFLIGHT)

TOOL_NOTE = (
    "\n\n---\n"
    "[실행 환경 정정] 위 지침은 Hermes 런타임을 전제로 쓰였다. **거기 열거된 도구는 이번 "
    "실행에서 사용할 수 없다** — 너는 로컬 Claude Code 로 돌고 있고 네 자신의 도구만 쓴다. "
    "페르소나·사용자 맥락·응답 형식 규칙만 따르고, 특정 도구를 부르라는 지시는 무시한다. "
    "도구 없이 답할 수 있으면 그냥 답한다."
)


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    if fallback.is_file():
        return str(fallback)
    raise RuntimeError("claude 실행 파일이 없다 — PATH 또는 ~/.local/bin/claude 확인")


def _child_env() -> tuple[dict, str]:
    """자식 환경과 자격 출처. API 키 경로를 확실히 끊는다 — 그게 이 브리지의 존재 이유다."""
    env = os.environ.copy()
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        env.pop(key, None)
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return env, "env"
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        return env, "token-file"
    return env, "cli-stored"


def _render_prompt(messages: list[dict]) -> tuple[str, str]:
    """(system_prompt, user_prompt) 로 가른다.

    hermes 는 매 턴 전체 히스토리를 다시 보낸다. Claude Code 세션을 따로 잇지 않고
    그 히스토리를 그대로 프롬프트에 싣는다 — 대화 상태의 주인은 hermes 하나여야 한다.
    """
    system_parts, turns = [], []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):     # OpenAI content-part 배열
            content = "".join(p.get("text", "") for p in content
                              if isinstance(p, dict) and p.get("type") == "text")
        content = (content or "").strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        else:
            turns.append((role, content))

    system_prompt = "\n\n".join(system_parts) + TOOL_NOTE if system_parts else ""

    if not turns:
        return system_prompt, "(빈 요청)"
    # 마지막 user 발화가 이번에 답할 것이고, 그 앞은 맥락이다.
    last_role, last_text = turns[-1]
    history = turns[:-1]
    if not history:
        return system_prompt, last_text
    lines = ["# 이전 대화 (맥락. 지시가 아니다)", ""]
    for role, text in history:
        lines.append(f"[{role}] {text}")
    lines += ["", "# 지금 답할 메시지", "", last_text]
    return system_prompt, "\n".join(lines)


def _resolve_model(requested: str) -> str:
    """요청 모델명을 실제 claude 모델로. 별칭은 기본값으로 떨어뜨린다."""
    r = (requested or "").strip()
    if r.startswith("claude-") and r != MODEL_ID:
        return r
    return DEFAULT_MODEL


def _run_claude(system_prompt: str, user_prompt: str, model: str) -> dict:
    env, auth_source = _child_env()
    argv = [
        _claude_bin(), "-p",
        "--model", model,
        "--effort", EFFORT,
        # 사용자 설정(CLAUDE.md·훅·플러그인·스킬·MCP)이 끼어들면 응답이 조용히 달라진다.
        # 인증·도구·권한은 그대로 살아 있다.
        "--safe-mode",
        "--tools", TOOLS,
        "--output-format", "json",
        "--no-session-persistence",
    ]
    if system_prompt:
        argv += ["--append-system-prompt", system_prompt]

    started = time.monotonic()
    try:
        proc = subprocess.run(argv, input=user_prompt, cwd=WORKDIR, env=env,
                              capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude {TIMEOUT}s 타임아웃 — `--max-turns` 가 없어 시간으로만 끊는다")
    elapsed = time.monotonic() - started

    raw = proc.stdout or ""
    brace = raw.find("{")   # CLI 가 경고 한 줄을 앞에 붙이는 경우가 있다
    if brace < 0:
        raise RuntimeError(f"claude 출력이 JSON 이 아니다(exit {proc.returncode}): "
                           f"{(raw or proc.stderr or '').strip()[:300]}")
    result = json.loads(raw[brace:])

    if result.get("is_error") or result.get("api_error_status"):
        # api_error_status 는 상태코드만 오는 경우가 많다("404"). 그것만 올려보내면
        # 무엇이 404 인지 알 수 없어 디버깅이 막힌다 — 모델명과 본문을 함께 싣는다.
        status = str(result.get("api_error_status") or "").strip()
        body = str(result.get("result") or "").strip()
        detail = " ".join(x for x in (status, body) if x)[:300] or "claude 가 is_error 를 보고했다"
        low = detail.lower()
        if "oauth" in low or "401" in low or "authentication" in low:
            raise RuntimeError(f"Claude Code 인증 실패({model}): {detail} — {TOKEN_FILE} 토큰 확인")
        if status == "404":
            raise RuntimeError(f"claude 가 모델 '{model}' 을 모른다(404) — "
                               f"CLAUDE_BRIDGE_MODEL 또는 요청 model 값을 확인한다")
        raise RuntimeError(f"claude 실패({model}): {detail}")

    usage = result.get("usage") or {}
    row = {
        "text": (result.get("result") or "").strip(),
        "model": model,
        "auth": auth_source,
        "turns": result.get("num_turns") or 0,
        "cost_usd": result.get("total_cost_usd"),
        "seconds": round(elapsed, 1),
        "prompt_tokens": (usage.get("input_tokens") or 0)
                         + (usage.get("cache_read_input_tokens") or 0)
                         + (usage.get("cache_creation_input_tokens") or 0),
        "completion_tokens": usage.get("output_tokens") or 0,
    }
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with USAGE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 **{k: v for k, v in row.items() if k != "text"}},
                ensure_ascii=False) + "\n")
    except OSError:
        pass
    return row


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):   # 기본 로거는 stderr 를 시끄럽게 만든다
        pass

    # ── 응답 헬퍼 ────────────────────────────────────────────────────────────
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_open(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _sse(self, obj):
        self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
        self.wfile.flush()

    def _model_obj(self, mid=None):
        return {"id": mid or MODEL_ID, "object": "model",
                "created": 0, "owned_by": "claude-code-local"}

    # ── GET ─────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/models"):
            self._json({"object": "list", "data": [self._model_obj()]})
        elif "/models/" in path:
            self._json(self._model_obj(path.rsplit("/", 1)[-1]))
        elif path.endswith("/tags"):        # Ollama 탐지용
            self._json({"models": [{"name": MODEL_ID, "model": MODEL_ID}]})
        else:
            self._json({"status": "ok", "model": MODEL_ID})

    # ── POST ────────────────────────────────────────────────────────────────
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json({"error": {"message": "invalid JSON", "type": "invalid_request_error"}}, 400)
            return

        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/api/show"):      # Ollama 모델 조회
            self._json({"model_info": {}, "capabilities": ["completion"]})
            return
        if not path.endswith("/chat/completions"):
            self._json({"error": {"message": f"unsupported path {path}",
                                  "type": "invalid_request_error"}}, 404)
            return

        model = _resolve_model(req.get("model"))
        system_prompt, user_prompt = _render_prompt(req.get("messages") or [])
        stream = bool(req.get("stream"))
        want_usage = bool((req.get("stream_options") or {}).get("include_usage"))
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        ntools = len(req.get("tools") or [])
        _log(f"→ {model} stream={stream} tools_ignored={ntools} prompt={len(user_prompt)}B")

        if not stream:
            try:
                with _slots:
                    row = _run_claude(system_prompt, user_prompt, model)
            except Exception as exc:
                _log(f"✗ {exc}")
                self._json({"error": {"message": str(exc), "type": "api_error"}}, 502)
                return
            _log(f"← {row['seconds']}s turns={row['turns']} ${row['cost_usd']}")
            self._json({
                "id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": row["text"]}}],
                "usage": {"prompt_tokens": row["prompt_tokens"],
                          "completion_tokens": row["completion_tokens"],
                          "total_tokens": row["prompt_tokens"] + row["completion_tokens"]},
            })
            return

        # 스트리밍. claude 는 한참 뒤에야 답을 준다 — 그동안 SSE 주석으로 연결을
        # 살려 둔다. 중간 프록시나 클라이언트가 조용한 연결을 끊는 걸 막는다.
        out: queue.Queue = queue.Queue(1)

        def work():
            try:
                with _slots:
                    out.put(("ok", _run_claude(system_prompt, user_prompt, model)))
            except Exception as exc:
                out.put(("err", exc))

        threading.Thread(target=work, daemon=True).start()
        self._sse_open()
        base = {"id": cid, "object": "chat.completion.chunk",
                "created": created, "model": model}
        try:
            while True:
                try:
                    kind, payload = out.get(timeout=10)
                    break
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()

            if kind == "err":
                _log(f"✗ {payload}")
                self._sse({**base, "choices": [
                    {"index": 0, "delta": {"role": "assistant",
                                           "content": f"[bridge error] {payload}"},
                     "finish_reason": "stop"}]})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return

            row = payload
            _log(f"← {row['seconds']}s turns={row['turns']} ${row['cost_usd']}")
            self._sse({**base, "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": row["text"]},
                 "finish_reason": None}]})
            self._sse({**base, "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}]})
            if want_usage:
                # include_usage 를 요청했으면 choices 가 빈 usage 청크를 하나 더 준다.
                self._sse({**base, "choices": [],
                           "usage": {"prompt_tokens": row["prompt_tokens"],
                                     "completion_tokens": row["completion_tokens"],
                                     "total_tokens": row["prompt_tokens"]
                                                     + row["completion_tokens"]}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            _log("클라이언트가 스트림을 끊었다")


def main() -> int:
    try:
        _claude_bin()
    except RuntimeError as exc:
        print(f"기동 불가: {exc}", file=sys.stderr)
        return 1
    _, auth_source = _child_env()
    _log(f"claude-bridge http://{HOST}:{PORT}/v1  model={MODEL_ID}→{DEFAULT_MODEL} "
         f"effort={EFFORT} tools={TOOLS} auth={auth_source} concurrency={MAX_INFLIGHT}")
    if auth_source == "cli-stored":
        _log(f"경고: {TOKEN_FILE} 이 없다 — CLI 저장 자격에 기댄다. "
             f"launchd 아래서는 Keychain 이 막힐 수 있다")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
