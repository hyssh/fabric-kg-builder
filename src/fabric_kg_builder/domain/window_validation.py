"""Private, command-local reuse of fully replayed window authority."""

import hashlib
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel


def _fingerprint(value):
    """Type-sensitive content digest; deliberately do not normalize Unicode."""
    digest = hashlib.sha256()

    def atom(tag, payload):
        digest.update(tag)
        digest.update(str(len(payload)).encode("ascii") + b":")
        digest.update(payload)

    def visit(item):
        if isinstance(item, BaseModel):
            visit(item.model_dump(mode="python"))
        elif isinstance(item, Enum):
            atom(b"E", (type(item).__module__ + "." + type(item).__qualname__).encode())
            visit(item.value)
        elif isinstance(item, Mapping):
            atom(b"D", str(len(item)).encode())
            if any(not isinstance(key, str) for key in item):
                raise ValueError("Window validation mappings require string keys")
            for key in sorted(item):
                visit(key)
                visit(item[key])
        elif isinstance(item, (list, tuple)):
            atom(b"L" if isinstance(item, list) else b"T", str(len(item)).encode())
            for child in item:
                visit(child)
        elif isinstance(item, str):
            atom(b"S", item.encode("utf-8"))
        elif item is None:
            atom(b"N", b"")
        elif isinstance(item, bool):
            atom(b"B", str(item).encode())
        elif isinstance(item, int):
            atom(b"I", str(item).encode())
        elif isinstance(item, float):
            atom(b"F", item.hex().encode())
        elif isinstance(item, datetime):
            atom(b"Z", item.isoformat().encode())
        elif isinstance(item, date):
            atom(b"A", item.isoformat().encode())
        elif isinstance(item, Decimal):
            atom(b"C", str(item).encode())
        elif isinstance(item, bytes):
            atom(b"Y", item)
        else:
            raise ValueError(f"Unsupported window validation input type: {type(item).__name__}")

    visit(value)
    return digest.hexdigest()


class WindowValidationOperation:
    """Never persisted/shared globally; every reuse rechecks content and sources."""

    def __init__(self):
        self._run = None
        self._run_fingerprint = None
        self._inputs = {}
        self._acceptances = set()

    def hydrate(self, value, handler, *, mode="python"):
        from .window_run import WindowedRun, _verify_chunks, _verify_prepared

        token = _fingerprint(value)
        current = _fingerprint(self._run) if self._run is not None else None
        if (
            self._run is not None and current == self._run_fingerprint
            and token in self._inputs.get(mode, set())
            and isinstance(value, (WindowedRun, dict))
        ):
            _verify_prepared(self._run.prepared)
            return self._run
        self._run = None
        self._inputs = {}
        self._acceptances.clear()
        checked = handler(value.model_dump(mode="python") if isinstance(value, WindowedRun) else value)
        _verify_chunks(checked.prepared, checked.chunk_plan)
        _verify_prepared(checked.prepared)
        self._run = checked
        self._run_fingerprint = _fingerprint(checked)
        self._inputs = {
            "python": {self._run_fingerprint},
            "json": {_fingerprint(checked.model_dump(mode="json"))},
        }
        self._inputs.setdefault(mode, set()).add(token)
        return checked

    def run(self, value):
        from .window_run import WindowedRun

        return self.hydrate(value, WindowedRun.model_validate)

    def acceptance_validated(self, acceptance, run):
        self.run(run)
        return (self._run_fingerprint, type(acceptance), _fingerprint(acceptance)) in self._acceptances

    def remember_acceptance(self, acceptance, run):
        self.run(run)
        self._acceptances.add((self._run_fingerprint, type(acceptance), _fingerprint(acceptance)))


def validation_context(operation):
    return {"window_validation_operation": operation}


def operation_from_info(info):
    context = info.context
    operation = context.get("window_validation_operation") if isinstance(context, dict) else None
    return operation if type(operation) is WindowValidationOperation else None
