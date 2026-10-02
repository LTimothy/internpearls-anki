"""Downloads a card picture the assistant suggested, by address only.

The model chooses the address, so the request is kept to what a picture needs:
https on every hop, and every hop's host resolved and checked before connecting, so
a redirect can neither drop to plain http nor reach this machine or its network.
The connection goes to the address that was checked, not to a second lookup.
"""
import http.client
import ipaddress
import socket
import urllib.parse

from . import ai_logic
from .net import (_DOWNLOAD_TIMEOUT, _IMAGE_TYPES, _USER_AGENT, HttpStatusError,
                  TransportError, wikimedia_image_url)

_MAX_REDIRECTS = 5
_REDIRECTS = (301, 302, 303, 307, 308)
_CHUNK = 64 * 1024


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


def checked_address(host, port):
    """The address to connect to for `host`, or RuntimeError when any address it
    resolves to is private, loopback, link-local or otherwise not public."""
    try:
        infos = _resolve(host, port)
    except OSError as e:
        raise TransportError(f"couldn't look up {host} ({e})") from e
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
    return str(addrs[0])


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An https connection to an address checked beforehand, verified against the
    host name the address was looked up for."""

    def __init__(self, host, ip, port, timeout):
        import ssl
        super().__init__(host, port, timeout=timeout,
                         context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _open_connection(host, ip, port, timeout):
    return _PinnedHTTPSConnection(host, ip, port, timeout)


def _https_parts(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise RuntimeError("image must be served over https")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return parts.hostname, parts.port or 443, target


def _read_capped(r, max_bytes):
    buf = bytearray()
    while True:
        chunk = r.read(_CHUNK)
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > max_bytes:
            raise RuntimeError("image is too large")


def _image_from(r, max_bytes):
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
    data = _read_capped(r, max_bytes)
    try:
        ai_logic.check_image_bytes(f"image.{ext}", data)
    except ValueError as e:
        raise RuntimeError(f"not an image ({e})") from None
    return data, ext


def fetch_card_image(url, max_bytes=5 * 1024 * 1024, timeout=_DOWNLOAD_TIMEOUT):
    """(bytes, extension) for a model-suggested picture. Refuses a non-https address
    or redirect, a host that resolves to a private or local address (checked on every
    hop before connecting), anything not served as a PNG, JPEG, GIF or WebP whose
    bytes match, and more than `max_bytes`. A Wikimedia File: page or original SVG is
    fetched as its rendered image (see net.wikimedia_image_url)."""
    if not url.startswith("https://"):
        raise RuntimeError("image URLs must be https")
    url = wikimedia_image_url(url)
    for _hop in range(_MAX_REDIRECTS + 1):
        host, port, target = _https_parts(url)
        ip = checked_address(host, port)
        conn = _open_connection(host, ip, port, timeout)
        try:
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
            return _image_from(r, max_bytes)
        except (TimeoutError, socket.timeout) as e:
            raise TransportError(
                "the network isn't responding (timed out). Check your internet "
                "connection and try again.") from e
        except (OSError, http.client.HTTPException) as e:
            raise TransportError(f"couldn't download the image ({e})") from e
        finally:
            conn.close()
    raise RuntimeError("image address redirects too many times")
