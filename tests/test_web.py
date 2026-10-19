"""Offline dashboard and API security acceptance tests."""
import os
import asyncio
import re
import time

import pytest
from fastapi.testclient import TestClient

from portfolio_analyzer.models import AnalysisResult
from portfolio_analyzer.secrets import SecretStore
from portfolio_analyzer.web import create_app


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    values = {}
    for key in list(os.environ):
        if key.startswith(('PORTFOLIO_',)) or key in {'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'LITELLM_API_KEY', 'TAVILY_API_KEY', 'ALPHA_VANTAGE_API_KEY'}:
            monkeypatch.delenv(key)
    monkeypatch.setattr(SecretStore, 'get', lambda self, name: os.environ.get('PORTFOLIO_SECRET_' + name.upper(), values.get(name)))
    monkeypatch.setattr(SecretStore, 'set', lambda self, name, value: values.__setitem__(name, value))
    monkeypatch.setattr(SecretStore, 'delete', lambda self, name: values.pop(name, None))
    return values


def csrf(client):
    response = client.get('/')
    assert response.status_code == 200
    return re.search(r'name="csrf-token" content="([^"]+)"', response.text).group(1)


def post(client, token, path, body):
    return client.post(path, json=body, headers={'x-csrf-token': token})


def test_dashboard_demo_without_email_or_schedule(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        status = client.get('/api/status').json()
        assert status['settings']['smtp'] is None
        assert not status['settings']['schedule']['enabled']
        assert status['next_run'] is None
        response = post(client, token, '/api/runs', {'source': 'demo'})
        assert response.status_code == 202
        run_id = response.json()['run_id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = client.get('/api/runs/' + run_id).json()
            if run['result'] is not None or run['status'] == 'failed':
                break
        assert run['status'] != 'failed', run
        result = AnalysisResult.model_validate(run['result'])
        assert result.run_id == run_id
        assert result.metrics['known_value']
        assert run['delivery'] != 'sent'
        assert client.get('/api/status').json()['history'][0]['id'] == run_id
        html = client.get('/reports/' + run_id)
        plain = client.get('/reports/' + run_id + '?plain=true')
        assert html.status_code == plain.status_code == 200
        assert 'text/html' in html.headers['content-type']
        assert 'text/plain' in plain.headers['content-type']
        assert '<html' in html.text.lower()
        assert 'Portfolio' in plain.text
        for name in ('request', 'result', 'submission'):
            schema = client.get('/api/schema/' + name)
            assert schema.status_code == 200
            assert schema.json()['properties']


def test_import_preview_requires_confirmation(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        body = {'csv': 'symbol,quantity,price,asset_type\nABC,2,10,stock\nUSD,5,,cash', 'as_of': '2026-10-18T12:00:00Z'}
        preview = post(client, token, '/api/import', body)
        assert preview.status_code == 200
        assert preview.json()['source'] == 'csv'
        assert client.get('/api/status').json()['snapshot'] is None
        assert post(client, token, '/api/import', body | {'confirm': True}).status_code == 200
        assert client.get('/api/status').json()['snapshot']['cash'] == '5'


def test_ticker_price_csv_needs_quantity_before_saving(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        body = {'csv': 'ticker,price\nSPUS,40.50', 'as_of': '2026-10-18T12:00:00Z'}
        preview = post(client, token, '/api/import', body)
        assert preview.status_code == 200
        assert preview.json()['positions'][0]['quantity'] is None
        assert post(client, token, '/api/import', body | {'confirm': True}).status_code == 400
        assert client.get('/api/status').json()['snapshot'] is None
        draft = preview.json()
        draft['positions'][0]['quantity'] = '3'
        assert post(client, token, '/api/portfolio', {'snapshot': draft}).status_code == 200


def test_csrf_origin_and_host_restrictions(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        assert client.post('/api/runs', json={'source': 'demo'}).status_code == 403
        token = csrf(client)
        assert client.post('/api/runs', json={'source': 'demo'}, headers={'x-csrf-token': token, 'origin': 'https://evil.example'}).status_code == 403
        assert client.get('/api/status', headers={'host': 'evil.example'}).status_code == 400
        assert client.get('/').headers['x-content-type-options'] == 'nosniff'
        assert 'frame-ancestors' in client.get('/').headers['content-security-policy']


def test_server_requires_https_and_strong_owner_password(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='HTTPS'):
        create_app(tmp_path, server=True)
    monkeypatch.setenv('PORTFOLIO_PUBLIC_URL', 'https://portfolio.example')
    monkeypatch.setenv('PORTFOLIO_SECRET_OWNER_PASSWORD', 'short')
    with pytest.raises(ValueError, match='16'):
        create_app(tmp_path, server=True)


def test_server_owner_login_rotates_session_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv('PORTFOLIO_PUBLIC_URL', 'https://portfolio.example')
    password = 'owner-password-for-tests-only'
    monkeypatch.setenv('PORTFOLIO_SECRET_OWNER_PASSWORD', password)
    with TestClient(create_app(tmp_path, server=True), base_url='https://portfolio.example') as client:
        assert client.get('/api/status').status_code == 401
        token = csrf(client)
        initial_cookie = client.cookies.get('portfolio_session')
        assert post(client, token, '/api/login', {'password': 'wrong-password'}).status_code == 401
        assert post(client, token, '/api/login', {'password': password}).status_code == 200
        assert client.cookies.get('portfolio_session') != initial_cookie
        status = client.get('/api/status')
        assert status.status_code == 200
        assert status.json()['server'] is True
        assert password not in status.text
        assert status.headers['strict-transport-security']
        assert post(client, token, '/api/settings', {'report_length': 'detailed'}).status_code == 403
        assert post(client, csrf(client), '/api/settings', {'report_length': 'detailed'}).status_code == 200


def test_server_bearer_api_does_not_require_browser_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv('PORTFOLIO_PUBLIC_URL', 'https://portfolio.example')
    monkeypatch.setenv('PORTFOLIO_SECRET_OWNER_PASSWORD', 'owner-password-for-tests-only')
    monkeypatch.setenv('PORTFOLIO_SECRET_API_TOKEN', 'private-test-api-token')
    with TestClient(create_app(tmp_path, server=True), base_url='https://portfolio.example') as client:
        assert client.get('/api/status', headers={'authorization': 'Bearer wrong'}).status_code == 401
        headers = {'authorization': 'Bearer private-test-api-token'}
        assert client.get('/api/status', headers=headers).status_code == 200
        assert client.post('/api/runs', json={'source': 'demo'}, headers=headers).status_code == 202


def test_recipient_change_clears_verification_and_scheduled_email(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        smtp = {'host': 'smtp.example.test', 'sender': 'owner@example.test', 'recipient': 'owner@example.test'}
        assert post(client, token, '/api/settings', {'smtp': smtp}).status_code == 200
        app.state.service.store.set('verified_recipient', smtp['recipient'])
        assert post(client, token, '/api/settings', {'scheduled_email': True}).status_code == 200
        response = post(client, token, '/api/settings', {'smtp': smtp | {'recipient': 'changed@example.test'}})
        assert response.status_code == 200, response.text
        status = client.get('/api/status').json()
        assert status['verified_recipient'] is None
        assert not status['settings']['scheduled_email']


def test_secrets_validation_and_presence_never_echo_values(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        secret = 'private-provider-key-test'
        response = post(client, token, '/api/secrets/model_api_key', {'value': secret})
        assert response.status_code == 200
        assert secret not in response.text
        status = client.get('/api/status')
        assert status.json()['secrets_present']['model_api_key'] is True
        assert status.json()['secrets_editable']['model_api_key'] is True
        info = status.json()['secrets']['model_api_key']
        assert info['present'] is True and info['source'] == 'keyring' and info['provider'] == 'colgate'
        assert status.json()['secrets']['model_api_key_colgate']['present'] is True
        assert status.json()['secret_storage']['available'] in (True, False)
        assert secret not in status.text
        assert post(client, token, '/api/secrets/not_allowed', {'value': secret}).status_code == 404
        response = post(client, token, '/api/secrets/model_api_key', {'value': {'private': secret}})
        assert response.status_code == 400
        assert secret not in response.text
        response = post(client, token, '/api/runs', {'source': secret})
        assert response.status_code == 422
        assert secret not in response.text


@pytest.mark.parametrize('path', ['/api/login', '/api/secrets/model_api_key', '/api/import', '/api/email/verify'])
def test_json_object_endpoints_reject_arrays(tmp_path, path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000', raise_server_exceptions=False) as client:
        response = post(client, csrf(client), path, ['private-test-value'])
        assert response.status_code == 400, response.text
        assert 'private-test-value' not in response.text


def test_non_ascii_owner_password_supported(tmp_path, monkeypatch):
    monkeypatch.setenv('PORTFOLIO_PUBLIC_URL', 'https://portfolio.example')
    password = 'owner-caf\u00e9-password-for-tests'
    monkeypatch.setenv('PORTFOLIO_SECRET_OWNER_PASSWORD', password)
    with TestClient(create_app(tmp_path, server=True), base_url='https://portfolio.example', raise_server_exceptions=False) as client:
        response = post(client, csrf(client), '/api/login', {'password': password})
        assert response.status_code == 200, response.text


def test_research_token_can_read_research_cancel_but_cannot_mutate_or_email(tmp_path, monkeypatch):
    from portfolio_analyzer import service as service_module
    from portfolio_analyzer.config import Settings, save_settings
    from portfolio_analyzer.delivery import SMTPConfig
    monkeypatch.setenv('PORTFOLIO_PUBLIC_URL', 'https://portfolio.example')
    monkeypatch.setenv('PORTFOLIO_SECRET_OWNER_PASSWORD', 'owner-password-for-tests-only')
    monkeypatch.setenv('PORTFOLIO_SECRET_API_TOKEN', 'restricted-research-test-token')
    async def slow_analysis(*args):
        await asyncio.sleep(60)
        pytest.fail('test run should be cancelled')
    monkeypatch.setattr(service_module, 'analyze_portfolio', slow_analysis)
    smtp = SMTPConfig(host='smtp.example.test', sender='owner@example.test', recipient='owner@example.test')
    save_settings(tmp_path, Settings(smtp=smtp))
    app = create_app(tmp_path, server=True)
    app.state.service.store.set('verified_recipient', smtp.recipient)
    headers = {'authorization': 'Bearer restricted-research-test-token'}
    with TestClient(app, base_url='https://portfolio.example', headers=headers) as client:
        assert client.get('/api/status').status_code == 200
        assert client.get('/api/schema/request').status_code == 200
        response = client.post('/api/runs', json={'source': 'demo'})
        assert response.status_code == 202
        run_id = response.json()['run_id']
        assert client.get('/api/runs/' + run_id).status_code == 200
        for path in ('/api/settings', '/api/portfolio', '/api/import', '/api/import/image',
                     '/api/runs/' + run_id + '/send', '/api/email/verification', '/api/email/verify',
                     '/api/secrets/model_api_key'):
            assert client.post(path, json={}).status_code == 403, path
        assert client.post('/api/runs', json={'source': 'demo', 'send_email': True}).status_code == 403
        assert client.post('/api/runs/' + run_id + '/cancel', json={}).status_code == 200
        assert client.get('/api/runs/' + run_id).json()['status'] == 'failed'


def test_model_connection_probe_uses_real_portfolio_metrics(tmp_path, monkeypatch):
    from portfolio_analyzer import agent, web
    from portfolio_analyzer.models import ResearchNarrative
    marker = object()
    monkeypatch.setattr(web, 'create_model', lambda *args: marker)
    calls = []
    async def generate(model, request, metrics, sources):
        assert model is marker
        assert metrics['position_values']
        assert set(metrics['position_values']) == {p.symbol for p in request.snapshot.positions}
        assert float(metrics['known_value']) > 0
        assert sources == []
        calls.append(request)
        return ResearchNarrative()
    monkeypatch.setattr(agent, 'generate_narrative', generate)
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        response = post(client, csrf(client), '/api/test/model', {})
        assert response.status_code == 200, response.text
        assert len(calls) == 1
        assert client.get('/api/status').json()['connection_tests']['model']['status'] == 'passed'
        assert post(client, csrf(client), '/api/settings', {'model_enabled': True}).status_code == 200
        assert 'model' not in client.get('/api/status').json()['connection_tests']


@pytest.mark.parametrize('method,path', [
    ('POST', '/api/broker/connect'), ('POST', '/api/broker/disconnect'),
    ('POST', '/api/broker/refresh'), ('GET', '/api/broker/accounts'),
    ('GET', '/api/broker/status'), ('POST', '/api/broker/input'),
    ('POST', '/api/broker/cancel'),
])
def test_brokerage_endpoints_are_removed(tmp_path, method, path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        response = client.request(method, path, headers={'x-csrf-token': token})
        assert response.status_code == 404
        assert 'broker' not in client.get('/api/status').json()
        assert 'id="broker-form"' not in client.get('/').text


def test_delete_finished_report_owner_only(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        run_id = post(client, token, '/api/runs', {'source': 'demo'}).json()['run_id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = client.get('/api/runs/' + run_id).json()
            if row['result'] is not None or row['status'] == 'failed':
                break
        assert client.delete('/api/runs/' + run_id, headers={'x-csrf-token': token}).status_code == 200
        assert client.get('/api/runs/' + run_id).status_code == 404
        # An unknown report and an unauthenticated delete are refused.
        assert client.delete('/api/runs/missing', headers={'x-csrf-token': token}).status_code == 404
        assert client.delete('/api/runs/' + run_id).status_code == 403


def test_model_key_delete_targets_the_effective_legacy_slot(tmp_path, isolated_credentials):
    # A key migrated to the legacy shared slot must be removable from the dashboard, not
    # redirected to an empty per-provider slot.
    values = isolated_credentials
    values['model_api_key'] = 'legacy-key-123'
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        assert client.get('/api/status').json()['secrets']['model_api_key']['present'] is True
        response = client.delete('/api/secrets/model_api_key', headers={'x-csrf-token': token})
        assert response.status_code == 200, response.text
        assert 'model_api_key' not in values
        assert client.get('/api/status').json()['secrets']['model_api_key']['present'] is False


def test_delete_run_refuses_active_and_removes_finished(tmp_path):
    from portfolio_analyzer.storage import Store
    store = Store(tmp_path)
    assert store.create_run('active-run')
    assert store.delete_run('active-run') is False
    store.update_run('active-run', status='failed')
    assert store.delete_run('active-run') is True
    assert store.run('active-run') is None


def test_safe_error_reason_maps_status_and_redacts():
    from portfolio_analyzer.web import safe_error_reason

    class Unauthorized(Exception):
        status_code = 401

    assert '401' in safe_error_reason(Unauthorized('nope'))
    redacted = safe_error_reason(Exception('bad key secret-abc'), 'secret-abc')
    assert 'secret-abc' not in redacted
