"""Hardened, bounded HTTP transport for the company-website fetch port.

The transport deliberately owns DNS resolution and socket creation.  The
connection is opened against an already policy-checked address while the
original hostname is retained for HTTP Host and TLS SNI/certificate checks.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from services.company_website_discovery_provider import (
    CompanyWebsiteFetchPort,
    CompanyWebsiteFetchResponse,
)


USER_AGENT = "LMStudio-CompanyContactDiscovery/1.0"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307})
_MAX_REDIRECTS = 3
_MAX_ADDRESSES = 2
_MAX_HEADER_VALUE = 4096
_MAX_RETRY_AFTER_SECONDS = 86400
_SAFE_MEDIA_TYPE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_DECIMAL = re.compile(r"^[0-9]+$")


class CompanyWebsiteTransportError(Exception):
    """Stable, secret-safe transport failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CompanyWebsiteClock(ABC):
    @abstractmethod
    def monotonic(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def utcnow(self) -> datetime:
        raise NotImplementedError


class SystemCompanyWebsiteClock(CompanyWebsiteClock):
    def monotonic(self) -> float:
        return time.monotonic()

    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)


class CompanyWebsiteDnsResolver(ABC):
    @abstractmethod
    def resolve(self, hostname: str, timeout_seconds: float) -> tuple[str, ...]:
        raise NotImplementedError


class SystemCompanyWebsiteDnsResolver(CompanyWebsiteDnsResolver):
    def resolve(self, hostname: str, timeout_seconds: float) -> tuple[str, ...]:
        result: list[tuple[str, ...]] = []
        failure: list[BaseException] = []
        completed = threading.Event()

        def resolve_now() -> None:
            try:
                answers = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
                result.append(tuple({item[4][0] for item in answers}))
            except BaseException as error:
                failure.append(error)
            finally:
                completed.set()

        threading.Thread(target=resolve_now, name="company-website-dns", daemon=True).start()
        if not completed.wait(timeout_seconds):
            raise TimeoutError
        if failure:
            raise failure[0]
        if not result:
            raise OSError("empty DNS result")
        return result[0]


class CompanyWebsiteHttpResponse(ABC):
    status: int

    @abstractmethod
    def header(self, name: str) -> Optional[str]:
        raise NotImplementedError

    @abstractmethod
    def read(self, size: int) -> bytes:
        raise NotImplementedError

    @abstractmethod
    def set_timeout(self, timeout_seconds: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class CompanyWebsiteHttpConnection(ABC):
    @abstractmethod
    def get(self, *, hostname: str, path: str, timeout_seconds: float) -> CompanyWebsiteHttpResponse:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class CompanyWebsiteConnectionFactory(ABC):
    @abstractmethod
    def connect(
        self, *, hostname: str, address: str, port: int, tls: bool, timeout_seconds: float
    ) -> CompanyWebsiteHttpConnection:
        raise NotImplementedError


class _SystemHttpResponse(CompanyWebsiteHttpResponse):
    def __init__(self, response: http.client.HTTPResponse) -> None:
        self._response = response
        self.status = response.status

    def header(self, name: str) -> Optional[str]:
        return self._response.getheader(name)

    def read(self, size: int) -> bytes:
        return self._response.read(size)

    def set_timeout(self, timeout_seconds: float) -> None:
        self._response.fp.raw._sock.settimeout(timeout_seconds)

    def close(self) -> None:
        self._response.close()


class _SystemHttpConnection(CompanyWebsiteHttpConnection):
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._response: Optional[_SystemHttpResponse] = None
        self._closed = False

    def get(self, *, hostname: str, path: str, timeout_seconds: float) -> CompanyWebsiteHttpResponse:
        if self._closed:
            raise OSError("closed")
        self._sock.settimeout(timeout_seconds)
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {hostname}\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
            "Accept: text/html, application/xhtml+xml;q=0.9\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        self._sock.sendall(request)
        response = _SystemHttpResponse(http.client.HTTPResponse(self._sock))
        response._response.begin()
        self._response = response
        return response

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._response is not None:
                self._response.close()
        finally:
            self._sock.close()


class SystemCompanyWebsiteConnectionFactory(CompanyWebsiteConnectionFactory):
    def connect(
        self, *, hostname: str, address: str, port: int, tls: bool, timeout_seconds: float
    ) -> CompanyWebsiteHttpConnection:
        ip = ipaddress.ip_address(address)
        sock = socket.socket(ip.version == 6 and socket.AF_INET6 or socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout_seconds)
            target = (address, port, 0, 0) if ip.version == 6 else (address, port)
            sock.connect(target)
            if tls:
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=hostname)
            return _SystemHttpConnection(sock)
        except Exception:
            sock.close()
            raise


def _normalize_url(value: str) -> Optional[str]:
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
        if _blocked_hostname(host):
            return None
        port = parsed.port
        if port is not None and port not in {80, 443}:
            return None
        scheme = parsed.scheme.casefold()
        netloc = host if port is None or port == (443 if scheme == "https" else 80) else f"{host}:{port}"
        path = parsed.path or "/"
        if ".." in path.split("/"):
            return None
        path = re.sub(r"/{2,}", "/", path)
        if not path.startswith("/"):
            path = "/" + path
        if path != "/":
            path = path.rstrip("/") or "/"
        return urlunsplit((scheme, netloc, path, "", ""))
    except (ValueError, UnicodeError):
        return None


def _blocked_hostname(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith((".local", ".internal", ".localhost"))


def _same_host_or_www(host: str, initial: str) -> bool:
    root = initial.removeprefix("www.")
    return host in {root, f"www.{root}"}


def _public_addresses(addresses: tuple[str, ...]) -> tuple[str, ...]:
    parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        for value in addresses:
            address = ipaddress.ip_address(value)
            if address.version == 6 and address.ipv4_mapped is not None:
                return ()
            if not address.is_global:
                return ()
            parsed.append(address)
    except ValueError:
        return ()
    return tuple(str(item) for item in sorted(set(parsed), key=lambda item: (item.version, int(item))))


class HardenedCompanyWebsiteHttpTransport(CompanyWebsiteFetchPort):
    """A GET-only transport with DNS pinning, bounded redirects, and streaming caps."""

    def __init__(
        self,
        *,
        resolver: Optional[CompanyWebsiteDnsResolver] = None,
        connection_factory: Optional[CompanyWebsiteConnectionFactory] = None,
        clock: Optional[CompanyWebsiteClock] = None,
    ) -> None:
        self._resolver = resolver or SystemCompanyWebsiteDnsResolver()
        self._connections = connection_factory or SystemCompanyWebsiteConnectionFactory()
        self._clock = clock or SystemCompanyWebsiteClock()
        for dependency, expected in (
            (self._resolver, CompanyWebsiteDnsResolver),
            (self._connections, CompanyWebsiteConnectionFactory),
            (self._clock, CompanyWebsiteClock),
        ):
            if not isinstance(dependency, expected):
                raise TypeError("invalid company website transport dependency")

    def fetch(self, *, url: str, timeout_seconds: int, max_response_bytes: int) -> CompanyWebsiteFetchResponse:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise CompanyWebsiteTransportError("company_website_timeout")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes <= 0:
            raise CompanyWebsiteTransportError("company_website_response_too_large")
        current = _normalize_url(url)
        if current is None:
            raise CompanyWebsiteTransportError("company_website_invalid_response")
        initial_host = urlsplit(current).hostname or ""
        deadline = self._clock.monotonic() + timeout_seconds
        history = {current}
        redirects = 0
        while True:
            response, connection = self._open(current, deadline)
            try:
                status = response.status
                location = response.header("Location") if status in _REDIRECT_STATUSES else None
                if status in _REDIRECT_STATUSES and location:
                    if len(location) > _MAX_HEADER_VALUE:
                        raise CompanyWebsiteTransportError("company_website_redirect_blocked")
                    next_url = _normalize_url(urljoin(current, location))
                    if next_url is None or not _same_host_or_www(urlsplit(next_url).hostname or "", initial_host):
                        raise CompanyWebsiteTransportError("company_website_redirect_blocked")
                    if next_url in history:
                        raise CompanyWebsiteTransportError("company_website_redirect_blocked")
                    redirects += 1
                    if redirects > _MAX_REDIRECTS:
                        raise CompanyWebsiteTransportError("company_website_redirect_limit")
                    history.add(next_url)
                    current = next_url
                    continue
                return self._read_response(current, response, max_response_bytes, deadline)
            finally:
                response.close()
                connection.close()

    def _open(self, url: str, deadline: float) -> tuple[CompanyWebsiteHttpResponse, CompanyWebsiteHttpConnection]:
        remaining = self._remaining(deadline)
        parsed = urlsplit(url)
        try:
            resolved = self._resolver.resolve(parsed.hostname or "", remaining)
        except CompanyWebsiteTransportError:
            raise
        except TimeoutError:
            raise CompanyWebsiteTransportError("company_website_timeout")
        except Exception:
            raise CompanyWebsiteTransportError("company_website_dns_failed")
        addresses = _public_addresses(resolved)
        if not addresses:
            raise CompanyWebsiteTransportError("company_website_destination_blocked")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        last_code = "company_website_connect_failed"
        for address in addresses[:_MAX_ADDRESSES]:
            connection: Optional[CompanyWebsiteHttpConnection] = None
            response: Optional[CompanyWebsiteHttpResponse] = None
            try:
                connection = self._connections.connect(
                    hostname=parsed.hostname or "", address=address, port=port,
                    tls=parsed.scheme == "https", timeout_seconds=self._remaining(deadline),
                )
                response = connection.get(hostname=parsed.hostname or "", path=parsed.path or "/", timeout_seconds=self._remaining(deadline))
                return response, connection
            except CompanyWebsiteTransportError:
                if connection is not None:
                    connection.close()
                raise
            except TimeoutError:
                last_code = "company_website_timeout"
            except ssl.SSLError:
                last_code = "company_website_tls_failed"
            except (OSError, ValueError):
                last_code = "company_website_connect_failed"
            finally:
                if connection is not None and response is None:
                    connection.close()
        raise CompanyWebsiteTransportError(last_code)

    def _read_response(
        self, url: str, response: CompanyWebsiteHttpResponse, limit: int, deadline: float
    ) -> CompanyWebsiteFetchResponse:
        content_length = response.header("Content-Length")
        expected: Optional[int] = None
        if content_length is not None:
            if len(content_length) > 32 or not _DECIMAL.fullmatch(content_length.strip()):
                raise CompanyWebsiteTransportError("company_website_invalid_response")
            expected = int(content_length.strip())
            if expected > limit:
                raise CompanyWebsiteTransportError("company_website_response_too_large")
        body = bytearray()
        while len(body) <= limit:
            remaining = limit + 1 - len(body)
            try:
                response.set_timeout(self._remaining(deadline))
                chunk = response.read(min(8192, remaining))
            except TimeoutError:
                raise CompanyWebsiteTransportError("company_website_timeout")
            except Exception:
                raise CompanyWebsiteTransportError("company_website_invalid_response")
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > limit:
                raise CompanyWebsiteTransportError("company_website_response_too_large")
        if expected is not None and len(body) != expected:
            raise CompanyWebsiteTransportError("company_website_invalid_response")
        media_type = self._content_type(response.header("Content-Type"))
        retry_after = self._retry_after(response.header("Retry-After"))
        return CompanyWebsiteFetchResponse(
            requested_url=url, final_url=url, http_status=response.status,
            content_type=media_type, body=bytes(body), retry_after=retry_after,
        )

    @staticmethod
    def _content_type(value: Optional[str]) -> str:
        if not value:
            return "application/octet-stream"
        media_type = value.split(";", 1)[0].strip().casefold()
        if len(media_type) > 128 or not _SAFE_MEDIA_TYPE.fullmatch(media_type):
            return "application/octet-stream"
        return media_type

    def _retry_after(self, value: Optional[str]) -> Optional[datetime]:
        if not value or len(value) > 64:
            return None
        value = value.strip()
        if _DECIMAL.fullmatch(value):
            seconds = int(value)
            if seconds <= _MAX_RETRY_AFTER_SECONDS:
                return self._clock.utcnow() + timedelta(seconds=seconds)
            return None
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed < self._clock.utcnow() or parsed - self._clock.utcnow() > timedelta(seconds=_MAX_RETRY_AFTER_SECONDS):
                return None
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock.monotonic()
        if remaining <= 0:
            raise CompanyWebsiteTransportError("company_website_timeout")
        return remaining


# Public aliases make the narrow injected boundary easy to discover without
# exposing the implementation's socket details.
CompanyWebsiteHttpResponsePort = CompanyWebsiteHttpResponse
CompanyWebsiteHttpConnectionPort = CompanyWebsiteHttpConnection
