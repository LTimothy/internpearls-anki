"""Work that runs without a menu click: the startup update check and the auto-sync poll.

Two things run on their own: an add-on-update check once per launch, and (only if the
user turned it on in Settings) a repeating poll that auto-syncs decks. Both dispatch
their network work through _run_in_background, which uses Anki's QueryOp to run off
the main thread when it's available, so a slow or dead host never freezes Anki,
however often the poll fires. The only work that touches mw.col (backing up and
importing, once something actually needs to change) still runs on the main thread
inside the completion callback, same as it does for a manual Sync decks click; that
part is unaffected by this and isn't the part that could hang.
"""
import traceback

from aqt import mw
from aqt.qt import QTimer
from aqt.utils import tooltip

# QueryOp is the standard way modern Anki add-ons run work off the main thread. It has
# been part of aqt's public surface since 2.1.45 (2021), which is older than the
# collection APIs this add-on already depends on (ImportAnkiPackageRequest, the
# with_scheduling/wait_for_completion backend options), so it should be present on any
# Anki build that can run this add-on at all. The import is still guarded: if it's ever
# missing, background checks fall back to running inline rather than the whole add-on
# failing to load.
try:
    from aqt.operations import QueryOp
except Exception:
    QueryOp = None

from .collection import _pre_sync_backup_or_skip_silently
from .config import (ADDON_VERSION, AUTO_SYNC_INTERVAL_DEFAULT_MIN,
                     AUTO_SYNC_INTERVAL_FLOOR_MIN, INSTALLED, STATE, _cfg, _load_json,
                     _save_json)
from .logic import clamp_interval_minutes, decide_addon_update_action, decks_to_update
from .net import _BG_TIMEOUT
from .sync import _fetch_manifest, _run_sync
from .ui import _bg_safe
from .updates import _addon_update_work


def _run_in_background(work, on_done):
    """Run `work()` off the main thread when possible, then call `on_done(result, error)`
    back on the main thread either way (`error` is None on success, `result` is None on
    failure). `work` must not touch `mw.col` or any Qt widget, since it may run on a
    worker thread; it should be pure computation plus network/file I/O.

    Uses QueryOp when available (the normal case; see the import guard near the top of
    this file) so the caller genuinely never blocks Anki's UI, no matter how often it's
    invoked. Falls back to calling `work()` directly, bounded by whatever timeout `work`
    itself uses, on any Anki build old enough to lack QueryOp.
    """
    def _safe_on_done(result, error):
        try:
            on_done(result, error)
        except Exception:
            print(traceback.format_exc())

    if QueryOp is not None:
        QueryOp(
            parent=mw,
            op=lambda _col: work(),
            success=lambda result: _safe_on_done(result, None),
        ).failure(lambda exc: _safe_on_done(None, exc)).run_in_background()
    else:
        try:
            result = work()
        except Exception as e:
            _safe_on_done(None, e)
        else:
            _safe_on_done(result, None)


@_bg_safe
def _check_addon_updates_background():
    """Runs once, shortly after Anki starts: fetch the public repo's version info off the
    main thread, then act on it per the Settings toggles (notify only, or auto-install).
    Skips the network call entirely if both toggles are off.
    """
    cfg = _cfg()
    if not (cfg["notify_addon_updates"] or cfg["auto_update_addon"]):
        return

    def _finish(result, error):
        if error or not result:
            return   # offline / GitHub hiccup — stay quiet, try again next launch
        latest = result["info"].get("version", "")
        state = _load_json(STATE, {})
        action = decide_addon_update_action(
            ADDON_VERSION, latest, cfg["auto_update_addon"], cfg["notify_addon_updates"],
            state.get("last_notified_addon_version"))
        if action == "none":
            return
        state["last_notified_addon_version"] = latest
        _save_json(STATE, state)

        if action == "auto_update" and result["package_path"]:
            try:
                mw.addonManager.install(result["package_path"])
                tooltip(f"Intern Pearls Deck Tools updated itself to v{latest}. Restart "
                       "Anki to use it.", period=8000, parent=mw)
            except Exception as e:
                tooltip(f"Intern Pearls: couldn't install v{latest} automatically ({e}). "
                       "Try Advanced → Check for add-on updates.", period=8000, parent=mw)
        else:
            # Either a plain notify, or auto-update was requested but the package
            # didn't download — either way, tell the user a newer version exists rather
            # than doing nothing.
            tooltip(
                f"Intern Pearls Deck Tools v{latest} is available (you have "
                f"v{ADDON_VERSION}). Intern Pearls → Advanced → Check for add-on "
                "updates to install.", period=8000, parent=mw)

    _run_in_background(lambda: _addon_update_work(cfg["auto_update_addon"]), _finish)


_auto_sync_in_progress = False
# Decks auto-sync has already said "template update pending, needs a manual sync"
# about, so the repeating poll doesn't re-announce them every interval. Session-scoped
# on purpose: a restart is allowed to remind once more.
_tpl_deferred_notified = set()


@_bg_safe
def _auto_sync_check():
    """Timer-triggered: if auto-sync is on and any deck changed, apply it without asking.

    The manifest fetch and every pending deck's .apkg download (the parts that can
    actually take a while, and run on every poll even though most polls find nothing
    new) all happen off the main thread via _run_in_background — fetch() is pure
    network/file I/O with no mw.col or Qt access, so it's as safe to run there as the
    manifest check already was. Only backing up and importing (the part that touches
    mw.col, and only runs when there's actually something to apply) still happens on
    the main thread inside the completion callback, matching the cost a manual Sync
    decks click already pays. A backup is still taken first, and if it fails, this
    aborts rather than importing unprotected, since there's no user to ask. The outcome
    is always a transient tooltip, never a blocking dialog, since this can fire mid-
    review.
    """
    global _auto_sync_in_progress
    cfg = _cfg()
    if not cfg["auto_sync_decks"] or _auto_sync_in_progress or mw.col is None:
        return

    def _fetch_work():
        # _BG_TIMEOUT, not the interactive default: this fires unattended as often as
        # once a minute, so a dead host must fail well inside the poll interval.
        manifest, fetch, source = _fetch_manifest(cfg, timeout=_BG_TIMEOUT)
        if not manifest:
            return None
        installed = _load_json(INSTALLED, {})
        todo = decks_to_update(manifest, installed, cfg["excluded"])
        if not todo:
            return None
        # Download every pending deck here, off the main thread, so a big deck on a
        # slow link can't freeze Anki. A per-deck failure is stored, not raised, so one
        # bad download doesn't take out decks that fetched fine; _run_sync's existing
        # per-deck try/except (unchanged) reports it the same way a live fetch failure
        # always has.
        downloaded = {}
        for d in todo:
            try:
                downloaded[d["name"]] = fetch(d)
            except Exception as e:
                downloaded[d["name"]] = e
        return {"manifest": manifest, "downloaded": downloaded, "source": source,
                "todo": todo, "installed": installed}

    def _apply(result, error):
        global _auto_sync_in_progress
        if error or not result:
            return   # offline, misconfigured, or nothing pending — stay quiet
        _auto_sync_in_progress = True
        try:
            if not _pre_sync_backup_or_skip_silently(cfg["export_deck"]):
                tooltip("Intern Pearls: auto-sync skipped, couldn't create a backup "
                       "first.", period=6000, parent=mw)
                return

            def _already_fetched(d):
                v = result["downloaded"][d["name"]]
                if isinstance(v, Exception):
                    raise v
                return v

            results, restored, _, deferred = _run_sync(
                cfg, result["manifest"], _already_fetched,
                result["todo"], result["installed"], defer_template_changes=True)
            ok = sum(1 for r in results if r.startswith("✓"))
            fail = len(results) - ok - len(deferred)
            # A deferred deck stays pending, so every later poll re-defers it. Only
            # mention each one once per Anki session, and stay quiet entirely on a
            # poll where re-deferrals were the only "activity".
            deferred_new = [n for n in deferred if n not in _tpl_deferred_notified]
            _tpl_deferred_notified.update(deferred)
            if not (ok or fail or deferred_new):
                return
            msg = f"Intern Pearls: auto-synced {ok} deck(s) (source: {result['source']})"
            if fail:
                msg += f", {fail} failed, open Sync decks for details"
            if deferred_new:
                msg += (f", {len(deferred_new)} deck(s) include a card-template "
                        "update — run Sync decks to review it")
            if restored:
                msg += f", preserved fields restored on {restored} card(s)"
            tooltip(msg, period=6000, parent=mw)
        finally:
            _auto_sync_in_progress = False

    _run_in_background(_fetch_work, _apply)


_auto_sync_timer = None


def _stop_auto_sync_timer():
    global _auto_sync_timer
    if _auto_sync_timer is not None:
        _auto_sync_timer.stop()
        _auto_sync_timer = None


def _restart_auto_sync_timer(minutes):
    """(Re)start the repeating poll at `minutes`, floored so it can't be configured into
    a busy-loop. GitHub load at this cadence is trivial: one small manifest.json request
    per interval, well under even the unauthenticated 60-requests-per-hour limit at the
    one-minute floor, let alone the 5000-per-hour a token gets.
    """
    _stop_auto_sync_timer()
    global _auto_sync_timer
    interval_ms = clamp_interval_minutes(
        minutes, AUTO_SYNC_INTERVAL_FLOOR_MIN, AUTO_SYNC_INTERVAL_DEFAULT_MIN) * 60 * 1000
    _auto_sync_timer = QTimer(mw)
    _auto_sync_timer.timeout.connect(_auto_sync_check)
    _auto_sync_timer.start(interval_ms)


def _schedule_background_checks():
    """Run once, a couple seconds after Anki finishes starting up: the add-on-update
    check, and, only if auto-sync is on in Settings, an immediate deck check plus the
    repeating poll that keeps checking while Anki stays open.
    """
    QTimer.singleShot(2000, _check_addon_updates_background)
    cfg = _cfg()
    if cfg["auto_sync_decks"]:
        QTimer.singleShot(4000, _auto_sync_check)
        _restart_auto_sync_timer(cfg["auto_sync_interval_minutes"])
