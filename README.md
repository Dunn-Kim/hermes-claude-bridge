# claude-bridge — hermes 를 로컬 Claude Code 로 태우는 브리지

hermes 가 OpenAI 호환 엔드포인트라고 믿는 로컬 서버를 세우고, 받은 요청을 **로컬 `claude`
바이너리**로 실행해 결과만 돌려준다. Anthropic API 를 부르지 않는다.

```
hermes ──HTTP(OpenAI 호환)──> 127.0.0.1:8789 ──subprocess──> claude -p ──> Claude Code 구독
```

## 왜 필요한가 — 버킷이 다르다

hermes 는 자기 자격으로 `api.anthropic.com` 을 직접 부른다. 그건 **Anthropic API 의
extra-usage 크레딧**을 쓰고, 그게 소진되면 에이전트가 통째로 죽는다. 반면 `claude -p` 는
**Claude Code 구독 엔타이틀먼트**라는 별개 버킷을 쓴다.

2026-08-20 **같은 시각** 실측:

| 경로 | 결과 |
|---|---|
| `hermes -z` (구독 토큰으로 Anthropic API 직접 호출) | `HTTP 400 You're out of extra usage` |
| `claude -p` (같은 토큰, Claude Code 경로) | 정상 |

그래서 "토큰을 바꾼다" 로는 안 되고 **실행 주체를 `claude` 바이너리로 옮겨야** 한다.
이 브리지가 그 일을 한다.

## 한계 — hermes 도구는 넘어가지 않는다

hermes 는 요청에 함수 19개(`terminal`·`memory`·`web_search`…)를 싣고 `tool_calls` 를
기다린다. 그런데 **`claude -p` 는 도구 호출을 되돌려주지 않고 자기가 실행한다** — 호출만
뱉고 멈추는 모드가 없다. 그래서 브리지는 hermes 도구를 무시하고 **Claude Code 자신의
도구**로 일을 시킨다. 일은 되지만 주체가 다르다.

시스템 프롬프트는 그대로 넘기되 "거기 적힌 도구 목록은 이 실행에 없다" 는 정정을 덧붙인다.
안 그러면 모델이 있지도 않은 `terminal` 을 부르려 든다(실측으로 정정문 유효 확인).

hermes 도구를 되살리려면 그것들을 MCP 서버로 노출해 `--mcp-config` 로 물려야 한다. 별건이다.

## 설치

```bash
# 1. launchd 에이전트 설치 (상시 기동)
cp ~/.hermes/claude-bridge/ai.hermes.claude-bridge.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.claude-bridge.plist

# 2. 확인
curl -s http://127.0.0.1:8789/v1/models
tail -f ~/.hermes/logs/claude-bridge.log
```

수동 기동(디버깅용):
```bash
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/claude-bridge/server.py
```

## hermes 를 브리지로 돌리기

`~/.hermes/config.yaml`:
```yaml
model:
  default: claude-code-local
  provider: custom
  base_url: 'http://127.0.0.1:8789/v1'
```
반영은 `hermes gateway restart` 후부터다(설정은 기동 시 읽는다).

일회성 확인은 설정을 안 건드리고:
```bash
CUSTOM_BASE_URL=http://127.0.0.1:8789/v1 hermes -z '2 더하기 3은?' --provider custom -m claude-code-local
```

## 설정 (환경변수)

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `CLAUDE_BRIDGE_PORT` | `8789` | 리슨 포트 |
| `CLAUDE_BRIDGE_HOST` | `127.0.0.1` | **루프백 고정 권장.** hermes 는 루프백 base_url 만 신뢰한다 |
| `CLAUDE_BRIDGE_MODEL` | `claude-opus-5` | 실제로 부를 모델 |
| `CLAUDE_BRIDGE_MODEL_ID` | `claude-code-local` | hermes 에 보여줄 모델 이름 |
| `CLAUDE_BRIDGE_EFFORT` | `high` | `claude --effort` |
| `CLAUDE_BRIDGE_TOOLS` | `Read,Glob,Grep` | `claude --tools`. 아래 보안 항목 참고 |
| `CLAUDE_BRIDGE_TIMEOUT` | `900` | 한 요청의 상한(초). `--max-turns` 가 없어 시간으로만 끊는다 |
| `CLAUDE_BRIDGE_CONCURRENCY` | `2` | 동시 `claude` 프로세스 수 |
| `CLAUDE_BRIDGE_WORKDIR` | `$HOME` | `claude` 의 cwd. 읽기 봉쇄가 여기로 걸린다 |

## 보안 — 기본이 읽기 전용인 이유

이 서버로 들어오는 지시문은 **Telegram 등 외부에서 온 사용자 입력**이다. 그게 곧바로 로컬
셸이 되면 안 되므로 기본 도구는 `Read,Glob,Grep` 이다. 읽기 범위도 `CLAUDE_BRIDGE_WORKDIR`
(기본 `$HOME`)로 봉쇄된다 — `--add-dir` 를 주지 않는다.

쓰기·실행이 필요하면 `CLAUDE_BRIDGE_TOOLS` 를 명시적으로 넓힌다(예:
`Read,Glob,Grep,Edit,Write,Bash`). **넓히는 순간 외부 메시지가 로컬 실행 권한을 얻는다**는
점을 알고 하는 것이다.

`--safe-mode` 로 돌아 CLAUDE.md·훅·플러그인·스킬·MCP 는 꺼져 있다. 이 실행의 규칙은
hermes 가 보낸 시스템 프롬프트 하나여야 한다.

## 인증

`CLAUDE_CODE_OAUTH_TOKEN` 을 `~/.hermes/aline_claude_oauth_token` 에서 읽어 자식 env 로만
넘긴다(argv 금지 — 프로세스 테이블에 노출된다). 환경의 `ANTHROPIC_*` 는 지운다. 토큰 갱신은
`claude setup-token`. 파일이 없으면 CLI 저장 자격으로 넘어가지만 launchd 아래서는 Keychain
이 막힐 수 있어 기동 시 경고를 낸다.

## 관측

- 서버 로그: `~/.hermes/logs/claude-bridge.log` (요청/소요/턴수/비용 한 줄씩)
- 사용량 JSONL: `~/.hermes/logs/claude-bridge-usage.jsonl`
  (`auth` 출처 포함 — 토큰 파일이 사라져 조용히 갈아탄 걸 사후에 잡으려고)
- `cost_usd` 는 CLI 가 보고한 **API 정가 환산**이다. 구독 실행이라 청구액이 아니다.

## 지원 엔드포인트

`GET /v1/models`, `GET /v1/models/{id}`, `POST /v1/chat/completions`(stream / non-stream,
`stream_options.include_usage` 지원). hermes 가 기동 시 찔러보는 Ollama 계열
(`/api/v1/models`, `/api/tags`, `/api/show`)도 최소 응답을 준다.
