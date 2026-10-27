# gamebrain-pack-dmz

A **game pack** for [gamebrain](https://github.com/koozyapno1/gamebrain) —
a generic game intel assistant that answers questions about a game from a
knowledge base and can speak the answers aloud.

This pack teaches it about **Call of Duty: Modern Warfare 4's DMZ map
(Hajin)**: the sector grid, points of interest, keys, loot and 3D-printer
recipes, the six operations, and all **13 bosses** (3 Commanders and 10
Lieutenants) — each with its YouTube guide.

## Install

```bash
git clone https://github.com/koozyapno1/gamebrain.git
cd gamebrain

git clone https://github.com/koozyapno1/gamebrain-pack-dmz.git packs/dmz

echo "PACK_DIR=./packs/dmz" >> .env

docker compose up -d
```

Or with plain Python:

```bash
export PACK_DIR=./packs/dmz
python app.py
```

Full instructions: **[gamebrain INSTALL.md](https://github.com/koozyapno1/gamebrain/blob/main/docs/INSTALL.md)**

## What it knows

| Area | Detail |
|---|---|
| **Sectors** | The 8×9 grid (A1–H9) — 72 sectors, with 243 POIs and their threat levels |
| **POIs** | 243 locations with descriptions, threat ratings and region mapping |
| **Keys** | 56 keys — rarity, grid square, location, and how to obtain each one |
| **Loot** | 37 items with rarity, value, and the best places to find them |
| **3D printer** | 18 recipes with ingredients and unlock levels |
| **Operations** | All 6, with threat, chapter, objectives, numbered steps and key rewards |
| **Bosses** | 13 — 3 Commanders (Colossus, Deathstalker, Tank) and 10 Lieutenants |
| **Videos** | 28 YouTube guides, resolved **per boss** with chapter deep-links |

### Boss damage rules

The pack carries the exact damage rules, because paraphrasing them can
invert the advice:

| Boss | Damage |
|---|---|
| **Colossus** | **BULLETS ONLY** — immune to every explosive |
| **Deathstalker** | Normal, but capture a SAM site first |
| **Tank** | **EXPLOSIVES ONLY** — bullets do no damage at all |

### Videos

Every boss gets **its own** videos: dedicated fight videos first, then
roundup guides that contain a chapter for that boss, deep-linked straight
to the timestamp (`?v=ID&t=790s`).

"Dedicated" is a property of the **(boss, video) pair** — a roundup is
dedicated for the boss it was made for and a roundup for the other twelve.
Two bosses genuinely have **no** standalone fight video (**Tank** and
**Jin-Tae**), and the pack says so honestly rather than linking to
something misleading.

## Files

```
pack.json    the manifest - tools, prompt, UI copy, reference tabs
kb.json      the knowledge base - all the game facts
```

No code, no keys, no secrets. Content only.

## Credits and sources

The knowledge base was compiled from public community sources. The
`kb.json` → `sources` section records them per area, including the
per-key rarity/grid/location data that complements the main dataset.

YouTube video links point to the original creators' channels. All game
names, locations and trademarks belong to their respective owners. This
pack is an unofficial community reference and is not affiliated with or
endorsed by the publisher.

## Licence

The pack's **code and manifest** are MIT (same as gamebrain). The **game
facts and video links** are community-sourced content — see `kb.json` →
`sources` for attribution.

## Write your own

The manifest format is documented in
[gamebrain's PACK_FORMAT.md](https://github.com/koozyapno1/gamebrain/blob/main/docs/PACK_FORMAT.md).
Adding a game is JSON, not Python.
