"""Card-picture downloads: https on every hop, no private or local addresses."""
import socket
import threading

import pytest

from internpearls import ai_fetch

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


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


class _Web:
    """Stands in for DNS and the network: `hosts` maps a name to the addresses it
    resolves to, `pages` maps (host, target) to the response served there."""

    def __init__(self, monkeypatch, hosts, pages):
        self.hosts, self.pages, self.connected = hosts, pages, []
        monkeypatch.setattr(ai_fetch, "_resolve", self.resolve)
        monkeypatch.setattr(ai_fetch, "_open_connection", self.open)

    def resolve(self, host, port):
        if host not in self.hosts:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM,
                 6, "", (a, port)) for a in self.hosts[host]]

    def open(self, host, ip, port, timeout):
        web = self

        class _Conn:
            def request(self, method, target, headers=None):
                web.connected.append((host, ip))
                self.response = web.pages[(host, target)]

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
    with pytest.raises(RuntimeError, match="https"):
        ai_fetch.fetch_card_image("https://img.example/a.png")
    assert web.connected == [("img.example", "93.184.216.34")]


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
        t.join()
        server.close()
    assert contacted == []
