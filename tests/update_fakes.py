"""A fake GitHub for update tests: release metadata, checksums and portable ZIPs, served without a network."""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
import zipfile
from dataclasses import dataclass, field

from session_bridge import net, releases

REPO = f"https://github.com/{releases.REPOSITORY}"


class _Response(io.BytesIO):
    def __init__(self, body: bytes, url: str, headers: dict, status: int = 200):
        super().__init__(body)
        self.url, self.headers, self.status = url, headers, status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@dataclass
class Route:
    body: bytes = b""
    status: int = 200
    headers: dict = field(default_factory=dict)
    final: str | None = None


class FakeWeb:
    """Replaces ``net._open``; every request must be for a known URL and allowed host."""

    def __init__(self):
        self.routes: dict[str, Route] = {}
        self.requests: list = []

    def add(self, url, body=b"", **options) -> None:
        self.routes[url] = Route(body, **options)

    def __call__(self, request, timeout, hosts):
        self.requests.append(request)
        route = self.routes.get(request.full_url)
        if route is None:
            raise urllib.error.URLError("no route")
        final = route.final or request.full_url
        if final != request.full_url and not hosts.allows(final, redirected=True):
            raise net.NetError("host")
        if route.status >= 300:
            raise urllib.error.HTTPError(request.full_url, route.status, "fake", route.headers, None)
        headers = {"Content-Length": str(len(route.body)), **route.headers}
        return _Response(route.body, final, headers, route.status)


def portable_zip(version: str, *, members: dict[str, bytes] | None = None, declared: str | None = None) -> bytes:
    """A portable ZIP like ``build_portable.py`` makes: everything under ``session-bridge/``."""
    files = {
        "session-bridge/pyproject.toml": f'[project]\nname = "session-bridge"\nversion = "{declared or version}"\n'.encode(),
        "session-bridge/scripts/install.ps1": b"# installer\n",
        f"session-bridge/dist/session_bridge-{version}-py3-none-any.whl": b"wheel",
    }
    if members is not None:
        files = members
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def publish(web: FakeWeb, version: str, archive: bytes | None = None, *, sums_for: bytes | None = None,
            digest: str | None = "auto", etag: str = '"v1"') -> dict:
    """Serve ``version`` as the latest release; returns the API payload."""
    archive = portable_zip(version) if archive is None else archive
    name = f"session-bridge-{version}-portable.zip"
    archive_sum = hashlib.sha256(archive if sums_for is None else sums_for).hexdigest()
    sums = f"{archive_sum}  {name}\n{'0' * 64}  session_bridge-{version}-py3-none-any.whl\n".encode()
    base = f"{REPO}/releases/download/v{version}/"
    cdn = "https://release-assets.githubusercontent.com/asset/"
    web.add(base + name, final=cdn + name)
    web.routes[base + name].body = archive
    web.add(base + "SHA256SUMS", sums, final=cdn + "SHA256SUMS")

    def asset(asset_name, body):
        value = hashlib.sha256(body).hexdigest() if digest == "auto" else digest
        return {
            "name": asset_name,
            "size": len(body),
            "url": f"https://api.github.com/repos/{releases.REPOSITORY}/releases/assets/1",
            "browser_download_url": base + asset_name,
            "digest": f"sha256:{value}" if value else None,
        }

    payload = {
        "tag_name": f"v{version}",
        "html_url": f"{REPO}/releases/tag/v{version}",
        "draft": False,
        "prerelease": False,
        "assets": [asset(name, archive), asset("SHA256SUMS", sums)],
    }
    web.add(releases.API_URL, json.dumps(payload).encode(), headers={"ETag": etag})
    return payload
