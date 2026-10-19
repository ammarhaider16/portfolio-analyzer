"""Credential storage. There is intentionally no plaintext fallback."""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path


class SecretStorageError(RuntimeError):
    pass


class SecretStore:
    # The OS keyring can be expensive to query and is polled by the dashboard.
    # Presence is memoized briefly so a 4-second status poll does not hammer it.
    CACHE_SECONDS = 5.0

    def __init__(self, data_dir: Path, server: bool = False):
        self.data_dir = Path(data_dir)
        self.server = server
        self.service = 'portfolio-analyzer:' + str(self.data_dir.resolve())
        self._cache: dict[str, str | None] = {}
        self._cache_at = 0.0
        self._metadata_path = self.data_dir / 'secret_metadata.json'

    @staticmethod
    def _key(name: str) -> str:
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]*', name):
            raise ValueError('Invalid secret name')
        return 'PORTFOLIO_SECRET_' + name.upper()

    def get(self, name: str) -> str | None:
        key = self._key(name)
        if key in os.environ:
            return os.environ[key]
        if key + '_FILE' in os.environ:
            path = Path(os.environ[key + '_FILE'])
            if not path.is_absolute():
                raise SecretStorageError('Mounted secret paths must be absolute')
            try:
                if path.stat().st_size > 1_048_576:
                    raise SecretStorageError('Mounted secret exceeds size limit')
                return path.read_text(encoding='utf-8').rstrip('\r\n')
            except OSError:
                raise SecretStorageError('Unable to read mounted secret') from None
        if self.server:
            return None
        return self._keyring_get(name)

    def _keyring_get(self, name: str) -> str | None:
        if not self._cache or (time.monotonic() - self._cache_at) >= self.CACHE_SECONDS:
            self._cache = {}
            self._cache_at = time.monotonic()
        if name not in self._cache:
            try:
                import keyring
                self._check_backend(keyring)
                self._cache[name] = keyring.get_password(self.service, name)
            except Exception:
                raise SecretStorageError('OS credential storage unavailable; configure a mounted secret or environment variable') from None
        return self._cache[name]

    def set(self, name: str, value: str) -> None:
        key = self._key(name)
        value = value.strip()
        if self.server or key in os.environ or key + '_FILE' in os.environ:
            raise SecretStorageError('Externally managed secrets must be changed in the deployment environment')
        if not value:
            raise SecretStorageError('Enter a credential')
        try:
            import keyring
            self._check_backend(keyring)
            keyring.set_password(self.service, name, value)
        except SecretStorageError:
            raise
        except Exception:
            raise SecretStorageError('OS credential storage unavailable; secret was not saved') from None
        self._cache = {}
        self.record_saved(name)

    def delete(self, name: str) -> None:
        key = self._key(name)
        if self.server or key in os.environ or key + '_FILE' in os.environ:
            raise SecretStorageError('Externally managed secrets must be removed in the deployment environment')
        try:
            import keyring
            self._check_backend(keyring)
            if keyring.get_password(self.service, name) is not None:
                keyring.delete_password(self.service, name)
        except SecretStorageError:
            raise
        except Exception:
            raise SecretStorageError('Unable to remove secret from OS credential storage') from None
        self._cache = {}
        self.clear_saved(name)

    # Nonsecret bookkeeping: when a keyring value was stored. Never the value itself.
    def saved_at(self, name: str) -> str | None:
        entry = self._metadata().get(name)
        return entry.get('saved_at') if isinstance(entry, dict) else None

    def record_saved(self, name: str, when: str | None = None) -> None:
        data = self._metadata()
        data[name] = {'saved_at': when or datetime.now(timezone.utc).isoformat()}
        self._write_metadata(data)

    def clear_saved(self, name: str) -> None:
        data = self._metadata()
        if data.pop(name, None) is not None:
            self._write_metadata(data)

    def storage_status(self) -> dict:
        """Report whether keyring storage can be used, with a user-facing reason."""
        if self.server:
            return {'available': False, 'backend': 'deployment',
                    'reason': 'Server mode reads secrets from the deployment environment or mounted files, so the dashboard is read-only.'}
        try:
            import keyring
            self._check_backend(keyring)
            return {'available': True, 'backend': type(keyring.get_keyring()).__name__, 'reason': None}
        except Exception:
            return {'available': False, 'backend': 'unavailable',
                    'reason': 'OS credential storage is unavailable. Set PORTFOLIO_SECRET_<NAME> or an absolute PORTFOLIO_SECRET_<NAME>_FILE path for each credential.'}

    def _metadata(self) -> dict:
        try:
            data = json.loads(self._metadata_path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_metadata(self, data: dict) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temp = self._metadata_path.with_suffix('.tmp')
            temp.write_text(json.dumps(data), encoding='utf-8')
            temp.replace(self._metadata_path)
        except OSError:
            # Metadata is cosmetic; never fail a credential operation because of it.
            pass

    @staticmethod
    def _check_backend(keyring):
        backend = keyring.get_keyring()
        # Explicitly reject plaintext, encrypted-file and automatic chain backends.
        # A chain may silently route to a plaintext alternative.
        module = type(backend).__module__
        allowed = ('keyring.backends.Windows', 'keyring.backends.macOS',
                   'keyring.backends.SecretService', 'keyring.backends.libsecret',
                   'keyring.backends.kwallet')
        if not module.startswith(allowed):
            raise SecretStorageError('Select an OS credential backend or use deployment secrets')
