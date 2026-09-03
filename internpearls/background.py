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
import tempfile
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

from .ai_logic import sweep_stale_scratch
from .collection import _pre_sync_backup_or_skip_silently, installed_matching_collection
from .config import (ADDON_VERSION, AUTO_SYNC_INTERVAL_CEILING_MIN,
                     AUTO_SYNC_INTERVAL_DEFAULT_MIN,
                     AUTO_SYNC_INTERVAL_FLOOR_MIN, INSTALLED, STATE,
                     SUPPORTED_MANIFEST_SCHEMA, _cfg, _load_json, _save_json)
from .logic import (clamp_interval_minutes, decide_addon_update_action,
                    decks_to_update, manifest_needs_newer_addon, plural)
from .net import _BG_TIMEOUT, _DOWNLOAD_TIMEOUT
from .sync import (_cached_fetch, _content_backup_decks, _fetch_manifest, _is_local,
                   _reconcile_pending, _refresh_reconcile_action_label, _run_sync)
from .ui import _bg_safe, manual_sync_in_progress
from .updates import _addon_update_work, _refresh_update_action_label


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
        # Refresh the menu label from this fresh fetch regardless of the notify-once
        # suppression below — that logic governs the transient tooltip, not what the
        # persistent menu label should currently show.
        _refresh_update_action_label(latest)
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

    _run_in_background(
        lambda: _addon_update_work(cfg["auto_update_addon"], cfg["gh_token"]), _finish)


_auto_sync_in_progress = False
# (deck name, version) pairs auto-sync has already said "template update pending, needs
# a manual sync" about, so the repeating poll doesn't re-announce them every interval.
# Session-scoped on purpose: a restart is allowed to remind once more. Keyed by version
# as well as name, like _deferred_decks below and for the same reason: a deck deferred
# at one version, dealt with by hand, and deferred again at the next was never
# announced the second time, so the only sign of it was the menu label.
_tpl_deferred_notified = set()
# Manifest schema values auto-sync has already told the user require an add-on update.
# Same session-scoped-once pattern as _tpl_deferred_notified — otherwise a schema
# mismatch would re-nag every poll interval until the add-on is updated.
_schema_blocked_notified = set()
# The reconcile-pending count (retired + relocated cards) auto-sync last nagged about
# by tooltip, so it only speaks up when that count first appears or grows — not every
# poll, and not again once it's already been mentioned at its current size. Auto-sync
# never archives or relocates on its own (see sync.py's _reconcile_action comment for
# why), so this tooltip plus the persistent "Reconcile my decks (N pending)" menu
# label it points at are the only things standing between a real backlog and it
# silently piling up unnoticed.
_last_reconcile_notified = 0
# (deck name, version) pairs auto-sync has already held back for a template or
# note-type change this session. A deferred deck stays pending forever, so without
# this every single poll re-downloaded its whole .apkg and took a fresh deck backup to
# reach the same decision it reached the first time, for as long as Anki stayed open.
# Keyed by version as well as name, so a source that pushes a fix mid-session is
# picked up rather than skipped along with the version that was deferred.
_deferred_decks = set()
# Whether the "couldn't create a backup" tooltip has already been shown this session.
# A backup that fails once usually fails every time (a deck that can't be exported, a
# full disk), and the same nag every poll interval is the pattern _tpl_deferred_notified
# exists to avoid. Cleared again by a tick whose backup succeeds.
_backup_failure_notified = False


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
    # A manual flow owns the collection and installed.json for as long as it runs, and
    # its dialogs run their own event loops that this poll's QueryOp callback can land
    # inside. Skip the tick entirely; the next one picks up whatever is still pending.
    if manual_sync_in_progress():
        return

    # Held for the WHOLE run, fetch phase included, and released in _apply's finally on
    # every path (success, failure, and the nothing-to-do return). The flag used to be
    # taken only around the import itself, leaving the minutes-long fetch phase
    # unguarded: a second tick, or a manual sync, could start on top of it.
    _auto_sync_in_progress = True

    # Reconciled here, on the main thread, before any work is handed to the background
    # thread below — installed_matching_collection touches mw.col, which _fetch_work
    # must not do (see _run_in_background's contract). See its docstring for why this
    # matters: without it, a collection restore that rolled back a prior sync would
    # leave auto-sync silently believing everything's still up to date.
    try:
        installed = installed_matching_collection(_load_json(INSTALLED, {}),
                                                  cfg["scope_tag"])
    except Exception:
        _auto_sync_in_progress = False   # nothing downstream will clear it from here
        raise

    def _fetch_work():
        # _BG_TIMEOUT, not the interactive default: this fires unattended as often as
        # once a minute, so a dead host must fail well inside the poll interval.
        # The deck downloads below get _BG_TIMEOUT too on a build without QueryOp,
        # because there they run inline on the main thread: a 60s per-read bound would
        # freeze Anki for a minute per deck on an unattended poll, which is exactly what
        # net.py's own tighter bound for unattended checks exists to prevent. With
        # QueryOp present (every current Anki) they run on a worker thread, where the
        # generous bound costs nobody anything and a big deck on a slow link finishes.
        manifest, fetch, source = _fetch_manifest(
            cfg, timeout=_BG_TIMEOUT,
            download_timeout=_DOWNLOAD_TIMEOUT if QueryOp is not None else _BG_TIMEOUT)
        if not manifest:
            return None
        if manifest_needs_newer_addon(manifest, SUPPORTED_MANIFEST_SCHEMA):
            return {"schema_blocked": manifest.get("schema")}
        # Decks already held back this session are dropped here, before any of the work
        # a pending deck costs: they are pending precisely because only a manual sync
        # can decide about them, so re-downloading and re-backing-up for them every
        # poll bought nothing at all (see _deferred_decks).
        todo = [d for d in decks_to_update(manifest, installed, cfg["excluded"])
                if (d["name"], d.get("version")) not in _deferred_decks]
        # Always returned (even with todo empty) rather than bailing to None here: a
        # retirement or reorg can ship without bumping any deck's version, so this is
        # the only place that would ever notice one between manual checks — _apply
        # needs the manifest regardless of whether content sync has anything to do.
        # Download every pending deck here, off the main thread, so a big deck on a
        # slow link can't freeze Anki. A per-deck failure is stored, not raised, so one
        # bad download doesn't take out decks that fetched fine; _run_sync's existing
        # per-deck try/except (unchanged) reports it the same way a live fetch failure
        # always has.
        # Through the session cache, like every interactive fetch: the poll used to be
        # the one path that ignored it, so a deck it had already downloaded (or that a
        # preview had) was fetched again on the next tick that found it pending.
        downloaded = {}
        for d in todo:
            try:
                downloaded[d["name"]] = _cached_fetch(fetch, d)
            except Exception as e:
                downloaded[d["name"]] = e
        return {"manifest": manifest, "downloaded": downloaded, "source": source,
                "todo": todo}

    def _apply(result, error):
        global _auto_sync_in_progress
        try:
            _apply_work(result, error)
        finally:
            _auto_sync_in_progress = False

    def _apply_work(result, error):
        global _last_reconcile_notified, _backup_failure_notified
        if error or not result:
            return   # offline, misconfigured, or unreachable — stay quiet
        if "schema_blocked" in result:
            schema = result["schema_blocked"]
            if schema not in _schema_blocked_notified:
                _schema_blocked_notified.add(schema)
                tooltip(
                    "Intern Pearls: the deck source needs a newer add-on version — "
                    "auto-sync is paused until you update. Advanced → Check for add-on "
                    "updates.", period=8000, parent=mw)
            return

        # Refreshed on every successful fetch, regardless of whether content sync has
        # anything to do this poll: auto-sync only ever applies content on its own, so
        # this is the one place that keeps the "Reconcile my decks" menu label (and,
        # the first time a backlog appears or grows, a one-time tooltip pointing at
        # it) honest between manual checks.
        _, fresh, _, moves, _, _, stranded = _reconcile_pending(result["manifest"], cfg)
        # `stranded` counts too: reconcile_decks and update_decks both treat a reworded
        # pair as pending work, so leaving it out here made the menu label disagree with
        # the screen it points at, and a backlog of nothing but reworded pairs was never
        # nudged about at all.
        pending = len(fresh) + len(moves) + len(stranded)
        _refresh_reconcile_action_label(pending)
        # Only on first appearance or growth, which is what the comment on
        # _last_reconcile_notified has always described. A plain inequality also fired
        # on a SHRINK, so partly tidying up a backlog re-nagged about the smaller one
        # that was left. The watermark still follows the count down, so growing again
        # after a partial tidy-up does speak up.
        if pending > _last_reconcile_notified:
            tooltip(
                f"Intern Pearls: {plural(pending, 'card')} "
                f"{'is' if pending == 1 else 'are'} ready to tidy up (retired, "
                "reworded, or moved by a deck update) — Advanced → Reconcile my "
                "decks.",
                period=8000, parent=mw)
        _last_reconcile_notified = pending

        if not result["todo"]:
            return   # nothing to sync this poll
        # Re-checked immediately before applying, not only at the top of the poll: this
        # callback arrives from a QueryOp and can land after a manual flow has started,
        # including from inside one of its modal dialogs' event loops.
        if manual_sync_in_progress():
            return

        # Scoped to the decks this tick is about to import and to the decks its imports
        # will actually rewrite a card in, the same way the interactive flows scope
        # theirs: an unattended sync applies whatever the manifest lists, wherever the
        # learner keeps those cards, and backing up only export_deck left anything
        # filed outside it unprotected. The packages are already on disk from the fetch
        # phase, so reading which of her notes each one matches costs no network.
        if not _pre_sync_backup_or_skip_silently(
                cfg["export_deck"],
                [d["name"] for d in result["todo"]]
                + _content_backup_decks(
                    [v for v in result["downloaded"].values() if _is_local(v)],
                    result["manifest"].get("front_aliases", {}), cfg["scope_tag"])):
            # Once per session, not once per poll: a backup that fails usually keeps
            # failing, and the same tooltip every interval is noise around a message
            # that has already been read.
            if not _backup_failure_notified:
                _backup_failure_notified = True
                tooltip("Intern Pearls: auto-sync skipped, couldn't create a backup "
                       "first.", period=6000, parent=mw)
            return
        _backup_failure_notified = False

        def _already_fetched(d):
            v = result["downloaded"][d["name"]]
            if isinstance(v, Exception):
                raise v
            return v

        results, restored, _, deferred, _, _, _ = _run_sync(
            cfg, result["manifest"], _already_fetched,
            result["todo"], defer_template_changes=True)
        ok = sum(1 for r in results if r.startswith("✓"))
        fail = len(results) - ok - len(deferred)
        # A deferred deck stays pending, so every later poll re-defers it. Only
        # mention each one once per Anki session, and stay quiet entirely on a
        # poll where re-deferrals were the only "activity". Recorded by version too, so
        # later polls skip the download and the backup for them entirely rather than
        # reaching this same decision again (see _fetch_work).
        versions = {d["name"]: d.get("version") for d in result["todo"]}
        deferred_keys = [(n, versions.get(n)) for n in deferred]
        deferred_new = [n for n, v in deferred_keys
                        if (n, v) not in _tpl_deferred_notified]
        _tpl_deferred_notified.update(deferred_keys)
        _deferred_decks.update(deferred_keys)
        if not (ok or fail or deferred_new):
            return
        msg = (f"Intern Pearls: auto-synced {plural(ok, 'deck')} "
               f"(source: {result['source']})")
        if fail:
            msg += f", {fail} failed, open Sync decks for details"
        if deferred_new:
            # "or note-type format" because both defer here, for the same schema
            # reason, and a deck held back for a conversion used to be announced as a
            # look change it doesn't carry (see _run_sync's own deferral row).
            msg += (f", {plural(len(deferred_new), 'deck')} "
                    f"{'includes' if len(deferred_new) == 1 else 'include'} a "
                    "card-template or note-type format update, run Sync decks to "
                    "review it")
        if restored:
            msg += f", preserved fields restored on {plural(restored, 'card')}"
        tooltip(msg, period=6000, parent=mw)

    try:
        _run_in_background(_fetch_work, _apply)
    except Exception:
        # _run_in_background normally routes every failure through _apply, which clears
        # the flag; this covers it failing before it ever gets that far.
        _auto_sync_in_progress = False
        raise


_auto_sync_timer = None


def _stop_auto_sync_timer():
    global _auto_sync_timer
    if _auto_sync_timer is not None:
        _auto_sync_timer.stop()
        _auto_sync_timer = None


def _restart_auto_sync_timer(minutes):
    """(Re)start the repeating poll at `minutes`, floored so it can't be configured into
    a busy-loop and capped so it can't overflow QTimer's own C int. GitHub load at this
    cadence is trivial: one small manifest.json request per interval, well under even
    the unauthenticated 60-requests-per-hour limit at the one-minute floor, let alone
    the 5000-per-hour a token gets.
    """
    _stop_auto_sync_timer()
    global _auto_sync_timer
    interval_ms = clamp_interval_minutes(
        minutes, AUTO_SYNC_INTERVAL_FLOOR_MIN, AUTO_SYNC_INTERVAL_DEFAULT_MIN,
        AUTO_SYNC_INTERVAL_CEILING_MIN) * 60 * 1000
    _auto_sync_timer = QTimer(mw)
    _auto_sync_timer.timeout.connect(_auto_sync_check)
    _auto_sync_timer.start(interval_ms)


@_bg_safe
def _sweep_ai_scratch_background():
    """Clear scratch directories a crashed AI-generation session left in the
    system temp dir. The wizard removes its own on close, so this only ever
    finds the leftovers of a run that never got to close. File I/O only, no
    collection access, so it goes off the main thread like the update check."""
    _run_in_background(lambda: sweep_stale_scratch(tempfile.gettempdir()),
                       lambda _result, _error: None)


@_bg_safe
def _schedule_background_checks():
    """Run once, a couple seconds after Anki finishes starting up: the add-on-update
    check, a sweep of any leftover AI scratch directories, and, only if auto-sync is on
    in Settings, an immediate deck check plus the repeating poll that keeps checking
    while Anki stays open.

    Wrapped like the two checks it schedules: this runs from startup wiring with no
    dialog around it, so anything it raises reaches Anki's own add-on error dialog on
    every launch. A misconfigured setting is worth a quiet failure, not that.
    """
    QTimer.singleShot(2000, _check_addon_updates_background)
    QTimer.singleShot(3000, _sweep_ai_scratch_background)
    cfg = _cfg()
    if cfg["auto_sync_decks"]:
        QTimer.singleShot(4000, _auto_sync_check)
        _restart_auto_sync_timer(cfg["auto_sync_interval_minutes"])
