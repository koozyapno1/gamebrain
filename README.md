# gamebrain

A **generic game intel assistant**. It answers questions about a game from a
knowledge base and can speak the answers aloud.

The engine knows nothing about any particular game. Every game-specific fact
lives in a **pack** — a directory of two JSON files. Adding a new game means
writing JSON, not Python.

```
gamebrain/
├── app.py            the FastAPI server
├── engine_pack.py    the generic, data-driven tool layer
├── ui/index.html     the browser UI (single file, no build step)
├── packs/
│   └── default/      the built-in example pack
└── docs/
    ├── INSTALL.md
    └── PACK_FORMAT.md
```

## Quick start

```bash
git clone https://github.com/koozyapno1/gamebrain.git
cd gamebrain
cp .env.example .env      # then edit it
docker compose up -d
```

Open **http://localhost:8095**.

With plain Python:

```bash
pip install -r requirements.txt
export PACK_DIR=./packs/default
python app.py
```

## How it works

1. The server loads `PACK_DIR` — a manifest (`pack.json`) and a knowledge
   base (`kb.json`).
2. The manifest declares the **tools** the model may call. Each tool names a
   KB collection and a query mode.
3. The **generic query engine** executes those tools over the KB. It never
   hardcodes a game.
4. The model picks a tool from its description, the engine returns JSON, and
   the answer is spoken over TTS and rendered in the browser.

```
browser  ──▶  FastAPI  ──▶  LLM  ──tool call──▶  QueryEngine  ──▶  kb.json
   ▲                                                                  │
   └──────────────── answer + video links ◀─────────────────────────┘
```

## Query modes

| Mode | What it does |
|---|---|
| `record` | One record by name, with fuzzy + alias matching |
| `list` | A whole collection, optionally filtered |
| `search` | Free-text search across a collection |
| `collection` | A raw sub-object from the KB |
| `videos` | Videos for an entity, with per-entity chapters |
| `sector` | Grid sector lookup plus free-text place search |
| `boss` | An entity with its videos and strategies attached |
| `item` | One loot item or recipe |
| `key` | Key-style lookup |
| `mission` | Mission or operation briefing |

All of them are documented in [`docs/PACK_FORMAT.md`](docs/PACK_FORMAT.md).

## Connecting your LLM

Two wire formats, selected with `LLM_API`:

| `LLM_API` | Request goes to | Use for |
|---|---|---|
| `ollama` *(default)* | `{LLM_URL}/api/chat` | Ollama |
| `openai` | `{LLM_URL}/chat/completions` | OpenRouter, OpenAI, vLLM, LM Studio, llama.cpp, LiteLLM, Groq, Together, Ollama's `/v1` |

The tool-calling schema is identical in both, so the same pack works either way.

```bash
LLM_API=openai
LLM_URL=https://openrouter.ai/api/v1
LLM_MODEL=anthropic/claude-3.5-sonnet
LLM_API_KEY=sk-or-v1-...
```

See [`docs/INSTALL.md`](docs/INSTALL.md) for the full list of environment
variables.

## Writing a pack

Copy `packs/default/` and edit the two JSON files. The
[PACK_FORMAT.md](docs/PACK_FORMAT.md) guide covers every field.

To publish one, put it in its own repository and tell people to clone it
next to the engine:

```bash
git clone https://github.com/koozyapno1/gamebrain-pack-dmz.git packs/dmz
echo "PACK_DIR=./packs/dmz" >> .env
```

## Available packs

| Pack | Game |
|---|---|
| [`gamebrain-pack-dmz`](https://github.com/koozyapno1/gamebrain-pack-dmz) | Call of Duty: Modern Warfare 4 — DMZ (Hajin) |
| [`gamebrain-pack-minecraft`](https://github.com/koozyapno1/gamebrain-pack-minecraft) | Minecraft |
| [`gamebrain-pack-fortnite`](https://github.com/koozyapno1/gamebrain-pack-fortnite) | Fortnite — Chapter 6 Season 3 |
| [`gamebrain-pack-apex-legends`](https://github.com/koozyapno1/gamebrain-pack-apex-legends) | Apex Legends — Season 30 |
| [`gamebrain-pack-rainbow-six-siege`](https://github.com/koozyapno1/gamebrain-pack-rainbow-six-siege) | Rainbow Six Siege — Year 11 |
| [`gamebrain-pack-call-of-duty`](https://github.com/koozyapno1/gamebrain-pack-call-of-duty) | Call of Duty: Modern Warfare 4 / Warzone |
| [`gamebrain-pack-grand-theft-auto-v`](https://github.com/koozyapno1/gamebrain-pack-grand-theft-auto-v) | GTA V / GTA Online |
| [`gamebrain-pack-minecraft-dugeons`](https://github.com/koozyapno1/gamebrain-pack-minecraft-dugeons) | Minecraft Dungeons |
| [`gamebrain-pack-cyberpunk-2077`](https://github.com/koozyapno1/gamebrain-pack-cyberpunk-2077) | Cyberpunk 2077 — Patch 2.31 |
| [`gamebrain-pack-rust`](https://github.com/koozyapno1/gamebrain-pack-rust) | Rust |
| [`gamebrain-pack-helldivers-2`](https://github.com/koozyapno1/gamebrain-pack-helldivers-2) | Helldivers 2 — Patch 7.2 |

## Testing a pack

```bash
curl -s http://localhost:8095/health | python3 -m json.tool   # pack_errors must be []
curl -s http://localhost:8095/tools  | python3 -m json.tool
curl -s -X POST http://localhost:8095/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "<a question your KB can answer>"}' | python3 -m json.tool
```

Look for `tools_used` in the response. If it is empty, the model answered from
memory instead of using your data — usually a weak model, or a tool
description that does not say *when* to use it.

## Licence

MIT — see [`LICENSE`](LICENSE). Game-specific facts and video links in a pack
are community-sourced content and are attributed in that pack's `kb.json` →
`sources`.
