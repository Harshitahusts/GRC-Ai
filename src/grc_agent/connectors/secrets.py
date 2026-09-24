"""Encrypts connector credentials at rest with a key kept outside the database."""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> SecretBox:
        """Key from GRC_CONNECTOR_KEY, or a key file created next to the database."""
        if env := os.getenv("GRC_CONNECTOR_KEY"):
            return cls(env.encode())
        path = data_dir / "connector_key"
        if not path.exists():
            path.write_bytes(Fernet.generate_key())
            path.chmod(0o600)
        return cls(path.read_bytes().strip())

    def seal(self, secrets: dict[str, str]) -> str:
        return self._fernet.encrypt(json.dumps(secrets).encode()).decode()

    def open(self, token: str) -> dict[str, str]:
        try:
            return json.loads(self._fernet.decrypt(token.encode()))
        except InvalidToken as exc:
            raise ValueError(
                "Stored credentials can't be decrypted (the connector key changed). "
                "Remove this connection and add it again."
            ) from exc


def mask(value: str) -> str:
    return "••••" + value[-4:] if len(value) > 8 else "••••"
