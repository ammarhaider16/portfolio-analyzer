"""Credential storage. There is intentionally no plaintext fallback."""
import os
import re
from pathlib import Path


class SecretStorageError(RuntimeError):
    pass


class SecretStore:
    def __init__(self, data_dir: Path, server: bool = False):
        self.data_dir = Path(data_dir)
        self.server = server
        self.service = 'portfolio-analyzer:' + str(self.data_dir.resolve())

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
        try:
            import keyring
            self._check_backend(keyring)
            return keyring.get_password(self.service, name)
        except Exception:
            raise SecretStorageError('OS credential storage unavailable; configure a mounted secret or environment variable') from None

    def set(self, name: str, value: str) -> None:
        key = self._key(name)
        if self.server or key in os.environ or key + '_FILE' in os.environ:
            raise SecretStorageError('Externally managed secrets must be changed in the deployment environment')
        try:
            import keyring
            self._check_backend(keyring)
            keyring.set_password(self.service, name, value)
        except Exception:
            raise SecretStorageError('OS credential storage unavailable; secret was not saved') from None

    def delete(self, name: str) -> None:
        key = self._key(name)
        if self.server or key in os.environ or key + '_FILE' in os.environ:
            raise SecretStorageError('Externally managed secrets must be removed in the deployment environment')
        try:
            import keyring
            self._check_backend(keyring)
            if keyring.get_password(self.service, name) is not None:
                keyring.delete_password(self.service, name)
        except Exception:
            raise SecretStorageError('Unable to remove secret from OS credential storage') from None

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
