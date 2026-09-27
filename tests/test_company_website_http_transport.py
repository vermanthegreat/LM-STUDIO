from __future__ import annotations

from datetime import datetime, timezone
import ssl

import http.client

import pytest

from services.company_website_discovery_provider import CompanyWebsiteDiscoveryProvider
from services.company_website_http_transport import (
    CompanyWebsiteClock,
    CompanyWebsiteConnectionFactory,
    CompanyWebsiteDnsResolver,
    CompanyWebsiteHttpConnection,
    CompanyWebsiteHttpResponse,
    CompanyWebsiteTransportError,
    HardenedCompanyWebsiteHttpTransport,
    SystemCompanyWebsiteConnectionFactory,
)
import services.company_website_http_transport as transport_module


class FakeClock(CompanyWebsiteClock):
    def __init__(self) -> None:
        self.t = 100.0

    def monotonic(self) -> float:
        return self.t

    def utcnow(self) -> datetime:
        return datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)


class AdvancingClock(FakeClock):
    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeResolver(CompanyWebsiteDnsResolver):
    def __init__(self, answers: dict[str, tuple[str, ...]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, float]] = []

    def resolve(self, hostname: str, timeout_seconds: float) -> tuple[str, ...]:
        self.calls.append((hostname, timeout_seconds))
        return self.answers[hostname]


class AdvancingResolver(FakeResolver):
    def __init__(self, answers: dict[str, tuple[str, ...]], clock: AdvancingClock, seconds: float) -> None:
        super().__init__(answers)
        self.clock = clock
        self.seconds = seconds

    def resolve(self, hostname: str, timeout_seconds: float) -> tuple[str, ...]:
        result = super().resolve(hostname, timeout_seconds)
        self.clock.advance(self.seconds)
        return result


class FakeResponse(CompanyWebsiteHttpResponse):
    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = {key.casefold(): value for key, value in (headers or {}).items()}
        self.offset = 0
        self.closed = False
        self.read_sizes: list[int] = []

    def header(self, name: str) -> str | None:
        return self.headers.get(name.casefold())

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        chunk = self.body[self.offset:self.offset + min(size, 3)]
        self.offset += len(chunk)
        return chunk

    def set_timeout(self, timeout_seconds: float) -> None:
        return

    def close(self) -> None:
        self.closed = True


class FakeConnection(CompanyWebsiteHttpConnection):
    def __init__(self, response: FakeResponse, calls: list[dict[str, object]]) -> None:
        self.response = response
        self.calls = calls
        self.closed = False

    def get(self, *, hostname: str, path: str, timeout_seconds: float) -> CompanyWebsiteHttpResponse:
        self.calls.append({"hostname": hostname, "path": path, "timeout": timeout_seconds})
        return self.response

    def close(self) -> None:
        self.closed = True


class AdvancingConnection(FakeConnection):
    def __init__(self, response: FakeResponse, calls: list[dict[str, object]], clock: AdvancingClock, seconds: float) -> None:
        super().__init__(response, calls)
        self.clock = clock
        self.seconds = seconds

    def get(self, *, hostname: str, path: str, timeout_seconds: float) -> CompanyWebsiteHttpResponse:
        result = super().get(hostname=hostname, path=path, timeout_seconds=timeout_seconds)
        self.clock.advance(self.seconds)
        return result


class FakeFactory(CompanyWebsiteConnectionFactory):
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []
        self.connections: list[FakeConnection] = []

    def connect(self, *, hostname: str, address: str, port: int, tls: bool, timeout_seconds: float) -> CompanyWebsiteHttpConnection:
        self.calls.append({"hostname": hostname, "address": address, "port": port, "tls": tls, "timeout": timeout_seconds})
        connection = FakeConnection(self.responses.pop(0), self.calls)
        self.connections.append(connection)
        return connection


class ScriptedFactory(FakeFactory):
    def __init__(self, responses: list[FakeResponse], clock: AdvancingClock, *, connect_seconds: float = 0.0, get_seconds: float = 0.0) -> None:
        super().__init__(responses)
        self.clock = clock
        self.connect_seconds = connect_seconds
        self.get_seconds = get_seconds
        self.set_timeout_values: list[float] = []

    def connect(self, *, hostname: str, address: str, port: int, tls: bool, timeout_seconds: float) -> CompanyWebsiteHttpConnection:
        self.set_timeout_values.append(timeout_seconds)
        self.clock.advance(self.connect_seconds)
        response = self.responses.pop(0)
        connection = AdvancingConnection(response, self.calls, self.clock, self.get_seconds)
        self.calls.append({"hostname": hostname, "address": address, "port": port, "tls": tls, "timeout": timeout_seconds})
        self.connections.append(connection)
        return connection


class TimeoutFactory(FakeFactory):
    def __init__(self, clock: AdvancingClock, error: BaseException, *, advance_seconds: float = 11) -> None:
        super().__init__([])
        self.clock = clock
        self.error = error
        self.advance_seconds = advance_seconds
        self.attempts = 0

    def connect(self, *, hostname: str, address: str, port: int, tls: bool, timeout_seconds: float) -> CompanyWebsiteHttpConnection:
        self.attempts += 1
        self.clock.advance(self.advance_seconds)
        raise self.error


class AdvancingResponse(FakeResponse):
    def __init__(self, *args, clock: AdvancingClock, set_timeout_seconds: float = 0.0, read_error: BaseException | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.clock = clock
        self.set_timeout_seconds = set_timeout_seconds
        self.read_error = read_error
        self.timeout_calls: list[float] = []

    def set_timeout(self, timeout_seconds: float) -> None:
        self.timeout_calls.append(timeout_seconds)
        self.clock.advance(self.set_timeout_seconds)
        if self.read_error is not None and self.set_timeout_seconds:
            raise self.read_error

    def read(self, size: int) -> bytes:
        if self.read_error is not None and not self.set_timeout_seconds:
            raise self.read_error
        return super().read(size)


def make_transport(response: FakeResponse, answers: tuple[str, ...] = ("93.184.216.34",)):
    resolver = FakeResolver({"example.com": answers})
    factory = FakeFactory([response])
    return HardenedCompanyWebsiteHttpTransport(resolver=resolver, connection_factory=factory, clock=FakeClock()), resolver, factory


def test_dependencies_are_nominal_and_constructor_does_not_resolve():
    with pytest.raises(TypeError):
        HardenedCompanyWebsiteHttpTransport(resolver=lambda *_: ("93.184.216.34",))
    with pytest.raises(TypeError):
        HardenedCompanyWebsiteHttpTransport(connection_factory=lambda **_: None)
    with pytest.raises(TypeError):
        HardenedCompanyWebsiteHttpTransport(clock=lambda: 1.0)


@pytest.mark.parametrize("address", [
    "10.0.0.1", "127.0.0.1", "169.254.169.254", "100.100.100.200",
    "192.0.2.1", "100.64.0.1", "fc00::1", "::1", "2001:db8::1",
])
def test_non_public_destinations_fail_closed(address: str):
    transport, _, _ = make_transport(FakeResponse(200, b"ok"), ("93.184.216.34", address))
    with pytest.raises(CompanyWebsiteTransportError) as error:
        transport.fetch(url="https://example.com", timeout_seconds=10, max_response_bytes=10)
    assert error.value.code == "company_website_destination_blocked"


def test_public_addresses_are_sorted_and_connection_is_pinned():
    response = FakeResponse(200, b"hello", {"Content-Type": "text/html; charset=utf-8", "Content-Length": "5"})
    transport, resolver, factory = make_transport(response, ("93.184.216.35", "93.184.216.34"))
    result = transport.fetch(url="HTTPS://EXAMPLE.COM//team/", timeout_seconds=10, max_response_bytes=5)
    assert result.requested_url == "https://example.com/team"
    assert result.final_url == result.requested_url
    assert result.body == b"hello"
    assert result.content_type == "text/html"
    assert resolver.calls[0][0] == "example.com"
    assert factory.calls[0]["address"] == "93.184.216.34"
    assert factory.calls[0]["hostname"] == "example.com"
    assert factory.calls[0]["tls"] is True
    assert factory.connections[0].closed and response.closed


@pytest.mark.parametrize("url", [
    "https://user:pass@example.com", "https://127.0.0.1", "https://localhost",
    "https://example.com/?q=1", "https://example.com/../team", "https://example.com:444/team",
])
def test_url_policy_rejects_unsafe_targets(url: str):
    transport, _, _ = make_transport(FakeResponse(200, b"ok"))
    with pytest.raises(CompanyWebsiteTransportError) as error:
        transport.fetch(url=url, timeout_seconds=10, max_response_bytes=10)
    assert error.value.code == "company_website_invalid_response"


def test_redirects_are_explicit_and_exact_host_boundary_is_enforced():
    first = FakeResponse(302, headers={"Location": "/team"})
    second = FakeResponse(200, b"ok", {"Content-Type": "text/html"})
    resolver = FakeResolver({"example.com": ("93.184.216.34",)})
    factory = FakeFactory([first, second])
    transport = HardenedCompanyWebsiteHttpTransport(resolver=resolver, connection_factory=factory, clock=FakeClock())
    result = transport.fetch(url="https://example.com", timeout_seconds=10, max_response_bytes=10)
    assert result.final_url == "https://example.com/team"
    assert [call["path"] for call in factory.calls if "path" in call] == ["/", "/team"]
    assert len(resolver.calls) == 2
    assert all(connection.closed for connection in factory.connections)

    blocked = FakeResponse(302, headers={"Location": "https://cdn.example.com/team"})
    transport, _, _ = make_transport(blocked)
    with pytest.raises(CompanyWebsiteTransportError) as error:
        transport.fetch(url="https://example.com", timeout_seconds=10, max_response_bytes=10)
    assert error.value.code == "company_website_redirect_blocked"


def test_response_cap_and_content_length_precheck_close_resources():
    overflow = FakeResponse(200, b"123456")
    transport, _, factory = make_transport(overflow)
    with pytest.raises(CompanyWebsiteTransportError) as error:
        transport.fetch(url="https://example.com", timeout_seconds=10, max_response_bytes=5)
    assert error.value.code == "company_website_response_too_large"
    assert overflow.closed and factory.connections[0].closed

    excessive = FakeResponse(200, b"", {"Content-Length": "6"})
    transport, _, _ = make_transport(excessive)
    with pytest.raises(CompanyWebsiteTransportError) as error:
        transport.fetch(url="https://example.com", timeout_seconds=10, max_response_bytes=5)
    assert error.value.code == "company_website_response_too_large"
    assert excessive.read_sizes == []


def test_statuses_remain_typed_and_retry_after_is_bounded():
    response = FakeResponse(429, b"busy", {"Content-Type": "text/html; charset=utf-8", "Retry-After": "30"})
    transport, _, _ = make_transport(response)
    result = transport.fetch(url="https://example.com", timeout_seconds=10, max_response_bytes=10)
    assert result.http_status == 429
    assert result.retry_after is not None
    assert result.retry_after.isoformat() == "2026-08-03T15:00:30+00:00"


def test_provider_integration_uses_transport_without_runner_or_persistence():
    body = b'<html><body><script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"CEO"}</script></body></html>'
    response = FakeResponse(200, body, {"Content-Type": "text/html", "Content-Length": str(len(body))})
    transport, _, _ = make_transport(response)
    provider = CompanyWebsiteDiscoveryProvider(transport)
    from discovery_models import DiscoveryOutcomeStatus, DiscoveryRequest
    from datetime import datetime, timezone
    request = DiscoveryRequest(
        job_id="job", lead_id=1, company_name="Example", normalized_domain="example.com",
        company_website="https://example.com", target_roles=["economic_buyer"], result_limit=1,
        approved_source_types=["website"], requested_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        requester_identity="operator", correlation_id="corr", provider_config_ref="test",
        max_pages=1, max_requests=1, timeout_seconds=10,
    )
    result = provider.execute(request)
    assert result.status is DiscoveryOutcomeStatus.SUCCEEDED
    assert result.candidates[0].name == "Jane Doe"


def test_https_preserves_original_hostname_for_tls_and_http_host():
    response = FakeResponse(200, b"ok", {"Content-Length": "2"})
    transport, resolver, factory = make_transport(response)

    result = transport.fetch(url="HTTPS://EXAMPLE.COM/team", timeout_seconds=10, max_response_bytes=10)

    assert result.body == b"ok"
    assert resolver.calls == [("example.com", 10.0)]
    assert factory.calls[0]["address"] == "93.184.216.34"
    assert factory.calls[0]["hostname"] == "example.com"
    assert factory.calls[0]["tls"] is True
    assert factory.calls[1] == {"hostname": "example.com", "path": "/team", "timeout": 10.0}


def test_system_https_path_uses_verified_tls_context_and_sni_and_closes_on_failure(monkeypatch):
    captured: dict[str, object] = {}

    class FakeSocket:
        def __init__(self, *_args: object) -> None:
            self.closed = False

        def settimeout(self, value: float) -> None:
            captured.setdefault("socket_timeouts", []).append(value)

        def connect(self, target: object) -> None:
            captured["target"] = target

        def close(self) -> None:
            self.closed = True
            captured["socket_closed"] = self.closed

    class FakeContext:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True

        def wrap_socket(self, sock: FakeSocket, *, server_hostname: str) -> object:
            captured["server_hostname"] = server_hostname
            captured["verify_mode"] = self.verify_mode
            captured["check_hostname"] = self.check_hostname
            raise ssl.SSLError("certificate secret.internal invalid for 93.184.216.34")

    monkeypatch.setattr(transport_module.socket, "socket", FakeSocket)
    monkeypatch.setattr(transport_module.ssl, "create_default_context", lambda: FakeContext())
    resolver = FakeResolver({"example.com": ("93.184.216.34",)})
    factory = SystemCompanyWebsiteConnectionFactory()
    transport = HardenedCompanyWebsiteHttpTransport(resolver=resolver, connection_factory=factory, clock=FakeClock())

    with pytest.raises(CompanyWebsiteTransportError) as error:
        transport.fetch(url="https://example.com", timeout_seconds=10, max_response_bytes=10)

    assert error.value.code == "company_website_tls_failed"
    assert captured["target"] == ("93.184.216.34", 443)
    assert captured["server_hostname"] == "example.com"
    assert captured["verify_mode"] == ssl.CERT_REQUIRED
    assert captured["check_hostname"] is True
    assert captured["socket_closed"] is True
    assert "secret.internal" not in str(error.value)
    assert "93.184.216.34" not in str(error.value)


def test_tls_failure_is_stable_redacted_and_does_not_retry():
    clock = AdvancingClock()
    resolver = FakeResolver({"example.com": ("93.184.216.34", "93.184.216.35")})
    factory = TimeoutFactory(clock, ssl.SSLError("CERTIFICATE_VERIFY_FAILED secret=token for 93.184.216.34"), advance_seconds=0)
    transport = HardenedCompanyWebsiteHttpTransport(resolver=resolver, connection_factory=factory, clock=clock)

    with pytest.raises(CompanyWebsiteTransportError) as error:
        transport.fetch(url="https://example.com", timeout_seconds=10, max_response_bytes=10)

    assert error.value.code == "company_website_tls_failed"
    assert str(error.value) == "company_website_tls_failed"
    assert factory.attempts == 2
    assert len(resolver.calls) == 1


def test_one_total_deadline_reduces_each_blocking_stage_and_redirect_hop():
    clock = AdvancingClock()
    first = FakeResponse(302, headers={"Location": "/final"})
    second = AdvancingResponse(200, b"ok", {"Content-Length": "2"}, clock=clock)
    resolver = AdvancingResolver({"example.com": ("93.184.216.34",)}, clock, 1)
    factory = ScriptedFactory([first, second], clock, connect_seconds=2, get_seconds=1)
    transport = HardenedCompanyWebsiteHttpTransport(resolver=resolver, connection_factory=factory, clock=clock)

    result = transport.fetch(url="https://example.com", timeout_seconds=10, max_response_bytes=10)

    assert result.final_url == "https://example.com/final"
    assert len(resolver.calls) == 2
    assert resolver.calls[1][1] < resolver.calls[0][1]
    connect_calls = [call for call in factory.calls if "address" in call]
    assert connect_calls[0]["timeout"] <= 9
    assert connect_calls[1]["timeout"] < connect_calls[0]["timeout"]
    get_timeouts = [call["timeout"] for call in factory.calls if "path" in call]
    assert get_timeouts[1] < get_timeouts[0]
    assert second.timeout_calls[0] < get_timeouts[1]
    assert second.timeout_calls[1] <= second.timeout_calls[0]
    assert all(connection.closed for connection in factory.connections)


def test_dns_consumption_exhausts_deadline_before_connect():
    clock = AdvancingClock()
    resolver = AdvancingResolver({"example.com": ("93.184.216.34",)}, clock, 11)
    factory = FakeFactory([FakeResponse(200, b"ok")])
    transport = HardenedCompanyWebsiteHttpTransport(resolver=resolver, connection_factory=factory, clock=clock)

    with pytest.raises(CompanyWebsiteTransportError) as error:
        transport.fetch(url="https://example.com", timeout_seconds=10, max_response_bytes=10)

    assert error.value.code == "company_website_timeout"
    assert factory.calls == []
    assert str(error.value) == "company_website_timeout"


@pytest.mark.parametrize("stage", ["connect", "tls", "header", "body", "redirect"])
def test_stage_deadline_exhaustion_is_stable_and_stops_later_stages(stage: str):
    clock = AdvancingClock()
    resolver = FakeResolver({"example.com": ("93.184.216.34",)})
    response = FakeResponse(200, b"ok", {"Content-Length": "2"})

    if stage in {"connect", "tls"}:
        factory: CompanyWebsiteConnectionFactory = TimeoutFactory(clock, TimeoutError("raw timeout secret"))
    elif stage == "header":
        response = AdvancingResponse(200, b"ok", {"Content-Length": "2"}, clock=clock, set_timeout_seconds=11, read_error=TimeoutError("header timeout secret"))
        factory = FakeFactory([response])
    elif stage == "body":
        response = AdvancingResponse(200, b"ok", {"Content-Length": "2"}, clock=clock, read_error=TimeoutError("body timeout secret"))
        factory = FakeFactory([response])
    else:
        class RedirectResponse(FakeResponse):
            def header(self, name: str) -> str | None:
                clock.advance(11)
                return super().header(name)

        response = RedirectResponse(302, headers={"Location": "/next"})
        factory = FakeFactory([response])

    transport = HardenedCompanyWebsiteHttpTransport(resolver=resolver, connection_factory=factory, clock=clock)
    with pytest.raises(CompanyWebsiteTransportError) as error:
        transport.fetch(url="https://example.com", timeout_seconds=10, max_response_bytes=10)

    assert error.value.code == "company_website_timeout"
    assert str(error.value) == "company_website_timeout"
    assert "secret" not in str(error.value)
    if stage in {"connect", "tls"}:
        assert getattr(factory, "attempts") == 1
    else:
        assert all(connection.closed for connection in getattr(factory, "connections"))
    if stage == "redirect":
        assert len(resolver.calls) == 1


def test_system_http_response_set_timeout_uses_transport_socket_when_fp_raw_cleared():
    class FakeSock:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)

    sock = FakeSock()
    http_response = http.client.HTTPResponse.__new__(http.client.HTTPResponse)
    http_response.status = 200
    http_response.fp = type("FP", (), {"raw": None})()
    wrapped = transport_module._SystemHttpResponse(http_response, sock)

    wrapped.set_timeout(7.5)

    assert sock.timeouts == [7.5]


def test_system_http_connection_updates_status_after_begin(monkeypatch):
    class FakeSock:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)

        def sendall(self, payload: bytes) -> None:
            return

        def close(self) -> None:
            return

    class FakeHttpResponse:
        status = 200

        def __init__(self, sock: FakeSock) -> None:
            self.fp = type("FP", (), {"raw": None})()

        def begin(self) -> None:
            return

        def getheader(self, name: str) -> str | None:
            return "text/html" if name.casefold() == "content-type" else None

        def read(self, size: int) -> bytes:
            return b""

    monkeypatch.setattr(transport_module.http.client, "HTTPResponse", FakeHttpResponse)
    connection = transport_module._SystemHttpConnection(FakeSock())
    response = connection.get(hostname="example.com", path="/", timeout_seconds=10)

    assert response.status == 200


@pytest.mark.parametrize(
    ("address", "expected_target"),
    [
        ("93.184.216.34", ("93.184.216.34", 443)),
        ("2606:2800:220:1:248:1893:25c8:1946", ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0)),
    ],
)
def test_system_connection_uses_correct_sockaddr_tuple_shape(address: str, expected_target: tuple[object, ...], monkeypatch):
    captured: dict[str, object] = {}

    class FakeSocket:
        def __init__(self, family: int, type: int) -> None:
            captured["family"] = family
            captured["timeouts"] = []

        def settimeout(self, value: float) -> None:
            captured.setdefault("timeouts", []).append(value)

        def connect(self, target: object) -> None:
            captured["target"] = target

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(transport_module.socket, "socket", FakeSocket)
    factory = SystemCompanyWebsiteConnectionFactory()

    with pytest.raises(Exception):
        factory.connect(
            hostname="example.com",
            address=address,
            port=443,
            tls=True,
            timeout_seconds=12.5,
        )

    assert captured["target"] == expected_target
    assert captured["timeouts"] == [12.5]
    assert captured["closed"] is True


def test_system_https_body_read_survives_repeated_timeout_updates(monkeypatch):
    class FakeSock:
        def __init__(self) -> None:
            self.timeouts: list[float] = []
            self.offset = 0
            self.payload = b"a" * 20

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)

        def sendall(self, payload: bytes) -> None:
            return

        def close(self) -> None:
            return

    class FakeHttpResponse:
        status = 200

        def __init__(self, sock: FakeSock) -> None:
            self._sock = sock
            self.fp = type("FP", (), {"raw": None})()
            self.offset = 0

        def begin(self) -> None:
            return

        def getheader(self, name: str) -> str | None:
            return None

        def read(self, size: int) -> bytes:
            chunk = self._sock.payload[self.offset:self.offset + min(size, 5)]
            self.offset += len(chunk)
            self.fp.raw = None
            return chunk

    monkeypatch.setattr(transport_module.http.client, "HTTPResponse", FakeHttpResponse)
    connection = transport_module._SystemHttpConnection(FakeSock())
    response = connection.get(hostname="example.com", path="/", timeout_seconds=10)
    body = bytearray()
    while True:
        response.set_timeout(4.0)
        chunk = response.read(5)
        if not chunk:
            break
        body.extend(chunk)

    assert body == b"a" * 20
    assert response._sock.timeouts.count(4.0) == 5
