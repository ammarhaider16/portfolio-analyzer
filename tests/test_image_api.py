"""Owner-only image preview and explicit portfolio save API coverage."""
import base64
import os
import re
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from portfolio_analyzer import screenshot_import, web
from portfolio_analyzer.secrets import SecretStore


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    for name in list(os.environ):
        if name.startswith('PORTFOLIO_'):
            monkeypatch.delenv(name)
    monkeypatch.setattr(SecretStore, 'get', lambda self, name: os.environ.get('PORTFOLIO_SECRET_' + name.upper()))
    monkeypatch.setattr(web, 'create_model', lambda *args: object())


def csrf(client):
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get('/').text).group(1)


def body(**overrides):
    output = BytesIO()
    Image.new('RGB', (10, 10), 'white').save(output, format='PNG')
    return {'image_base64': base64.b64encode(output.getvalue()).decode(), 'content_type': 'image/png',
            'as_of': '2026-10-18T12:00:00Z', **overrides}


def draft():
    return {'positions': [{'symbol': 'ABC', 'quantity': None, 'price': '10', 'asset_type': 'stock',
                           'sector': None, 'previous_close': None}], 'cash': None,
            'warnings': ['Review before saving'], 'source': 'image'}


def test_image_preview_preserves_snapshot_and_requires_explicit_complete_save(tmp_path, monkeypatch):
    async def extract(*args):
        return draft()
    monkeypatch.setattr(screenshot_import, 'extract_portfolio_image', extract)
    with TestClient(web.create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        headers = {'x-csrf-token': csrf(client)}
        old = {'positions': [{'symbol': 'OLD', 'quantity': '2', 'price': '5'}], 'cash': '12',
               'as_of': '2026-10-17T12:00:00Z'}
        assert client.post('/api/portfolio', json={'snapshot': old}, headers=headers).status_code == 200
        before = client.get('/api/status').json()['snapshot']
        response = client.post('/api/import/image', json=body(), headers=headers)
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview['positions'][0]['quantity'] is None
        assert preview['cash'] is None
        assert client.get('/api/status').json()['snapshot'] == before
        for field in ('warnings',):
            preview.pop(field)
        assert client.post('/api/portfolio', json={'snapshot': preview}, headers=headers).status_code == 422
        preview['positions'][0]['quantity'] = '3'
        assert client.post('/api/portfolio', json={'snapshot': preview}, headers=headers).status_code == 422
        preview['cash'] = '8'
        saved = client.post('/api/portfolio', json={'snapshot': preview}, headers=headers)
        assert saved.status_code == 200, saved.text
        assert client.get('/api/status').json()['snapshot']['positions'][0]['symbol'] == 'ABC'


@pytest.mark.parametrize('overrides,status', [({'image_base64': '!!!!'}, 400),
                                           ({'content_type': 'image/svg+xml'}, 422),
                                           ({'as_of': '2026-10-18T12:00:00'}, 422),
                                           ({'as_of': 'invalid'}, 422)])
def test_invalid_upload_rejected_before_vision(tmp_path, monkeypatch, overrides, status):
    async def extract(*args):
        pytest.fail('Vision extraction must not run')
    monkeypatch.setattr(screenshot_import, 'extract_portfolio_image', extract)
    with TestClient(web.create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/import/image', json=body(**overrides), headers={'x-csrf-token': csrf(client)})
        assert response.status_code == status


def test_image_size_and_media_content_rejected(tmp_path):
    with TestClient(web.create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        headers = {'x-csrf-token': csrf(client)}
        oversize = base64.b64encode(b'x' * (5 * 1024 * 1024 + 1)).decode()
        response = client.post('/api/import/image', json=body(image_base64=oversize), headers=headers)
        assert response.status_code in (400, 413, 422), response.text
        response = client.post('/api/import/image', json=body(content_type='image/jpeg'), headers=headers)
        assert response.status_code == 400
        assert client.get('/api/status').json()['snapshot'] is None


def test_vision_failure_is_safe_and_unsaved(tmp_path, monkeypatch):
    async def fail(*args):
        raise RuntimeError('secret-provider-key')
    monkeypatch.setattr(screenshot_import, 'extract_portfolio_image', fail)
    with TestClient(web.create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/import/image', json=body(), headers={'x-csrf-token': csrf(client)})
        assert response.status_code == 400
        assert 'secret-provider-key' not in response.text
        assert client.get('/api/status').json()['snapshot'] is None


def test_upload_requires_owner_and_csrf_research_token_forbidden(tmp_path, monkeypatch):
    monkeypatch.setenv('PORTFOLIO_PUBLIC_URL', 'https://portfolio.example')
    monkeypatch.setenv('PORTFOLIO_SECRET_OWNER_PASSWORD', 'owner-password-for-tests-only')
    monkeypatch.setenv('PORTFOLIO_SECRET_API_TOKEN', 'research-token')
    async def extract(*args):
        return draft()
    monkeypatch.setattr(screenshot_import, 'extract_portfolio_image', extract)
    with TestClient(web.create_app(tmp_path, server=True), base_url='https://portfolio.example') as client:
        assert client.post('/api/import/image', json=body()).status_code in (401, 403)
        assert client.post('/api/import/image', json=body(), headers={'authorization': 'Bearer research-token'}).status_code == 403
        token = csrf(client)
        assert client.post('/api/login', json={'password': 'owner-password-for-tests-only'},
                           headers={'x-csrf-token': token}).status_code == 200
        assert client.post('/api/import/image', json=body()).status_code == 403
        headers = {'x-csrf-token': csrf(client)}
        assert client.post('/api/import/image', json=body(), headers=headers | {'origin': 'https://evil.test'}).status_code == 403
        assert client.post('/api/import/image', json=body(), headers=headers).status_code == 200
