import os
import sys
from types import SimpleNamespace
import pytest
from portfolio_analyzer.config import ModelConfig, Settings
from portfolio_analyzer.secrets import SecretStore, SecretStorageError
from portfolio_analyzer.service import MODEL_PROVIDER_SLOTS, read_secret, resolve_model_key, resolve_secret


class FakeKeyring:
    """In-memory stand-in for a supported OS keyring backend."""

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


@pytest.fixture
def keyring_store(tmp_path, monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setitem(sys.modules, 'keyring', fake)
    for key in list(os.environ):
        if key.startswith('PORTFOLIO_SECRET_') or key in {'LITELLM_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'TAVILY_API_KEY', 'ALPHA_VANTAGE_API_KEY'}:
            monkeypatch.delenv(key)
    return SecretStore(tmp_path), fake


def test_environment_and_mount(tmp_path, monkeypatch):
    store = SecretStore(tmp_path, server=True)
    monkeypatch.setenv('PORTFOLIO_SECRET_API', 'hidden')
    assert store.get('api') == 'hidden'
    with pytest.raises(SecretStorageError):
        store.set('api', 'new')
    secret = tmp_path / 'mounted'
    secret.write_text('mounted-secret\n')
    monkeypatch.setenv('PORTFOLIO_SECRET_OTHER_FILE', str(secret))
    assert store.get('other') == 'mounted-secret'
    assert store.get('absent') is None


def test_no_plaintext_fallback(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError('backend secret error')
    monkeypatch.setitem(sys.modules, 'keyring', SimpleNamespace(set_password=fail))
    with pytest.raises(SecretStorageError, match='not saved'):
        SecretStore(tmp_path).set('api', 'sensitive')
    assert list(tmp_path.iterdir()) == []


def test_plaintext_keyring_rejected(tmp_path, monkeypatch):
    Plaintext = type('Plaintext', (), {'__module__': 'keyrings.alt.file'})
    monkeypatch.setitem(sys.modules, 'keyring', SimpleNamespace(get_keyring=lambda: Plaintext(), get_password=lambda *a: pytest.fail('must reject backend')))
    with pytest.raises(SecretStorageError, match='unavailable'):
        SecretStore(tmp_path).get('private')


def test_relative_mount_and_secret_name_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('PORTFOLIO_SECRET_API_FILE', 'relative-path')
    with pytest.raises(SecretStorageError, match='absolute'):
        SecretStore(tmp_path, server=True).get('api')
    with pytest.raises(ValueError, match='name'):
        SecretStore(tmp_path).get('../private')


def test_server_never_writes_or_falls_back(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'keyring', SimpleNamespace(get_keyring=lambda: pytest.fail('server must not use keyring')))
    store = SecretStore(tmp_path, server=True)
    assert store.get('missing') is None
    for method, args in [(store.set, ('missing', 'value')), (store.delete, ('missing',))]:
        with pytest.raises(SecretStorageError, match='Externally managed'):
            method(*args)


def test_per_provider_slots_resolve_independently(keyring_store):
    store, _ = keyring_store
    store.set('model_api_key_openai', 'openai-secret')
    openai = Settings(model=ModelConfig(provider='openai', model='gpt', base_url='https://api.openai.com/v1'))
    anthropic = Settings(model=ModelConfig(provider='anthropic', model='claude', base_url='https://api.anthropic.com'))
    assert resolve_model_key(openai, store) == ('openai-secret', 'keyring', MODEL_PROVIDER_SLOTS['openai'])
    assert resolve_model_key(anthropic, store)[0] is None


def test_legacy_model_slot_remains_readable(keyring_store):
    store, _ = keyring_store
    store.set('model_api_key', 'legacy-secret')
    settings = Settings(model=ModelConfig(provider='openai', model='gpt', base_url='https://api.openai.com/v1'))
    assert resolve_model_key(settings, store) == ('legacy-secret', 'keyring', 'model_api_key')
    assert read_secret(store, 'model_api_key') == 'legacy-secret'


def test_keyring_overrides_provider_alias(keyring_store, monkeypatch):
    store, _ = keyring_store
    store.set('model_api_key_openai', 'stored-secret')
    monkeypatch.setenv('OPENAI_API_KEY', 'env-secret')
    # A dashboard-saved key wins so an ambient alias cannot silently shadow it.
    assert resolve_secret(store, 'model_api_key_openai', ('OPENAI_API_KEY',)) == ('stored-secret', 'keyring')


def test_provider_alias_is_a_fallback_when_nothing_is_saved(keyring_store, monkeypatch):
    store, _ = keyring_store
    monkeypatch.setenv('OPENAI_API_KEY', 'env-secret')
    assert resolve_secret(store, 'model_api_key_openai', ('OPENAI_API_KEY',)) == ('env-secret', 'alias')


def test_keyring_used_only_when_no_environment(keyring_store):
    store, _ = keyring_store
    store.set('tavily_api_key', 'stored-tavily')
    assert resolve_secret(store, 'tavily_api_key', ('TAVILY_API_KEY',)) == ('stored-tavily', 'keyring')


def test_canonical_environment_beats_provider_alias(keyring_store, monkeypatch):
    store, _ = keyring_store
    monkeypatch.setenv('PORTFOLIO_SECRET_MODEL_API_KEY_OPENAI', 'canonical-secret')
    monkeypatch.setenv('OPENAI_API_KEY', 'alias-secret')
    assert resolve_secret(store, 'model_api_key_openai', ('OPENAI_API_KEY',)) == ('canonical-secret', 'env')


def test_set_trims_and_rejects_whitespace(keyring_store):
    store, _ = keyring_store
    store.set('tavily_api_key', '  padded  ')
    assert store.get('tavily_api_key') == 'padded'
    with pytest.raises(SecretStorageError, match='credential'):
        store.set('tavily_api_key', '   ')


def test_saved_at_metadata_is_nonsecret_and_cleared_on_delete(keyring_store):
    store, _ = keyring_store
    store.set('tavily_api_key', 'value')
    assert store.saved_at('tavily_api_key')
    assert 'value' not in (store._metadata_path.read_text(encoding='utf-8'))
    store.delete('tavily_api_key')
    assert store.get('tavily_api_key') is None
    assert store.saved_at('tavily_api_key') is None


def test_storage_status_reports_available_backend(keyring_store):
    store, _ = keyring_store
    assert store.storage_status() == {'available': True, 'backend': 'Backend', 'reason': None}


def test_storage_status_reports_server_and_plaintext(tmp_path, monkeypatch):
    assert SecretStore(tmp_path, server=True).storage_status()['available'] is False
    Plaintext = type('Plaintext', (), {'__module__': 'keyrings.alt.file'})
    monkeypatch.setitem(sys.modules, 'keyring', SimpleNamespace(get_keyring=lambda: Plaintext()))
    status = SecretStore(tmp_path).storage_status()
    assert status['available'] is False and status['reason']
