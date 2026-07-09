"""HTTP and GitHub fetch helpers.

Network calls run on Anki's UI thread, so a slow/unreachable host freezes the app (the
macOS beachball) for however long the socket takes to give up. First-contact calls
(the manifest, the version check) use a short timeout so an offline machine or captive
portal fails fast with a clear dialog instead of hanging. Only the large .apkg
downloads — reached only after first contact already proved we're online — get a
generous timeout so a big deck on a slow link isn't cut off mid-transfer.
"""
import socket
import urllib.error
import urllib.request

from .config import ANKI_REPO

_CONNECT_TIMEOUT = 6     # seconds; fail-fast bound for reaching the source at all
_DOWNLOAD_TIMEOUT = 60   # seconds; per-read bound for pulling a deck once we're online
# A tighter bound for the two checks that run on their own, unprompted: the deck-sync
# poll and the add-on-update check. These can fire as often as once a minute, so a slow
# or dead host has to fail well before the interactive 6-second bound would. Background
# checks that use QueryOp (see background._run_in_background) run this off the main
# thread anyway, so the timeout mostly matters for the fallback path on an Anki build
# without QueryOp.
_BG_TIMEOUT = 3          # seconds; fail-fast bound for unattended background checks


def _http_get(url, token=None, accept=None, timeout=_CONNECT_TIMEOUT):
    """GET `url`, raising a plain RuntimeError with an actionable message on failure.

    Every network call in this add-on goes through here, so this is the one place that
    needs to turn urllib's exceptions into something a non-technical error dialog can
    show as-is, rather than a Python traceback repr.
    """
    headers = {"User-Agent": "internpearls-addon"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(
                "access denied (check that your token is valid and can read this "
                "repo)") from e
        if e.code == 404:
            raise RuntimeError(
                "not found (check the repo name, branch, and file path)") from e
        raise RuntimeError(f"server returned HTTP {e.code}") from e
    except (TimeoutError, socket.timeout) as e:
        # Bare socket timeout (isn't always wrapped in URLError); surface it fast.
        raise RuntimeError(
            "the network isn't responding (timed out). Check your internet connection "
            "and try again.") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"couldn't reach the network ({e.reason})") from e


def _gh_raw(repo, path, token, ref, timeout=_CONNECT_TIMEOUT):
    """Raw bytes of a file in a (possibly private) repo via the contents API."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"
    return _http_get(url, token=token, accept="application/vnd.github.raw",
                     timeout=timeout)


def _gh_public_raw(path, ref="main", timeout=_CONNECT_TIMEOUT):
    """Raw bytes of a file in the public add-on repo, via the Contents API rather than
    raw.githubusercontent.com.

    raw.githubusercontent.com is served through a CDN that can lag well behind a push.
    Confirmed directly: right after pushing a new version.json, the Contents API
    reflected it immediately, while the raw CDN link for the same file and branch still
    served the previous content more than two minutes later. That gap is exactly why
    "Check for add-on updates" once failed to see a version that had already been
    pushed. Anything this add-on fetches about itself now goes through the API instead.
    No token is needed since this repo is public; version.json still lists the raw CDN
    URL under "download" as a convenience for a person opening it by hand, where a
    brief delay is harmless.
    """
    url = f"https://api.github.com/repos/{ANKI_REPO}/contents/{path}?ref={ref}"
    return _http_get(url, accept="application/vnd.github.raw", timeout=timeout)
