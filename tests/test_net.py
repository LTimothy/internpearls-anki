"""Tests for internpearls/net.py: what a failed fetch says, and the chunked read a
cancellable download needs.

urllib is stubbed per test, so nothing here touches a network. What matters about the
error branches is not the exception type but the sentence a user is shown: net.py exists
to turn urllib's exceptions into text an error dialog can print as-is, so each branch is
asserted on its message rather than on the class it came from.
"""
import socket
import urllib.error

import pytest


class _Response:
    """The context-manager object urlopen returns, reading `payload` out in whatever
    sizes it is asked for (a bare read() empties it in one go, the way the real one
    does). `headers` and `url` default to empty/None since most callers here don't
    care; the image-fetch tests set them to exercise content-type and redirect checks."""

    def __init__(self, payload, headers=None, url=None):
        self._data = payload
        self.headers = headers or {}
        self._url = url
        self.reads = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=None):
        if size is None:
            chunk, self._data = self._data, b""
        else:
            chunk, self._data = self._data[:size], self._data[size:]
        self.reads.append(len(chunk))
        return chunk

    def geturl(self):
        return self._url


def _urlopen(monkeypatch, result, capture=None):
    """Point net's urlopen at `result`: a response to return, or an exception to raise."""
    from internpearls import net

    def fake(req, timeout=None):
        if capture is not None:
            capture.append((req, timeout))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(net.urllib.request, "urlopen", fake)


def _http_error(code):
    return urllib.error.HTTPError("http://example.invalid", code, "nope", {}, None)


def test_a_plain_get_returns_the_body(monkeypatch):
    from internpearls import net
    _urlopen(monkeypatch, _Response(b"payload"))
    assert net._http_get("http://example.invalid") == b"payload"


def test_a_token_and_accept_ride_along_on_the_request(monkeypatch):
    """The two headers every GitHub fetch depends on: the raw media type, and the
    Authorization a private repo needs (and a public one must work without)."""
    from internpearls import net
    seen = []
    _urlopen(monkeypatch, _Response(b"{}"), capture=seen)
    net._http_get("http://example.invalid", token="t0ken", accept="application/x.raw")
    req, _timeout = seen[0]
    assert req.get_header("Authorization") == "Bearer t0ken"
    assert req.get_header("Accept") == "application/x.raw"

    seen.clear()
    _urlopen(monkeypatch, _Response(b"{}"), capture=seen)
    net._http_get("http://example.invalid")
    assert seen[0][0].get_header("Authorization") is None, (
        "a token-less fetch must send no Authorization header at all: a public repo "
        "rejects an empty bearer token")


@pytest.mark.parametrize("code, phrase", [(401, "access denied"), (403, "access denied"),
                                          (404, "not found")])
def test_the_two_actionable_http_failures_say_what_to_check(monkeypatch, code, phrase):
    from internpearls import net
    _urlopen(monkeypatch, _http_error(code))
    with pytest.raises(RuntimeError) as e:
        net._http_get("http://example.invalid")
    assert phrase in str(e.value)


def test_any_other_http_status_reports_its_code(monkeypatch):
    from internpearls import net
    _urlopen(monkeypatch, _http_error(500))
    with pytest.raises(RuntimeError) as e:
        net._http_get("http://example.invalid")
    assert "HTTP 500" in str(e.value)


@pytest.mark.parametrize("raised", [TimeoutError(), socket.timeout()])
def test_a_timeout_reads_as_a_network_problem_not_a_traceback(monkeypatch, raised):
    """A bare socket timeout isn't always wrapped in URLError, so it gets its own
    branch; both spellings must land on the same sentence."""
    from internpearls import net
    _urlopen(monkeypatch, raised)
    with pytest.raises(RuntimeError) as e:
        net._http_get("http://example.invalid")
    assert "timed out" in str(e.value)


def test_an_unreachable_host_names_the_reason(monkeypatch):
    from internpearls import net
    _urlopen(monkeypatch, urllib.error.URLError("nodename nor servname provided"))
    with pytest.raises(RuntimeError) as e:
        net._http_get("http://example.invalid")
    assert "couldn't reach the network" in str(e.value)
    assert "nodename" in str(e.value)


def test_the_configured_timeout_reaches_urlopen(monkeypatch):
    from internpearls import net
    seen = []
    _urlopen(monkeypatch, _Response(b""), capture=seen)
    net._http_get("http://example.invalid", timeout=net._BG_TIMEOUT)
    assert seen[0][1] == net._BG_TIMEOUT


def test_without_a_callback_the_body_is_read_in_one_call(monkeypatch):
    """The existing callers' path, unchanged: no chunk loop, no per-chunk cost."""
    from internpearls import net
    response = _Response(b"x" * (net._CHUNK * 3))
    _urlopen(monkeypatch, response)
    assert len(net._http_get("http://example.invalid")) == net._CHUNK * 3
    assert len(response.reads) == 1


def test_a_callback_gets_the_running_total_and_the_whole_body_still_comes_back(monkeypatch):
    from internpearls import net
    _urlopen(monkeypatch, _Response(b"x" * (net._CHUNK * 2 + 7)))
    seen = []

    def on_chunk(so_far):
        seen.append(so_far)
        return True

    data = net._http_get("http://example.invalid", on_chunk=on_chunk)
    assert len(data) == net._CHUNK * 2 + 7
    assert seen == [net._CHUNK, net._CHUNK * 2, net._CHUNK * 2 + 7], (
        "on_chunk must report bytes so far, once per chunk")


def test_a_short_body_still_calls_back_at_least_once(monkeypatch):
    """A one-deck run's only chance to notice Cancel is inside the download, so even a
    small file has to pump."""
    from internpearls import net
    _urlopen(monkeypatch, _Response(b"tiny"))
    seen = []
    net._http_get("http://example.invalid", on_chunk=lambda n: seen.append(n) or True)
    assert seen == [4]


def test_a_falsy_callback_aborts_the_download_as_its_own_exception(monkeypatch):
    """DownloadCancelled rather than a generic failure, so a caller can tell the learner
    clicking Cancel apart from the source being broken."""
    from internpearls import net
    _urlopen(monkeypatch, _Response(b"x" * (net._CHUNK * 4)))
    calls = []

    def on_chunk(so_far):
        calls.append(so_far)
        return len(calls) < 2

    with pytest.raises(net.DownloadCancelled):
        net._http_get("http://example.invalid", on_chunk=on_chunk)
    assert calls == [net._CHUNK, net._CHUNK * 2], "the read must stop at the cancel"


def test_download_cancelled_is_still_a_runtime_error(monkeypatch):
    """Every failure this module raises is a RuntimeError, so a caller that doesn't know
    about cancellation still handles it rather than showing a traceback."""
    from internpearls import net
    assert issubclass(net.DownloadCancelled, RuntimeError)


def test_a_deck_fetch_passes_its_callback_through(monkeypatch):
    """_gh_raw is the deck-download path, the one long enough to want out of partway."""
    from internpearls import net
    _urlopen(monkeypatch, _Response(b"apkg bytes"))
    seen = []
    data = net._gh_raw("owner/repo", "decks/Example.apkg", "t0ken", "main",
                       on_chunk=lambda n: seen.append(n) or True)
    assert data == b"apkg bytes"
    assert seen == [len(b"apkg bytes")]


def test_a_deck_fetch_asks_the_contents_api_for_raw_bytes(monkeypatch):
    from internpearls import net
    seen = []
    _urlopen(monkeypatch, _Response(b""), capture=seen)
    net._gh_raw("owner/repo", "decks/Example.apkg", "t0ken", "main")
    req, _timeout = seen[0]
    assert req.full_url == (
        "https://api.github.com/repos/owner/repo/contents/decks/Example.apkg?ref=main")
    assert req.get_header("Accept") == "application/vnd.github.raw"


def test_the_addon_fetches_itself_through_the_api_not_the_raw_cdn(monkeypatch):
    """The hard constraint: the CDN can serve a stale version.json for minutes after a
    release, which once hid a shipped update from the update check."""
    from internpearls import net
    seen = []
    _urlopen(monkeypatch, _Response(b"{}"), capture=seen)
    net._gh_public_raw("version.json")
    assert seen[0][0].full_url.startswith("https://api.github.com/repos/")
    assert "raw.githubusercontent.com" not in seen[0][0].full_url


# --- fetch_card_image: review-time download of a model-suggested image URL ---


def test_fetch_card_image_rejects_http_url():
    from internpearls import net
    with pytest.raises(RuntimeError):
        net.fetch_card_image("http://example.com/x.png")


def test_fetch_card_image_checks_content_type(monkeypatch):
    from internpearls import net
    _urlopen(monkeypatch, _Response(b"<html>", headers={"Content-Type": "text/html"}))
    with pytest.raises(RuntimeError):
        net.fetch_card_image("https://example.com/x.png")


def test_fetch_card_image_happy(monkeypatch):
    from internpearls import net
    body = b"\x89PNG\r\n\x1a\n00"
    _urlopen(monkeypatch, _Response(
        body, headers={"Content-Type": "image/png", "Content-Length": str(len(body))}))
    data, ext = net.fetch_card_image("https://example.com/pic.png")
    assert data == body and ext == "png"


def test_fetch_card_image_content_type_with_parameters_is_accepted(monkeypatch):
    """A server that adds a charset parameter must not be rejected on a bare string
    mismatch against the bare media type."""
    from internpearls import net
    body = b"\x89PNG..."
    _urlopen(monkeypatch, _Response(
        body, headers={"Content-Type": "image/png; charset=binary"}))
    data, ext = net.fetch_card_image("https://example.com/pic.png")
    assert data == body and ext == "png"


def test_fetch_card_image_rejects_oversize_declared_by_content_length(monkeypatch):
    from internpearls import net
    _urlopen(monkeypatch, _Response(
        b"x" * 20, headers={"Content-Type": "image/png", "Content-Length": "20"}))
    with pytest.raises(RuntimeError):
        net.fetch_card_image("https://example.com/pic.png", max_bytes=10)


def test_fetch_card_image_rejects_oversize_stream_with_no_content_length(monkeypatch):
    """The header lying or missing entirely must not let bytes past the cap: the
    running total from the actual read has to be what's enforced."""
    from internpearls import net
    _urlopen(monkeypatch, _Response(
        b"x" * 50, headers={"Content-Type": "image/png"}))
    with pytest.raises(RuntimeError):
        net.fetch_card_image("https://example.com/pic.png", max_bytes=10)


def test_fetch_card_image_accepts_exactly_the_cap(monkeypatch):
    from internpearls import net
    body = b"x" * 10
    _urlopen(monkeypatch, _Response(body, headers={"Content-Type": "image/png"}))
    data, ext = net.fetch_card_image("https://example.com/pic.png", max_bytes=10)
    assert len(data) == 10


def test_fetch_card_image_rejects_a_redirect_to_http(monkeypatch):
    """urllib follows redirects by default; a server that 302s an https URL to a plain
    http one must not let the download silently succeed over an insecure channel."""
    from internpearls import net
    _urlopen(monkeypatch, _Response(
        b"\x89PNG", headers={"Content-Type": "image/png"},
        url="http://evil.example.com/pic.png"))
    with pytest.raises(RuntimeError) as e:
        net.fetch_card_image("https://example.com/pic.png")
    assert "https" in str(e.value)
