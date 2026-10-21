"""Transport redirects must preserve the public-network and SEC boundaries."""
import asyncio
import http.client
import threading
import time

import pytest

from betafolio import providers


def fake_transport(monkeypatch, responses):
    requests = []
    resolved = []
    def dns(host, *args, **kwargs):
        resolved.append(host)
        return [(2, 1, 6, "", ("127.0.0.1" if host == "private.example" else "8.8.8.8", 443))]
    class Response:
        def __init__(self, status, location=""):
            self.status, self.location = status, location
        def getheader(self, name, default=""):
            return self.location if name == "Location" else "text/plain"
        def read(self, count):
            return b"public source text"
    class Connection:
        def __init__(self, host, **kwargs):
            self.host = host
        def request(self, method, path, headers):
            requests.append((self.host, path, headers))
        def getresponse(self):
            return Response(*responses.pop(0))
        def close(self):
            pass
    monkeypatch.setattr(providers.socket, "getaddrinfo", dns)
    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    return requests, resolved


def test_follows_relative_https_redirect_with_fresh_validation(monkeypatch):
    requests, resolved = fake_transport(monkeypatch, [(301, "/canonical"), (200,)])
    assert asyncio.run(providers.retrieve_public_text("https://public.example/feed", raw=True)) == "public source text"
    assert [request[1] for request in requests] == ["/feed", "/canonical"]
    assert len(resolved) == 4  # Validation and pinned address check at each hop.


@pytest.mark.parametrize("target", ["https://private.example/data", "http://public.example/data", "https://user:password@public.example/data"])
def test_redirect_rejects_unsafe_target_before_request(monkeypatch, target):
    requests, _ = fake_transport(monkeypatch, [(302, target)])
    with pytest.raises(ValueError):
        asyncio.run(providers.retrieve_public_text("https://public.example/feed"))
    assert len(requests) == 1


def test_redirect_into_sec_requires_contact_before_dns(monkeypatch):
    requests, resolved = fake_transport(monkeypatch, [(302, "https://data.sec.gov/data")])
    with pytest.raises(ValueError, match="identifying contact"):
        asyncio.run(providers.retrieve_public_text("https://public.example/feed"))
    assert "data.sec.gov" not in resolved
    assert len(requests) == 1


def test_sec_contact_not_forwarded_to_non_sec_destination(monkeypatch):
    requests, _ = fake_transport(monkeypatch, [(302, "https://public.example/data"), (200,)])
    asyncio.run(providers.retrieve_public_text("https://www.sec.gov/data", user_agent="Analyst contact@example.com"))
    assert requests[0][2]["User-Agent"] == "Analyst contact@example.com"
    assert requests[1][2]["User-Agent"] == "PortfolioResearch/1.0"


def test_redirect_count_is_bounded(monkeypatch):
    requests, _ = fake_transport(monkeypatch, [(302, f"/hop{index}") for index in range(4)])
    with pytest.raises(ValueError, match="redirect limit"):
        asyncio.run(providers.retrieve_public_text("https://public.example/feed"))
    assert len(requests) == 4


def test_redirect_loop_is_bounded(monkeypatch):
    requests, _ = fake_transport(monkeypatch, [(302, "/feed")])
    with pytest.raises(ValueError, match="redirect loop"):
        asyncio.run(providers.retrieve_public_text("https://public.example/feed"))
    assert len(requests) == 1


def blocking_transport(monkeypatch):
    """A peer that keeps a read alive until the client shuts down its socket."""
    state = {"active": 0, "peak": 0, "requests": 0}
    lock = threading.Lock()
    local = threading.local()
    class Sock:
        def __init__(self, *args):
            self.closed = threading.Event()
        def connect(self, address):
            local.sock = self
        def settimeout(self, value):
            pass
        def do_handshake(self):
            pass
        def shutdown(self, how):
            self.closed.set()
        def close(self):
            self.closed.set()
    class Context:
        def wrap_socket(self, sock, **kwargs):
            return sock
    class Response:
        status = 200
        def getheader(self, *args):
            return "text/plain"
        def read(self, count):
            assert local.sock.closed.wait(2), "Cancellation/deadline failed to stop the peer read"
            return b"partial"
    class Connection:
        def __init__(self, host, context, **kwargs):
            self._context = context
            self.counted = False
        def request(self, *args, **kwargs):
            self.connect()
            with lock:
                state["requests"] += 1
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                self.counted = True
        def getresponse(self):
            return Response()
        def close(self):
            if self.counted:
                with lock:
                    state["active"] -= 1
                self.counted = False
    monkeypatch.setattr(providers.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(providers.socket, "socket", Sock)
    monkeypatch.setattr("ssl.create_default_context", Context)
    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    return state


def test_cancelled_network_workers_keep_global_capacity_until_exit(monkeypatch):
    async def exercise():
        state = blocking_transport(monkeypatch)
        tasks = [asyncio.create_task(providers.retrieve_public_text("https://public.example/feed")) for _ in range(8)]
        for _ in range(100):
            if state["active"] == 4:
                break
            await asyncio.sleep(.005)
        assert state["active"] == 4
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for _ in range(100):
            if state["active"] == 0:
                break
            await asyncio.sleep(.005)
        assert state == {"active": 0, "peak": 4, "requests": 4}
    asyncio.run(exercise())


def test_absolute_deadline_shuts_down_a_peer_that_keeps_read_alive(monkeypatch):
    async def exercise():
        state = blocking_transport(monkeypatch)
        monkeypatch.setattr(providers, "SOURCE_REQUEST_SECONDS", .05)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await providers.retrieve_public_text("https://public.example/feed")
        assert time.monotonic() - started < .5
        assert state["active"] == 0
    asyncio.run(exercise())
