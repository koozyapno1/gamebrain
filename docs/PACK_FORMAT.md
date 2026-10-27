# Writing a game pack

A pack turns gamebrain into an assistant for **any** game. It is pure
JSON — no Python, no code. If you can write a JSON file, you can ship a
pack.

```
mypack/
├── pack.json    <- the manifest: tools, prompt, UI copy, reference tabs
└── kb.json      <- the knowledge base: your game facts
```

Copy `packs/default/` to start.

---

## The manifest

```json
{
  "id": "minecraft",
  "name": "Minecraft Command Centre",
  "subtitle": "Survival guide - Overworld",
  "version": "1.0.0",
  "kb": "kb.json",

  "ui": {
    "brand_title": "MINECRAFT COMMAND CENTRE",
    "brand_subtitle": "Survival Ops - Overworld - Radio 1",
    "grid_title": "Region Grid",
    "grid_question": "What is in region {sector}?",
    "grid_cols": 8,
    "grid_rows": 9
  },

  "quick_prompts": [
    "Where is a diamond?",
    "What does the Ender Dragon drop?"
  ],

  "known_facts": [
    "There are 7 dimensions.",
    "Diamonds spawn below Y=-59."
  ],

  "entity_words": ["diamond", "ender", "nether", "biome", "ore"],

  "system_prompt": "You are the Minecraft survival assistant.",

  "tools": [ ... ],
  "ref_tabs": [ ... ]
}
```

### Fields

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Short machine name. Used in URLs and logs. |
| `name` | yes | Display name. |
| `subtitle` | no | Second line under the title. |
| `kb` | no | KB filename. Defaults to `kb.json`. |
| `ui` | no | Copy and grid settings for the UI. |
| `quick_prompts` | no | Buttons shown in the left pane. |
| `known_facts` | no | Facts the model may state without a tool. |
| `entity_words` | no | Extra words that signal a pack question. |
| `system_prompt` | no | Your game's rules. Appended to the engine prompt. |
| `tools` | yes* | The tools the LLM can call. |
| `ref_tabs` | no | Tabs in the right-hand reference pane. |

`*` A pack with no tools still works — the assistant answers from general
knowledge only. But then the KB is unused, so you almost always want
tools.

---

## Tools

Each tool tells the model what it can ask for and how to answer it.

```json
{
  "name": "item_lookup",
  "description": "Look up an item: what it is, how rare, and where to find it. ALWAYS use this instead of guessing.",
  "parameters": {
    "type": "object",
    "properties": {
      "item": { "type": "string", "description": "Item name, e.g. 'Diamond Sword'" }
    },
    "required": ["item"]
  },
  "match_words": ["item", "where", "find", "drop"],
  "query": {
    "mode": "item",
    "params": ["item"]
  }
}
```

| Field | Meaning |
|---|---|
| `name` | The tool name the LLM calls. Must be unique. |
| `description` | **The most important field.** This is all the model sees when deciding to call the tool. Say *when* to use it and *what comes back*. |
| `parameters` | JSON Schema for the arguments. |
| `match_words` | Words that suggest this tool. Optional; used for routing hints. |
| `query` | How to execute it. See below. |

### Query modes

| Mode | What it does | Extra keys |
|---|---|---|
| `item` | Find one record by name, with fuzzy fallback. | `params` |
| `boss` | Find one entity, attach its videos and strategies. | `params`, `include` |
| `videos` | Find videos for an entity, with chapter deep-links. | `params`, `videos_collection`, `entities_collection`, `match`, `video_entity_field` |
| `sector` | Grid sector lookup plus free-text place search. | `params` |
| `key` | Key-style lookup with token-overlap matching. | `params` |
| `mission` | Mission/operation briefing. | `params` |
| `record` | Generic single-record lookup. | `collection`, `match`, `params`, `include` |
| `list` | Whole collection, optionally filtered. | `collection`, `filter`, `include` |
| `search` | Free-text search across a collection. | `collection`, `params`, `fields`, `limit` |
| `collection` | Raw sub-object from the KB. | `collection` |

`params` names the tool argument that carries the search term. If you
omit it, the first string argument is used.

### The `item` mode

The KB section it reads is `loot_items`. Each record can have:

```json
{
  "name": "Medkit",
  "rarity": "epic",
  "category": "healing",
  "tier": 3,
  "value": 400,
  "stack": 2,
  "description": "Restores 75 health over 5 seconds.",
  "spots": ["Military Base", "Farmstead"],
  "how_to_get": ["loot", "kit"],
  "recipe": ["bandage", "alcohol"],
  "recipe_not_revealed": false,
  "use": "Heals a teammate",
  "place_grids": ["B5", "C7"]
}
```

### The `boss` mode (entities with videos)

The KB needs an `entities` list and a `videos` list:

```json
{
  "entities": [
    {
      "name": "Colossus",
      "kind": "Commander",
      "grid": "F2",
      "drops": "Mega",
      "weakness": "Bullets only",
      "damage_type": "bullets_only",
      "damage_rule": "BULLETS ONLY - explosives do nothing to it",
      "do_not_use": "grenades, thermite, C4",
      "best_against": "PPSh-41, Cronen Squall",
      "how_to_beat": ["Shoot the legs", "Keep your distance"],
      "videos": ["abc123", "def456"],
      "video_chapters": { "def456": "2:15" },
      "has_dedicated_video": true,
      "no_dedicated_note": ""
    }
  ],
  "videos": [
    {
      "id": "abc123",
      "title": "Full Colossus fight",
      "author": "SomeChannel",
      "scope": "Colossus - full fight",
      "tags": ["commander"],
      "upload_date": "2026-01-01",
      "note": "Current game version.",
      "dedicated": true,
      "entity": "Colossus"
    }
  ]
}
```

> **`dedicated` is a property of the (entity, video) PAIR.** A "roundup"
> video that covers ten bosses is *dedicated* for the one boss it was
> made for and a *roundup* for the other nine. The engine compares
> `video.entity` to the entity's name, so set `entity` correctly on every
> video. Getting this wrong labels a roundup as a dedicated fight video.

`video_chapters` maps a video id to a `m:ss` timestamp; the engine
appends `&t=<seconds>s` so the link jumps straight to that boss.

### The `sector` mode

Reads `grid_index` (a dict of sector → list of POIs), `pois`, `keys` and
`operations`:

```json
{
  "grid_index": { "B5": [ { "name": "Air Base", "label": "Major Location", "threat": "high" } ] },
  "pois": [ { "name": "Air Base", "grid": "B5", "label": "Major Location", "place": "North", "threat": "high", "text": "..." } ],
  "keys": [ { "name": "Commandant Office", "grid": "B5", "dexerto": { "rarity": "Epic", "how_to_get": "Lieutenant drop", "location": "..." } } ],
  "operations": { "Silent Ledger": { "grid": "F4", "place": "Casino", "detail": { "Threat assessment": "Medium" }, "steps": ["..."] } }
}
```

### The `key` mode

Reads `key_lookup`, a dict keyed by normalised name:

```json
{
  "key_lookup": {
    "commandant office": {
      "name": "Commandant Office",
      "rarity": "Epic",
      "grid": "B5",
      "location": "Military Base",
      "how_to_get": "Lieutenant drop",
      "sources": ["dexerto"]
    }
  }
}
```

### The `mission` mode

Reads `operations` (a dict) and `dmz_orders` (a list).

---

## Reference tabs

```json
"ref_tabs": [
  { "id": "items", "label": "Items", "kind": "list", "collection": "items" },
  { "id": "guides", "label": "Guides", "kind": "list", "collection": "guides" }
]
```

| Field | Meaning |
|---|---|
| `id` | DOM id, also the `/ref/<id>` path. |
| `label` | Button text. |
| `kind` | The fetch path the UI uses. Use `list` for a plain KB list. |
| `collection` | KB key holding the list. |
| `fields` | Which fields to show under each entry. |
| `quick` | `true` adds a shortcut button in the bottom bar. |
| `quick_label` | Text for that shortcut. |

The `bosses` and `videos` kinds are special: they read `entities` and
`videos` and render the video links.

---

## Connecting your own LLM

A pack does **not** contain an LLM connection — that belongs to the
**engine**, and it is configured with environment variables. Your pack
works with whatever model the person running the engine has configured.

The engine supports two wire formats:

| `LLM_API` | Request goes to | Use for |
|---|---|---|
| `ollama` *(default)* | `{LLM_URL}/api/chat` | Ollama and anything speaking its native format |
| `openai` | `{LLM_URL}/chat/completions` | OpenRouter, OpenAI, vLLM, LM Studio, llama.cpp, LiteLLM, Groq, Together, Ollama's `/v1` |

```bash
# local Ollama
LLM_API=ollama
LLM_URL=http://127.0.0.1:11434
LLM_MODEL=qwen3:8b
LLM_API_KEY=

# a hosted provider
LLM_API=openai
LLM_URL=https://openrouter.ai/api/v1
LLM_MODEL=anthropic/claude-3.5-sonnet
LLM_API_KEY=sk-or-v1-...
```

The tool-calling schema is **identical in both formats**, so the same
pack works with either — nothing in the pack changes.

**If your model is good at tool calling, the pack just works.** The
model decides which tool to call from the tool's `description` — that is
the only text it sees about your data. So make the description say *when*
to use the tool and *what comes back*.

**Never put an API key in a pack.** A pack is content and gets published
to a public repository. Keys belong in the engine's `.env`.

Full details: [INSTALL.md §3](INSTALL.md#3-choose-your-llm).

---

## The knowledge base

`kb.json` is one JSON object. **The engine only reads the sections your
tools and tabs reference** — anything else in the file is ignored. So
you can keep extra notes in it, or split the data however suits you.

The sections the built-in modes expect:

| Section | Used by | Shape |
|---|---|---|
| `loot_items` | `item` | list of item records |
| `entities` | `boss` | list of entity records |
| `videos` | `videos` | list of video records |
| `boss_strategies` | `boss` | list of `{topic, detail}` |
| `grid_index` | `sector` | dict of sector → POI list |
| `pois` | `sector` | list of POI records |
| `keys` | `sector` | list of key records |
| `operations` | `sector`, `mission` | dict of name → operation |
| `operation_rewards` | `mission` | dict of slug → reward list |
| `dmz_orders` | `mission` | list of order records |
| `key_lookup` | `key` | dict of normalised name → key record |

---

## Writing the prompt

`system_prompt` is appended to the engine's own rules. Use it for the
things the model must not get wrong:

```json
"system_prompt": "You are the DMZ intel assistant.\n\nBOSS damage rules: copy the boss_guide 'damage_rule' field VERBATIM. NEVER paraphrase it. Say the rule in capitals."
```

Good prompt content:

- **Quotable fields.** If a field must be exact, say so and name the field.
- **Domain vocabulary.** "There are 10 Lieutenants and 3 Commanders."
- **Honesty rules.** "If a boss has no dedicated video, say so — do not
  invent a link."
- **Video rules.** The engine already tells the model to include video
  links; reinforce it if your pack has them.

Avoid: anything the model already knows, and any instruction that
duplicates the engine's rules.

---

## Testing your pack

```bash
# 1. does it load?
curl -s http://localhost:8095/health | python3 -m json.tool

# 2. are the tools exposed?
curl -s http://localhost:8095/tools | python3 -m json.tool

# 3. does a tool actually return data?
curl -s -X POST http://localhost:8095/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"<a question your KB can answer>"}' | python3 -m json.tool
```

Look for `tools_used` in the response. If it is empty, the model
answered from memory instead of using your data — usually a weak model
or a tool description that does not say *when* to use it.

The response also includes `pack_errors` from `/health`. An empty list
means the manifest parsed cleanly.

Reload after every edit:

```bash
curl -X POST http://localhost:8095/pack/reload
```

---

## Publishing a pack

1. Put the folder in its own git repo: `pack.json` + `kb.json` + a
   README.
2. Tell people to clone it next to their engine and set `PACK_DIR`.
3. Keep your own secrets out — a pack should be **content only**. If it
   needs a key, read it from an environment variable.

The reference implementation is
[gamebrain-pack-dmz](https://github.com/koozyapno1/gamebrain-pack-dmz).
