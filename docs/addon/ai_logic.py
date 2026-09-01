"""Pure logic for AI card generation: no aqt/anki imports.

Prompt assembly, model-output parsing/validation, mechanical checks, the local
GUID namespace, and usage arithmetic. Everything here is unit-testable with
plain pytest; subprocess and Qt live in ai_cli.py and ai_dialog.py.
"""
import binascii
import json
import os
import re

GUID_PREFIX = "iplocal-"
GENERATED_TAG_LEAF = "Generated"
GENERATED_DECK_LEAF = "Generated"

_FENCE_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.S)
_IMAGE_SOURCE_RE = re.compile(r"^(attached:[\w .\-]+|url:https://\S+|svg:<svg.*)$", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_CLOZE_OK_RE = re.compile(r"\{\{c\d+::[^{}]+?\}\}")
_CLOZE_OPEN_RE = re.compile(r"\{\{c\d+")
PRIMARY_FIELD = {"Study Deck - Basic": "Front", "Study Deck - Cloze": "Text",
                 "Study Deck - Image ID": "Image", "Basic": "Front"}
LONG_ANSWER_WORDS = 60


def generated_guid():
    return GUID_PREFIX + binascii.hexlify(os.urandom(10)).decode()


def is_generated_guid(guid):
    return isinstance(guid, str) and guid.startswith(GUID_PREFIX)


def _find_json(text):
    m = _FENCE_RE.search(text or "")
    if m:
        return m.group(1)
    # fall back to the outermost [...] span
    start, end = (text or "").find("["), (text or "").rfind("]")
    return text[start:end + 1] if 0 <= start < end else text


def parse_cards_json(text, allowed_types, field_map):
    """Parse a model reply into card dicts. Returns (cards, errors); any error
    empties cards, so a caller never imports a half-valid batch silently."""
    errors = []
    try:
        data = json.loads(_find_json(text))
    except Exception as e:
        return [], [f"reply was not valid JSON: {e}"]
    if not isinstance(data, list) or not data:
        return [], ["reply must be a non-empty JSON list of cards"]
    cards = []
    for i, raw in enumerate(data, 1):
        if not isinstance(raw, dict):
            errors.append(f"card {i}: not an object")
            continue
        ntype = raw.get("note_type")
        if ntype not in allowed_types:
            errors.append(f"card {i}: unknown note type {ntype!r}")
            continue
        fields = raw.get("fields")
        if not isinstance(fields, dict):
            errors.append(f"card {i}: missing fields object")
            continue
        known = set(field_map[ntype])
        extra = set(fields) - known
        if extra:
            errors.append(f"card {i}: unknown field(s) {sorted(extra)} for {ntype}")
            continue
        primary = PRIMARY_FIELD.get(ntype, field_map[ntype][0])
        if not str(fields.get(primary, "")).strip():
            errors.append(f"card {i}: empty primary field {primary}")
            continue
        tags_val = raw.get("tags")
        if tags_val is not None and not isinstance(tags_val, list):
            errors.append(f"card {i}: tags must be a list")
            continue
        images = raw.get("images") or []
        bad_img = [im for im in images
                   if not (isinstance(im, dict)
                           and _IMAGE_SOURCE_RE.match(str(im.get("source", ""))))]
        if bad_img:
            errors.append(f"card {i}: invalid image source (allowed: attached:, "
                          f"url:https:, svg:<svg)")
            continue
        cards.append({
            "note_type": ntype,
            "fields": {k: str(fields.get(k, "")) for k in field_map[ntype]},
            "tags": [str(t) for t in (raw.get("tags") or [])],
            "images": [{"source": str(im["source"]),
                        "alt": str(im.get("alt", "")),
                        "attribution": str(im.get("attribution", ""))}
                       for im in images],
            "rationale": str(raw.get("rationale", "")),
        })
    return ([], errors) if errors else (cards, [])


def _plain(html):
    """Strip HTML tags from text."""
    return _TAG_RE.sub("", html or "").strip()


def _norm_front(text):
    """Normalize front text for duplicate detection: strip tags, lowercase, collapse whitespace."""
    return " ".join(_plain(text).lower().split())


def mechanical_checks(cards, existing_fronts):
    """Check drafted cards for duplicates, cloze syntax, and length. existing_fronts
    is {normalized front: original front} for her collection, built collection-side
    with the same _norm_front over _her_front_to_guid keys; {} skips duplicate
    detection (throttled/offline reads must never block generation). Returns one
    list of check-result dicts (code, level, message, optional "existing") per card.
    """
    out = []
    for card in cards:
        entries = []
        ntype, fields = card["note_type"], card["fields"]
        primary = PRIMARY_FIELD.get(ntype, next(iter(fields)))
        norm = _norm_front(fields.get(primary, ""))

        if norm and norm in existing_fronts:
            entries.append({"code": "duplicate", "level": "block",
                            "existing": existing_fronts[norm],
                            "message": "possible duplicate of an existing card"})

        if ntype == "Study Deck - Cloze":
            text = fields.get("Text", "")
            good = _CLOZE_OK_RE.findall(text)
            openers = _CLOZE_OPEN_RE.findall(text)
            if not good:
                entries.append({"code": "cloze", "level": "block",
                                "message": "cloze note has no valid deletion"})
            elif len(openers) > len(good):
                entries.append({"code": "cloze", "level": "block",
                                "message": "malformed cloze braces"})

        answer = fields.get("Back", "") + " " + fields.get("Why", "")
        if len(_plain(answer).split()) > LONG_ANSWER_WORDS:
            entries.append({"code": "long-answer", "level": "warn",
                            "message": "answer is long; consider trimming"})

        if not entries:
            entries = [{"code": "ok", "level": "ok", "message": "checks pass"}]
        out.append(entries)
    return out


_CONTRACT = """## Output contract
Reply with ONLY a JSON list, no prose before or after. Each element:
{"note_type": <one of the allowed types>,
 "fields": {<every field for that type, "" when empty>},
 "tags": [<short topic tags>],
 "images": [{"source": "attached:<filename>" | "url:https://..." | "svg:<svg...>",
             "alt": "<what it shows>", "attribution": "<source credit>"}],
 "rationale": "<one line: why this card earns its place>"}
Never invent an image; never generate a raster image. Images come only from the
attached files, a real web source found during verification, or simple SVG you
draw yourself for structural diagrams."""


def build_prompt(skills, source, note_types, field_map, count, instructions="",
                 attachments=(), cards=None, feedback="", notes=None,
                 checks=None):
    notes = notes or {}
    parts = []
    for s in skills:
        parts.append(s.strip())
    schema = {t: field_map[t] for t in note_types}
    parts.append("## Allowed note types and their fields\n"
                 + json.dumps(schema, indent=1))
    parts.append(_CONTRACT)
    parts.append(f"## Task\nDraft about {count} flashcards from the source "
                 "material below. Quality over count.")
    if instructions.strip():
        parts.append("## User instructions\n" + instructions.strip())
    if attachments:
        parts.append("## Attached files\n"
                     + "\n".join(f"- {name}" for name in attachments))
    parts.append("## Source material\n" + source.strip())
    if cards is not None:
        lines = []
        for i, card in enumerate(cards):
            tag = notes.get(i)
            head = f"### Card {i + 1}" + ("" if tag else " (keep verbatim)")
            lines.append(head + "\n" + json.dumps(
                {"note_type": card["note_type"], "fields": card["fields"],
                 "tags": card["tags"], "images": card["images"]}, indent=1))
            if tag:
                lines.append(f"Revision note for card {i + 1}: {tag}")
            if checks and any(c["level"] != "ok" for c in checks[i]):
                lines.append("Automated checks: " + "; ".join(
                    c["message"] for c in checks[i] if c["level"] != "ok"))
        parts.append("## Current draft\nRevise per the notes and feedback. "
                     "Return the FULL updated list. Cards marked keep verbatim "
                     "must be returned unchanged.\n" + "\n".join(lines))
        if feedback.strip():
            parts.append("## Feedback on the whole set\n" + feedback.strip())
    return "\n\n".join(parts) + "\n"


_WEB_TOOLS = {"WebSearch", "WebFetch", "web_search", "web_fetch",
              "google_web_search"}
_WINDOW_S = 7 * 86400


def _num(v, cast):
    """Coerce a value pulled out of untrusted vendor JSON, or fall back to 0."""
    try:
        return cast(v)
    except (TypeError, ValueError):
        return cast(0)


def _usage_tokens(usage):
    if not isinstance(usage, dict):
        return 0
    # total_tokens, when present, already covers the input/output components;
    # summing both would double-count, so prefer it and never add the two.
    if "total_tokens" in usage:
        return _num(usage.get("total_tokens"), int)
    return sum(_num(usage.get(k, 0), int) for k in
               ("input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "output_tokens"))


def _as_dict(v):
    return v if isinstance(v, dict) else {}


def parse_stream_event(kind, line):
    """Parse one line of subprocess output from a vendor CLI. Fed raw, possibly
    malformed JSON straight from a subprocess, so any shape that isn't exactly
    what's expected (missing keys, wrong nested types) must return None rather
    than raise."""
    try:
        d = json.loads(line)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    t = d.get("type")
    if kind == "claude":
        if t == "result":
            if "result" not in d:
                return None  # not a real result line, nothing to report
            return {"type": "result", "text": d.get("result") or "",
                    "tokens": _usage_tokens(d.get("usage"))}
        if t == "assistant":
            content = _as_dict(d.get("message")).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name", "")
                    if name in _WEB_TOOLS:
                        return {"type": "phase", "phase": "Verify online"}
                    return {"type": "phase", "phase": "Working"}
        return None
    if kind == "codex":
        if t == "token_count":
            rl = d.get("rate_limits")
            if isinstance(rl, dict) and rl:
                primary_raw, secondary_raw = rl.get("primary"), rl.get("secondary")
                if not isinstance(primary_raw, dict) and not isinstance(secondary_raw, dict):
                    return None  # rate_limits present but unusable; not a "no limits" line
                primary, secondary = _as_dict(primary_raw), _as_dict(secondary_raw)
                return {"type": "rate_limits",
                        "primary_pct": _num(primary.get("used_percent"), float),
                        "secondary_pct": _num(secondary.get("used_percent"), float),
                        "resets": primary.get("resets_at") or ""}
            info = d.get("info")
            return ({"type": "usage", "tokens": _usage_tokens(info)}
                    if isinstance(info, dict) else None)
        if t in ("item.completed", "turn.completed") and d.get("text"):
            return {"type": "result", "text": d["text"],
                    "tokens": _usage_tokens(d.get("usage"))}
        return None
    if kind == "agy":
        if t == "result":
            return {"type": "result", "text": d.get("result") or d.get("text")
                    or "", "tokens": _usage_tokens(d.get("usage"))}
        if t == "step_update":
            return {"type": "phase", "phase": str(d.get("step_type") or
                                                  "Working")}
        return None
    return None


def record_usage(reg, kind, tokens, now):
    runs = [r for r in reg.get(kind, []) if now - r["ts"] <= _WINDOW_S]
    runs.append({"ts": now, "tokens": int(tokens)})
    reg = dict(reg)
    reg[kind] = runs
    return reg


def usage_line(reg, kind, now, free_tier=False):
    runs = [r for r in reg.get(kind, []) if now - r["ts"] <= _WINDOW_S]
    today = [r for r in runs if now - r["ts"] <= 86400]
    tokens = sum(r["tokens"] for r in today)
    line = (f"Today via this add-on: {len(today)} runs, "
            f"~{round(tokens / 1000)}k tokens")
    if free_tier:
        line += f" ({len(today)} runs today on the free tier)"
    return line


def rate_limit_line(evt):
    # int() truncation, not :.0f: Python's format rounds .5 to even (87.5 -> "88"),
    # which overstates percent left; truncating to 87 is the conservative direction.
    return (f"5h window {int(100 - evt['primary_pct'])}% left, "
            f"week {int(100 - evt['secondary_pct'])}% left"
            + (f", resets {evt['resets']}" if evt.get("resets") else ""))


_SKILL_PATH = os.path.join(os.path.dirname(__file__), "skills",
                           "internpearls_authoring", "SKILL.md")


def load_bundled_skill():
    with open(_SKILL_PATH, encoding="utf8") as fh:
        return fh.read()


def active_skills(deck_skill):
    skills = [load_bundled_skill()]
    if deck_skill and deck_skill.get("enabled") and deck_skill.get("text"):
        skills.append(deck_skill["text"])
    return skills
