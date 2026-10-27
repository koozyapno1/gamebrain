"""Pack loading and the generic, data-driven tool layer.

A *pack* is a directory containing:
    pack.json     - the manifest (entities, tools, prompt, ref tabs, UI copy)
    kb.json       - the knowledge base (arbitrary shape, your data)

The engine never knows what game a pack is for. Every tool is described by
the manifest and executed by a *generic query engine* over the KB, so adding
a new game means writing JSON, not Python.

The manifest format is documented in docs/PACK_FORMAT.md.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# Text helpers (fuzzy matching - shared by every pack)
# --------------------------------------------------------------------------

STOP = {"the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "is",
        "where", "can", "i", "find", "what", "s", "me", "my", "get", "are",
        "do", "you", "know", "tell", "about", "with", "any", "there", "that",
        "this", "it", "be", "was", "were", "have", "has", "can", "will"}


def toks(s: str) -> List[str]:
    """Lowercase word tokens with stopwords removed."""
    s = (s or "").lower().replace("'", " ").replace("-", " ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return [t for t in s.split() if t not in STOP and len(t) > 1]


def norm_name(s: str) -> List[str]:
    """Normalised token list for name comparison.

    The pack manifest can add extra words to strip (e.g. "dmz", "key") via
    ``pack.json`` -> ``match.stopwords``.
    """
    s = (s or "").lower().replace("'", " ").replace("&", " ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    toks_ = [t for t in s.split() if t not in STOP and len(t) > 1]
    return toks_


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


# Light stemmer, used ONLY for record-mode matching so that a question
# like "how do I breed animals" still finds "Animal Breeding".
# norm_name() itself is left alone - the DMZ pack's key_lookup dict is
# keyed on its exact output and changing it would break every key lookup.
_SUFFIXES = (("ies", "y"), ("ing", ""), ("es", ""), ("s", ""))


def _stem(t: str) -> str:
    for suf, rep in _SUFFIXES:
        if len(t) > len(suf) + 2 and t.endswith(suf):
            return t[: -len(suf)] + rep
    return t


def _mtoks(s: str) -> set:
    """Stemmed token set for fuzzy record matching."""
    return {_stem(t) for t in norm_name(s)}


# --------------------------------------------------------------------------
# The pack
# --------------------------------------------------------------------------

class Pack:
    """A loaded game pack: manifest + knowledge base."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.manifest: Dict[str, Any] = {}
        self.kb: Dict[str, Any] = {}
        self.errors: List[str] = []

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(cls, root: str) -> "Pack":
        p = cls(root)
        man_path = os.path.join(p.root, "pack.json")
        if not os.path.exists(man_path):
            raise FileNotFoundError(f"no pack.json in {p.root}")
        with open(man_path, encoding="utf-8") as f:
            p.manifest = json.load(f)

        kb_rel = p.manifest.get("kb", "kb.json")
        kb_path = os.path.join(p.root, kb_rel)
        if os.path.exists(kb_path):
            with open(kb_path, encoding="utf-8") as f:
                p.kb = json.load(f)
        else:
            p.errors.append(f"kb file not found: {kb_rel}")

        p._validate()
        return p

    def _validate(self) -> None:
        """Check the manifest is coherent. Missing pieces degrade gracefully
        but a broken tool definition is a hard error - it would silently
        never fire."""
        if not self.manifest.get("id"):
            self.errors.append("manifest missing 'id'")
        if not self.manifest.get("name"):
            self.errors.append("manifest missing 'name'")
        for t in self.manifest.get("tools", []) or []:
            name = t.get("name")
            if not name:
                self.errors.append("a tool has no 'name'")
            elif not t.get("query"):
                self.errors.append(f"tool '{name}' has no 'query'")
            elif not t.get("parameters"):
                self.errors.append(f"tool '{name}' has no 'parameters'")
        for sec in self.manifest.get("ref_tabs", []) or []:
            if not sec.get("kind"):
                self.errors.append(f"ref tab '{sec.get('id')}' has no 'kind'")

    # -- accessors -------------------------------------------------------

    @property
    def id(self) -> str:
        return self.manifest.get("id", "unknown")

    @property
    def name(self) -> str:
        return self.manifest.get("name", self.id)

    @property
    def subtitle(self) -> str:
        return self.manifest.get("subtitle", "")

    @property
    def tools(self) -> List[Dict[str, Any]]:
        return self.manifest.get("tools", []) or []

    @property
    def ref_tabs(self) -> List[Dict[str, Any]]:
        return self.manifest.get("ref_tabs", []) or []

    def ui(self) -> Dict[str, Any]:
        return self.manifest.get("ui", {}) or {}

    def system_prompt(self) -> str:
        """The pack's prompt fragment, injected into the engine's prompt."""
        return self.manifest.get("system_prompt", "") or ""

    def quick_prompts(self) -> List[str]:
        return self.manifest.get("quick_prompts", []) or []

    def facts(self) -> List[str]:
        """Facts the model may state without a tool call."""
        return self.manifest.get("known_facts", []) or []

    def entity_words(self) -> List[str]:
        """Words that indicate a question needs this pack's tools.

        Used to route around the fast "pre-answer" path, which would
        otherwise answer a pack question from general knowledge.
        """
        words = list(self.manifest.get("entity_words", []) or [])
        for t in self.tools:
            words.extend(t.get("match_words", []) or [])
        # de-duplicate, keep order
        seen, out = set(), []
        for w in words:
            w = (w or "").lower()
            if w and w not in seen:
                seen.add(w)
                out.append(w)
        return out


# --------------------------------------------------------------------------
# Generic query engine
# --------------------------------------------------------------------------

def _dig(data: Any, path: str, default: Any = None) -> Any:
    """Fetch a dotted path, e.g. ``pois[*].name``.

    Supports ``a.b``, ``a.b.c`` and ``[*]`` to iterate a list. Returns
    ``default`` when any step is missing.
    """
    if not path:
        return default
    cur = data
    for part in path.split("."):
        if part.endswith("[*]"):
            key = part[:-3]
            if not isinstance(cur, dict):
                return default
            cur = cur.get(key)
            if not isinstance(cur, list):
                return default
            # the rest of the path is applied to each element downstream
            rest = path.split(".", 0)
            return cur
        if isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        elif isinstance(cur, list):
            # allow numeric index
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur


def _iter_section(kb: Dict[str, Any], path: str) -> List[Any]:
    """Return the list at a dotted path, following ``[*]`` markers."""
    parts = path.split(".")
    cur: Any = kb
    for i, part in enumerate(parts):
        if part.endswith("[*]"):
            key = part[:-3]
            if not isinstance(cur, dict) or key not in cur:
                return []
            cur = cur[key]
            if not isinstance(cur, list):
                return []
            return cur
        if isinstance(cur, dict):
            if part not in cur:
                return []
            cur = cur[part]
        else:
            return []
    return cur if isinstance(cur, list) else []


class QueryEngine:
    """Executes a manifest-declared tool against the pack's KB.

    A tool definition in the manifest looks like::

        {
          "name": "boss_guide",
          "description": "Look up how to beat a boss.",
          "parameters": {
            "type": "object",
            "properties": {"boss": {"type": "string", "description": "..."}}
          },
          "query": {
            "mode": "record",          # record | list | search | custom
            "collection": "bosses",    # KB path
            "match": "name",           # field used for exact/fuzzy match
            "params": ["boss"],        # which tool args feed the lookup
            "include": ["grid", "drops", "how_to_beat"],
            "extra": "boss_strategies" # optional extra KB list to attach
          }
        }
    """

    def __init__(self, pack: Pack):
        self.pack = pack
        self.kb = pack.kb

    # -- public API ------------------------------------------------------

    def run(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        tool = next((t for t in self.pack.tools if t.get("name") == tool_name),
                    None)
        if not tool:
            return {"error": f"unknown tool {tool_name}"}
        q = tool.get("query", {}) or {}
        mode = q.get("mode", "record")
        try:
            if mode == "record":
                return self._record(tool, q, args)
            if mode == "list":
                return self._list(tool, q, args)
            if mode == "search":
                return self._search(tool, q, args)
            if mode == "collection":
                return self._collection(tool, q, args)
            if mode == "videos":
                return self._videos(tool, q, args)
            if mode == "sector":
                return self._sector(tool, q, args)
            if mode == "boss":
                return self._boss(tool, q, args)
            if mode == "item":
                return self._item(tool, q, args)
            if mode == "key":
                return self._key(tool, q, args)
            if mode == "mission":
                return self._mission(tool, q, args)
            return {"error": f"unsupported query mode '{mode}'"}
        except Exception as e:  # a broken pack must not kill the tool loop
            return {"error": f"{type(e).__name__}: {e}", "tool": tool_name}

    # -- modes -----------------------------------------------------------

    def _record(self, tool, q, args) -> Dict[str, Any]:
        """Look up ONE record by name, with fuzzy fallback."""
        coll = _iter_section(self.kb, q.get("collection", ""))
        if not coll:
            return {"found": False,
                    "error": f"no data in KB section '{q.get('collection')}'"}
        match_field = q.get("match", "name")
        param = (q.get("params") or [None])[0]
        raw = (args.get(param) or "").strip() if param else ""
        # Fall back to the first string argument when the manifest does not
        # name one - otherwise the lookup silently returns nothing.
        if not param:
            for _k, _v in (args or {}).items():
                if isinstance(_v, str) and _v.strip():
                    raw = _v.strip()
                    break

        if not raw:
            # no argument -> return a compact list of everything
            fields = q.get("include") or ["name"]
            return {
                "found": True,
                "total": len(coll),
                "items": [{f: rec.get(f) for f in fields if f in rec}
                          for rec in coll[:60]],
            }

        want = norm_name(raw)
        want_str = " ".join(want)
        want_set = set(want)
        want_stem = _mtoks(raw)

        # Score every candidate and take the best, rather than taking the
        # FIRST substring hit. Otherwise "how do I raid a base" hits
        # "Base Building" (because "base" is a substring) before it ever
        # reaches "Raiding".
        best, best_score = None, 0.0
        best_kind = ""

        def _consider(score, kind, rec):
            nonlocal best, best_score, best_kind
            if score > best_score:
                best, best_score, best_kind = rec, score, kind

        for rec in coll:
            if not isinstance(rec, dict):
                continue
            # NOTE: `and` binds tighter than `or`, so this must NOT be
            # written as `q.get("match") or match_field and rec.get(...)`
            # - that always evaluates to the match FIELD NAME ("name")
            # and never reads the record, so every lookup missed.
            nm = norm_name(str(rec.get(match_field, "")))
            nm_str = " ".join(nm)

            # 1. exact match on the name -> highest confidence
            if nm_str == want_str:
                _consider(1.0, "exact", rec)
                continue

            # 2. aliases - an alias is a deliberate, pack-authored pointer,
            #    so an alias hit outranks a name substring hit.
            for alias in rec.get("aliases", []) or []:
                am = " ".join(norm_name(str(alias)))
                if not am:
                    continue
                if am == want_str:
                    _consider(0.98, "alias-exact", rec)
                    break
                if am in want_str or want_str in am:
                    _consider(0.85, "alias-substr", rec)
                    break
                a_stem = _mtoks(str(alias))
                if a_stem and want_stem:
                    inter = len(a_stem & want_stem) / min(len(a_stem), len(want_stem))
                    if inter >= 0.5:
                        _consider(0.55 + 0.25 * inter, "alias-fuzzy", rec)
                        break

            # 3. substring match either way ("chem" -> "the chemist")
            if nm_str and (nm_str in want_str or want_str in nm_str):
                _consider(0.70, "name-substr", rec)
                continue

            # 4. token overlap, with light stemming
            if want_set and nm:
                inter = len(want_set & set(nm)) / min(len(want_set), len(nm))
                if inter > 0:
                    _consider(0.5 * inter, "name-fuzzy", rec)
            n_stem = _mtoks(str(rec.get(match_field, "")))
            if want_stem and n_stem:
                inter = len(want_stem & n_stem) / min(len(want_stem), len(n_stem))
                if inter >= 0.5:
                    _consider(0.45 + 0.3 * inter, "name-stem", rec)

        rec = best if best_score >= 0.4 else None
        if rec is None:
            return {
                "found": False,
                "error": f"no match for '{raw}'",
                "available": [str(r.get(match_field)) for r in coll
                              if isinstance(r, dict)][:60],
            }

        out: Dict[str, Any] = {"found": True}
        fields = q.get("include")
        if fields:
            for f in fields:
                if isinstance(rec.get(f), (str, list, dict, int, float, bool)) \
                        or rec.get(f) is None:
                    out[f] = rec.get(f)
        else:
            for k, v in rec.items():
                if not k.startswith("_"):
                    out[k] = v
        # optional attached list (e.g. strategies for every boss)
        extra = q.get("extra")
        if extra:
            ex = _iter_section(self.kb, extra)
            if ex:
                out[q.get("extra_key", "extra")] = ex
        return out

    def _list(self, tool, q, args) -> Dict[str, Any]:
        """Return a whole collection, optionally filtered."""
        coll = _iter_section(self.kb, q.get("collection", ""))
        items = [r for r in coll if isinstance(r, dict)]
        filt = q.get("filter")
        if filt:
            field = filt.get("field")
            allowed = [str(x).lower() for x in (filt.get("in") or [])]
            if field and allowed:
                items = [r for r in items
                         if str(r.get(field, "")).lower() in allowed]
        fields = q.get("include")
        out = []
        for r in items:
            if fields:
                out.append({f: r.get(f) for f in fields if f in r})
            else:
                out.append(r)
        return {"items": out, "count": len(out)}

    def _search(self, tool, q, args) -> Dict[str, Any]:
        """Free-text search across a collection."""
        coll = _iter_section(self.kb, q.get("collection", ""))
        param = (q.get("params") or [None])[0]
        raw = (args.get(param) or "").strip() if param else ""
        # Fall back to the first string argument when the manifest does not
        # name one - otherwise the lookup silently returns nothing.
        if not param:
            for _k, _v in (args or {}).items():
                if isinstance(_v, str) and _v.strip():
                    raw = _v.strip()
                    break
        if not raw:
            return {"found": False, "error": "need a search term"}
        t = set(toks(raw))
        fields = q.get("fields") or ["name", "label", "place", "text", "sub"]
        limit = q.get("limit", 12)
        scored = []
        for r in coll:
            if not isinstance(r, dict):
                continue
            hay = " ".join(str(r.get(f, "")) for f in fields).lower()
            ht = set(toks(hay))
            hit = len(t & ht)
            if hit:
                nm = str(r.get(q.get("match", "name"), "")).lower()
                scored.append((hit + (10 if raw.lower() in nm else 0), r))
        scored.sort(key=lambda x: -x[0])
        include = q.get("include")
        results = []
        for _, r in scored[:limit]:
            if include:
                results.append({f: r.get(f) for f in include if f in r})
            else:
                results.append(r)
        return {"query": raw, "results": results, "count": len(results)}

    def _collection(self, tool, q, args) -> Dict[str, Any]:
        """Return raw sub-objects from the KB (e.g. a dict keyed by name)."""
        data = _dig(self.kb, q.get("collection", ""))
        if data is None:
            return {"found": False, "error": "not found"}
        if isinstance(data, dict):
            return {"items": list(data.values()), "count": len(data)}
        if isinstance(data, list):
            return {"items": data, "count": len(data)}
        return {"item": data, "found": True}

    # -- videos ----------------------------------------------------------

    def _videos(self, tool, q, args) -> Dict[str, Any]:
        """Find video guides, resolved against a per-entity video index.

        The KB is expected to hold:
          <videos collection>  - a list of video records, each with an id
                                  and optional title/author/scope/notes
          <entity collection>  - the entities, each with a "videos" list of
                                  video ids and a "video_chapters" map

        "dedicated" is a property of the (entity, video) PAIR, not of the
        video: a roundup is dedicated for the entity it was made for and a
        roundup for every other entity. Getting this wrong labels a
        roundup as a dedicated fight video.
        """
        vcoll = q.get("videos_collection", "videos")
        ecoll = q.get("entities_collection", "entities")
        videos = _iter_section(self.kb, vcoll)
        entities = _iter_section(self.kb, ecoll)
        vmap = {v.get("id"): v for v in videos if isinstance(v, dict)}

        param = (q.get("params") or [None])[0]
        raw = (args.get(param) or "").strip() if param else ""
        # Fall back to the first string argument when the manifest does not
        # name one. Without this a caller that omits "params" silently gets
        # an empty result instead of the record it asked for.
        if not param:
            for k, v in (args or {}).items():
                if isinstance(v, str) and v.strip():
                    raw = v.strip()
                    break
        match_field = q.get("match", "name")
        include = q.get("include")
        limit = q.get("limit", 20)

        def _entity_videos(ent: Dict[str, Any]) -> List[Dict[str, Any]]:
            """Build the video list for one entity, with chapter deep-links."""
            chapters = ent.get("video_chapters", {}) or {}
            out = []
            for vid in (ent.get("videos") or []):
                v = vmap.get(vid) or {}
                ch = chapters.get(vid, "")
                url = v.get("url") or f"https://www.youtube.com/watch?v={vid}"
                if ch and "&t=" not in url:
                    try:
                        mm, ss = str(ch).split(":")
                        url = f"{url}&t={int(mm) * 60 + int(ss)}s"
                    except Exception:
                        pass
                out.append({
                    "id": vid,
                    "title": v.get("title", vid),
                    "author": v.get("author", ""),
                    "url": url,
                    "entity": ent.get(match_field, ""),
                    # dedicated = per PAIR
                    "dedicated": bool(v.get("dedicated"))
                    and v.get(q.get("video_entity_field", "entity"))
                    == ent.get(match_field),
                    "chapter": ch,
                    "note": v.get("note", ""),
                    "upload_date": v.get("upload_date", ""),
                })
            return out

        # --- no argument: every video, grouped by entity -----------------
        if not raw:
            out = []
            for v in videos:
                if not isinstance(v, dict):
                    continue
                rec = {
                    "id": v.get("id"),
                    "title": v.get("title"),
                    "author": v.get("author"),
                    "scope": v.get("scope"),
                    "tags": v.get("tags", []),
                    "entity": v.get(q.get("video_entity_field", "entity"), "ALL"),
                    "note": v.get("note", ""),
                    "upload_date": v.get("upload_date", ""),
                    "dedicated": bool(v.get("dedicated")),
                    "url": v.get("url")
                    or f"https://www.youtube.com/watch?v={v.get('id')}",
                }
                out.append(rec)
            return {"items": out[:limit], "count": len(out)}

        # --- find the entity ---------------------------------------------
        want = set(norm_name(raw))
        want_str = " ".join(want)
        hit = None
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            nm = " ".join(norm_name(str(ent.get(match_field, ""))))
            if nm == want_str or (nm and (nm in want_str or want_str in nm)):
                hit = ent
                break
        if hit is None and want:
            best, score = None, 0.0
            for ent in entities:
                if not isinstance(ent, dict):
                    continue
                nm = set(norm_name(str(ent.get(match_field, ""))))
                if not nm:
                    continue
                inter = len(want & nm) / min(len(want), len(nm))
                if inter > score:
                    best, score = ent, inter
            if score >= 0.5:
                hit = best

        if hit is None:
            return {"found": False, "error": f"no match for '{raw}'",
                    "available": [str(e.get(match_field)) for e in entities
                                  if isinstance(e, dict)][:60]}

        return {
            "found": True,
            "entity": hit.get(match_field),
            "videos": _entity_videos(hit),
            "has_dedicated_video": any(
                v["dedicated"] for v in _entity_videos(hit)),
            "no_dedicated_note": hit.get("no_dedicated_note", ""),
        }

    # -- composite modes -------------------------------------------------
    # These are still fully data-driven: every path they read is named in
    # the pack manifest. The logic (how a sector relates to a POI, how a
    # boss relates to a video) is generic; the *names* are the pack's.

    def _sector(self, tool, q, args) -> Dict[str, Any]:
        """Look up a grid sector, or search POIs by name."""
        kb = self.kb
        sparams = q.get("params") or ["sector", "query"]
        sector = (args.get(sparams[0]) or "").strip().upper()
        query = (args.get(sparams[1]) if len(sparams) > 1 else "") or ""
        query = query.strip()
        if not sector and not query:
            for _k, _v in (args or {}).items():
                if isinstance(_v, str) and _v.strip():
                    if _v.strip().upper() == _v.strip() and len(_v.strip()) <= 4:
                        sector = _v.strip().upper()
                    else:
                        query = _v.strip()
                    break

        if sector:
            gi = kb.get("grid_index", {}) or {}
            entry = gi.get(sector) or []
            pois = list(entry) if isinstance(entry, list) else []
            seen = {p.get("name") for p in pois}
            for p in kb.get("pois", []) or []:
                if (p.get("grid") or "").upper() == sector \
                        and p.get("name") not in seen:
                    pois.append({
                        "name": p.get("name"), "label": p.get("label"),
                        "threat": p.get("threat") or p.get("threats"),
                        "place": p.get("place"), "layer": p.get("layer"),
                        "text": (p.get("text") or "")[:200]})
                    seen.add(p.get("name"))
            keys = [k for k in (kb.get("keys", []) or [])
                    if (k.get("grid") or "").upper() == sector]
            ops = []
            for slug, op in (kb.get("operations") or {}).items():
                og = (op.get("grid") or "").upper()
                isec = op.get("in_sector") or []
                if sector == og or sector in isec:
                    ops.append({"slug": slug, "grid": op.get("grid"),
                                "place": op.get("place"),
                                "threat": (op.get("detail") or {})
                                .get("Threat assessment")})
            out = {"sector": sector, "pois": [], "keys": [], "operations": ops}
            for p in pois[:40]:
                out["pois"].append({
                    "name": p.get("name"), "label": p.get("label"),
                    "threat": p.get("threat") or (p.get("threats") or []),
                    "place": p.get("place"),
                    "text": (p.get("text") or "")[:220]})
            for k in keys:
                d = k.get("dexerto") or {}
                out["keys"].append({
                    "name": k.get("name"), "rarity": d.get("rarity"),
                    "how_to_get": d.get("how_to_get"),
                    "location": d.get("location")})
            return out

        if query:
            t = set(toks(query))
            scored = []
            for p in kb.get("pois", []) or []:
                hay = " ".join(str(p.get(k, "")) for k in
                               ("name", "label", "place", "text", "sub")).lower()
                ht = set(toks(hay))
                hit = len(t & ht)
                if hit:
                    nm = (p.get("name") or "").lower()
                    scored.append((hit + (10 if query.lower() in nm else 0), p))
            scored.sort(key=lambda x: -x[0])
            return {"query": query, "results": [
                {"name": p.get("name"), "label": p.get("label"),
                 "threat": p.get("threat") or p.get("threats"),
                 "grid": p.get("grid"), "place": p.get("place"),
                 "text": (p.get("text") or "")[:200]}
                for _, p in scored[:12]]}
        return {"error": "need sector or query"}

    def _boss(self, tool, q, args) -> Dict[str, Any]:
        """Look up an entity (a boss) with its video links attached."""
        kb = self.kb
        entities = kb.get("entities", []) or []
        strategies = kb.get("boss_strategies", []) or []
        bparam = (q.get("params") or ["boss"])[0]
        query = (args.get(bparam) or "").strip().lower()
        if not query:
            # fall back to the first string argument
            for _k, _v in (args or {}).items():
                if isinstance(_v, str) and _v.strip():
                    query = _v.strip().lower()
                    break

        if not query:
            return {
                "found": True,
                "total": len(entities),
                "items": [{"name": e.get("name"), "kind": e.get("kind"),
                           "grid": e.get("grid"), "drops": e.get("drops"),
                           "weakness": e.get("weakness"),
                           "difficulty": e.get("difficulty")}
                          for e in entities],
                "strategies": [{"topic": s.get("topic"),
                                "detail": s.get("detail")} for s in strategies],
            }

        def norm(s):
            return re.sub(r"^(the|lt|lieutenant|commander|cmd)\s+", "",
                          str(s).lower().strip())

        nq = norm(query)
        for e in entities:
            if norm(e.get("name")) == nq or (nq and nq in norm(e.get("name"))):
                out = {k: v for k, v in e.items() if not k.startswith("_")}
                out["found"] = True
                out["videos"] = self._videos(tool, {
                    "videos_collection": "videos",
                    "entities_collection": "entities",
                    "match": "name",
                    "video_entity_field": "entity",
                    "params": ["topic"],
                }, {"topic": e.get("name")}).get("videos", [])
                out["strategies"] = [{"topic": s.get("topic"),
                                      "detail": s.get("detail")}
                                     for s in strategies]
                return out

        # generic kind query: "commander", "lieutenant", "boss"
        for kind_word, kind in (("commander", "Commander"),
                                ("lieutenant", "Lieutenant"),
                                ("boss", None)):
            if query in (kind_word, kind_word + "s"):
                items = [e for e in entities
                         if kind is None or e.get("kind") == kind]
                return {"found": True, "kind": f"{kind or 'Boss'} list",
                        "items": [{"name": e.get("name"),
                                   "kind": e.get("kind"),
                                   "grid": e.get("grid"),
                                   "drops": e.get("drops"),
                                   "difficulty": e.get("difficulty"),
                                   "where": e.get("where")}
                                  for e in items]}

        hits = [e for e in entities if query in json.dumps(e).lower()]
        if hits:
            return {"found": True, "kind": "match",
                    "items": [{"name": e.get("name"), "kind": e.get("kind"),
                               "grid": e.get("grid"), "drops": e.get("drops")}
                              for e in hits]}
        return {"found": False, "error": f"no boss matching '{query}'",
                "available": [e.get("name") for e in entities]}

    def _item(self, tool, q, args) -> Dict[str, Any]:
        """Look up a loot item or a printer recipe."""
        kb = self.kb
        iparam = (q.get("params") or ["item"])[0]
        item = (args.get(iparam) or "").strip()
        if not item:
            for _k, _v in (args or {}).items():
                if isinstance(_v, str) and _v.strip():
                    item = _v.strip()
                    break
        t = toks(item)
        for it in kb.get("loot_items", []) or []:
            nm = (it.get("name") or "").lower()
            itk = toks(nm)
            if item.lower() == nm or (t and t[0] in itk) \
                    or " ".join(norm_name(nm)) == " ".join(norm_name(item)):
                return {
                    "name": it.get("name"), "rarity": it.get("rarity"),
                    "category": it.get("category"), "tier": it.get("tier"),
                    "value": it.get("value"), "stack": it.get("stack"),
                    "description": (it.get("description") or "")[:300],
                    "spots": it.get("spots"),
                    "how_to_get": it.get("how_to_get") or it.get("howToGet"),
                    "recipe": it.get("recipe"),
                    "recipe_not_revealed": it.get("recipe_not_revealed",
                                                 it.get("recipeNotRevealed")),
                    "use": it.get("use"),
                    "place_grids": it.get("place_grids"),
                }
        recipes = kb.get("printer_recipes") or []
        if isinstance(recipes, dict):
            recipes = list(recipes.values())
        ri = kb.get("recipe_index") or {}
        for r in (recipes + list(ri.values() if isinstance(ri, dict) else ri)):
            if isinstance(r, dict):
                nm = (r.get("name") or r.get("item") or r.get("slug")
                      or "").lower()
                if t and t[0] in toks(nm):
                    return {"source": "printer_recipe", **r}
        return {"item": item, "found": False}

    def _key(self, tool, q, args) -> Dict[str, Any]:
        """Look up a key by name."""
        kb = self.kb
        kparam = (q.get("params") or ["key"])[0]
        key = (args.get(kparam) or "").strip()
        if not key:
            for _k, _v in (args or {}).items():
                if isinstance(_v, str) and _v.strip():
                    key = _v.strip()
                    break
        kl = kb.get("key_lookup", {}) or {}
        n = " ".join(norm_name(key))
        if n in kl:
            return kl[n]
        t = set(toks(key))
        best, score = None, 0
        for rec in kl.values():
            kt = set(toks(rec.get("name", "")))
            if not kt or not t:
                continue
            inter = len(t & kt) / min(len(t), len(kt))
            if inter > score:
                best, score = rec, inter
        if best and score >= 0.5:
            return best
        return {"key": key, "found": False}

    def _mission(self, tool, q, args) -> Dict[str, Any]:
        """Full briefing for an operation or an order."""
        kb = self.kb
        mparam = (q.get("params") or ["mission"])[0]
        m = (args.get(mparam) or "").strip().lower()
        if not m:
            for _k, _v in (args or {}).items():
                if isinstance(_v, str) and _v.strip():
                    m = _v.strip().lower()
                    break
        ops = kb.get("operations", {}) or {}
        rewards = kb.get("operation_rewards", {}) or {}

        for slug, op in ops.items():
            m_slug = slugify(m)
            key = slug.lower().replace(" ", "-").replace("&", "and")
            rw = rewards.get(key) or rewards.get(slug) or []
            if m == slug.lower() or m_slug == slugify(slug):
                det = op.get("detail", {}) or {}
                return {
                    "name": slug, "grid": op.get("grid"),
                    "place": op.get("place"),
                    "threat": det.get("Threat assessment"),
                    "location": det.get("Location"),
                    "equipment": det.get("Recommended equipment"),
                    "chapter": det.get("Chapter"),
                    "objectives": det.get("Main objectives"),
                    "rewards": det.get("Rewards"),
                    "steps": op.get("steps", []),
                    "key_reward": (rw[0].get("name") if rw else None),
                }

        for o in kb.get("dmz_orders", []) or []:
            if m and (m in (o.get("name") or "").lower()
                      or m in json.dumps(o).lower()):
                return {"kind": "Order", **o}

        # fuzzy: partial name match
        for slug, op in ops.items():
            if m and m in slug.lower():
                det = op.get("detail", {}) or {}
                return {"name": slug, "grid": op.get("grid"),
                        "place": op.get("place"),
                        "threat": det.get("Threat assessment"),
                        "steps": op.get("steps", [])}
        return {"mission": m, "found": False,
                "available": list(ops.keys())}
