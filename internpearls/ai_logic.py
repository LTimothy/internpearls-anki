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
PRIMARY_FIELD = {"Study Deck - Basic": "Front", "Study Deck - Cloze": "Text",
                 "Study Deck - Image ID": "Image", "Basic": "Front"}


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
            errors.append(f"card {i}: unknown field(s) {sorted(extra)} "
                          f"(field 'Sideways' style) for {ntype}")
            continue
        primary = PRIMARY_FIELD.get(ntype, field_map[ntype][0])
        if not str(fields.get(primary, "")).strip():
            errors.append(f"card {i}: empty primary field {primary}")
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
