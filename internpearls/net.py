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

_CONNECT_TIMEOUT = 10    # seconds; fail-fast bound for reaching the source at all
_DOWNLOAD_TIMEOUT = 60   # seconds; per-read bound for pulling a deck once we're online
# This bound is only ever hit on an interactive, user-initiated fetch (the manifest, the
# version check), never the unattended poll below, so a few extra seconds of patience
# before it gives up is the right trade: GitHub's API occasionally takes longer than a
# tight 6s under load, and failing a click that would have succeeded at 8s reads as a
# flaky "server not available" the user then has to retry by hand. Deck downloads get the
# far more generous _DOWNLOAD_TIMEOUT, and the background poll its own tight _BG_TIMEOUT,
# so loosening this one doesn't slow either of those.
# A tighter bound for the two checks that run on their own, unprompted: the deck-sync
# poll and the add-on-update check. These can fire as often as once a minute, so a slow
# or dead host has to fail well before the interactive bound would. Background
# checks that use QueryOp (see background._run_in_background) run this off the main
# thread anyway, so the timeout mostly matters for the fallback path on an Anki build
# without QueryOp.
_BG_TIMEOUT = 3          # seconds; fail-fast bound for unattended background checks

# How much of a download is read per `on_chunk` call. Small enough that a slow link
# still pumps the UI several times a second, large enough that a fast one isn't
# dominated by the callback.
_CHUNK = 64 * 1024


class TransportError(RuntimeError):
    """The host could not be reached at all: DNS failure, refused connection, timeout.

    Its own class because "couldn't reach the source" and "reached it and it can't be
    used" need opposite advice, and every failure here used to arrive as a plain
    RuntimeError, so the caller that words those two messages could only ever guess.
    An offline learner was told to check her GitHub token. Still a RuntimeError, like
    everything else this module raises, so a caller that doesn't care catches it anyway.
    An HTTP status is deliberately NOT one of these: a 401, 403 or 404 means the host
    answered, and what it answered is about the repo, the branch or the token.
    """


class DownloadCancelled(RuntimeError):
    """An `on_chunk` callback asked to stop a download that was still in flight.

    Its own class so a caller can tell "the learner clicked Cancel" apart from a real
    network failure and word its own message accordingly. Still a RuntimeError, like
    every other failure this module raises, so a caller that doesn't care catches it
    anyway.
    """


def _http_get(url, token=None, accept=None, timeout=_CONNECT_TIMEOUT, on_chunk=None,
              on_response=None):
    """GET `url`, raising a RuntimeError with an actionable message on failure, or a
    TransportError (a RuntimeError too) when the host was never reached at all.

    Every network call in this add-on goes through here, so this is the one place that
    needs to turn urllib's exceptions into something a non-technical error dialog can
    show as-is, rather than a Python traceback repr.

    `on_response(r)` is called once the connection is open, before any body is read, with
    the response object itself (so a caller can check `.headers` or `.geturl()`, e.g. the
    final URL after a redirect). Raising from inside it propagates unchanged, since
    whatever it raises isn't one of the exception types handled below.

    `on_chunk(bytes_so_far)` opts into a chunked read: it is called after each chunk and
    returns falsy to abort, raising DownloadCancelled. It exists because a deck download
    is one blocking call on Anki's UI thread, so nothing repaints and no click is
    processed for its whole duration, which leaves a progress dialog's Cancel button
    decorative until something pumps the event loop from in here (that something is
    `ui.cancellable_progress`'s `pump`). Passing nothing keeps the read exactly what it
    was, a single call, so no existing caller pays for the loop.
    """
    headers = {"User-Agent": "internpearls-addon"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if on_response is not None:
                on_response(r)
            if on_chunk is None:
                return r.read()
            buf = bytearray()
            while True:
                chunk = r.read(_CHUNK)
                if not chunk:
                    return bytes(buf)
                buf += chunk
                if not on_chunk(len(buf)):
                    raise DownloadCancelled("cancelled before anything was imported")
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
        raise TransportError(
            "the network isn't responding (timed out). Check your internet connection "
            "and try again.") from e
    except urllib.error.URLError as e:
        raise TransportError(f"couldn't reach the network ({e.reason})") from e


def _gh_raw(repo, path, token, ref, timeout=_CONNECT_TIMEOUT, on_chunk=None):
    """Raw bytes of a file in a (possibly private) repo via the contents API.

    `on_chunk` is _http_get's, passed through: this is the deck-download path, the one
    fetch long enough for the learner to want out of it partway.
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"
    return _http_get(url, token=token, accept="application/vnd.github.raw",
                     timeout=timeout, on_chunk=on_chunk)


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


_IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
                "image/webp": "webp", "image/svg+xml": "svg"}


def fetch_card_image(url, max_bytes=5 * 1024 * 1024):
    """Download a model-suggested card image, the only thing that ever touches the
    network for it (the model supplies just the URL, never the request). Goes through
    _http_get so a failure reads like every other network error in this add-on.

    Refuses anything that isn't plainly an image: https only (checked on the request URL
    and, since urllib follows redirects by default, again on the final URL after any
    redirect), a known image content-type (ignoring parameters like `; charset=`), and a
    hard `max_bytes` cap enforced against the bytes actually read as they arrive, not
    just a Content-Length header the server can lie about or omit.
    """
    if not url.startswith("https://"):
        raise RuntimeError("image URLs must be https")
    ext = {}

    def on_response(r):
        final_url = r.geturl() or url
        if not final_url.startswith("https://"):
            raise RuntimeError("image must be served over https")
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in _IMAGE_TYPES:
            raise RuntimeError(f"not an image ({ctype or 'no content type'})")
        ext["value"] = _IMAGE_TYPES[ctype]
        clen = r.headers.get("Content-Length")
        if clen:
            try:
                declared = int(clen)
            except ValueError:
                declared = None
            if declared is not None and declared > max_bytes:
                raise RuntimeError("image is too large")

    def on_chunk(so_far):
        if so_far > max_bytes:
            raise RuntimeError("image is too large")
        return True

    data = _http_get(url, timeout=_DOWNLOAD_TIMEOUT, on_response=on_response,
                     on_chunk=on_chunk)
    return data, ext["value"]
