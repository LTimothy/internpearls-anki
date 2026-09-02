"""Pure logic for AI card generation: no aqt/anki imports.

Prompt assembly, model-output parsing/validation, mechanical checks, the local
GUID namespace, and usage arithmetic. Everything here is unit-testable with
plain pytest; subprocess and Qt live in ai_cli.py and ai_dialog.py.
"""
import binascii
import hashlib
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
# svg_to_media's reject list: a <script> element, an on*= event-handler attribute
# (whitespace before "=" and any case), or a javascript: URI. \bon\w+ requires "on" to
# start a word, so it doesn't false-positive on ordinary attribute/value text like
# "none" or "font-size".
_SVG_SCRIPT_RE = re.compile(r"<script", re.I)
_SVG_EVENT_ATTR_RE = re.compile(r"\bon\w+\s*=", re.I)
_SVG_JS_URI_RE = re.compile(r"javascript\s*:", re.I)
PRIMARY_FIELD = {"Study Deck - Basic": "Front", "Study Deck - Cloze": "Text",
                 "Study Deck - Image ID": "Image", "Basic": "Front", "Cloze": "Text"}
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


def mechanical_checks(cards, existing_fronts, image_errors=None):
    """Check drafted cards for duplicates, cloze syntax, length, and image
    resolution failures. existing_fronts is {normalized front: original front}
    for her collection, built collection-side with the same _norm_front over
    _her_front_to_guid keys; {} skips duplicate detection (throttled/offline
    reads must never block generation). image_errors is {card index: [message,
    ...]}, one entry per image that failed to resolve (download, decode, or
    read): computed by the caller (ai_dialog, which owns the network/disk
    access this module deliberately has none of) and passed in as plain data,
    so this stays a pure function. Returns one list of check-result dicts
    (code, level, message, optional "existing") per card.
    """
    image_errors = image_errors or {}
    out = []
    for i, card in enumerate(cards):
        entries = []
        ntype, fields = card["note_type"], card["fields"]
        primary = PRIMARY_FIELD.get(ntype, next(iter(fields)))
        norm = _norm_front(fields.get(primary, ""))

        for msg in image_errors.get(i, []):
            entries.append({"code": "image", "level": "block",
                            "message": f"image could not be used: {msg}"})

        if norm and norm in existing_fronts:
            entries.append({"code": "duplicate", "level": "block",
                            "existing": existing_fronts[norm],
                            "message": "possible duplicate of an existing card"})

        # Keyed on the primary field being "Text" (a cloze-style deletion field),
        # not on the exact note type name, so a core "Cloze" note is validated
        # the same as "Study Deck - Cloze" rather than skipping the check.
        if primary == "Text":
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


_MODE_INSTRUCTIONS = {
    "thorough": ("## Mode: Thorough\nDraft the cards. Then verify the factual "
                "claims against outside sources you can reach, correcting anything "
                "wrong or unsupported. Finally self-review the whole set against "
                "the rules above before returning it."),
    "quick": ("## Mode: Quick\nProduce the cards in a single pass. Do not spend "
             "turns verifying facts online or self-reviewing; return your first "
             "draft."),
}


def build_prompt(skills, source, note_types, field_map, count, instructions="",
                 attachments=(), cards=None, feedback="", notes=None,
                 checks=None, mode="thorough"):
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
    parts.append(_MODE_INSTRUCTIONS.get(mode, _MODE_INSTRUCTIONS["thorough"]))
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
            text = d.get("result") or ""
            # subtype stays "success" even when is_error is true; the CLI's
            # human-readable failure explanation (e.g. an expired login) rides
            # in the same "result" field a successful run uses for card text,
            # so this must be split out here rather than left for a caller to
            # mistake for the model's reply.
            if d.get("is_error") is True:
                return {"type": "error", "text": text}
            return {"type": "result", "text": text,
                    "tokens": _usage_tokens(d.get("usage"))}
        if t == "assistant":
            content = _as_dict(d.get("message")).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name", "")
                    if isinstance(name, str) and name in _WEB_TOOLS:
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
        if t in ("item.completed", "turn.completed"):
            # Shape unconfirmed against a live CLI (neither codex nor
            # Antigravity is installed here): accept text either top-level or
            # nested under "item", so whichever one the vendor actually
            # emits still gets picked up rather than silently discarded.
            text = d.get("text")
            if not isinstance(text, str) or not text:
                nested = _as_dict(d.get("item")).get("text")
                text = nested if isinstance(nested, str) and nested else None
            if text:
                return {"type": "result", "text": text,
                        "tokens": _usage_tokens(d.get("usage"))}
        return None
    if kind == "agy":
        if t == "result":
            text = d.get("result") or d.get("text")
            if not isinstance(text, str) or not text:
                nested = _as_dict(d.get("item")).get("text")
                text = nested if isinstance(nested, str) else ""
            return {"type": "result", "text": text or "",
                    "tokens": _usage_tokens(d.get("usage"))}
        if t == "step_update":
            return {"type": "phase", "phase": str(d.get("step_type") or
                                                  "Working")}
        return None
    return None


def _usage_runs(reg, kind):
    """Recorded usage rows for one backend, tolerating a hand-edited or corrupt
    ai_usage.json the same way _duration_runs does: a row that isn't a dict, or
    whose "ts"/"tokens" isn't a plain number, is dropped rather than raising.
    Without this, a single bad entry raised out of usage_line, called from
    dialog construction, so the wizard would fail to even open."""
    runs = reg.get(kind)
    if not isinstance(runs, list):
        return []
    out = []
    for r in runs:
        if not isinstance(r, dict):
            continue
        ts = r.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        tokens = r.get("tokens", 0)
        if not isinstance(tokens, (int, float)) or isinstance(tokens, bool):
            tokens = 0
        out.append({"ts": float(ts), "tokens": int(tokens)})
    return out


def record_usage(reg, kind, tokens, now):
    runs = [r for r in _usage_runs(reg, kind) if now - r["ts"] <= _WINDOW_S]
    runs.append({"ts": now, "tokens": int(tokens)})
    reg = dict(reg)
    reg[kind] = runs
    return reg


def usage_line(reg, kind, now, free_tier=False):
    runs = [r for r in _usage_runs(reg, kind) if now - r["ts"] <= _WINDOW_S]
    today = [r for r in runs if now - r["ts"] <= 86400]
    tokens = sum(r["tokens"] for r in today)
    line = (f"Today via this add-on: {len(today)} runs, "
            f"~{round(tokens / 1000)}k tokens")
    if free_tier:
        line += f" ({len(today)} runs today on the free tier)"
    return line


DURATION_WINDOW = 10   # last N runs kept per backend+mode
MODE_LABELS = {"thorough": "Thorough", "quick": "Quick"}


def _duration_runs(reg, key):
    """Recorded durations for one backend+mode key, or [] for anything a
    hand-edited state file could hold that isn't a clean list of numbers."""
    durations = reg.get("durations")
    if not isinstance(durations, dict):
        return []
    runs = durations.get(key)
    if not isinstance(runs, list):
        return []
    out = []
    for v in runs:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(float(v))
    return out


def record_duration(reg, kind, mode, seconds):
    """Record one completed run's duration for kind+mode, keeping only the
    most recent DURATION_WINDOW. A corrupt existing "durations" block (wrong
    type, garbage entries) is dropped rather than propagated."""
    key = f"{kind}-{mode}"
    runs = _duration_runs(reg, key) + [float(seconds)]
    reg = dict(reg)
    durations = dict(reg.get("durations")) if isinstance(reg.get("durations"), dict) else {}
    durations[key] = runs[-DURATION_WINDOW:]
    reg["durations"] = durations
    return reg


def median_duration(reg, kind, mode):
    """Median duration (seconds) of the recorded runs for kind+mode, or None
    with no history."""
    runs = sorted(_duration_runs(reg, f"{kind}-{mode}"))
    if not runs:
        return None
    n, mid = len(runs), len(runs) // 2
    return runs[mid] if n % 2 else (runs[mid - 1] + runs[mid]) / 2


def format_duration(seconds):
    """Human duration: "48s", "1m 40s", "2m"."""
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    m, s = divmod(total, 60)
    return f"{m}m {s}s" if s else f"{m}m"


def duration_estimate_line(reg, kind, mode):
    """"your recent Thorough runs averaged 1m 40s", or None with no history
    for this backend+mode. Never invented: the caller shows elapsed time
    alone when this is None."""
    median = median_duration(reg, kind, mode)
    if median is None:
        return None
    label = MODE_LABELS.get(mode, mode.capitalize())
    return f"your recent {label} runs averaged {format_duration(median)}"


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


USER_SKILL_MAX_CHARS = 20000


def active_skills(deck_skill, user_skill=""):
    """Bundled first, then a consented deck skill, then the learner's own
    rules. The order is the cache-friendly stable prefix: the part that
    changes least goes first."""
    skills = [load_bundled_skill()]
    if deck_skill and deck_skill.get("enabled") and deck_skill.get("text"):
        skills.append(deck_skill["text"])
    user = (user_skill or "").strip()
    if user:
        skills.append(user)
    return skills


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_VENDOR_DIR = os.path.join(os.path.dirname(__file__), "vendor")
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _pypdf():
    """Import the vendored pypdf, adding its directory to sys.path on first
    use only. Never imported at module load time, so a build missing the
    vendor directory still lets a normal Anki launch import this module."""
    import sys
    if _VENDOR_DIR not in sys.path:
        sys.path.insert(0, _VENDOR_DIR)
    import pypdf
    return pypdf


def extract_attachment(path, dest_dir):
    """Extract source material from one attached file. Images are copied into
    dest_dir as-is; a PDF's text is returned and its embedded images are
    written into dest_dir under collision-safe names. A page that fails to
    extract text is skipped rather than failing the whole document. Raises
    ValueError for an unsupported extension, or a PDF that can't be parsed at
    all (encrypted, corrupt, or not really a PDF).

    The returned dict's "images_undecoded" is True when the PDF has embedded
    images this environment could not decode: in practice, pypdf needs
    Pillow to do it and Anki's bundled Python doesn't carry it, so a real
    user's PDF images silently produce "images": [] otherwise, which reads
    exactly like a PDF that never had any. Listing a page's image *ids* needs
    no Pillow (it's pure PDF-structure parsing), only decoding one does, so
    that count is used to tell "no images" apart from "images present, can't
    decode them here" without needing Pillow to answer the question."""
    ext = os.path.splitext(path)[1].lower()
    base = os.path.basename(path)
    # stem sanitized so a hostile filename can't traverse dest_dir or collide
    # with another attachment's output; same scheme used for PDF-embedded images below
    stem = _SAFE_STEM_RE.sub("_", os.path.splitext(base)[0]) or "attachment"
    if ext in IMAGE_EXTS:
        with open(path, "rb") as fh:
            data = fh.read()
        # content hash disambiguates two distinct attachments sharing a basename
        # (e.g. two "figure1.png" uploads) without needing to see dest_dir's state
        digest = hashlib.sha256(data).hexdigest()[:8]
        name = f"{stem}-{digest}{ext}"
        with open(os.path.join(dest_dir, name), "wb") as fh:
            fh.write(data)
        return {"text": "", "images": [name], "images_undecoded": False}
    if ext != ".pdf":
        raise ValueError(f"unsupported attachment type: {ext}")

    try:
        pypdf = _pypdf()
        reader = pypdf.PdfReader(path)
        pages = reader.pages
    except Exception as e:
        raise ValueError(f"could not read PDF {base}: {e}") from e

    texts, images = [], []
    images_undecoded = False
    for pnum, page in enumerate(pages, 1):
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            pass
        if images_undecoded:
            continue   # already learned Pillow's unavailable this run; it won't be for the next page either
        try:
            page_images = page.images
            count = len(page_images)
        except Exception:
            continue   # this page's image list itself didn't parse; its text still made it in above
        for inum in range(count):
            try:
                img = page_images[inum]
            except ImportError:
                # pypdf needs Pillow only to decode an image, not to list its
                # id: this is the "present but undecodable" case, not "no images"
                images_undecoded = True
                break
            except Exception:
                continue   # this one image didn't decode; the rest of the page still can
            # pypdf's own docs warn img.name "can contain arbitrary
            # characters" (it's read from the PDF's internal resource
            # naming) - sanitize before it becomes a filename extension
            raw_ext = os.path.splitext(img.name)[1].lstrip(".").lower()
            img_ext = "." + raw_ext if re.fullmatch(r"[a-z0-9]{1,5}", raw_ext) else ".png"
            name = f"{stem}-p{pnum}-img{inum}{img_ext}"
            with open(os.path.join(dest_dir, name), "wb") as fh:
                fh.write(img.data)
            images.append(name)

    return {"text": "\n".join(t for t in texts if t.strip()), "images": images,
            "images_undecoded": images_undecoded}


def svg_to_media(markup, index):
    """Model-drawn SVG as a media file: (filename, bytes), or ValueError for anything
    that isn't SVG or carries a script vector. SVG renders inside Anki's own webview, so
    a <script> element, an on*= event-handler attribute, or a javascript: URI is
    executable content on a card, not decoration; rejected rather than sanitized. `index`
    is coerced to int so it can never carry a path separator into the filename."""
    m = (markup or "").strip()
    if not m.startswith("<svg"):
        raise ValueError("not svg markup")
    if _SVG_SCRIPT_RE.search(m):
        raise ValueError("svg with a <script> element rejected")
    if _SVG_EVENT_ATTR_RE.search(m):
        raise ValueError("svg with an event-handler attribute rejected")
    if _SVG_JS_URI_RE.search(m):
        raise ValueError("svg with a javascript: URI rejected")
    return f"generated-{int(index)}.svg", m.encode("utf8")
