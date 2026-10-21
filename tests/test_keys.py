"""Phase 3 acceptance tests: per-provider keys, delete, test-without-save, status metadata."""
import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

from betafolio import agent, web
from betafolio.config import ModelConfig, Settings, save_settings
from betafolio.service import create_model
from betafolio.web import create_app


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def get_keyring(self):
        return type('Backend', (), {'__module__': 'keyring.backends.SecretService'})()

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.values[(service, name)] = value

    def delete_password(self, service, name):
        self.values.pop((service, name), None)


@pytest.fixture(autouse=True)
def credentials(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith('PORTFOLIO_') or key in {'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'LITELLM_API_KEY', 'TAVILY_API_KEY'}:
            monkeypatch.delenv(key)
    monkeypatch.setitem(sys.modules, 'keyring', FakeKeyring())


def csrf(client):
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get('/').text).group(1)


def post(client, token, path, body):
    return client.post(path, json=body, headers={'x-csrf-token': token})


def openai_settings():
    return Settings(model=ModelConfig(provider='openai', model='gpt-4o-mini', base_url='https://api.openai.com/v1'))


def test_status_secrets_and_storage_shape(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        status = client.get('/api/status').json()
        secrets = status['secrets']
        for name in ('model_api_key', 'model_api_key_openai', 'model_api_key_anthropic',
                     'model_api_key_custom', 'tavily_api_key', 'smtp_password'):
            assert set(secrets[name]) == {'present', 'source', 'saved_at', 'provider', 'editable', 'managed_by', 'env'}
        assert secrets['model_api_key_openai']['provider'] == 'openai'
        assert secrets['tavily_api_key']['provider'] is None
        assert secrets['model_api_key']['provider'] == 'custom'
        assert set(status['secret_storage']) >= {'available', 'backend', 'reason'}
        assert status['secret_storage']['available'] is True
        assert status['secrets_present']['model_api_key'] is False
        assert status['secrets_editable']['model_api_key'] is True


def test_save_records_saved_at_and_trims(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        response = post(client, token, '/api/secrets/tavily_api_key', {'value': '  padded-key  '})
        assert response.status_code == 200, response.text
        info = client.get('/api/status').json()['secrets']['tavily_api_key']
        assert info['present'] is True and info['source'] == 'keyring' and info['saved_at']
        assert client.app.state.service.secrets.get('tavily_api_key') == 'padded-key'


def test_whitespace_only_secret_is_rejected(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        response = post(client, token, '/api/secrets/tavily_api_key', {'value': '   '})
        assert response.status_code == 400
        assert client.get('/api/status').json()['secrets_present']['tavily_api_key'] is False


def test_delete_route_removes_key_and_metadata(tmp_path):
    save_settings(tmp_path, openai_settings())
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        assert post(client, token, '/api/secrets/model_api_key_openai', {'value': 'openai-secret'}).status_code == 200
        info = client.get('/api/status').json()['secrets']['model_api_key_openai']
        assert info['present'] is True and info['saved_at']
        response = client.request('DELETE', '/api/secrets/model_api_key_openai', headers={'x-csrf-token': token})
        assert response.status_code == 200, response.text
        info = client.get('/api/status').json()['secrets']['model_api_key_openai']
        assert info['present'] is False and info['saved_at'] is None


def test_delete_query_convention_and_saved_key_slot(tmp_path):
    save_settings(tmp_path, openai_settings())
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        # A bare model_api_key targets the currently selected provider's own slot.
        assert post(client, token, '/api/secrets/model_api_key', {'value': 'openai-secret'}).status_code == 200
        secrets = client.get('/api/status').json()['secrets']
        assert secrets['model_api_key_openai']['present'] is True
        assert secrets['model_api_key_custom']['present'] is False
        response = client.request('DELETE', '/api/secrets?name=model_api_key', headers={'x-csrf-token': token})
        assert response.status_code == 200, response.text
        assert client.get('/api/status').json()['secrets']['model_api_key_openai']['present'] is False


def test_switching_provider_does_not_reuse_other_key(tmp_path):
    save_settings(tmp_path, openai_settings())
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        assert post(client, token, '/api/secrets/model_api_key_openai', {'value': 'openai-secret'}).status_code == 200
        response = post(client, token, '/api/settings', {'model': {'provider': 'anthropic', 'model': 'claude', 'base_url': 'https://api.anthropic.com'}})
        assert response.status_code == 200, response.text
        secrets = client.get('/api/status').json()['secrets']
        assert secrets['model_api_key_openai']['present'] is True
        assert secrets['model_api_key']['provider'] == 'anthropic'
        assert secrets['model_api_key']['present'] is False
        service = client.app.state.service
        with pytest.raises(ValueError, match='model API key'):
            create_model(service.settings, service.secrets)


def test_provider_alias_does_not_lock_unrelated_slot(tmp_path, monkeypatch):
    save_settings(tmp_path, Settings(model=ModelConfig(provider='anthropic', model='claude', base_url='https://api.anthropic.com')))
    monkeypatch.setenv('OPENAI_API_KEY', 'openai-env-secret')
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        secrets = client.get('/api/status').json()['secrets']
        # A convenience alias is an overridable fallback, not a lock.
        assert secrets['model_api_key_openai']['editable'] is True
        assert secrets['model_api_key_openai']['source'] == 'alias'
        assert secrets['model_api_key_openai']['managed_by'] == 'OPENAI_API_KEY'
        assert secrets['model_api_key_anthropic']['editable'] is True
        assert secrets['model_api_key']['provider'] == 'anthropic'
        assert secrets['model_api_key']['editable'] is True
        assert secrets['model_api_key']['present'] is False


def test_deployment_managed_slot_rejects_edit_without_echo(tmp_path, monkeypatch):
    save_settings(tmp_path, openai_settings())
    monkeypatch.setenv('PORTFOLIO_SECRET_MODEL_API_KEY_OPENAI', 'openai-env-secret')
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        response = post(client, token, '/api/secrets/model_api_key', {'value': 'replacement-secret'})
        assert response.status_code == 400
        assert 'environment' in response.json()['detail']
        assert 'openai-env-secret' not in response.text
        assert 'replacement-secret' not in response.text


def test_test_endpoint_uses_value_without_persisting(tmp_path, monkeypatch):
    captured = {}

    def fake_create(settings, store, api_key=None):
        captured['key'] = api_key
        return object()

    async def fake_generate(model, request, metrics, sources, **kwargs):
        from betafolio.models import ResearchNarrative
        return ResearchNarrative(executive_summary="Ready")

    monkeypatch.setattr(web, 'create_model', fake_create)
    monkeypatch.setattr(agent, 'generate_narrative', fake_generate)
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        secret = 'typed-but-never-saved'
        response = post(client, token, '/api/test/model', {'value': secret})
        assert response.status_code == 200, response.text
        assert captured['key'] == secret
        assert secret not in response.text
        status = client.get('/api/status')
        assert status.json()['secrets_present']['model_api_key'] is False
        assert secret not in status.text
        assert client.app.state.service.secrets.get('model_api_key') is None


def test_test_endpoint_rejects_whitespace_value(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        response = post(client, csrf(client), '/api/test/model', {'value': '   '})
        assert response.status_code == 400
