#!/usr/bin/env python3
"""Compatibility entrypoint for the native tool/function-call gateway.

The implementation is split into feature modules. Importing
``src.toolcall_gateway`` returns ``src.gateway_app`` for backward
compatibility with existing tests and callers that monkeypatch module globals.
"""
from __future__ import annotations

import sys

try:  # package import: import src.toolcall_gateway
    from . import gateway_app as _app
except ImportError:  # script execution: python src/toolcall_gateway.py
    import gateway_app as _app  # type: ignore

if __name__ == "__main__":
    _app.main()
else:
    sys.modules[__name__] = _app
