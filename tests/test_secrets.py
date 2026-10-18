import sys
from types import SimpleNamespace
import pytest
from portfolio_analyzer.secrets import SecretStore, SecretStorageError


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
