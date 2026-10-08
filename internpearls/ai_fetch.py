"""Downloads a card picture the assistant suggested, by address only.

The model chooses the address, so the request is kept to what a picture needs:
https on every hop, and every hop's host resolved and checked before connecting, so
a redirect can neither drop to plain http nor reach this machine or its network.
Direct connections go to a checked address. A system https proxy tunnels by host
name after the same local address checks.
"""
import base64
import http.client
import ipaddress
import re
import socket
import threading
import time
import urllib.parse
import urllib.request

from . import ai_logic
from .net import (_DOWNLOAD_TIMEOUT, _IMAGE_TYPES, _USER_AGENT, HttpStatusError,
                  TransportError, wikimedia_image_url)

_MAX_REDIRECTS = 5
_REDIRECTS = (301, 302, 303, 307, 308)
_CHUNK = 64 * 1024
_MAX_ABANDONED_LOOKUPS = 8
_lookup_lock = threading.Lock()
_running_lookups = 0
_abandoned_lookups = 0


def _is_public(ip):
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return _is_public(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return _is_public(ip.sixtofour)
        if ip.teredo is not None:
            return _is_public(ip.teredo[1])
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified
                or not ip.is_global)


def _resolve(host, port):
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _timed_out():
    return TransportError("the network isn't responding (timed out). Check your "
                          "internet connection and try again.")


def _run_within(call, seconds):
    """Give up after `seconds`, leaving the callable to finish on its own thread."""
    global _running_lookups, _abandoned_lookups
    box = {"finished": False, "abandoned": False}

    def run():
        global _running_lookups, _abandoned_lookups
        try:
            box["result"] = call()
        except Exception as e:
            box["error"] = e
        finally:
            with _lookup_lock:
                box["finished"] = True
                if box["abandoned"]:
                    _abandoned_lookups -= 1
                else:
                    _running_lookups -= 1
    with _lookup_lock:
        # Reserve a slot so concurrent timeouts cannot exceed the cap.
        if _running_lookups + _abandoned_lookups >= _MAX_ABANDONED_LOOKUPS:
            raise _timed_out()
        _running_lookups += 1
        try:
            t = threading.Thread(target=run, daemon=True)
            t.start()
        except Exception:
            _running_lookups -= 1
            raise
    t.join(max(0.0, seconds))
    with _lookup_lock:
        if not box["finished"]:
            box["abandoned"] = True
            _running_lookups -= 1
            _abandoned_lookups += 1
            raise _timed_out()
    if "error" in box:
        raise box["error"]
    return box["result"]


def _resolve_within(host, port, seconds):
    """_resolve, given up on after `seconds` (the lookup itself cannot be cancelled,
    so it is left to finish on its own thread)."""
    try:
        return _run_within(lambda: _resolve(host, port), seconds)
    except (OSError, UnicodeError) as e:
        raise TransportError(f"couldn't look up {host} ({e})") from e


def checked_addresses(host, port, seconds=_DOWNLOAD_TIMEOUT):
    """Every address `host` resolves to, in order, or RuntimeError when any of them is
    private, loopback, link-local or otherwise not public."""
    infos = _resolve_within(host, port, seconds)
    addrs = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            addrs.append(ipaddress.ip_address(raw))
        except ValueError:
            raise RuntimeError(f"{host} did not resolve to a usable address") from None
    if not addrs:
        raise RuntimeError(f"{host} did not resolve to any address")
    if not all(_is_public(ip) for ip in addrs):
        raise RuntimeError("image address points to a private or local network")
    return list(dict.fromkeys(str(ip) for ip in addrs))


# Absent in a Python built without ssl, such as the browser demo's.
_HTTPSConnection = getattr(http.client, "HTTPSConnection", None)

if _HTTPSConnection is not None:
    class _PinnedHTTPSConnection(_HTTPSConnection):
        """An https connection to an address checked beforehand, verified against the
        host name the address was looked up for."""

        def __init__(self, host, ip, port, timeout, on_socket=None, deadline=None):
            import ssl
            super().__init__(host, port, timeout=timeout,
                             context=ssl.create_default_context())
            self._ip = ip
            self._on_socket, self._deadline = on_socket, deadline

        def connect(self):
            family = socket.AF_INET6 if ":" in self._ip else socket.AF_INET
            self.sock = socket.socket(family, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            if self._on_socket is not None:
                self._on_socket(self.sock)
            self.sock.connect((self._ip, self.port))
            self.sock = self._context.wrap_socket(
                self.sock, server_hostname=self.host, do_handshake_on_connect=False)
            if self._on_socket is not None:
                self._on_socket(self.sock)
            if self._deadline is not None:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout()
                self.sock.settimeout(min(self.timeout, remaining))
            self.sock.do_handshake()


    class _ProxyHTTPSConnection(_HTTPSConnection):
        def __init__(self, host, port, timeout, context, address, on_socket):
            super().__init__(host, port, timeout=timeout, context=context)
            self._address, self._on_socket = address, on_socket

        def connect(self):
            family, kind, proto, _, address = self._address
            self.sock = socket.socket(family, kind, proto)
            self.sock.settimeout(self.timeout)
            self._on_socket(self.sock)
            self.sock.connect(address)
            host = self._tunnel_host.encode("idna").decode("ascii")
            authority = f"[{host}]:{self._tunnel_port}" if ":" in host else (
                f"{host}:{self._tunnel_port}")
            headers = {**self._tunnel_headers, "Host": authority}
            request = [f"CONNECT {authority} HTTP/1.1\r\n"]
            request.extend(f"{key}: {value}\r\n" for key, value in headers.items())
            self.sock.sendall(("".join(request) + "\r\n").encode("latin-1"))
            # Leave bytes after the blank line on the socket for TLS.
            size = lines = 0
            while True:
                reply = bytearray()
                while not reply.endswith(b"\r\n\r\n"):
                    byte = self.sock.recv(1)
                    if not byte:
                        raise OSError("Incomplete CONNECT reply")
                    reply += byte
                    size += 1
                    lines += byte == b"\n"
                    if size > 64 * 1024 or lines > 100:
                        raise OSError("CONNECT reply is too large")
                status = re.fullmatch(rb"HTTP/1\.[0-9] +([0-9]{3})(?: +[^\r\n]*)?",
                                      reply.split(b"\r\n", 1)[0])
                if status is None or int(status[1]) < 100:
                    raise OSError("Malformed CONNECT reply")
                code = int(status[1])
                if code >= 200:
                    break
            if not 200 <= code < 300:
                raise OSError(f"Tunnel connection failed: {code}")
            self.sock = self._context.wrap_socket(
                self.sock, server_hostname=self._tunnel_host,
                do_handshake_on_connect=False)
            self._on_socket(self.sock)
            self.sock.do_handshake()


def _open_connection(host, ip, port, timeout, on_socket=None, deadline=None):
    if _HTTPSConnection is None:
        raise TransportError("https isn't available here")
    return _PinnedHTTPSConnection(host, ip, port, timeout, on_socket, deadline)


def _proxy_for(host, port=None):
    proxies = urllib.request.getproxies()
    url = proxies.get("https")
    try:
        ipv6 = ipaddress.ip_address(host).version == 6
    except ValueError:
        ipv6 = False
    if ipv6:
        for name in proxies.get("no", "").split(","):
            try:
                if (ipaddress.ip_address(name.strip().strip("[]")) ==
                        ipaddress.ip_address(host)):
                    return None
            except ValueError:
                continue
        host = f"[{host}]"
    authority = host if port is None else f"{host}:{port}"
    if not url or urllib.request.proxy_bypass(host) or (
            port is not None and urllib.request.proxy_bypass(authority)):
        return None
    if "://" not in url:
        url = "http://" + url
    try:
        return urllib.parse.urlsplit(url)
    except ValueError:
        raise TransportError("invalid https proxy address") from None


def _proxy_address(proxy):
    if _HTTPSConnection is None:
        raise TransportError("https isn't available here")
    if proxy.scheme != "http":
        raise TransportError(f"unsupported proxy scheme: {proxy.scheme or 'unspecified'}")
    try:
        proxy_host, proxy_port = proxy.hostname, proxy.port or 80
    except ValueError:
        raise TransportError("invalid https proxy address") from None
    if not proxy_host:
        raise TransportError("invalid https proxy address")
    return proxy_host, proxy_port


def _open_proxy_connection(host, proxy, port, timeout, address, on_socket):
    proxy_host, proxy_port = _proxy_address(proxy)
    import ssl
    conn = _ProxyHTTPSConnection(proxy_host, proxy_port, timeout=timeout,
                                 context=ssl.create_default_context(),
                                 address=address, on_socket=on_socket)
    headers = {}
    if proxy.username is not None:
        user = urllib.parse.unquote(proxy.username)
        password = urllib.parse.unquote(proxy.password or "")
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        headers["Proxy-Authorization"] = f"Basic {token}"
    conn.set_tunnel(host, port, headers=headers)
    return conn


def _https_parts(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise RuntimeError("image must be served over https")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return parts.hostname, parts.port or 443, target


def _read_capped(r, max_bytes, deadline=None):
    """The body, at most `max_bytes`, read in whatever pieces arrive (read1 returns a
    short read rather than waiting to fill a chunk) with the deadline checked after
    each one."""
    read = getattr(r, "read1", None) or r.read
    buf = bytearray()
    while True:
        chunk = read(_CHUNK)
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > max_bytes:
            raise RuntimeError("image is too large")
        if deadline is not None and time.monotonic() > deadline:
            raise _timed_out()


def _image_from(r, max_bytes, deadline=None):
    ctype = (r.getheader("Content-Type") or "").split(";")[0].strip().lower()
    if ctype not in _IMAGE_TYPES:
        raise RuntimeError(f"not an image ({ctype or 'no content type'})")
    ext = _IMAGE_TYPES[ctype]
    clen = r.getheader("Content-Length")
    if clen:
        try:
            declared = int(clen)
        except ValueError:
            declared = None
        if declared is not None and declared > max_bytes:
            raise RuntimeError("image is too large")
    data = _read_capped(r, max_bytes, deadline)
    try:
        ai_logic.check_image_bytes(f"image.{ext}", data)
    except ValueError as e:
        raise RuntimeError(f"not an image ({e})") from None
    return data, ext


def _connect_any(host, ips, port, timeout, deadline, on_socket=None):
    """A connection to the first of `ips` that accepts one, each attempt given
    whatever time is left before `deadline`."""
    last = None
    for ip in ips:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _timed_out()
        conn = _open_connection(host, ip, port, min(timeout, remaining),
                                on_socket, deadline)
        try:
            conn.connect()
            if time.monotonic() >= deadline:
                conn.close()
                raise _timed_out()
            return conn
        except (OSError, ValueError) as e:
            conn.close()
            if time.monotonic() >= deadline:
                raise _timed_out() from None
            if isinstance(e, ValueError):
                raise
            last = e
    raise TransportError(f"couldn't reach {host} ({last})") from last


def _connect_proxy(host, proxy, port, timeout, deadline, on_socket):
    proxy_host, proxy_port = _proxy_address(proxy)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _timed_out()
    try:
        addresses = _resolve_within(proxy_host, proxy_port, remaining)
    except TransportError:
        if time.monotonic() >= deadline:
            raise _timed_out() from None
        raise TransportError("couldn't look up the proxy") from None
    refused, timed_out = False, False
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _timed_out()
        conn = _open_proxy_connection(host, proxy, port, min(timeout, remaining),
                                      address, on_socket)
        try:
            conn.connect()
            if time.monotonic() >= deadline:
                conn.close()
                raise _timed_out()
            return conn
        except (OSError, http.client.HTTPException, ValueError) as e:
            conn.close()
            if time.monotonic() >= deadline:
                raise _timed_out() from None
            # A proxy's response text can echo its credentials.
            refused = str(e).startswith("Tunnel connection failed:")
            timed_out = isinstance(e, (TimeoutError, socket.timeout))
    if timed_out:
        raise _timed_out() from None
    if refused:
        raise TransportError("the proxy refused the connection") from None
    raise TransportError("couldn't connect through the proxy") from None


def fetch_card_image(url, max_bytes=5 * 1024 * 1024, timeout=_DOWNLOAD_TIMEOUT,
                     deadline_s=_DOWNLOAD_TIMEOUT):
    """(bytes, extension) for a model-suggested picture. Refuses a non-https address
    or redirect, a host that resolves to a private or local address (checked on every
    hop before connecting), anything not served as a PNG, JPEG, GIF or WebP whose
    bytes match, and more than `max_bytes`. The whole fetch, every lookup and hop
    included, stops after `deadline_s`: a slow lookup is abandoned and a trickling
    connection is shut down. A Wikimedia File: page or original SVG is fetched as its
    rendered image (see net.wikimedia_image_url)."""
    if not url.startswith("https://"):
        raise RuntimeError("image must be served over https")
    url = wikimedia_image_url(url)
    deadline = time.monotonic() + deadline_s
    for _hop in range(_MAX_REDIRECTS + 1):
        host, port, target = _https_parts(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _timed_out()
        ips = checked_addresses(host, port, remaining)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _timed_out()
        proxy = _run_within(lambda: _proxy_for(host, urllib.parse.urlsplit(url).port),
                            remaining)
        conn = None
        raw = []
        expired = threading.Event()

        def expire():
            # The socket captured at connect: http.client drops conn.sock once a
            # close-delimited response starts, but the reader still holds this one.
            expired.set()
            for sock in raw:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except (OSError, ValueError):
                    pass
        watchdog = threading.Timer(max(0.0, deadline - time.monotonic()), expire)
        watchdog.daemon = True
        watchdog.start()

        def capture(sock):
            raw.append(sock)
            if expired.is_set():
                expire()

        try:
            if proxy is None:
                conn = _connect_any(host, ips, port, timeout, deadline, capture)
            else:
                conn = _connect_proxy(host, proxy, port, timeout, deadline, capture)
            if getattr(conn, "sock", None) is not None:
                raw.append(conn.sock)
            if expired.is_set():
                expire()
            conn.request("GET", target, headers={"User-Agent": _USER_AGENT,
                                                 "Accept": "image/*"})
            r = conn.getresponse()
            if r.status in _REDIRECTS:
                location = r.getheader("Location")
                if not location:
                    raise RuntimeError("image redirect gave no address")
                url = urllib.parse.urljoin(url, location)
                continue
            if r.status == 404:
                raise HttpStatusError(
                    "no image at that address (the site returned 404)", 404)
            if r.status != 200:
                raise HttpStatusError(f"server returned HTTP {r.status}", r.status)
            try:
                result = _image_from(r, max_bytes, deadline)
            except RuntimeError:
                if expired.is_set():
                    raise _timed_out() from None
                raise
            if expired.is_set():
                raise _timed_out()
            return result
        except (TimeoutError, socket.timeout) as e:
            raise _timed_out() from e
        except (OSError, http.client.HTTPException) as e:
            if expired.is_set():
                raise _timed_out() from e
            raise TransportError(f"couldn't download the image ({e})") from e
        except ValueError as e:
            # A read on a socket the deadline already shut down.
            if expired.is_set():
                raise _timed_out() from e
            raise
        finally:
            watchdog.cancel()
            if conn is not None:
                conn.close()
    raise RuntimeError("image address redirects too many times")
