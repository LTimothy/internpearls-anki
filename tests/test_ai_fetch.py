"""Card-picture downloads: https on every hop, no private or local addresses."""
import socket
import threading
import urllib.request

import pytest

from internpearls import ai_fetch

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture(autouse=True)
def no_proxy_by_default(monkeypatch):
    monkeypatch.setattr(ai_fetch.urllib.request, "getproxies", lambda: {})


class _Response:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._body = body

    def getheader(self, name, default=None):
        return self._headers.get(name.lower(), default)

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._body)
        out, self._body = self._body[:n], self._body[n:]
        return out


class _Sock:
    def __init__(self):
        self.down = False

    def shutdown(self, how):
        self.down = True


class _Web:
    """Stands in for DNS and the network: `hosts` maps a name to the addresses it
    resolves to, `pages` maps (host, target) to the response served there."""

    def __init__(self, monkeypatch, hosts, pages):
        self.hosts, self.pages, self.connected = hosts, pages, []
        self.refusing, self.refused = set(), []
        self.sock_factory = _Sock
        monkeypatch.setattr(ai_fetch, "_resolve", self.resolve)
        monkeypatch.setattr(ai_fetch, "_open_connection", self.open)

    def resolve(self, host, port):
        if host not in self.hosts:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM,
                 6, "", (a, port)) for a in self.hosts[host]]

    def open(self, host, ip, port, timeout, on_socket=None, deadline=None):
        web = self

        class _Conn:
            sock = None

            def connect(self):
                if ip in web.refusing:
                    web.refused.append(ip)
                    raise ConnectionRefusedError("refused")
                web.connected.append((host, ip))
                self.sock = web.sock_factory()

            def request(self, method, target, headers=None):
                self.response = web.pages[(host, target)]
                if callable(self.response):
                    self.response = self.response(self.sock)

            def getresponse(self):
                return self.response

            def close(self):
                pass
        return _Conn()


def _png():
    return _Response(200, {"Content-Type": "image/png"}, PNG)


def test_a_public_image_downloads(monkeypatch):
    web = _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
               {("img.example", "/a.png?x=1"): _png()})
    assert ai_fetch.fetch_card_image("https://img.example/a.png?x=1") == (PNG, "png")
    assert web.connected == [("img.example", "93.184.216.34")]


def test_a_direct_download_ignores_the_machines_proxy_settings(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://environment.example:8080")
    monkeypatch.setattr(urllib.request, "getproxies_macosx_sysconf", lambda: {
        "https": "http://system.example:8080"}, raising=False)
    web = _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
               {("img.example", "/a.png?x=1"): _png()})
    assert ai_fetch.fetch_card_image("https://img.example/a.png?x=1") == (PNG, "png")
    assert web.connected == [("img.example", "93.184.216.34")]


def test_an_overlong_dns_label_is_a_lookup_transport_error():
    host = "x" * 64 + ".example"
    with pytest.raises(ai_fetch.TransportError, match=f"couldn't look up {host}") as raised:
        ai_fetch._resolve_within(host, 443, 1)
    assert isinstance(raised.value.__cause__, UnicodeError)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.1.2.3", "172.16.0.5", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fd00::1",
    "::ffff:127.0.0.1", "2002:7f00:1::1",
])
def test_a_host_on_a_private_or_local_address_is_refused_before_connecting(
        monkeypatch, address):
    web = _Web(monkeypatch, {"img.example": [address]},
               {("img.example", "/a.png"): _png()})
    with pytest.raises(RuntimeError, match="private or local"):
        ai_fetch.fetch_card_image("https://img.example/a.png")
    assert web.connected == []


def test_a_host_with_any_private_address_is_refused(monkeypatch):
    web = _Web(monkeypatch, {"img.example": ["93.184.216.34", "10.0.0.1"]},
               {("img.example", "/a.png"): _png()})
    with pytest.raises(RuntimeError, match="private or local"):
        ai_fetch.fetch_card_image("https://img.example/a.png")
    assert web.connected == []


def test_a_redirect_to_plain_http_is_refused(monkeypatch):
    web = _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
               {("img.example", "/a.png"): _Response(
                   302, {"Location": "http://img.example/a.png"})})
    with pytest.raises(RuntimeError) as raised:
        ai_fetch.fetch_card_image("https://img.example/a.png")
    assert str(raised.value) == "image must be served over https"
    assert web.connected == [("img.example", "93.184.216.34")]


def test_a_plain_http_address_is_refused_in_the_same_words_as_a_redirect():
    with pytest.raises(RuntimeError) as raised:
        ai_fetch.fetch_card_image("http://img.example/a.png")
    assert str(raised.value) == "image must be served over https"


def test_a_redirect_to_a_private_address_is_refused_before_connecting(monkeypatch):
    web = _Web(monkeypatch,
               {"img.example": ["93.184.216.34"], "router.example": ["192.168.0.1"]},
               {("img.example", "/a.png"): _Response(
                   301, {"Location": "https://router.example/admin.png"}),
                ("router.example", "/admin.png"): _png()})
    with pytest.raises(RuntimeError, match="private or local"):
        ai_fetch.fetch_card_image("https://img.example/a.png")
    assert web.connected == [("img.example", "93.184.216.34")]


def test_a_public_redirect_is_followed(monkeypatch):
    web = _Web(monkeypatch,
               {"img.example": ["93.184.216.34"], "cdn.example": ["151.101.1.1"]},
               {("img.example", "/a.png"): _Response(
                   308, {"Location": "https://cdn.example/b.png"}),
                ("cdn.example", "/b.png"): _png()})
    assert ai_fetch.fetch_card_image("https://img.example/a.png") == (PNG, "png")
    assert [h for h, _ in web.connected] == ["img.example", "cdn.example"]


def test_endless_redirects_stop(monkeypatch):
    _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
         {("img.example", "/a.png"): _Response(302, {"Location": "/a.png"})})
    with pytest.raises(RuntimeError, match="redirects"):
        ai_fetch.fetch_card_image("https://img.example/a.png")


def test_an_image_whose_bytes_are_not_that_image_is_refused(monkeypatch):
    _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
         {("img.example", "/a.png"): _Response(
             200, {"Content-Type": "image/png"}, b"<html><script>x()</script>")})
    with pytest.raises(RuntimeError, match="not an image"):
        ai_fetch.fetch_card_image("https://img.example/a.png")


def test_an_oversize_image_is_refused(monkeypatch):
    _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
         {("img.example", "/a.png"): _Response(
             200, {"Content-Type": "image/png"}, PNG + b"\x00" * 100)})
    with pytest.raises(RuntimeError, match="too large"):
        ai_fetch.fetch_card_image("https://img.example/a.png", max_bytes=50)


def test_a_404_names_the_address_not_the_deck_source(monkeypatch):
    _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
         {("img.example", "/a.png"): _Response(404)})
    with pytest.raises(RuntimeError) as exc:
        ai_fetch.fetch_card_image("https://img.example/a.png")
    assert "no image at that address" in str(exc.value)
    assert "repo" not in str(exc.value)


def test_a_loopback_server_is_never_contacted():
    """The real resolver and socket path: a server listening on this machine must
    see no connection at all."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(0.5)
    port = server.getsockname()[1]
    contacted = []

    def accept():
        try:
            conn, _ = server.accept()
            contacted.append(True)
            conn.close()
        except OSError:
            pass
    t = threading.Thread(target=accept)
    t.start()
    try:
        for url in (f"https://127.0.0.1:{port}/a.png", f"https://localhost:{port}/a.png",
                    f"https://0x7f000001:{port}/a.png"):
            with pytest.raises(RuntimeError):
                ai_fetch.fetch_card_image(url, timeout=1)
    finally:
        server.close()
        t.join(3)
    assert not t.is_alive()
    assert contacted == []


# === deadline, slow lookups, and every address tried ========================

def test_each_public_address_is_tried_in_turn(monkeypatch):
    web = _Web(monkeypatch, {"img.example": ["93.184.216.34", "93.184.216.35"]},
               {("img.example", "/a.png"): _png()})
    web.refusing = {"93.184.216.34"}
    assert ai_fetch.fetch_card_image("https://img.example/a.png") == (PNG, "png")
    assert web.refused == ["93.184.216.34"]
    assert web.connected == [("img.example", "93.184.216.35")]


@pytest.fixture
def stalled_tls_server(monkeypatch):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(2)
    port = server.getsockname()[1]
    stop = threading.Event()
    hellos = []

    def serve():
        try:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                hellos.append(conn.recv(4096))
                stop.wait(3)
        except OSError:
            pass

    thread = threading.Thread(target=serve)
    thread.start()
    monkeypatch.setattr(ai_fetch, "_resolve", lambda host, p: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))])
    is_public = ai_fetch._is_public
    monkeypatch.setattr(ai_fetch, "_is_public", lambda ip:
                        str(ip) == "127.0.0.1" or is_public(ip))
    try:
        yield port, hellos
    finally:
        stop.set()
        server.close()
        thread.join(3)
        assert not thread.is_alive()


def test_a_stalled_direct_tls_handshake_stops_at_the_deadline(stalled_tls_server):
    import time
    port, hellos = stalled_tls_server
    start = time.monotonic()
    with pytest.raises(ai_fetch.TransportError, match="timed out"):
        ai_fetch.fetch_card_image(f"https://img.example:{port}/a.png", deadline_s=0.5)
    assert time.monotonic() - start < 0.65
    assert hellos and hellos[0].startswith(b"\x16\x03")


@pytest.mark.parametrize("fetch", [True, False], ids=["watchdog", "socket-timeout"])
def test_a_slow_direct_connect_leaves_only_remaining_time_for_tls(
        monkeypatch, stalled_tls_server, fetch):
    import time
    port, hellos = stalled_tls_server

    class _SlowConnect(socket.socket):
        def connect(self, address):
            time.sleep(0.325)
            return super().connect(address)

    monkeypatch.setattr(ai_fetch.socket, "socket", _SlowConnect)
    start = time.monotonic()
    with pytest.raises(ai_fetch.TransportError, match="timed out"):
        if fetch:
            ai_fetch.fetch_card_image(f"https://img.example:{port}/a.png", deadline_s=0.5)
        else:
            ai_fetch._connect_any("img.example", ["127.0.0.1"], port, 1, start + 0.5)
    assert time.monotonic() - start < 0.65
    assert hellos and hellos[0].startswith(b"\x16\x03")


@pytest.mark.parametrize("fails", [False, True])
def test_abandoned_lookups_are_capped_until_their_threads_finish(monkeypatch, fails):
    import time
    monkeypatch.setattr(ai_fetch, "_MAX_ABANDONED_LOOKUPS", 2, raising=False)
    stop = threading.Event()
    threads = []
    real_thread = threading.Thread

    def thread(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        threads.append(t)
        return t

    def blocked():
        stop.wait()
        if fails:
            raise OSError("lookup failed")
        return "resolved"

    monkeypatch.setattr(ai_fetch.threading, "Thread", thread)
    try:
        for _ in range(2):
            with pytest.raises(ai_fetch.TransportError, match="timed out") as raised:
                ai_fetch._run_within(blocked, 0.05)
            assert str(raised.value) == str(ai_fetch._timed_out())
        assert len(threads) == 2
        assert all(t.is_alive() and t.daemon for t in threads)
        start = time.monotonic()
        with pytest.raises(ai_fetch.TransportError, match="timed out") as raised:
            ai_fetch._run_within(blocked, 2)
        assert str(raised.value) == str(ai_fetch._timed_out())
        assert time.monotonic() - start < 0.5
        assert len(threads) == 2
    finally:
        stop.set()
        for t in threads:
            t.join(3)
    assert all(not t.is_alive() for t in threads)
    assert ai_fetch._run_within(lambda: "resolved again", 1) == "resolved again"


def test_fast_lookups_do_not_use_up_the_abandoned_lookup_cap(monkeypatch):
    monkeypatch.setattr(ai_fetch, "_MAX_ABANDONED_LOOKUPS", 2, raising=False)
    for _ in range(4):
        assert ai_fetch._run_within(lambda: "resolved", 1) == "resolved"

    def failed():
        raise OSError("lookup failed")

    with pytest.raises(OSError, match="lookup failed"):
        ai_fetch._run_within(failed, 1)
    assert ai_fetch._run_within(lambda: "resolved", 1) == "resolved"


def test_running_lookups_reserve_slots_before_they_can_be_abandoned(monkeypatch):
    monkeypatch.setattr(ai_fetch, "_MAX_ABANDONED_LOOKUPS", 2, raising=False)
    stop = threading.Event()
    started = [threading.Event(), threading.Event()]
    workers, results = [], []

    def blocked(index):
        workers.append(threading.current_thread())
        started[index].set()
        stop.wait()
        return index

    def lookup(index):
        try:
            results.append(ai_fetch._run_within(lambda: blocked(index), 3))
        except Exception as e:
            results.append(e)

    callers = [threading.Thread(target=lookup, args=(i,)) for i in range(2)]
    try:
        for i, t in enumerate(callers):
            t.start()
            assert started[i].wait(1)
        with pytest.raises(ai_fetch.TransportError, match="timed out"):
            ai_fetch._run_within(lambda: "extra lookup", 1)
    finally:
        stop.set()
        for t in callers:
            if t.ident is not None:
                t.join(4)
        for t in workers:
            t.join(4)
    assert all(not t.is_alive() for t in callers + workers)
    assert sorted(results) == [0, 1]
    assert ai_fetch._run_within(lambda: "resolved again", 1) == "resolved again"


def test_a_lookup_that_hangs_is_abandoned_at_the_deadline(monkeypatch):
    import time
    web = _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
               {("img.example", "/a.png"): _png()})
    real = web.resolve

    def slow(host, port):
        time.sleep(2)
        return real(host, port)
    monkeypatch.setattr(ai_fetch, "_resolve", slow)
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        ai_fetch.fetch_card_image("https://img.example/a.png", deadline_s=0.3)
    assert time.monotonic() - start < 1.5
    assert web.connected == []


class _Trickle(_Response):
    """A body that arrives a few bytes at a time until its socket is shut down."""

    def __init__(self, sock):
        super().__init__(200, {"Content-Type": "image/png"}, PNG)
        self.sock = sock

    def read(self, n=-1):
        import time
        while not self.sock.down:
            time.sleep(0.05)
            return b"\x00"
        return b""


def test_a_trickling_download_is_cut_off_at_the_deadline(monkeypatch):
    import time
    _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
         {("img.example", "/a.png"): _Trickle})
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        ai_fetch.fetch_card_image("https://img.example/a.png", deadline_s=0.5)
    assert time.monotonic() - start < 2


def test_the_deadline_covers_every_hop(monkeypatch):
    import time
    web = _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
               {("img.example", "/a.png"): _Response(302, {"Location": "/b.png"}),
                ("img.example", "/b.png"): _Response(302, {"Location": "/a.png"})})
    real = web.resolve

    def slowish(host, port):
        time.sleep(0.2)
        return real(host, port)
    monkeypatch.setattr(ai_fetch, "_resolve", slowish)
    with pytest.raises(RuntimeError, match="timed out"):
        ai_fetch.fetch_card_image("https://img.example/a.png", deadline_s=0.5)
    assert len(web.connected) < 4


def test_a_close_delimited_trickle_on_a_real_socket_stops_at_the_deadline(monkeypatch):
    """HTTP/1.0 with Connection: close and no length: http.client lets go of
    conn.sock once the response starts, so the deadline has to reach the socket
    it captured at connect and the reader has to check it between pieces."""
    import http.client
    import time
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(2)
    port = server.getsockname()[1]
    stop = threading.Event()

    def serve():
        try:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: image/png\r\n"
                             b"Connection: close\r\n\r\n" + PNG[:8])
                for _ in range(100):
                    conn.sendall(b"\x00")
                    if stop.wait(0.2):
                        break
        except OSError:
            pass
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    monkeypatch.setattr(ai_fetch, "_resolve", lambda host, p: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))])
    monkeypatch.setattr(
        ai_fetch, "_open_connection",
        lambda host, ip, p, timeout, on_socket=None, deadline=None: http.client.HTTPConnection(
            "127.0.0.1", port, timeout=timeout))
    start = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            ai_fetch.fetch_card_image("https://img.example/a.png", deadline_s=1.0)
        assert time.monotonic() - start < 3
    finally:
        stop.set()
        server.close()
        t.join(3)
    assert not t.is_alive()



class _ClosedAfterShutdown(_Response):
    """A read on an SSL socket the deadline shut down raises ValueError."""

    def __init__(self, sock):
        super().__init__(200, {"Content-Type": "image/png"}, PNG)
        self.sock = sock

    def read(self, n=-1):
        import time
        while not self.sock.down:
            time.sleep(0.05)
            return b"\x00"
        raise ValueError("Read on closed or unwrapped SSL socket.")


def test_a_read_on_a_socket_shut_by_the_deadline_reads_as_a_timeout(monkeypatch):
    _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
         {("img.example", "/a.png"): _ClosedAfterShutdown})
    with pytest.raises(RuntimeError, match="timed out"):
        ai_fetch.fetch_card_image("https://img.example/a.png", deadline_s=0.4)


@pytest.mark.parametrize("ctype", ["image/svg+xml", "text/html", None])
def test_only_a_raster_image_type_is_accepted(monkeypatch, ctype):
    """A downloaded SVG would bypass the drawn-SVG checks, and a page is not a
    picture: both are refused on their declared type."""
    headers = {"Content-Type": ctype} if ctype else {}
    _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
         {("img.example", "/a.png"): _Response(200, headers, b"<svg></svg>")})
    with pytest.raises(RuntimeError, match="not an image"):
        ai_fetch.fetch_card_image("https://img.example/a.png")


def test_a_wikimedia_file_page_is_fetched_as_its_rendered_image(monkeypatch):
    web = _Web(monkeypatch, {"en.wikipedia.org": ["93.184.216.34"]},
               {("en.wikipedia.org", "/wiki/Special:FilePath/Capnogram.png?width=1200"):
                _png()})
    assert ai_fetch.fetch_card_image(
        "https://en.wikipedia.org/wiki/File:Capnogram.png") == (PNG, "png")
    assert web.connected == [("en.wikipedia.org", "93.184.216.34")]


def test_a_content_type_with_parameters_is_accepted(monkeypatch):
    _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
         {("img.example", "/a.png"): _Response(
             200, {"Content-Type": "image/png; charset=binary"}, PNG)})
    assert ai_fetch.fetch_card_image("https://img.example/a.png") == (PNG, "png")


def test_a_declared_length_over_the_cap_is_refused_before_reading(monkeypatch):
    """The body here would fit; the declared length alone refuses it."""
    _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
         {("img.example", "/a.png"): _Response(
             200, {"Content-Type": "image/png", "Content-Length": "999999"}, PNG)})
    with pytest.raises(RuntimeError, match="too large"):
        ai_fetch.fetch_card_image("https://img.example/a.png", max_bytes=len(PNG) + 10)


def test_a_body_exactly_at_the_cap_is_accepted(monkeypatch):
    _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
         {("img.example", "/a.png"): _Response(200, {"Content-Type": "image/png"}, PNG)})
    assert ai_fetch.fetch_card_image(
        "https://img.example/a.png", max_bytes=len(PNG)) == (PNG, "png")


def test_without_https_support_a_picture_fails_as_a_transport_error(monkeypatch):
    monkeypatch.setattr(ai_fetch, "_HTTPSConnection", None)
    with pytest.raises(ai_fetch.TransportError):
        ai_fetch._open_connection("example.org", "93.184.216.34", 443, 5)


def _proxy_settings(monkeypatch, url, bypass=()):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": url})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: host in bypass)


class _ProxyWeb(_Web):
    def __init__(self, monkeypatch, hosts, pages):
        super().__init__(monkeypatch, hosts, pages)
        self.tunnels, self.requests = [], []
        self.connect_error = None
        web = self
        resolve = self.resolve

        def proxy_resolve(host, port):
            if host == "proxy.corp" or host.startswith("10.0.0."):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", port))]
            return resolve(host, port)

        monkeypatch.setattr(ai_fetch, "_resolve", proxy_resolve)

        class _Conn:
            sock = None

            def __init__(self, host, port, timeout, context, address, on_socket):
                self.proxy_host, self.proxy_port = host, port
                self.timeout, self.context = timeout, context
                self.on_socket = on_socket

            def set_tunnel(self, host, port, headers=None):
                self.host, self.port, self.headers = host, port, headers or {}

            def connect(self):
                web.tunnels.append(self)
                if web.connect_error is not None:
                    raise web.connect_error
                self.sock = web.sock_factory()
                self.on_socket(self.sock)

            def request(self, method, target, headers=None):
                web.requests.append((self.host, method, target, headers))
                self.response = web.pages[(self.host, target)]
                if callable(self.response):
                    self.response = self.response(self.sock)

            def getresponse(self):
                self.sock = None
                return self.response

            def close(self):
                pass
        monkeypatch.setattr(ai_fetch, "_ProxyHTTPSConnection", _Conn)


@pytest.mark.parametrize("port", [443, 8443])
def test_a_picture_uses_the_https_proxy_and_tunnels_by_name(monkeypatch, port):
    import ssl
    _proxy_settings(monkeypatch, "http://10.0.0.2:8080")
    web = _ProxyWeb(monkeypatch, {"img.example": ["93.184.216.34"]},
                    {("img.example", "/a.png"): _png()})
    assert ai_fetch.fetch_card_image(f"https://img.example:{port}/a.png") == (PNG, "png")
    assert web.connected == []
    assert len(web.tunnels) == 1
    tunnel = web.tunnels[0]
    assert (tunnel.proxy_host, tunnel.proxy_port) == ("10.0.0.2", 8080)
    assert (tunnel.host, tunnel.port) == ("img.example", port)
    assert tunnel.context.check_hostname
    assert tunnel.context.verify_mode == ssl.CERT_REQUIRED
    assert 0 < tunnel.timeout <= ai_fetch._DOWNLOAD_TIMEOUT


@pytest.mark.parametrize("proxy, host, port, headers", [
    ("proxy.corp:8080", "proxy.corp", 8080, {}),
    ("10.0.0.5:3128", "10.0.0.5", 3128, {}),
    ("user:p%40ss@proxy.corp:8080", "proxy.corp", 8080,
     {"Proxy-Authorization": "Basic dXNlcjpwQHNz"}),
])
def test_a_proxy_without_a_scheme_tunnels_through_its_host_and_port(
        monkeypatch, proxy, host, port, headers):
    _proxy_settings(monkeypatch, proxy)
    web = _ProxyWeb(monkeypatch, {"img.example": ["93.184.216.34"]},
                    {("img.example", "/a.png"): _png()})
    assert ai_fetch.fetch_card_image("https://img.example/a.png") == (PNG, "png")
    assert web.connected == []
    assert len(web.tunnels) == 1
    tunnel = web.tunnels[0]
    assert (tunnel.proxy_host, tunnel.proxy_port) == (host, port)
    assert (tunnel.host, tunnel.port) == ("img.example", 443)
    assert tunnel.headers == headers
    assert "Proxy-Authorization" not in web.requests[0][3]


def test_a_proxy_without_a_scheme_is_bypassed_for_a_bypassed_host(monkeypatch):
    _proxy_settings(monkeypatch, "proxy.corp:8080", bypass=("img.example",))
    web = _ProxyWeb(monkeypatch, {"img.example": ["93.184.216.34"]},
                    {("img.example", "/a.png"): _png()})
    assert ai_fetch.fetch_card_image("https://img.example/a.png") == (PNG, "png")
    assert web.connected == [("img.example", "93.184.216.34")]
    assert web.tunnels == []


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["93.184.216.34", "10.0.0.1"]])
def test_a_proxy_does_not_allow_a_private_target(monkeypatch, addresses):
    _proxy_settings(monkeypatch, "http://10.0.0.2:8080")
    web = _ProxyWeb(monkeypatch, {"img.example": addresses}, {})
    with pytest.raises(RuntimeError, match="private or local"):
        ai_fetch.fetch_card_image("https://img.example/a.png")
    assert web.tunnels == []
    assert web.connected == []


def test_a_proxy_does_not_skip_the_target_lookup(monkeypatch):
    _proxy_settings(monkeypatch, "http://10.0.0.2:8080")
    web = _ProxyWeb(monkeypatch, {}, {})
    with pytest.raises(ai_fetch.TransportError, match="couldn't look up img.example"):
        ai_fetch.fetch_card_image("https://img.example/a.png")
    assert web.tunnels == []
    assert web.connected == []


def test_a_host_on_the_proxy_bypass_list_connects_directly(monkeypatch):
    _proxy_settings(monkeypatch, "http://10.0.0.2:8080", bypass=("img.example",))
    web = _ProxyWeb(monkeypatch, {"img.example": ["93.184.216.34"]},
                    {("img.example", "/a.png"): _png()})
    assert ai_fetch.fetch_card_image("https://img.example/a.png") == (PNG, "png")
    assert web.connected == [("img.example", "93.184.216.34")]
    assert web.tunnels == []


def test_proxy_credentials_are_decoded_and_only_sent_on_connect(monkeypatch):
    _proxy_settings(monkeypatch, "http://user%20name:p%40ss@10.0.0.2:8080")
    web = _ProxyWeb(monkeypatch, {"img.example": ["93.184.216.34"]},
                    {("img.example", "/a.png"): _png()})
    assert ai_fetch.fetch_card_image("https://img.example/a.png") == (PNG, "png")
    assert web.tunnels[0].headers == {
        "Proxy-Authorization": "Basic dXNlciBuYW1lOnBAc3M="}
    assert "Proxy-Authorization" not in web.requests[0][3]


@pytest.mark.parametrize("scheme", ["socks5", "https"])
def test_an_unsupported_proxy_scheme_is_a_transport_error(monkeypatch, scheme):
    _proxy_settings(monkeypatch, f"{scheme}://user:secret@10.0.0.2:8080")
    web = _ProxyWeb(monkeypatch, {"img.example": ["93.184.216.34"]},
                    {("img.example", "/a.png"): _png()})
    with pytest.raises(ai_fetch.TransportError, match=scheme) as raised:
        ai_fetch.fetch_card_image("https://img.example/a.png")
    assert "secret" not in str(raised.value)
    assert "user" not in str(raised.value)
    assert web.tunnels == []
    assert web.connected == []


@pytest.mark.parametrize("status", [407, 403])
def test_a_proxy_refusal_is_a_transport_error_without_credentials(monkeypatch, status):
    import traceback
    _proxy_settings(monkeypatch, "http://user%20name:p%40ss@10.0.0.2:8080")
    web = _ProxyWeb(monkeypatch, {"img.example": ["93.184.216.34"]},
                    {("img.example", "/a.png"): _png()})
    web.connect_error = OSError(f"Tunnel connection failed: {status} user name:p@ss")
    with pytest.raises(ai_fetch.TransportError, match="proxy.*refused") as raised:
        ai_fetch.fetch_card_image("https://img.example/a.png")
    message = "".join(traceback.format_exception(
        type(raised.value), raised.value, raised.value.__traceback__))
    for credential in ("user name", "p@ss", "user%20name", "p%40ss"):
        assert credential not in message
    assert web.connected == []


@pytest.mark.parametrize("bypass", [("img.example",), ("cdn.example",)])
def test_a_redirect_reads_the_proxy_for_the_new_host(monkeypatch, bypass):
    _proxy_settings(monkeypatch, "http://10.0.0.2:8080", bypass=bypass)
    reads = []

    def proxies():
        reads.append(True)
        return {"https": f"http://10.0.0.{len(reads) + 1}:8080"}
    monkeypatch.setattr(urllib.request, "getproxies", proxies)
    web = _ProxyWeb(monkeypatch,
                    {"img.example": ["93.184.216.34"], "cdn.example": ["151.101.1.1"]},
                    {("img.example", "/a.png"): _Response(
                        302, {"Location": "https://cdn.example/b.png"}),
                     ("cdn.example", "/b.png"): _png()})
    assert ai_fetch.fetch_card_image("https://img.example/a.png") == (PNG, "png")
    assert len(reads) == 2
    assert [host for host, _ in web.connected] == list(bypass)
    tunnel = web.tunnels[0]
    if bypass == ("img.example",):
        assert (tunnel.host, tunnel.proxy_host) == ("cdn.example", "10.0.0.3")
    else:
        assert (tunnel.host, tunnel.proxy_host) == ("img.example", "10.0.0.2")


def test_a_real_proxy_receives_connect_by_name_and_407_is_a_transport_error(monkeypatch):
    import re
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(2)
    port = server.getsockname()[1]
    requests = []
    errors = []

    def serve():
        try:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                requests.append(request)
                conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                             b"Content-Length: 0\r\n\r\n")
        except OSError as e:
            errors.append(e)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _proxy_settings(monkeypatch, f"http://user%20name:p%40ss@127.0.0.1:{port}")
    monkeypatch.setattr(ai_fetch, "_open_connection", lambda *args: pytest.fail(
        "a configured proxy must not connect directly"))
    monkeypatch.setattr(ai_fetch, "_resolve", lambda host, p: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "",
         ("93.184.216.34" if host == "img.example" else "127.0.0.1", p))])
    try:
        with pytest.raises(ai_fetch.TransportError, match="proxy.*refused") as raised:
            ai_fetch.fetch_card_image("https://img.example/a.png", timeout=1, deadline_s=2)
        assert "p@ss" not in str(raised.value)
        assert "p%40ss" not in str(raised.value)
    finally:
        server.close()
        thread.join(3)
    assert not thread.is_alive()
    assert errors == []
    assert len(requests) == 1
    lines = requests[0].decode("ascii").split("\r\n")
    assert re.fullmatch(r"CONNECT img\.example:443 HTTP/1\.[01]", lines[0])
    assert "Proxy-Authorization: Basic dXNlciBuYW1lOnBAc3M=" in lines[1:]


def test_a_proxy_download_still_shuts_down_a_trickling_socket(monkeypatch):
    import time

    class _BlockedUntilShutdown(_Response):
        def __init__(self, sock):
            super().__init__(200, {"Content-Type": "image/png"}, PNG)
            self.sock = sock

        def read(self, n=-1):
            while not self.sock.down:
                time.sleep(0.01)
            raise ValueError("Read on closed or unwrapped SSL socket.")

    _proxy_settings(monkeypatch, "http://10.0.0.2:8080")
    web = _ProxyWeb(monkeypatch, {"img.example": ["93.184.216.34"]},
                    {("img.example", "/a.png"): _BlockedUntilShutdown})
    sockets = []

    def sock():
        sockets.append(_Sock())
        return sockets[-1]
    web.sock_factory = sock
    start = time.monotonic()
    with pytest.raises(ai_fetch.TransportError, match="timed out"):
        ai_fetch.fetch_card_image("https://img.example/a.png", deadline_s=0.4)
    assert time.monotonic() - start < 2
    assert len(web.tunnels) == 1
    assert sockets[0].down


def test_a_proxy_fetch_without_https_support_is_a_transport_error(monkeypatch):
    _proxy_settings(monkeypatch, "http://10.0.0.2:8080")
    _Web(monkeypatch, {"img.example": ["93.184.216.34"]},
         {("img.example", "/a.png"): _png()})
    monkeypatch.setattr(ai_fetch, "_HTTPSConnection", None)
    with pytest.raises(ai_fetch.TransportError, match="https isn't available here"):
        ai_fetch.fetch_card_image("https://img.example/a.png")


@pytest.mark.parametrize("interim", [b"", b"HTTP/1.1 100 Continue\r\n\r\n",
                                    b"HTTP/1.1 103 Early Hints\r\n\r\n"])
def test_a_trickling_connect_reply_stops_at_the_deadline(monkeypatch, interim):
    import time
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(2)
    port = server.getsockname()[1]
    stop = threading.Event()
    requests = []

    def serve():
        try:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    request += chunk
                requests.append(request)
                conn.sendall(interim)
                for byte in b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n":
                    conn.sendall(bytes([byte]))
                    if stop.wait(0.03):
                        return
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _proxy_settings(monkeypatch, f"http://127.0.0.1:{port}")
    monkeypatch.setattr(ai_fetch, "_resolve", lambda host, p: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "",
         ("93.184.216.34" if host == "img.example" else "127.0.0.1", p))])
    start = time.monotonic()
    try:
        with pytest.raises(ai_fetch.TransportError, match="timed out"):
            ai_fetch.fetch_card_image("https://img.example/a.png", deadline_s=0.3)
        assert time.monotonic() - start < 1.0
        assert requests[0].startswith(b"CONNECT img.example:443 HTTP/1.")
    finally:
        stop.set()
        server.close()
        thread.join(3)
    assert not thread.is_alive()


def test_a_hanging_proxy_lookup_stops_at_the_deadline(monkeypatch):
    import time
    stop = threading.Event()
    looked_up = []

    def resolve(host, port):
        looked_up.append(host)
        if host == "proxy.corp":
            stop.wait(1.2)
            raise socket.gaierror("lookup stalled")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    getaddrinfo = socket.getaddrinfo

    def lookup(host, port, *args, **kwargs):
        if host == "proxy.corp":
            return resolve(host, port)
        return getaddrinfo(host, port, *args, **kwargs)

    _proxy_settings(monkeypatch, "http://proxy.corp:8080")
    monkeypatch.setattr(ai_fetch, "_resolve", resolve)
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    start = time.monotonic()
    try:
        with pytest.raises(ai_fetch.TransportError, match="timed out"):
            ai_fetch.fetch_card_image("https://img.example/a.png", deadline_s=0.3)
        assert time.monotonic() - start < 1.0
        assert looked_up == ["img.example", "proxy.corp"]
    finally:
        stop.set()


def test_a_port_specific_proxy_bypass_only_matches_that_port(monkeypatch):
    monkeypatch.setenv("no_proxy", "img.example:8443")
    monkeypatch.setenv("https_proxy", "http://10.0.0.2:8080")
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
    monkeypatch.setattr(urllib.request, "proxy_bypass",
                        urllib.request.proxy_bypass_environment)
    web = _ProxyWeb(monkeypatch, {"img.example": ["93.184.216.34"]},
                    {("img.example", "/a.png"): _png()})
    assert ai_fetch.fetch_card_image("https://img.example:8443/a.png") == (PNG, "png")
    assert web.connected == [("img.example", "93.184.216.34")]
    assert web.tunnels == []
    web.pages[("img.example", "/a.png")] = _png()
    assert ai_fetch.fetch_card_image("https://img.example/a.png") == (PNG, "png")
    assert web.connected == [("img.example", "93.184.216.34")]
    assert len(web.tunnels) == 1
    assert (web.tunnels[0].host, web.tunnels[0].port) == ("img.example", 443)


@pytest.mark.parametrize("lookup", ["getproxies", "proxy_bypass"])
def test_proxy_discovery_stops_at_the_deadline(monkeypatch, lookup):
    import time
    _proxy_settings(monkeypatch, "http://10.0.0.2:8080")
    web = _ProxyWeb(monkeypatch, {"img.example": ["93.184.216.34"]},
                    {("img.example", "/a.png"): _png()})
    stop = threading.Event()
    original = getattr(urllib.request, lookup)

    def blocked(*args):
        stop.wait(0.7)
        return original(*args)

    monkeypatch.setattr(urllib.request, lookup, blocked)
    start = time.monotonic()
    try:
        with pytest.raises(ai_fetch.TransportError, match="timed out"):
            ai_fetch.fetch_card_image("https://img.example/a.png", deadline_s=0.1)
        assert time.monotonic() - start < 0.4
        assert web.connected == []
        assert web.tunnels == []
    finally:
        stop.set()


@pytest.mark.parametrize("authority", ["img.example", "img.example:443",
                                       "img.example:8443"])
def test_a_macos_hostname_exception_bypasses_explicit_ports(monkeypatch, authority):
    _proxy_settings(monkeypatch, "http://10.0.0.2:8080")
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host:
                        urllib.request._proxy_bypass_macosx_sysconf(
                            host, {"exclude_simple": False, "exceptions": ["img.example"]}))
    web = _ProxyWeb(monkeypatch, {"img.example": ["93.184.216.34"]},
                    {("img.example", "/a.png"): _png()})
    assert ai_fetch.fetch_card_image(f"https://{authority}/a.png") == (PNG, "png")
    assert web.connected == [("img.example", "93.184.216.34")]
    assert web.tunnels == []


@pytest.mark.parametrize("authority", ["img.example", "img.example:443",
                                       "img.example:8443"])
def test_an_environment_hostname_exception_bypasses_explicit_ports(monkeypatch, authority):
    monkeypatch.setenv("no_proxy", "img.example")
    monkeypatch.setenv("https_proxy", "http://10.0.0.2:8080")
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
    monkeypatch.setattr(urllib.request, "proxy_bypass",
                        urllib.request.proxy_bypass_environment)
    web = _ProxyWeb(monkeypatch, {"img.example": ["93.184.216.34"]},
                    {("img.example", "/a.png"): _png()})
    assert ai_fetch.fetch_card_image(f"https://{authority}/a.png") == (PNG, "png")
    assert web.connected == [("img.example", "93.184.216.34")]
    assert web.tunnels == []


@pytest.mark.parametrize("entry", ["[2606:4700::1111]", "2606:4700::1111"])
@pytest.mark.parametrize("port", [None, 443, 8443])
@pytest.mark.parametrize("host, bypass", [
    ("2606:4700::1111", True), ("2606:4700::1111:443", False),
])
def test_an_ipv6_proxy_exception_matches_only_the_exact_address(
        monkeypatch, entry, port, host, bypass):
    monkeypatch.setenv("no_proxy", entry)
    monkeypatch.setenv("https_proxy", "http://10.0.0.2:8080")
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
    monkeypatch.setattr(urllib.request, "proxy_bypass",
                        urllib.request.proxy_bypass_environment)
    proxy = ai_fetch._proxy_for(host, port)
    assert (proxy is None) == bypass
    if proxy is not None:
        assert proxy.geturl() == "http://10.0.0.2:8080"


@pytest.mark.parametrize("host, entry, bypass", [
    ("2606:4700::1111", "2606:4700:0:0::1111", True),
    ("2606:4700:0:0::1111", "2606:4700::1111", True),
    ("2606:4700::1111", " [2606:4700:0:0::1111] ", True),
    ("2606:4700:0:0::1111", " [2606:4700::1111] ", True),
    ("2606:4700::1111", "2606:4700:0:0::1112", False),
    ("2606:4700::1111", "img.example", False),
    ("2606:4700::1111", "img.example,2606:4700:0:0::1111", True),
])
def test_an_ipv6_proxy_exception_compares_addresses(monkeypatch, host, entry, bypass):
    monkeypatch.setenv("no_proxy", entry)
    monkeypatch.setenv("https_proxy", "http://10.0.0.2:8080")
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
    monkeypatch.setattr(urllib.request, "proxy_bypass",
                        urllib.request.proxy_bypass_environment)
    assert (ai_fetch._proxy_for(host, 443) is None) == bypass


@pytest.mark.parametrize("entry, port, bypass", [
    ("[2606:4700::1111]:8443", 8443, True),
    ("[2606:4700::1111]:8443", 443, False),
    ("::1111", 8443, False),
    ("2606:4700::111", 8443, False),
])
def test_an_ipv6_proxy_exception_respects_ports_and_rejects_suffixes(
        monkeypatch, entry, port, bypass):
    monkeypatch.setenv("no_proxy", entry)
    monkeypatch.setenv("https_proxy", "http://10.0.0.2:8080")
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
    monkeypatch.setattr(urllib.request, "proxy_bypass",
                        urllib.request.proxy_bypass_environment)
    assert (ai_fetch._proxy_for("2606:4700::1111", port) is None) == bypass


@pytest.fixture
def connect_reply(monkeypatch):
    import ssl

    class _Socket:
        def __init__(self, reply):
            self.reply = reply
            self.handshake = False

        def settimeout(self, timeout):
            pass

        def connect(self, address):
            pass

        def sendall(self, request):
            pass

        def recv(self, size):
            chunk, self.reply = self.reply[:size], self.reply[size:]
            return chunk

        def do_handshake(self):
            self.handshake = True

        def close(self):
            pass

    def connection(reply):
        sock = _Socket(reply)
        monkeypatch.setattr(ai_fetch.socket, "socket", lambda *args: sock)
        monkeypatch.setattr(ssl.SSLContext, "wrap_socket", lambda *args, **kwargs: sock)
        conn = ai_fetch._ProxyHTTPSConnection(
            "proxy.corp", 8080, timeout=1, context=ssl.create_default_context(),
            address=(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8080)),
            on_socket=lambda sock: None)
        conn.set_tunnel("img.example", 443)
        return conn, sock

    return connection


@pytest.mark.parametrize("interim", [b"", b"HTTP/1.1 100 Continue\r\n\r\n",
                                    b"HTTP/1.1 103 Early Hints\r\n\r\n",
                                    b"HTTP/1.1 100 Continue\r\n\r\n"
                                    b"HTTP/1.1 103 Early Hints\r\n\r\n"])
@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_a_connect_accepts_any_2xx_after_interim_replies(connect_reply, interim, status):
    conn, sock = connect_reply(interim + f"HTTP/1.1 {status} OK\r\n\r\n".encode()
                               + b"tunnel bytes")
    conn.connect()
    assert sock.handshake
    assert sock.recv(100) == b"tunnel bytes"


@pytest.mark.parametrize("status", [300, 407])
def test_a_connect_rejects_a_final_failure_after_interim_replies(connect_reply, status):
    conn, sock = connect_reply(b"HTTP/1.1 103 Early Hints\r\n\r\n"
                               + f"HTTP/1.1 {status} Failed\r\n\r\n".encode())
    with pytest.raises(OSError, match=f"Tunnel connection failed: {status}"):
        conn.connect()
    assert not sock.handshake


def test_a_connect_requires_a_final_reply_after_interim_replies(connect_reply):
    conn, sock = connect_reply(b"HTTP/1.1 103 Early Hints\r\n\r\n")
    with pytest.raises(OSError, match="Incomplete CONNECT reply"):
        conn.connect()
    assert not sock.handshake


@pytest.mark.parametrize("reply", [
    (b"HTTP/1.1 103 Early Hints\r\n" + b"X: " + b"x" * 33000 + b"\r\n\r\n") * 2,
    (b"HTTP/1.1 103 Early Hints\r\n" + b"X: x\r\n" * 49 + b"\r\n") * 2,
], ids=["total-size", "line-count"])
def test_connect_reply_caps_are_shared_by_all_blocks(connect_reply, reply):
    conn, sock = connect_reply(reply + b"HTTP/1.1 200 OK\r\n\r\n")
    with pytest.raises(OSError, match="CONNECT reply is too large"):
        conn.connect()
    assert not sock.handshake


@pytest.mark.parametrize("headers", [b"X: first\r\n second\r\n\tthird\r\n",
                                    b"unused line\r\n"])
def test_connect_reply_headers_are_ignored(connect_reply, headers):
    conn, sock = connect_reply(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n"
                               + b"tunnel bytes")
    conn.connect()
    assert sock.handshake
    assert sock.recv(100) == b"tunnel bytes"


@pytest.mark.parametrize("reply", [
    b"HTTP/1.1 200 Connection Established\r\n\r\n\x15\x03\x03\x00\x02\x02\x28",
    b"HTTP/1.1 200 OK\r\n" + (b"X: " + b"x" * 4096 + b"\r\n") * 17 + b"\r\n",
    b"HTTP/1.1 200 OK\r\n" + b"X: x\r\n" * 101 + b"\r\n",
    b"HTTP/2 200 OK\r\n\r\n",
    b"HTTP/1.1 20 OK\r\n\r\n",
    b"HTTP/1.1 secret OK\r\n\r\n",
    b"HTTP/1.1 099 secret\r\n\r\n",
], ids=["tls-alert", "total-size", "line-count", "invalid-version", "short-status",
        "invalid-status", "invalid-status-range"])
def test_a_connect_reply_fails_fast_without_losing_tunnel_bytes(monkeypatch, reply):
    import time
    import traceback
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(2)
    port = server.getsockname()[1]
    stop = threading.Event()
    requests = []

    def serve():
        try:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    request += chunk
                requests.append(request)
                conn.sendall(reply)
                stop.wait(3)
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _proxy_settings(monkeypatch, f"http://user:secret@127.0.0.1:{port}")
    monkeypatch.setattr(ai_fetch, "_resolve", lambda host, p: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "",
         ("93.184.216.34" if host == "img.example" else "127.0.0.1", p))])
    start = time.monotonic()
    try:
        with pytest.raises(ai_fetch.TransportError) as raised:
            ai_fetch.fetch_card_image("https://img.example/a.png", deadline_s=2)
        assert time.monotonic() - start < 1
        assert "timed out" not in str(raised.value)
        message = "".join(traceback.format_exception(
            type(raised.value), raised.value, raised.value.__traceback__))
        assert "secret" not in message
        assert requests[0].startswith(b"CONNECT img.example:443 HTTP/1.")
    finally:
        stop.set()
        server.close()
        thread.join(3)
    assert not thread.is_alive()
