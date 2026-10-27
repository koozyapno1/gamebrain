# Installing gamebrain

This guide takes you from a bare machine to a running game intel assistant.

**Time:** ~15 minutes. **Difficulty:** easy — copy and paste.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Install](#2-install)
3. [Choose your LLM](#3-choose-your-llm)
4. [Add text-to-speech (optional)](#4-add-text-to-speech-optional)
5. [Install a game pack](#5-install-a-game-pack)
6. [Put it behind a domain (optional)](#6-put-it-behind-a-domain-optional)
7. [Run it as a service](#7-run-it-as-a-service)
8. [Using it on your phone](#8-using-it-on-your-phone)
9. [Verify the install](#9-verify-the-install)
10. [Troubleshooting](#10-troubleshooting)
11. [Upgrading](#11-upgrading)

---

## 1. What you need

| | Minimum | Recommended |
|---|---|---|
| OS | Linux, macOS, or WSL2 on Windows | Linux |
| RAM | 4 GB | 8 GB+ |
| Disk | 2 GB | 20 GB+ |
| CPU | any x86-64 or ARM64 | any |
| GPU | not required | NVIDIA with 6 GB+ VRAM |

You also need **an LLM**. The engine talks to any OpenAI-compatible
endpoint, and [Ollama](https://ollama.com) is the easiest local option.
A pack's knowledge base is small, so even a 3B model does fine — a 7–8B
model is noticeably better at using tools correctly.

---

## 2. Install

### Option A — Docker (recommended)

```bash
git clone https://github.com/koozyapno1/gamebrain.git
cd gamebrain
cp .env.example .env
docker compose up -d
```

Check it worked:

```bash
curl -s http://localhost:8095/health
```

You should see something like:

```json
{"ok":true,"pack":"default","kb_loaded":true,"tools":2,"model":"qwen3:8b","tts":false}
```

Open <http://localhost:8095> and you have a working assistant.

### Option B — plain Python

```bash
git clone https://github.com/koozyapno1/gamebrain.git
cd gamebrain

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export PACK_DIR=./packs/default
export OLLAMA_URL=http://127.0.0.1:11434
export OLLAMA_MODEL=qwen3:8b

python app.py
```

---

## 3. Choose your LLM

The engine talks to **your** model server — it never calls anyone else's
API on your behalf. Pick the wire format that matches your server with
`LLM_API`.

| `LLM_API` | Request goes to | Use for |
|---|---|---|
| `ollama` *(default)* | `{LLM_URL}/api/chat` | Ollama, and anything that speaks its native format |
| `openai` | `{LLM_URL}/chat/completions` | OpenRouter, OpenAI, vLLM, LM Studio, llama.cpp, LiteLLM, Groq, Together, Ollama's `/v1` |

The tool-calling schema is **identical in both formats**, so the same pack
works with either — you can switch without touching your pack.

### Ollama (local, free, private)

```bash
# install Ollama: https://ollama.com/download
ollama pull qwen3:8b        # ~5 GB, good tool-calling
ollama serve
```

```bash
LLM_API=ollama
LLM_URL=http://127.0.0.1:11434
LLM_MODEL=qwen3:8b
LLM_API_KEY=                 # local Ollama needs no auth
```

Smaller models that also work: `qwen3:4b` (fast, ~2.6 GB),
`llama3.1:8b`. Avoid models below 3B — they cannot reliably call tools.

> **Important:** the engine uses *native tool calling*. If your model does
> not support it, the assistant answers from its own memory instead of
> using the knowledge base. `/health` shows the model name; test a
> question that should need a tool (see [§9](#9-verify-the-install)).

### A hosted provider (OpenAI-compatible)

```bash
LLM_API=openai
LLM_URL=https://openrouter.ai/api/v1
LLM_MODEL=anthropic/claude-3.5-sonnet
LLM_API_KEY=sk-or-v1-....
```

The key is sent as `Authorization: Bearer <key>`. Nothing else is sent,
and your key stays in your own `.env` — the engine never logs it.

**More examples:**

```bash
# OpenAI
LLM_API=openai
LLM_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-...

# Groq
LLM_API=openai
LLM_URL=https://api.groq.com/openai/v1
LLM_MODEL=llama-3.3-70b-versatile
LLM_API_KEY=gsk_...

# vLLM on your own box
LLM_API=openai
LLM_URL=http://192.168.1.50:8000/v1
LLM_MODEL=your-model-name
LLM_API_KEY=                 # vLLM usually needs no key

# llama.cpp server
LLM_API=openai
LLM_URL=http://127.0.0.1:8080/v1
LLM_MODEL=local-model

# Ollama, but through its OpenAI-compatible endpoint
LLM_API=openai
LLM_URL=http://127.0.0.1:11434/v1
LLM_MODEL=qwen3:8b
```

**If your provider uses a different auth header** (some use `x-api-key`):

```bash
LLM_API_KEY=your-key
LLM_API_KEY_HEADER=x-api-key
```

`LLM_API_KEY_HEADER` replaces the `Authorization` header when set.

> **Note on `LLM_URL`:** for `LLM_API=openai` give the **base** URL
> (`.../v1`) — the engine appends `/chat/completions`. If you paste the
> full path including `/chat/completions`, that is detected and used
> as-is, so both work.

> **Keeping your own LLM private:** if you run Ollama on your LAN or
> tailnet, bind it to a private interface and firewall the port. Ollama
> listens on `0.0.0.0` by default, which means every host that can
> route to it can use your model. A typical setup is
> `OLLAMA_HOST=127.0.0.1:11434` (local only) plus a firewall rule, or
> `ufw allow from 192.168.1.0/24 to any port 11434 proto tcp` and
> nothing else.

---

## 4. Add text-to-speech (optional)

The assistant works fine without speech. To make it speak, you need any
endpoint that implements OpenAI's `/v1/audio/speech`.

The easiest is [speaches](https://github.com/speaches-ai/speaches) — it
serves Kokoro and other models, CPU or GPU:

```bash
docker run -d --name speaches -p 8000:8000 \
  ghcr.io/speaches-ai/speaches:latest \
  --model-id speaches-ai/Kokoro-82M-v1.0-ONNX
```

Then in `.env`:

```bash
TTS_URL=http://127.0.0.1:8000
TTS_MODEL=speaches-ai/Kokoro-82M-v1.0-ONNX
TTS_VOICE=af_bella
```

Restart and check:

```bash
curl -s http://localhost:8095/tts/voices
```

**GPU tip:** on NVIDIA, run speaches with the CUDA image
(`--gpus all`) and check it is actually using the GPU. ONNX Runtime binds
each node to the *first* provider that supports it, so a provider list
built from an unordered `set` can silently put CPU first and make
synthesis ~30× slower. If speech is slow but the GPU shows 0% usage,
that is the cause.

---

## 5. Install a game pack

A pack is a folder with `pack.json` and `kb.json`.

### From a published pack

```bash
git clone https://github.com/koozyapno1/gamebrain-pack-dmz.git packs/dmz
```

Then set it in `.env`:

```bash
PACK_DIR=./packs/dmz
```

Restart. The whole UI — title, tabs, quick questions, grid — now reflects
the new pack.

### Write your own

Copy the template and edit it:

```bash
cp -r packs/default packs/mygame
```

Then edit `packs/mygame/pack.json` and `packs/mygame/kb.json`.
Full details in **[PACK_FORMAT.md](PACK_FORMAT.md)**.

Quick version:

```json
{
  "id": "mygame",
  "name": "My Game Intel",
  "kb": "kb.json",
  "ui": { "brand_title": "MY GAME" },
  "quick_prompts": ["Where is the sword?", "What does the dragon drop?"],
  "tools": [
    {
      "name": "item_lookup",
      "description": "Look up an item and where to find it.",
      "parameters": {
        "type": "object",
        "properties": { "item": { "type": "string" } },
        "required": ["item"]
      },
      "query": { "mode": "item", "params": ["item"] }
    }
  ],
  "ref_tabs": [
    { "id": "items", "label": "Items", "kind": "list", "collection": "items" }
  ]
}
```

and `kb.json`:

```json
{
  "items": [
    { "name": "Iron Sword", "rarity": "rare", "grid": "A1",
      "spots": ["Dragon's Hoard"] }
  ]
}
```

Hot-reload while you edit:

```bash
curl -X POST http://localhost:8095/pack/reload
```

---

## 6. Put it behind a domain (optional)

The app works fine on a LAN IP. For HTTPS, put Caddy or nginx in front.

**Caddy** (automatic HTTPS):

```caddy
game.example.com {
    reverse_proxy localhost:8095
}
```

**nginx:**

```nginx
server {
    listen 443 ssl http2;
    server_name game.example.com;

    location / {
        proxy_pass http://127.0.0.1:8095;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE - the chat streams, so buffering must be off
        proxy_buffering off;
        proxy_read_timeout 600s;
    }
}
```

> **The `proxy_buffering off` matters.** The chat endpoint is a
> Server-Sent Events stream. If nginx buffers it, the browser waits for
> the whole answer before showing anything, and long tool loops can trip
> proxy timeouts.

---

## 7. Run it as a service

### systemd

`/etc/systemd/system/gamebrain.service`:

```ini
[Unit]
Description=gamebrain
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=gamebrain
WorkingDirectory=/opt/gamebrain
ExecStart=/opt/gamebrain/.venv/bin/python app.py
Restart=always
RestartSec=5
EnvironmentFile=/opt/gamebrain/.env
Environment=PORT=8095

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now gamebrain
sudo systemctl status gamebrain
```

### Docker Compose (already covered)

The included `docker-compose.yml` restarts on failure and keeps the
service up across reboots with `restart: unless-stopped`.

---

## 8. Using it on your phone

The UI is responsive: on a narrow screen the three panes collapse into one
and a bottom nav switches between them.

- **Voice input** works in Chrome and Edge (Web Speech API). Firefox and
  Safari do not support it — use the text box there.
- **Voice output** uses the browser's own `speechSynthesis` as a fallback
  if the TTS endpoint is unreachable.
- Add it to your home screen for a full-screen app-like experience.

---

## 9. Verify the install

```bash
# 1. the service is up
curl -s http://localhost:8095/health

# 2. the pack loaded and the tools are exposed
curl -s http://localhost:8095/tools | head -c 400

# 3. the UI is served
curl -s http://localhost:8095/ | head -c 200

# 4. it actually answers using the knowledge base
curl -s -X POST http://localhost:8095/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"<a question that needs the pack>"}'
```

For step 4, ask something the KB definitely contains and that general
knowledge would get wrong. If the answer comes back with no `tools_used`,
the model answered from its own memory — the LLM either is not loaded or
does not support tool calling.

`GET /health` reports `pack_errors` — an empty list means the manifest
parsed cleanly.

---

## 10. Troubleshooting

**"Connection refused" on port 8095**
The service is not running. `docker compose ps`, or
`systemctl status gamebrain`. Check the logs: `docker compose logs -f`
or `journalctl -u gamebrain -f`.

**The assistant ignores the knowledge base**
The model is not making tool calls. Check `/health` shows the right model,
and make sure it supports native tool calling (see [§3](#3-choose-your-llm)).
Ask a question that needs a specific fact from the pack — if it
hallucinates, that confirms it.

**No sound**
Check `TTS_URL` is set and reachable: `curl -s $TTS_URL/v1/models`. The
UI's health lamps show `tts on/off`. Also check the browser tab is not
muted, and that the 🔊 button is toggled on (it shows 🔊✓ when enabled).

**The chat shows "network error" mid-answer**
The SSE stream was cut. If you are behind a reverse proxy, set
`proxy_buffering off` and a long `proxy_read_timeout`. The engine already
sends a heartbeat every 2 seconds, so the usual cause is a proxy that
buffers.

**The page looks stale after a deploy**
The UI is served with no-store headers, but a hard refresh may be needed.
`GET /build` returns the current build stamp.

**`pack_errors` is not empty**
The manifest is malformed. `pack.json` must be valid JSON, and every
tool needs `name`, `description`, `parameters` and `query`. The error
message names the offending tool.

**The UI is blank / the buttons do nothing**
Usually a JavaScript syntax error. Open the browser console. If you
edited `ui/index.html` by hand, check the script still parses:
`node --check` on the extracted script block.

---

## 11. Upgrading

```bash
cd /opt/gamebrain
git pull
sudo systemctl restart gamebrain
```

If you changed the UI, bump `UI_BUILD` in `.env` so browsers pick it up.

**Keep your packs separate from the engine** so `git pull` never touches
them. Put them in `/opt/packs/<name>` and point `PACK_DIR` there — that
is what the published DMZ pack does.
