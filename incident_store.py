#!/usr/bin/env python3
"""Bounded, crash-resistant storage for system-tool guard."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


MAX_SAMPLE_BYTES = 20 * 1024 * 1024


def default_state_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "system-tool"


class IncidentStore:
    def __init__(self, root: Path | None = None, max_sample_bytes: int = MAX_SAMPLE_BYTES) -> None:
        self.root = (root or default_state_root()).expanduser().resolve()
        self.incident_dir = self.root / "incidents"
        self.sample_path = self.root / "samples.jsonl"
        self.max_sample_bytes = max_sample_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self.incident_dir.mkdir(parents=True, exist_ok=True)

    def _atomic_json(self, path: Path, payload: dict[str, Any]) -> None:
        path = path.resolve()
        if self.root != path.parent and self.root not in path.parents:
            raise ValueError("state path escaped root")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def heartbeat(self, payload: dict[str, Any]) -> None:
        self._atomic_json(self.root / "heartbeat.json", payload)

    def mark_clean_exit(self, payload: dict[str, Any]) -> None:
        self._atomic_json(self.root / "clean-exit.json", payload)

    def read_marker(self, name: str) -> dict[str, Any] | None:
        if name not in {"heartbeat.json", "clean-exit.json"}:
            raise ValueError("unsupported marker")
        try:
            value = json.loads((self.root / name).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def append_sample(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        if self.sample_path.exists() and self.sample_path.stat().st_size + len(encoded.encode()) > self.max_sample_bytes:
            previous = self.sample_path.with_suffix(".jsonl.1")
            if previous.exists():
                previous.unlink()
            self.sample_path.replace(previous)
        with self.sample_path.open("a", encoding="utf-8") as handle:
            handle.write(encoded)

    def write_incident(self, payload: dict[str, Any]) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        kind = str(payload.get("kind", "incident"))
        safe_kind = "".join(char if char.isalnum() or char in "-_" else "_" for char in kind)[:40]
        path = self.incident_dir / f"{stamp}-{safe_kind}.json"
        counter = 1
        while path.exists():
            path = self.incident_dir / f"{stamp}-{safe_kind}-{counter}.json"
            counter += 1
        self._atomic_json(path, payload)
        return path

    def recent_incidents(self, limit: int = 20) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self.incident_dir.glob("*.json"), reverse=True)[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data["report_path"] = str(path)
                rows.append(data)
            except (OSError, ValueError, TypeError):
                continue
        return rows
