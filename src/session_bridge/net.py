"""HTTPS requests and downloads: named hosts only, bounded sizes, and a digest checked before use.

Standard library only, because the WSL bootstrap loads this module from a bare wheel.
A host list says where a request may go; the SHA-256 digest is what makes a download trustworthy.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


class NetError(Exception):
    """A request failed or broke a rule; ``reason`` is a short code and ``status`` the HTTP status, if any."""

    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class Hosts:
    """HTTPS hosts a request may start at, and host suffixes a redirect may lead to."""

    start: frozenset[str]
    redirects: tuple[str, ...] = ()

    def allows(self, url: str, *, redirected: bool = False) -> bool:
        try:
            parts = urllib.parse.urlsplit(url)
            host = (parts.hostname or "").lower()
            port = parts.port
        except ValueError:
            return False
        if parts.scheme != "https" or port not in (None, 443) or parts.username or parts.password:
            return False
        if host in self.start:
            return True
        return redirected and any(host.endswith(suffix) for suffix in self.redirects)


@dataclass(frozen=True)
class Reply:
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


_CHUNK = 64 * 1024


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: Hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.hosts.allows(newurl, redirected=True):
            raise NetError("host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(request: urllib.request.Request, timeout: float, hosts: Hosts):
    """The one place that touches the network; tests replace it."""
    opener = urllib.request.build_opener(_Redirects(hosts))
    return opener.open(request, timeout=timeout)


def _request(url: str, hosts: Hosts, headers: dict[str, str] | None, user_agent: str) -> urllib.request.Request:
    if not hosts.allows(url):
        raise NetError("host")
    return urllib.request.Request(url, headers={"User-Agent": user_agent, **(headers or {})})


def _final_url(response, url: str, hosts: Hosts) -> str:
    final = getattr(response, "url", None) or url
    if final != url and not hosts.allows(final, redirected=True):
        raise NetError("host")
    return final


def _size(response) -> int | None:
    value = response.headers.get("Content-Length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise NetError("size") from None


def fetch(
    url: str,
    *,
    hosts: Hosts,
    max_bytes: int,
    timeout: float,
    headers: dict[str, str] | None = None,
    user_agent: str = "session-bridge",
) -> Reply:
    """A small response held in memory. A 304 answer is returned, not raised; other errors raise."""
    request = _request(url, hosts, headers, user_agent)
    try:
        with _open(request, timeout, hosts) as response:
            final = _final_url(response, url, hosts)
            declared = _size(response)
            if declared is not None and declared > max_bytes:
                raise NetError("size")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise NetError("size")
            names = {name.lower(): value for name, value in response.headers.items()}
            return Reply(getattr(response, "status", 200), final, names, body)
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return Reply(304, url, {name.lower(): value for name, value in error.headers.items()})
        raise NetError("status", error.code) from None
    except NetError:
        raise
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        raise NetError("network") from None


def download(
    url: str,
    directory: Path,
    *,
    hosts: Hosts,
    max_bytes: int,
    timeout: float,
    sha256: str,
    size: int | None = None,
    user_agent: str = "session-bridge",
) -> Path:
    """Download into a new file in ``directory``; it is returned only when its size and SHA-256 match.

    The caller owns the returned file and moves or deletes it. Nothing is left behind on failure.
    """
    request = _request(url, hosts, None, user_agent)
    temporary: Path | None = None
    try:
        with _open(request, timeout, hosts) as response:
            _final_url(response, url, hosts)
            declared = _size(response)
            if declared is not None and (declared > max_bytes or (size is not None and declared != size)):
                raise NetError("size")
            fd, raw = tempfile.mkstemp(prefix="download-", suffix=".tmp", dir=directory)
            temporary = Path(raw)
            digest = hashlib.sha256()
            received = 0
            with os.fdopen(fd, "wb") as output:
                while block := response.read(_CHUNK):
                    received += len(block)
                    if received > max_bytes or (size is not None and received > size):
                        raise NetError("size")
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
        if size is not None and received != size:
            raise NetError("size")
        if digest.hexdigest() != sha256.lower():
            raise NetError("digest")
        result, temporary = temporary, None
        return result
    except urllib.error.HTTPError as error:
        raise NetError("status", error.code) from None
    except NetError:
        raise
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        raise NetError("network") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
