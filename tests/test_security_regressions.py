"""Independent offline regression coverage for authentication trust boundaries."""
import os
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from portfolio_analyzer.secrets import SecretStore
from portfolio_analyzer.web import create_app

ORIGIN = 'https://portfolio.example'
PASSWORD = 'fixture-owner-password-for-security'


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    for name in list(os.environ):
        if name.startswith('PORTFOLIO_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('PORTFOLIO_PUBLIC_URL', ORIGIN)
    monkeypatch.setenv('PORTFOLIO_SECRET_OWNER_PASSWORD', PASSWORD)
    monkeypatch.setattr(SecretStore, 'get', lambda self, name: os.environ.get('PORTFOLIO_SECRET_' + name.upper()))


def csrf(client):
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get('/').text).group(1)


def login(client):
    assert client.post('/api/login', json={'password': PASSWORD},
                       headers={'x-csrf-token': csrf(client)}).status_code == 200
    return client.cookies.get('portfolio_session')


def test_local_cookie_cannot_become_server_owner(tmp_path):
    with TestClient(create_app(tmp_path, server=False), base_url='http://127.0.0.1:8000') as local:
        old_csrf = csrf(local)
        cookie = local.cookies.get('portfolio_session')
    with TestClient(create_app(tmp_path, server=True), base_url=ORIGIN) as server:
        server.cookies.set('portfolio_session', cookie)
        assert server.get('/api/status').status_code == 401
        assert server.post('/api/settings', json={'report_length': 'detailed'},
                           headers={'x-csrf-token': old_csrf}).status_code == 401
        server.cookies.clear()
        login(server)
        assert server.get('/api/status').status_code == 200


@pytest.mark.parametrize('changed', ['origin', 'password'])
def test_server_cookie_revoked_by_audience_change(tmp_path, monkeypatch, changed):
    with TestClient(create_app(tmp_path, server=True), base_url=ORIGIN) as old:
        cookie = login(old)
        token = csrf(old)
    new_origin = ORIGIN
    if changed == 'origin':
        new_origin = 'https://other-portfolio.example'
        monkeypatch.setenv('PORTFOLIO_PUBLIC_URL', new_origin)
    else:
        monkeypatch.setenv('PORTFOLIO_SECRET_OWNER_PASSWORD', PASSWORD + '-rotated')
    with TestClient(create_app(tmp_path, server=True), base_url=new_origin) as new:
        new.cookies.set('portfolio_session', cookie)
        assert new.get('/api/status').status_code == 401
        assert new.post('/api/settings', json={'report_length': 'detailed'},
                        headers={'x-csrf-token': token}).status_code == 401


def test_throttled_attacker_does_not_lock_out_distinct_owner_ip(tmp_path):
    app = create_app(tmp_path, server=True)
    with TestClient(app, base_url=ORIGIN, client=('192.0.2.10', 1000)) as attacker:
        token = csrf(attacker)
        for _ in range(10):
            assert attacker.post('/api/login', json={'password': 'wrong'},
                                 headers={'x-csrf-token': token}).status_code == 401
        assert attacker.post('/api/login', json={'password': PASSWORD},
                             headers={'x-csrf-token': token}).status_code == 429
        # Caller-controlled forwarding headers must not defeat the actual peer limiter.
        assert attacker.post('/api/login', json={'password': PASSWORD}, headers={
            'x-csrf-token': token, 'x-forwarded-for': '192.0.2.20'}).status_code == 429
        with TestClient(app, base_url=ORIGIN, client=('192.0.2.20', 2000)) as owner:
            login(owner)
            assert owner.get('/api/status').status_code == 200


def test_successful_login_clears_peer_failures(tmp_path):
    with TestClient(create_app(tmp_path, server=True), base_url=ORIGIN) as client:
        for _ in range(2):
            token = csrf(client)
            for _ in range(9):
                assert client.post('/api/login', json={'password': 'wrong'},
                                   headers={'x-csrf-token': token}).status_code == 401
            login(client)


def test_logout_revokes_copied_cookie_and_persisted_key(tmp_path):
    app = create_app(tmp_path, server=True)
    with TestClient(app, base_url=ORIGIN) as owner:
        cookie = login(owner)
        copied_token = csrf(owner)
        with TestClient(app, base_url=ORIGIN) as copied:
            copied.cookies.set('portfolio_session', cookie)
            assert copied.get('/api/status').status_code == 200
            assert owner.post('/api/logout', headers={'x-csrf-token': csrf(owner)}).status_code == 200
            assert copied.get('/api/status').status_code == 401
            assert copied.post('/api/settings', json={},
                               headers={'x-csrf-token': copied_token}).status_code == 401
    with TestClient(create_app(tmp_path, server=True), base_url=ORIGIN) as restarted:
        restarted.cookies.set('portfolio_session', cookie)
        assert restarted.get('/api/status').status_code == 401


@pytest.mark.parametrize('path,limit', [('/api/import', 1_000_000), ('/api/import/image', 7_100_000)])
async def test_chunked_actual_body_limit_without_content_length(tmp_path, path, limit):
    app = create_app(tmp_path, server=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8000') as client:
        page = await client.get('/')
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        async def chunks():
            for _ in range(limit // 100_000):
                yield b'x' * 100_000
            yield b'x' * (limit % 100_000 + 1)
        request = client.build_request('POST', path, content=chunks(), headers={'x-csrf-token': token})
        assert 'content-length' not in request.headers
        assert request.headers['transfer-encoding'] == 'chunked'
        response = await client.send(request)
        assert response.status_code == 413
        assert app.state.service.store.snapshot() is None
