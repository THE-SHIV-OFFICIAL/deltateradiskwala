"""Safe, real media resolver adapters.

TeraBox and DiskWalla change their private endpoints often. This bot does not
pretend that a made-up URL is a successful download. Configure a resolver
service endpoint in .env; it must return JSON containing a direct media URL.
Direct public media URLs are supported as a useful fallback.
"""

from __future__ import annotations

import ipaddress
import mimetypes
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp


class ExtractionError(Exception):
    pass


@dataclass(frozen=True)
class MediaResult:
    source: str
    title: str
    direct_url: str
    mime: str
    size: int | None = None


def _host(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ExtractionError("Please send a valid http(s) link.")
    return parsed.hostname.lower()


def _is_private_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        try:
            return any(
                ipaddress.ip_address(info[4][0]).is_private
                for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            )
        except OSError:
            return False


def _source(url: str) -> str | None:
    host = _host(url)
    if "terabox" in host or "1024tera" in host:
        return "terabox"
    if "diskwalla" in host:
        return "diskwalla"
    return None


class MediaResolver:
    def __init__(self, settings):
        self.settings = settings
        self.timeout = aiohttp.ClientTimeout(total=90, connect=15, sock_read=75)

    async def resolve(self, url: str) -> MediaResult:
        if len(url) > self.settings.max_url_length:
            raise ExtractionError("That URL is too long.")
        source = _source(url)
        if source:
            endpoint = self.settings.terabox_api_url if source == "terabox" else self.settings.diskwalla_api_url
            api_key = self.settings.terabox_api_key if source == "terabox" else self.settings.diskwalla_api_key
            cookie = self.settings.terabox_cookie if source == "terabox" else self.settings.diskwalla_cookie
            if not endpoint:
                label = "TeraBox" if source == "terabox" else "DiskWalla"
                raise ExtractionError(f"{label} resolver is not configured by the bot owner yet.")
            return await self._resolver_request(endpoint, api_key, cookie, url, source)

        host = _host(url)
        if _is_private_host(host):
            raise ExtractionError("Private/local network URLs are not allowed.")
        mime = mimetypes.guess_type(urlparse(url).path)[0] or "application/octet-stream"
        if not (mime.startswith(("video/", "audio/", "image/", "application/pdf"))):
            raise ExtractionError("Only TeraBox, DiskWalla, or direct media/PDF links are supported.")
        name = urlparse(url).path.rsplit("/", 1)[-1] or "download"
        return MediaResult("direct", name[:180], url, mime)

    async def _resolver_request(
        self, endpoint: str, api_key: str, cookie: str, url: str, source: str
    ) -> MediaResult:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
        if cookie:
            headers["Cookie"] = cookie
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(endpoint, json={"url": url}, headers=headers) as response:
                    if response.status >= 400:
                        raise ExtractionError(f"Resolver returned HTTP {response.status}.")
                    payload = await response.json(content_type=None)
        except ExtractionError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ExtractionError("Resolver timed out or could not be reached.") from exc
        direct = payload.get("direct_url") or payload.get("download_url") or payload.get("url")
        if not isinstance(direct, str) or not direct.startswith(("http://", "https://")):
            raise ExtractionError("Resolver response did not contain a direct media URL.")
        if _is_private_host(_host(direct)):
            raise ExtractionError("Resolver returned a private URL; download blocked.")
        mime = str(payload.get("mime") or payload.get("content_type") or "application/octet-stream")
        title = str(payload.get("title") or payload.get("name") or f"{source}_media")
        size = payload.get("size")
        return MediaResult(source, title[:180], direct, mime, int(size) if str(size).isdigit() else None)
