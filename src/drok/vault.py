"""Encrypted secrets. Agents only ever see {{secret:NAME}} placeholders; values are swapped in at execution
time and scrubbed from anything that flows back into a transcript."""

import re

from cryptography.fernet import Fernet

from .db import DB, now

PLACEHOLDER = re.compile(r"\{\{secret:([A-Za-z0-9_\-]+)\}\}")


class Vault:
    def __init__(self, db: DB, key: str):
        self.db = db
        self.fernet = Fernet(key.encode())

    def set(self, name: str, value: str, description: str = "") -> None:
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
            raise ValueError("Secret names may only contain letters, digits, _ and -")
        enc = self.fernet.encrypt(value.encode()).decode()
        self.db.run(
            "INSERT INTO secrets (name, value, description, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value, description = excluded.description",
            name, enc, description, now(),
        )

    def delete(self, name: str) -> None:
        self.db.run("DELETE FROM secrets WHERE name = ?", name)

    def names(self) -> list[dict]:
        """Secrets agents may reference. Names starting with '_' are managed by Drok itself (e.g. OAuth tokens)."""
        return self.db.all("SELECT name, description, created_at FROM secrets WHERE name NOT LIKE '\\_%' ESCAPE '\\' "
                           "ORDER BY name")

    def get(self, name: str) -> str | None:
        row = self.db.one("SELECT value FROM secrets WHERE name = ?", name)
        return self.fernet.decrypt(row["value"].encode()).decode() if row else None

    def _values(self) -> dict[str, str]:
        return {
            r["name"]: self.fernet.decrypt(r["value"].encode()).decode()
            for r in self.db.all("SELECT name, value FROM secrets")
        }

    def fill(self, text: str) -> str:
        """Replace placeholders with real values. Unknown names are left as-is so the agent sees the miss."""
        if "{{secret:" not in text:
            return text
        values = self._values()
        return PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)

    def redact(self, text: str) -> str:
        values = self._values()
        # Longest first so a secret that contains another is fully masked.
        for name, value in sorted(values.items(), key=lambda kv: -len(kv[1])):
            if len(value) >= 4:
                text = text.replace(value, f"{{{{secret:{name}}}}}")
        return text
