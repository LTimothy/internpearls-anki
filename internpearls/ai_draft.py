"""One saved AI draft and its resolved pictures per collection and source."""
import datetime
import hashlib
import json
import os
import re
import shutil
import tempfile

from . import config

STATE_FIELDS = ("cards", "included", "notes", "checks", "updated", "verdicts",
                "edited_since_check", "imported", "deck_name", "mode", "count",
                "source", "instructions", "note_types", "revision_shape_mismatch")
INDEX_MAPS = ("notes", "verdicts", "image_data")
INDEX_SETS = ("updated", "edited_since_check", "imported", "decided")


def draft_path():
    return config._collection_state_path(config.AI_DRAFT)


def _image_folder(path):
    return os.path.join(os.path.dirname(path), "ai_draft_images")


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        config.replace_file(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def save(path, state, image_data, scratch):
    state = dict(state, version=1, saved_at=datetime.datetime.now().isoformat(
        timespec="seconds"))
    folder = _image_folder(path)
    images, used = {}, set()
    files = {}
    for card in state["cards"]:
        for image in card["images"]:
            kind, _, name = image["source"].partition(":")
            if kind not in ("file", "attached") or not scratch or not _local_name(name):
                continue
            src = os.path.join(scratch, name)
            if os.path.islink(src) or not os.path.isfile(src):
                continue
            ext = os.path.splitext(name)[1].lstrip(".").lower()
            if ext not in ("svg", "png", "jpg", "jpeg", "gif", "webp"):
                continue
            with open(src, "rb") as fh:
                data = fh.read()
            asset = hashlib.sha256(data).hexdigest() + "." + ext
            dest = os.path.join(folder, asset)
            if not os.path.isfile(dest):
                _write(dest, data)
            used.add(asset)
            files[name] = asset
    for i, per in image_data.items():
        images[i] = []
        for j, result in enumerate(per):
            item = {k: result[k] for k in ("state", "kind", "name", "ext", "host", "error")
                    if k in result}
            if result.get("state") == "ok":
                data = result["bytes"]
                ext = (result.get("ext") or
                       os.path.splitext(result.get("name", ""))[1].lstrip(".") or "svg").lower()
                if ext not in ("svg", "png", "jpg", "jpeg", "gif", "webp"):
                    raise ValueError("Invalid draft image type")
                name = hashlib.sha256(data).hexdigest() + "." + ext
                dest = os.path.join(folder, name)
                if not os.path.isfile(dest):
                    _write(dest, data)
                used.add(name)
                item["asset"] = name
            images[i].append(item)
    state["image_data"] = images
    state["scratch_files"] = files
    _write(path, json.dumps(state, ensure_ascii=False, indent=2).encode("utf8"))
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            if name not in used:
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass


def _validate(state, field_map):
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("Invalid draft version")
    datetime.datetime.fromisoformat(state["saved_at"])
    cards = state["cards"]
    if not isinstance(cards, list) or not cards:
        raise ValueError("Invalid draft cards")
    for card in cards:
        if not isinstance(card, dict) or card.get("note_type") not in field_map:
            raise ValueError("Invalid draft note type")
        fields = card["fields"]
        if not isinstance(fields, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in fields.items()):
            raise ValueError("Invalid draft fields")
        if not isinstance(card["tags"], list) or not all(
                isinstance(tag, str) for tag in card["tags"]):
            raise ValueError("Invalid draft tags")
        if not isinstance(card["images"], list) or not all(
                isinstance(im, dict) and isinstance(im.get("source"), str)
                and isinstance(im.get("attribution", ""), str) for im in card["images"]):
            raise ValueError("Invalid draft images")
        if not isinstance(card["rationale"], str):
            raise ValueError("Invalid draft rationale")
    n = len(cards)
    if (not isinstance(state["included"], list) or len(state["included"]) != n
            or not all(isinstance(v, bool) for v in state["included"])):
        raise ValueError("Invalid draft decisions")
    checks = state["checks"]
    if not isinstance(checks, list) or len(checks) != n:
        raise ValueError("Invalid draft checks")
    for per in checks:
        if not isinstance(per, list) or not all(
                isinstance(c, dict) and c.get("level") in ("block", "warn", "info", "ok")
                and isinstance(c.get("code"), str) and isinstance(c.get("message"), str)
                for c in per):
            raise ValueError("Invalid draft checks")
    for key in INDEX_MAPS:
        if not isinstance(state[key], dict):
            raise ValueError("Invalid draft map")
        state[key] = {int(i): v for i, v in state[key].items()}
        if any(i < 0 or i >= n for i in state[key]):
            raise ValueError("Invalid draft card index")
    for key in INDEX_SETS:
        if not isinstance(state[key], list) or any(
                type(i) is not int or i < 0 or i >= n for i in state[key]):
            raise ValueError("Invalid draft card index")
        state[key] = set(state[key])
    if not all(isinstance(v, str) for v in state["notes"].values()):
        raise ValueError("Invalid draft notes")
    for verdict in state["verdicts"].values():
        if (not isinstance(verdict, dict)
                or verdict.get("verdict") not in ("confirmed", "corrected", "unverified")
                or not isinstance(verdict.get("note"), str)
                or not isinstance(verdict.get("sources"), list)):
            raise ValueError("Invalid draft verdict")
        correction = verdict.get("correction")
        if correction is not None and (not isinstance(correction, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in correction.items())):
            raise ValueError("Invalid draft correction")
        if not all(isinstance(src, dict) and isinstance(src.get("title"), str)
                   and isinstance(src.get("url"), str) for src in verdict["sources"]):
            raise ValueError("Invalid draft sources")
    for key in ("source", "instructions", "deck_name", "feedback"):
        if not isinstance(state[key], str):
            raise ValueError("Invalid draft text")
    if (state["mode"] not in ("quick", "thorough")
            or (state["count"] is not None and type(state["count"]) is not int)
            or not isinstance(state["note_types"], list)
            or any(nt not in field_map for nt in state["note_types"])
            or type(state["revision_shape_mismatch"]) is not bool):
        raise ValueError("Invalid draft options")


def load(path, field_map):
    try:
        with open(path, encoding="utf8") as fh:
            state = json.load(fh)
        _validate(state, field_map)
        files = state["scratch_files"]
        if not isinstance(files, dict) or any(not _local_name(name) for name in files):
            raise ValueError("Invalid draft image names")
        state["file_data"] = {name: _read_asset(path, asset) for name, asset in files.items()}
        for i, per in state["image_data"].items():
            if not isinstance(per, list) or len(per) != len(state["cards"][i]["images"]):
                raise ValueError("Invalid resolved images")
            for result in per:
                if not isinstance(result, dict) or result.get("state") not in ("ok", "error"):
                    raise ValueError("Invalid resolved image")
                if result["state"] == "ok":
                    name = result["asset"]
                    result["bytes"] = _read_asset(path, name)
                    if result.get("kind") not in ("url", "svg", "file", "attached"):
                        raise ValueError("Invalid image kind")
                    local = result.get("name", "")
                    if result["kind"] in ("file", "attached") and not _local_name(local):
                        raise ValueError("Invalid scratch filename")
        return state, False
    except FileNotFoundError:
        if not os.path.exists(path):
            return None, False
    except Exception:
        pass
    bad = path + ".bad"
    number = 1
    while os.path.exists(bad):
        bad = path + f".{number}.bad"
        number += 1
    try:
        config.replace_file(path, bad)
    except OSError:
        pass
    return None, True


def _local_name(name):
    return (isinstance(name, str) and bool(name) and os.path.basename(name) == name
            and name not in (".", "..") and "\\" not in name)


def _read_asset(path, name):
    if not isinstance(name, str) or not re.fullmatch(
            r"[0-9a-f]{64}\.(svg|png|jpg|jpeg|gif|webp)", name):
        raise ValueError("Invalid image filename")
    image_path = os.path.join(_image_folder(path), name)
    if os.path.islink(image_path):
        raise ValueError("Invalid image path")
    with open(image_path, "rb") as fh:
        data = fh.read()
    if hashlib.sha256(data).hexdigest() != name.split(".")[0]:
        raise ValueError("Damaged draft image")
    return data


def restore_images(state, scratch):
    for name, data in state.pop("file_data").items():
        _write(os.path.join(scratch, name), data)
    for i, per in state["image_data"].items():
        for j, result in enumerate(per):
            if result["state"] != "ok":
                continue
            name = result.pop("asset")
            if result["kind"] in ("attached", "file"):
                name = result["name"]
            path = os.path.join(scratch, name)
            _write(path, result["bytes"])
            result["path"] = path


def discard(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    shutil.rmtree(_image_folder(path), ignore_errors=True)
