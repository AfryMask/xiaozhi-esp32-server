# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

This is a multi-language monorepo. Everything lives under `main/`:

- `main/xiaozhi-server/` — **Python** real-time backend. WebSocket + HTTP server that ESP32 devices connect to for voice interaction. This is the runtime brain.
- `main/manager-api/` — **Java / Spring Boot 3 (JDK 21)** admin REST API. Stores users, devices, agents, models, knowledge in MySQL + Redis. Exposes Knife4j docs at `/xiaozhi/doc.html`.
- `main/manager-web/` — **Vue 2 + Element UI** admin console (智控台).
- `main/manager-mobile/` — **uni-app 3 / Vue 3 / Vite** cross-platform mobile admin (H5, iOS, Android, WeChat MiniProgram). Uses pnpm.
- `main/digital-human/` — Standalone Python test harness with a browser page for exercising the WS/OTA endpoints and the wakeword runtime.
- `docs/` — Deployment, integration, and feature documentation (Chinese-first; English in `docs/readme/`).

Top-level `Dockerfile-server` builds the Python server image; `Dockerfile-web` builds a single image with the Java API + Vue frontend served via embedded nginx.

## Two deployment modes (important)

The Python server has two operating modes controlled by `read_config_from_api` in `main/xiaozhi-server/config.yaml`:

1. **Minimal / standalone** — `read_config_from_api: false`. All model selections, secrets, and prompts come from `config.yaml`. No database. The OTA HTTP endpoint at `:8003` is served by xiaozhi-server itself.
2. **Full / database-backed** — `read_config_from_api: true`. xiaozhi-server pulls config from `manager-api`, and the OTA endpoint is served by `manager-api` (port 8002) instead. Requires MySQL + Redis (see `main/xiaozhi-server/docker-compose_all.yml`).

When debugging "why is module X not picking up my change", first check which mode is active — in full mode, editing `config.yaml` does nothing for module selection; settings live in the database and are edited via the web console.

## Architecture of xiaozhi-server (the Python service)

Entry: `main/xiaozhi-server/app.py` boots two asyncio servers concurrently:
- `WebSocketServer` on `:8000` at `/xiaozhi/v1/` — the device voice channel.
- `SimpleHttpServer` on `:8003` — OTA endpoint (`/xiaozhi/ota/`) and vision endpoint (`/mcp/vision/explain`).

`auth_key` resolution order: `server.auth_key` in config → `manager-api.secret` → random uuid. It signs JWTs for vision and OTA→WS handoff.

### Provider plugin pattern

Every replaceable AI subsystem lives under `core/providers/<kind>/`, where `<kind>` is one of `asr`, `llm`, `vllm`, `tts`, `vad`, `intent`, `memory`, `tools`. Each provider is a subpackage (e.g. `core/providers/llm/openai/`, `core/providers/tts/edge/`) implementing the base class in `base.py`. Selection happens by name in `selected_module` of `config.yaml` and is wired by `core/utils/modules_initialize.py`. To add a new provider, drop a new subdirectory matching the existing structure — there is no central registry to edit.

### Function-call / MCP plugin pattern

`plugins_func/` contains tool implementations exposed to the LLM. `plugins_func/loadplugins.py` and `register.py` perform decorator-based hot registration; concrete tools live under `plugins_func/functions/`. Tools can also be loaded over MCP via `mcp_server_settings.json` and the `mcp_endpoint` config.

### Message handling

`core/connection.py` owns a per-device session. Incoming WebSocket frames are routed through `core/handle/` — `receiveAudioHandle.py` for audio, `textMessageProcessor.py` + the `textHandler/` registry for JSON text messages. Outgoing audio goes through `sendAudioHandle.py` with `audioRateController.py` for pacing.

## Architecture of manager-api (the Java service)

Standard Spring Boot layout under `src/main/java/xiaozhi/`:
- `modules/` — feature packages (`agent`, `device`, `model`, `knowledge`, `voiceclone`, `timbre`, `correctword`, `llm`, `sms`, `sys`, `config`, `security`). Each follows controller → service → dao → entity.
- `common/` — cross-cutting: Shiro auth, Redis, MyBatis-Plus base classes, exception handler, i18n, XSS filter.

Database migrations are **Liquibase** under `src/main/resources/db/changelog/`. MyBatis XML mappers live in `src/main/resources/mapper/<module>/`. Never edit a previously-shipped changelog file — add a new one.

xiaozhi-server pulls config from this service via `config/manage_api_client.py` when `read_config_from_api: true`.

## Common commands

### Python server (`main/xiaozhi-server/`)

```bash
pip install -r requirements.txt   # Python 3.10 recommended; some deps pinned (torch 2.2.2, numpy 1.26.4, websockets 14.2)
python app.py                     # run server (reads config.yaml)
python performance_tester.py      # benchmark configured ASR/LLM/VLLM/TTS providers
```

`ffmpeg` must be on PATH (checked at startup by `check_ffmpeg_installed`).

### Java API (`main/manager-api/`)

```bash
mvn spring-boot:run                       # dev run, requires MySQL 8 + Redis 5 reachable
mvn clean package -Dmaven.test.skip=true  # produce xiaozhi-esp32-api.jar
```

API docs after launch: http://localhost:8002/xiaozhi/doc.html

### Web admin (`main/manager-web/`)

```bash
npm install
npm run serve     # dev server with HMR (Vue CLI)
npm run build     # production bundle
```

### Mobile admin (`main/manager-mobile/`) — uses pnpm, not npm

```bash
pnpm i
pnpm dev:h5            # browser
pnpm dev:mp-weixin     # WeChat MiniProgram — import dist/dev/mp-weixin into 微信开发者工具
pnpm build:app         # App build — then use HBuilderX cloud packaging
pnpm type-check
pnpm lint && pnpm lint:fix
```

Env files live in `env/` (not project root). Copy `env/.env.example` → `env/.env.development` and set at least `VITE_SERVER_BASEURL`, `VITE_UNI_APPID`, `VITE_WX_APPID`.

### Digital-human test page (`main/digital-human/`)

```bash
pip install -r wakeword_runtime/requirements.txt
python start.py                                 # serves http://127.0.0.1:8006/index.html
```

### Docker

`docker-setup.sh` is an Ubuntu-x86 one-shot installer. The hand-written compose files are `main/xiaozhi-server/docker-compose.yml` (server only) and `main/xiaozhi-server/docker-compose_all.yml` (server + Java/web + MySQL + Redis).

## Default ports

| Port | Service |
|---|---|
| 8000 | xiaozhi-server WebSocket (`/xiaozhi/v1/`) |
| 8001 | manager-api WebSocket bridge for server config push |
| 8002 | manager-api + manager-web (nginx-fronted in the docker image) |
| 8003 | xiaozhi-server HTTP (OTA + vision; only active in minimal mode) |
| 8006 | digital-human test page + wakeword event bridge |

## Conventions worth knowing

- **Config placeholders**: `config.yaml` ships with literal Chinese placeholders like `你的接入点 websocket地址` and `你的API密钥`. Code checks `"你" in value` to detect "unset" — preserve that pattern when adding new config keys with placeholders.
- **Working directory for the Python server is `main/xiaozhi-server/`**. `config.py`, `core/`, and relative paths like `models/` and `data/` all assume this CWD. The Docker image sets `WORKDIR` accordingly.
- **`data/` and `models/` are runtime, not source**. `data/` holds user-edited `.config.yaml` overrides and runtime state; `models/` holds downloaded model weights (e.g. `SenseVoiceSmall/model.pt`). Both are bind-mounted in the compose files.
- **Liquibase, not Flyway**: schema changes ship as new changelog files referenced from the master changelog. Adding a column means a new changeset, never editing an existing one.
- **README is Chinese-first**. The canonical `README.md` is in Chinese; localized copies live in `docs/readme/`. Deployment guides are `docs/Deployment.md` (minimal) and `docs/Deployment_all.md` (full).
