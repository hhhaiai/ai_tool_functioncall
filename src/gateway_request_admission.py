"""Compatibility boundary for HTTP request-admission acquisition."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from .gateway_admission import ADMISSION_SERVICE, AdmissionLease
from .gateway_config import _gateway_config


def acquire_request_slot() -> AdmissionLease:
    return ADMISSION_SERVICE.acquire(_gateway_config())


@contextmanager
def request_slot_scope() -> Iterator[None]:
    lease = acquire_request_slot()
    try:
        yield
    finally:
        lease.release()


# Backward-compatible names re-exported by gateway_tool_runtime/gateway_app.
_acquire_request_slot = acquire_request_slot
_request_slot_scope = request_slot_scope


__all__ = [
    "_acquire_request_slot",
    "_request_slot_scope",
    "acquire_request_slot",
    "request_slot_scope",
]
