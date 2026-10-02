"""HTTP and GitHub fetch helpers.

Network calls run on Anki's UI thread, so a slow/unreachable host freezes the app (the
macOS beachball) for however long the socket takes to give up. First-contact calls
(the manifest, the version check) use a short timeout so an offline machine or captive
portal fails fast with a clear dialog instead of hanging. Only the large .apkg
downloads — reached only after first contact already proved we're online — get a
generous timeout so a big deck on a slow link isn't cut off mid-transfer.
"""
import http.client
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import quote, unquote, urlsplit

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
# or dead host has to fail well before the interactive bound would. These checks run
# off the main thread (see background._run_in_background), so this bound is about how
# long an unattended poll may hold its own slot open, not about a frozen UI.
_BG_TIMEOUT = 3          # seconds; fail-fast bound for unattended background checks

# Whole-fetch bounds. The per-read timeouts above only catch a host that goes silent; one
# that trickles a byte at a time never trips them, and an unattended poll stuck on it
# holds auto-sync's slot until Anki restarts. Deck and add-on package downloads (the
# fetches that pass _DOWNLOAD_TIMEOUT) get the long bound, everything else the short one.
_FETCH_DEADLINE = 120         # seconds
_DOWNLOAD_DEADLINE = 30 * 60  # seconds

# How much of a download is read per `on_chunk` call. Small enough that a slow link
# still pumps the UI several times a second, large enough that a fast one isn't
# dominated by the callback.
_CHUNK = 64 * 1024

# Descriptive, with a contact URL: Wikimedia's user-agent policy refuses generic ones.
_USER_AGENT = f"internpearls-addon (+https://github.com/{ANKI_REPO})"


class TransportError(RuntimeError):
    """The host could not be reached at all: DNS failure, refused connection, timeout.

    Its own class because "couldn't reach the source" and "reached it and it can't be
    used" need opposite advice, and every failure here used to arrive as a plain
    RuntimeError, so the caller that words those two messages could only ever guess.
    An offline learner was told to check their GitHub token. Still a RuntimeError, like
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


class RateLimitedError(RuntimeError):
    """GitHub answered 403 because this connection is out of unauthenticated requests,
    not because a token was wrong or the repo is private.

    Its own class so `_gh_public_raw` can fall back to the raw CDN specifically on this
    failure, and not on a genuine auth failure (401, or a 403 that isn't about the rate
    limit), where falling back would just as likely fail again for the same reason. Still
    a RuntimeError, like every other failure this module raises, so a caller that doesn't
    care catches it anyway.
    """


class HttpStatusError(RuntimeError):
    """An HTTP failure that answered with a status code, carried as `.code` so a caller
    can branch on it (a 401/403/404 is worth retrying differently than a 5xx is)
    without parsing the message text back apart. Still a RuntimeError, like everything
    else this module raises, so a caller that doesn't care just catches that.
    """

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def _rate_limit_message(e, token=None):
    """None if this 403 isn't GitHub's unauthenticated rate limit; otherwise the
    sentence to raise, with a reset time when `X-RateLimit-Reset` is present.

    GitHub's core API allows 60 requests per hour per IP with no token, shared by
    every unattended check this add-on runs on its own (the launch-time update check,
    the deck auto-sync poll), so a shared connection can exhaust it without the learner
    doing anything themselves. That 403 looks identical to a real auth failure unless
    the rate-limit headers are checked for, which is what used to send the learner to
    check a token they never sent.

    `token` says whether this request already carried one: a token was still limited, so
    telling the learner to sign in with one (what the no-token case says) is nonsense
    advice when they already have.
    """
    headers = e.headers
    if headers is not None and headers.get("X-RateLimit-Remaining") == "0":
        pass
    else:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        if "rate limit" not in body.lower():
            return None
    reset_clause = ""
    reset_hdr = headers.get("X-RateLimit-Reset") if headers is not None else None
    if reset_hdr:
        try:
            reset_dt = datetime.fromtimestamp(int(reset_hdr))
        except (TypeError, ValueError, OSError):
            reset_dt = None
        if reset_dt is not None:
            minutes = max(1, round((reset_dt - datetime.now()).total_seconds() / 60))
            unit = "minute" if minutes == 1 else "minutes"
            reset_clause = f"; it resets at {reset_dt.strftime('%H:%M')} (about " \
                            f"{minutes} {unit})"
    trailer = ("Your token's limit will reset then." if token else
               "Signing in with a token in Manage decks raises the limit.")
    return ("GitHub's request limit for this connection is used up" + reset_clause +
            ". " + trailer)


def _deadline_for(timeout):
    """The whole-fetch bound that goes with a per-read `timeout`."""
    return _DOWNLOAD_DEADLINE if timeout >= _DOWNLOAD_TIMEOUT else _FETCH_DEADLINE


def _origin(url):
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or {"https": 443, "http": 80}.get(scheme)
    return scheme, (parts.hostname or "").lower(), port


def _keeps_credentials(old_url, new_url):
    """Whether a redirect from `old_url` to `new_url` may carry the token: only to the
    same scheme, host and port. Anywhere else would hand the learner's GitHub token to
    whoever that host is, or send it in the clear after an https start."""
    return _origin(old_url) == _origin(new_url)


class _CredentialSafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not _keeps_credentials(req.full_url, new.full_url):
            new.remove_header("Authorization")
        return new


class _Watch:
    """The connections one fetch opens, so its deadline can close them from a timer
    thread while urllib is blocked reading headers or body."""

    def __init__(self):
        self.expired = False
        self._conns = []
        self._lock = threading.Lock()

    def _cut(self, conn):
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def track(self, conn):
        connect = conn.connect

        def watched_connect():
            connect()
            with self._lock:
                if self.expired:
                    self._cut(conn)
        conn.connect = watched_connect
        with self._lock:
            self._conns.append(conn)

    def expire(self):
        with self._lock:
            self.expired = True
            for conn in self._conns:
                self._cut(conn)


_current = threading.local()


def _watched(handler_cls):
    class Watched(handler_cls):
        def do_open(self, http_class, req, **kw):
            watch = getattr(_current, "watch", None)

            def connection(*a, **k):
                conn = http_class(*a, **k)
                if watch is not None:
                    watch.track(conn)
                return conn
            return super().do_open(connection, req, **kw)
    return Watched


_WatchedHTTP = _watched(urllib.request.HTTPHandler)
_WatchedHTTPS = _watched(urllib.request.HTTPSHandler)


def _open(req, timeout):
    """Open `req`. The one place a request reaches the network, and the seam tests
    stub."""
    opener = urllib.request.build_opener(_CredentialSafeRedirect(), _WatchedHTTP(),
                                         _WatchedHTTPS())
    return opener.open(req, timeout=timeout)


def _too_slow(deadline):
    minutes = deadline / 60
    span = (f"{round(minutes)} minutes" if minutes >= 2
            else f"{max(1, round(deadline))} seconds")
    return TransportError(
        f"the source took too long to answer (over {span}). Check your internet "
        "connection and try again.")


def _mb(n):
    return f"{n / (1024 * 1024):g} MB"


def _http_get(url, token=None, accept=None, timeout=_CONNECT_TIMEOUT, on_chunk=None,
              on_response=None, max_bytes=None, deadline=None):
    """GET `url`, raising a RuntimeError with an actionable message on failure, or a
    TransportError (a RuntimeError too) when the host was never reached at all.

    Every network call in this add-on goes through here, so this is the one place that
    needs to turn urllib's exceptions into something a non-technical error dialog can
    show as-is, rather than a Python traceback repr.

    `on_response(r)` is called once the connection is open, before any body is read, with
    the response object itself (so a caller can check `.headers` or `.geturl()`, e.g. the
    final URL after a redirect). Raising from inside it propagates unchanged, since
    whatever it raises isn't one of the exception types handled below.

    `on_chunk(bytes_so_far)` is called after each chunk read and
    returns falsy to abort, raising DownloadCancelled. It exists because a deck download
    is one blocking call on Anki's UI thread, so nothing repaints and no click is
    processed for its whole duration, which leaves a progress dialog's Cancel button
    decorative until something pumps the event loop from in here (that something is
    `ui.cancellable_progress`'s `pump`).

    `max_bytes` refuses a body larger than that, checked against Content-Length first
    and then against what actually arrives. `deadline` bounds the whole fetch, connect
    to last byte, in seconds; None takes `_deadline_for(timeout)`. A token is sent only
    to the URL asked for and to same-origin https redirects (see _keeps_credentials).
    """
    if deadline is None:
        deadline = _deadline_for(timeout)
    watch = _Watch()
    timer = threading.Timer(deadline, watch.expire)
    timer.daemon = True
    _current.watch = watch
    timer.start()
    try:
        return _http_get_watched(url, token, accept, timeout, on_chunk, on_response,
                                 max_bytes, deadline, watch)
    except DownloadCancelled:
        raise
    except Exception as e:
        if watch.expired:
            raise _too_slow(deadline) from e
        raise
    finally:
        timer.cancel()
        _current.watch = None


def _read_body(r, on_chunk, max_bytes, deadline, watch):
    if max_bytes is not None:
        try:
            declared = int((r.headers or {}).get("Content-Length") or -1)
        except (TypeError, ValueError):
            declared = -1
        if declared > max_bytes:
            raise RuntimeError(f"the file is larger than {_mb(max_bytes)}, so it "
                               "wasn't downloaded")
    read = getattr(r, "read1", None) or r.read
    end = time.monotonic() + deadline
    buf = bytearray()
    while True:
        if watch.expired or time.monotonic() > end:
            raise _too_slow(deadline)
        chunk = read(_CHUNK)
        if not chunk:
            break
        buf += chunk
        if max_bytes is not None and len(buf) > max_bytes:
            raise RuntimeError(f"the file is larger than {_mb(max_bytes)}, so the "
                               "download was stopped")
        if on_chunk is not None and not on_chunk(len(buf)):
            raise DownloadCancelled("cancelled before anything was imported")
    if watch.expired:
        raise _too_slow(deadline)
    return bytes(buf)


def _http_get_watched(url, token, accept, timeout, on_chunk, on_response, max_bytes,
                      deadline, watch):
    headers = {"User-Agent": _USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with _open(req, timeout=timeout) as r:
            if on_response is not None:
                on_response(r)
            return _read_body(r, on_chunk, max_bytes, deadline, watch)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            rate_limit_msg = _rate_limit_message(e, token=token)
            if rate_limit_msg is not None:
                raise RateLimitedError(rate_limit_msg) from e
        if e.code in (401, 403):
            if token:
                raise HttpStatusError(
                    "access denied (check that your token is valid and can read "
                    "this repo)", e.code) from e
            raise HttpStatusError("access denied", e.code) from e
        if e.code == 404:
            raise HttpStatusError(
                "not found (check the repo name, branch, and file path)", e.code) from e
        raise HttpStatusError(f"server returned HTTP {e.code}", e.code) from e
    except (TimeoutError, socket.timeout) as e:
        # Bare socket timeout (isn't always wrapped in URLError); surface it fast.
        raise TransportError(
            "the network isn't responding (timed out). Check your internet connection "
            "and try again.") from e
    except urllib.error.URLError as e:
        raise TransportError(f"couldn't reach the network ({e.reason})") from e
    except (OSError, http.client.HTTPException) as e:
        # The connection dropped after it opened: a reset, a truncated body, a TLS
        # error mid-read. Still the network, not the repo or the token.
        raise TransportError(
            f"the connection dropped partway through ({e}). Check your internet "
            "connection and try again.") from e


def _gh_raw(repo, path, token, ref, timeout=_CONNECT_TIMEOUT, on_chunk=None,
            max_bytes=None):
    """Raw bytes of a file in a (possibly private) repo via the contents API.

    `on_chunk` is _http_get's, passed through: this is the deck-download path, the one
    fetch long enough for the learner to want out of it partway. `path` comes from the
    deck source's manifest, so it is quoted: a '?' or '#' in it stays part of the path.
    """
    url = (f"https://api.github.com/repos/{repo}/contents/{quote(path, safe='/')}"
           f"?ref={ref}")
    return _http_get(url, token=token, accept="application/vnd.github.raw",
                     timeout=timeout, on_chunk=on_chunk, max_bytes=max_bytes)


def _gh_public_raw(path, ref="main", timeout=_CONNECT_TIMEOUT, token=None):
    """Raw bytes of a file in the public add-on repo. Tries, in order: the Contents
    API with `token` (when the learner has one configured), the Contents API with no
    token, then raw.githubusercontent.com if the API is rate limited.

    The Contents API is preferred over raw.githubusercontent.com because that CDN is
    served through a cache that can lag well behind a push. Confirmed directly: right
    after pushing a new version.json, the Contents API reflected it immediately, while
    the raw CDN link for the same file and branch still served the previous content
    more than two minutes later. That gap is exactly why "Check for add-on updates"
    once failed to see a version that had already been pushed.

    No token is required, since this repo is public, but the API's unauthenticated
    limit is a shared 60 requests per hour per IP, easily used up by the launch-time
    update check and the deck auto-sync poll running on their own. A learner's own
    token (read scope on any repo is enough) raises that to 5,000 per hour, so it's
    tried first when configured. A token that can't read this public repo at all, say a
    fine-grained token scoped to other repos, is retried without it rather than failing
    outright, so a bad token never leaves this check worse off than having none. If
    both attempts are rate limited rather than refused, the raw CDN is the last resort:
    no API quota to run out, at the cost of the CDN's own lag, which version.json's
    "download" field already documents for a person opening it by hand.

    Only an auth-shaped failure (401/403/404) on the token attempt is worth retrying
    without the token: dropping it can't fix an unreachable host (TransportError) or a
    server error (5xx), and retrying either just doubles the wait on an already-dead
    connection.
    """
    url = f"https://api.github.com/repos/{ANKI_REPO}/contents/{path}?ref={ref}"
    raw_url = f"https://raw.githubusercontent.com/{ANKI_REPO}/{ref}/{path}"
    if token:
        try:
            return _http_get(url, token=token, accept="application/vnd.github.raw",
                             timeout=timeout)
        except RateLimitedError:
            return _http_get(raw_url, timeout=timeout)
        except HttpStatusError as e:
            if e.code not in (401, 403, 404):
                raise
            # a token that can't read this repo; fall through and retry without it
    try:
        return _http_get(url, accept="application/vnd.github.raw", timeout=timeout)
    except RateLimitedError:
        return _http_get(raw_url, timeout=timeout)


# Raster only: SVG is an active format, and one downloaded from an arbitrary URL is
# unreviewable, while the model can already draw its own (checked) SVG via
# ai_logic.svg_to_media. Excluding image/svg+xml here closes that bypass rather than
# trying to validate hostile SVG from an untrusted host.
_IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
                "image/webp": "webp"}

_WIKI_FILE_PAGE = re.compile(
    r"^https://([a-z-]+(?:\.m)?\.wikipedia\.org|commons(?:\.m)?\.wikimedia\.org)"
    r"/wiki/File:([^?#]+)", re.I)
_WIKI_UPLOAD_SVG = re.compile(
    r"^https://upload\.wikimedia\.org/wikipedia/([a-z-]+)/[0-9a-f]/[0-9a-f]{2}/"
    r"([^/?#]+\.svg)(?:[?#].*)?$", re.I)


def wikimedia_image_url(url):
    """A Wikimedia address that names an image without serving one as a raster file
    (a File: page is HTML; an original SVG is refused above), rewritten to the
    Special:FilePath redirect that serves a PNG or JPEG rendering. Any other URL is
    returned unchanged."""
    m = _WIKI_FILE_PAGE.match(url)
    if m:
        host = m.group(1).lower().replace(".m.", ".")
        name = m.group(2)
    else:
        m = _WIKI_UPLOAD_SVG.match(url)
        if not m:
            return url
        project = m.group(1).lower()
        host = ("commons.wikimedia.org" if project == "commons"
                else f"{project}.wikipedia.org")
        name = m.group(2)
    name = quote(unquote(name).replace(" ", "_"))
    return f"https://{host}/wiki/Special:FilePath/{name}?width=1200"


def fetch_card_image(url, max_bytes=5 * 1024 * 1024):
    """Download a model-suggested card image, the only thing that ever touches the
    network for it (the model supplies just the URL, never the request). Goes through
    _http_get so a failure reads like every other network error in this add-on.

    Refuses anything that isn't plainly an image: https only (checked on the request URL
    and, since urllib follows redirects by default, again on the final URL after any
    redirect), a known image content-type (ignoring parameters like `; charset=`), and a
    hard `max_bytes` cap enforced against the bytes actually read as they arrive, not
    just a Content-Length header the server can lie about or omit. A Wikimedia File:
    page or original SVG is fetched as its rendered image (see wikimedia_image_url).
    """
    if not url.startswith("https://"):
        raise RuntimeError("image URLs must be https")
    url = wikimedia_image_url(url)
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

    try:
        data = _http_get(url, timeout=_DOWNLOAD_TIMEOUT, on_response=on_response,
                         on_chunk=on_chunk)
    except HttpStatusError as e:
        # _http_get words a 404 for the deck-source repo it mostly serves. An image
        # address is one the assistant suggested, so repo advice would mislead.
        if e.code == 404:
            raise HttpStatusError("no image at that address (the site returned 404)",
                                  e.code) from e
        raise
    return data, ext["value"]
