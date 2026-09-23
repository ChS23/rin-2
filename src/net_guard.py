"""Защита от SSRF для инструментов, в которые модель передаёт произвольный URL.

Бот живёт в docker-сети рядом с другими сервисами (valkey, web, gateway-net),
а скачанное можно тут же отправить в чат — значит «скачай http://<внутренний>/...»
от любого участника чата был бы каналом утечки. Поэтому:
  - только http(s);
  - IP-литерал в URL обязан быть публичным;
  - имена резолвятся через резолвер, который отбрасывает приватные/loopback/
    link-local адреса — соединение идёт ровно на проверенный IP (без DNS-rebinding);
  - редиректы проходим вручную и проверяем каждый хоп заново.
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver

MAX_REDIRECTS = 5
_REDIRECT_CODES = {301, 302, 303, 307, 308}


class BlockedURL(Exception):
    pass


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


class _PublicOnlyResolver(AbstractResolver):
    def __init__(self):
        self._inner = DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET):
        infos = await self._inner.resolve(host, port, family)
        public = [i for i in infos if _is_public(i["host"])]
        if not public:
            raise OSError(f"{host} ведёт во внутреннюю сеть")
        return public

    async def close(self) -> None:
        await self._inner.close()


def check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL(f"схема {parts.scheme!r} не разрешена")
    host = parts.hostname
    if not host:
        raise BlockedURL("в ссылке нет хоста")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return  # имя — его проверит резолвер при соединении
    if not _is_public(host):
        raise BlockedURL(f"{host} — внутренний адрес")


def safe_session(**kwargs) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=_PublicOnlyResolver()), **kwargs)


async def safe_get(session: aiohttp.ClientSession, url: str, **kwargs) -> aiohttp.ClientResponse:
    """GET через safe_session с ручными редиректами: каждый хоп проверяется заново."""
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url)
        resp = await session.get(url, allow_redirects=False, **kwargs)
        location = resp.headers.get("Location")
        if resp.status in _REDIRECT_CODES and location:
            resp.release()
            url = urljoin(url, location)
            continue
        return resp
    raise BlockedURL("слишком много редиректов")
