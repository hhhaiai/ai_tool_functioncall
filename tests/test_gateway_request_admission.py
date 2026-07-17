from __future__ import annotations

import pytest

from src import gateway_request_admission as request_admission


class _Lease:
    def __init__(self) -> None:
        self.release_calls = 0

    def release(self) -> None:
        self.release_calls += 1


def test_acquire_request_slot_uses_current_gateway_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {"max_concurrent_requests": 9}
    lease = _Lease()
    seen: list[dict[str, int]] = []
    monkeypatch.setattr(request_admission, "_gateway_config", lambda: config)
    monkeypatch.setattr(
        request_admission.ADMISSION_SERVICE,
        "acquire",
        lambda received: seen.append(received) or lease,
    )
    assert request_admission.acquire_request_slot() is lease
    assert seen == [config]


def test_request_slot_scope_releases_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _Lease()
    monkeypatch.setattr(request_admission, "acquire_request_slot", lambda: lease)
    with request_admission.request_slot_scope():
        assert lease.release_calls == 0
    assert lease.release_calls == 1


def test_request_slot_scope_releases_after_body_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _Lease()
    monkeypatch.setattr(request_admission, "acquire_request_slot", lambda: lease)
    with pytest.raises(RuntimeError, match="body marker"):
        with request_admission.request_slot_scope():
            raise RuntimeError("body marker")
    assert lease.release_calls == 1


def test_request_slot_scope_does_not_release_when_acquire_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        request_admission,
        "acquire_request_slot",
        lambda: (_ for _ in ()).throw(RuntimeError("acquire marker")),
    )
    with pytest.raises(RuntimeError, match="acquire marker"):
        with request_admission.request_slot_scope():
            raise AssertionError("scope body must not run")


def test_legacy_runtime_and_facade_exports_use_the_new_boundary() -> None:
    from src import gateway_app, gateway_tool_runtime

    assert gateway_tool_runtime._acquire_request_slot is request_admission._acquire_request_slot
    assert gateway_tool_runtime._request_slot_scope is request_admission._request_slot_scope
    assert gateway_app._acquire_request_slot is request_admission._acquire_request_slot
    assert gateway_app._request_slot_scope is request_admission._request_slot_scope

