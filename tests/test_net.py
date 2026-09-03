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


def _http_error(code, headers=None, body=b""):
    import io
    fp = io.BytesIO(body) if body else None
    return urllib.error.HTTPError("http://example.invalid", code, "nope",
                                  headers or {}, fp)


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


def test_a_403_with_no_remaining_quota_reads_as_a_rate_limit_not_a_bad_token(
        monkeypatch):
    """The real bug this fix exists for: GitHub's unauthenticated 60/hour limit was
    exhausted, so it answered 403 the same as a bad token would, and the old message
    sent an owner who never sent a token to go check one."""
    from internpearls import net
    _urlopen(monkeypatch, _http_error(
        403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000700"}))
    with pytest.raises(net.RateLimitedError) as e:
        net._http_get("http://example.invalid")
    msg = str(e.value)
    assert "GitHub's request limit for this connection is used up" in msg
    assert "resets at" in msg
    # 1700000700 is well in the past by the time this runs, so the clamp floors this
    # at "1 minute" (singular) rather than "1 minutes".
    assert "about 1 minute)" in msg
    assert "Signing in with a token in Manage decks raises the limit" in msg
    assert "token is valid" not in msg


def test_a_403_rate_limit_with_a_token_sent_points_at_the_token_and_pluralizes(
        monkeypatch):
    """A token was sent and still got rate limited: telling her to sign in with one
    (the no-token sentence) is nonsense advice, so the trailing sentence has to change.
    A reset a few minutes out also has to come out plural."""
    import time

    from internpearls import net
    reset_ts = int(time.time()) + 300  # about 5 minutes out
    _urlopen(monkeypatch, _http_error(
        403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset_ts)}))
    with pytest.raises(net.RateLimitedError) as e:
        net._http_get("http://example.invalid", token="t0ken")
    msg = str(e.value)
    assert "about 5 minutes)" in msg
    assert "Your token's limit will reset then." in msg
    assert "Signing in with a token" not in msg


def test_a_403_without_the_rate_limit_headers_or_token_is_plain_access_denied(
        monkeypatch):
    """No X-RateLimit-Remaining header, no "rate limit" in the body, and no token was
    sent: a genuine auth failure on an anonymous request, so the message must not
    presume a token exists to check."""
    from internpearls import net
    _urlopen(monkeypatch, _http_error(403))
    with pytest.raises(RuntimeError) as e:
        net._http_get("http://example.invalid")
    msg = str(e.value)
    assert msg == "access denied"
    assert "token" not in msg


def test_a_403_with_a_token_sent_names_the_token_in_the_message(monkeypatch):
    """The same genuine-auth-failure branch, but a token was actually sent this time, so
    the message may reasonably tell the caller to check it."""
    from internpearls import net
    _urlopen(monkeypatch, _http_error(403))
    with pytest.raises(RuntimeError) as e:
        net._http_get("http://example.invalid", token="t0ken")
    assert "access denied (check that your token is valid and can read this repo)" \
        in str(e.value)


def test_a_403_whose_body_mentions_the_rate_limit_is_recognized_without_the_header(
        monkeypatch):
    """A defensive second signal: some GitHub responses carry the explanation in the
    JSON body rather than (or in addition to) the header."""
    from internpearls import net
    _urlopen(monkeypatch, _http_error(
        403, body=b'{"message": "API rate limit exceeded for 1.2.3.4."}'))
    with pytest.raises(net.RateLimitedError):
        net._http_get("http://example.invalid")


def test_gh_public_raw_with_a_bad_token_retries_without_it(monkeypatch):
    """A fine-grained token scoped to other repos gets refused reading this public repo;
    the fetch must not fail outright when dropping the token would have worked fine
    all along."""
    from internpearls import net
    calls = []

    def fake(req, timeout=None):
        calls.append(req.get_header("Authorization"))
        if req.get_header("Authorization"):
            raise _http_error(403)
        return _Response(b"{}")

    monkeypatch.setattr(net.urllib.request, "urlopen", fake)
    data = net._gh_public_raw("version.json", token="badtoken")
    assert data == b"{}"
    assert calls == ["Bearer badtoken", None]


def test_gh_public_raw_propagates_a_transport_error_on_the_token_attempt_without_retrying(
        monkeypatch):
    """An unreachable host isn't fixed by dropping the token: retrying just doubles the
    wait on a connection that was never going to answer either way."""
    from internpearls import net
    calls = []

    def fake(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.URLError("nodename nor servname provided")

    monkeypatch.setattr(net.urllib.request, "urlopen", fake)
    with pytest.raises(net.TransportError):
        net._gh_public_raw("version.json", token="t0ken")
    assert len(calls) == 1, "a dead connection must not be retried a second time"


def test_gh_public_raw_propagates_a_server_error_on_the_token_attempt_without_retrying(
        monkeypatch):
    """A 5xx is the server's own problem, not the token's; dropping the token can't fix
    it, so it isn't worth a second request either."""
    from internpearls import net
    calls = []

    def fake(req, timeout=None):
        calls.append(req.full_url)
        raise _http_error(500)

    monkeypatch.setattr(net.urllib.request, "urlopen", fake)
    with pytest.raises(RuntimeError) as e:
        net._gh_public_raw("version.json", token="t0ken")
    assert "HTTP 500" in str(e.value)
    assert len(calls) == 1


def test_gh_public_raw_chains_through_token_then_rate_limit_then_cdn(monkeypatch):
    """The full three-hop path in one test: a token that can't read this repo falls
    through to an unauthenticated retry, which is itself rate limited, so the raw CDN
    is the last resort. The tests above each prove one link; this proves the chain."""
    from internpearls import net
    seen = []

    def fake(req, timeout=None):
        seen.append((req.full_url, req.get_header("Authorization")))
        if req.get_header("Authorization"):
            raise _http_error(403)  # auth-shaped: this token can't read this repo
        if "api.github.com" in req.full_url:
            raise _http_error(403, headers={"X-RateLimit-Remaining": "0"})
        return _Response(b"cdn bytes")

    monkeypatch.setattr(net.urllib.request, "urlopen", fake)
    data = net._gh_public_raw("version.json", ref="main", token="badtoken")
    assert data == b"cdn bytes"
    assert seen == [
        ("https://api.github.com/repos/LTimothy/internpearls-anki/contents/"
         "version.json?ref=main", "Bearer badtoken"),
        ("https://api.github.com/repos/LTimothy/internpearls-anki/contents/"
         "version.json?ref=main", None),
        ("https://raw.githubusercontent.com/LTimothy/internpearls-anki/main/"
         "version.json", None),
    ]


def test_gh_public_raw_falls_back_to_the_raw_cdn_when_rate_limited(monkeypatch):
    """The API's own quota is out; the raw CDN needs no token and no quota, so it's the
    last resort rather than a failed check."""
    from internpearls import net
    seen_urls = []

    def fake(req, timeout=None):
        seen_urls.append(req.full_url)
        if "api.github.com" in req.full_url:
            raise _http_error(403, headers={"X-RateLimit-Remaining": "0"})
        return _Response(b"cdn bytes")

    monkeypatch.setattr(net.urllib.request, "urlopen", fake)
    data = net._gh_public_raw("version.json", ref="main")
    assert data == b"cdn bytes"
    assert seen_urls == [
        "https://api.github.com/repos/LTimothy/internpearls-anki/contents/"
        "version.json?ref=main",
        "https://raw.githubusercontent.com/LTimothy/internpearls-anki/main/"
        "version.json",
    ]


def test_the_update_check_passes_the_configured_token(monkeypatch, anki):
    """updates.py's own fetch has to actually use the learner's token, not just accept
    the parameter: the whole point is raising the check's rate limit. Goes through
    check_updates() itself, the real entry point that reads _cfg()["gh_token"], so this
    proves the config is actually wired up rather than just that the parameter, handed
    in directly, threads through to _gh_public_raw."""
    from internpearls import updates
    anki.mw._config = {"github_token": "her-token"}
    seen = {}

    def fake_public_raw(path, ref="main", timeout=None, token=None):
        seen["token"] = token
        return b'{"version": "0.1.0"}'

    monkeypatch.setattr(updates, "_gh_public_raw", fake_public_raw)
    updates.check_updates()
    assert seen["token"] == "her-token"

    seen.clear()
    updates._fetch_addon_version_info(token=updates._cfg()["gh_token"])
    assert seen["token"] == "her-token"


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


def test_fetch_card_image_refuses_svg(monkeypatch):
    """A downloaded SVG bypasses ai_logic.svg_to_media's script check entirely, so a
    web image is raster only; a model that wants an SVG draws one instead."""
    from internpearls import net
    _urlopen(monkeypatch, _Response(
        b"<svg></svg>", headers={"Content-Type": "image/svg+xml"}))
    with pytest.raises(RuntimeError):
        net.fetch_card_image("https://example.com/x.svg")


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
