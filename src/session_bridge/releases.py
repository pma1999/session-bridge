"""The latest Session Bridge release published on GitHub, read through the public API."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import net
from .errors import BridgeError

REPOSITORY = "pma1999/session-bridge"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
PAGE_PREFIX = f"https://github.com/{REPOSITORY}/releases/"
# Release files are served from github.com and then redirected to GitHub's file host.
HOSTS = net.Hosts(frozenset({"api.github.com", "github.com"}), (".githubusercontent.com",))
TIMEOUT = 5.0
MAX_METADATA = 1024 * 1024
_VERSION = re.compile(r"v?(\d{1,4})\.(\d{1,4})\.(\d{1,6})")
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text) -> Version | None:
        """``1.2.3`` or ``v1.2.3``; anything else, such as a pre-release, is not a version here."""
        match = _VERSION.fullmatch(text) if isinstance(text, str) else None
        return cls(*map(int, match.groups())) if match else None

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def tag(self) -> str:
        return f"v{self}"


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str | None = None


@dataclass(frozen=True)
class Release:
    version: Version
    page_url: str
    assets: dict[str, Asset] = field(default_factory=dict)

    @property
    def portable(self) -> str:
        return f"session-bridge-{self.version}-portable.zip"

    def as_dict(self) -> dict:
        assets = [
            {"name": asset.name, "download_url": asset.url, "size": asset.size, "sha256": asset.sha256}
            for asset in self.assets.values()
        ]
        return {"version": str(self.version), "page_url": self.page_url, "assets": assets}

    @classmethod
    def from_dict(cls, value) -> Release | None:
        """A release read back from the update state; anything malformed reads as no release."""
        if not isinstance(value, dict):
            return None
        version = Version.parse(value.get("version"))
        page = value.get("page_url")
        if version is None or not _page(page, version):
            return None
        assets = {}
        for item in value.get("assets") or []:
            if isinstance(item, dict):
                asset = _asset(version, item.get("name"), item.get("download_url"), item.get("size"), item.get("sha256"))
                if asset is not None:
                    assets[asset.name] = asset
        return cls(version, page, assets)


@dataclass(frozen=True)
class Lookup:
    """One answer from GitHub: a release, or None when nothing changed since ``etag``."""

    release: Release | None
    etag: str | None
    changed: bool = True


def _page(url, version: Version) -> bool:
    return isinstance(url, str) and url == f"{PAGE_PREFIX}tag/{version.tag}"


def _asset(version: Version, name, url, size, sha256) -> Asset | None:
    """A release file, only if it is downloaded from this release's own address."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name):
        return None
    if url != f"{PAGE_PREFIX}download/{version.tag}/{name}":
        return None
    if type(size) is not int or size < 0:
        return None
    if not (isinstance(sha256, str) and re.fullmatch(r"[0-9a-f]{64}", sha256)):
        sha256 = None
    return Asset(name, url, size, sha256)


def parse(payload: dict) -> Release | None:
    """The GitHub API's release object, keeping only a final ``vX.Y.Z`` release of this repository."""
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    version = Version.parse(payload.get("tag_name"))
    if version is None or not _page(payload.get("html_url"), version):
        return None
    assets = {}
    for item in payload.get("assets") or []:
        if isinstance(item, dict):
            # "url" is the API's address for the asset; "browser_download_url" is the file itself.
            digest = _DIGEST.fullmatch(item["digest"]) if isinstance(item.get("digest"), str) else None
            asset = _asset(
                version, item.get("name"), item.get("browser_download_url"), item.get("size"),
                digest.group(1) if digest else None,
            )
            if asset is not None:
                assets[asset.name] = asset
    return Release(version, payload["html_url"], assets)


def latest(etag: str | None = None) -> Lookup:
    """Ask GitHub for the latest release; with ``etag``, an unchanged answer costs no rate limit."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        reply = net.fetch(API_URL, hosts=HOSTS, max_bytes=MAX_METADATA, timeout=TIMEOUT, headers=headers)
    except net.NetError as error:
        if error.status in (403, 429):
            raise BridgeError("update_unavailable", "error.update_unavailable.rate_limited") from None
        if error.status == 404:
            return Lookup(None, None)
        raise BridgeError("update_unavailable", reason=error.reason, status=error.status) from None
    if reply.status == 304:
        return Lookup(None, etag, changed=False)
    try:
        payload = json.loads(reply.body)
    except ValueError:
        raise BridgeError("update_unavailable", reason="invalid_response") from None
    return Lookup(parse(payload), reply.headers.get("etag"))
