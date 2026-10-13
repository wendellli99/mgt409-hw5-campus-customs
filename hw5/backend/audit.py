"""Append-only JSON audit: flock, atomic replace, complete events, secret redaction."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
AUDIT_PATH = ROOT / 'output' / 'audit_trail.json'
SECRET_KEYS = {'authorization', 'api_key', 'portkey_api_key', 'approval_token', 'human_approval_token'}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def serializable(value: Any) -> Any:
    if hasattr(value, 'model_dump'):
        return serializable(value.model_dump(mode='json'))
    if is_dataclass(value):
        return serializable(asdict(value))
    if isinstance(value, dict):
        return {str(key): '[REDACTED]' if str(key).lower() in SECRET_KEYS else serializable(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def append_event(*, run_id: str, ticket_id: int | None, agent: str, event_type: str,
                 message: str, audit_path: Path | None = None, **details: Any) -> dict[str, Any]:
    path = audit_path or AUDIT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    record = serializable({'event_id': uuid.uuid4().hex, 'run_id': run_id,
                           'ticket_id': ticket_id, 'agent': agent,
                           'event_type': event_type, 'message': message,
                           'timestamp': now(), 'model': 'gpt-6-luna',
                           'tokens': {}, **details})
    lock_path = path.with_suffix(path.suffix + '.lock')
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Fail clearly if earlier evidence is invalid rather than overwriting it.
        previous = json.loads(path.read_text()) if path.exists() else []
        if not isinstance(previous, list):
            raise ValueError('The audit trail must be a JSON list; existing evidence was preserved.')
        previous.append(record)
        fd, temp_name = tempfile.mkstemp(prefix='.audit-', suffix='.json', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as temp:
                json.dump(previous, temp, indent=2, ensure_ascii=False)
                temp.write('\n')
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        fcntl.flock(lock, fcntl.LOCK_UN)
    return record


def read_events(ticket_id: int | None = None, run_id: str | None = None,
                limit: int = 300, audit_path: Path | None = None) -> list[dict[str, Any]]:
    path = audit_path or AUDIT_PATH
    if not path.exists():
        return []
    records = json.loads(path.read_text())
    if not isinstance(records, list):
        raise ValueError('Audit trail is not a JSON list.')
    return [row for row in records
            if (ticket_id is None or row.get('ticket_id') == ticket_id)
            and (run_id is None or row.get('run_id') == run_id)][-limit:]
