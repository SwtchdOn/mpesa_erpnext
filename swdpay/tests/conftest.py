"""Registers the canonical frappe stub before any test module imports swdpay code.

Both test files must use THIS stub (sys.modules["frappe"]) — never register their own —
so results don't depend on pytest's collection order.
"""

import sys
import types


class StubValidationError(Exception):
    pass


def _stub_throw(message, *args, **kwargs):
    raise StubValidationError(message)


frappe_stub = types.ModuleType("frappe")
frappe_utils_stub = types.ModuleType("frappe.utils")
frappe_utils_stub.flt = lambda value, precision=None: (
    round(float(value or 0), precision) if precision is not None else float(value or 0)
)
frappe_utils_stub.getdate = lambda value=None: value
frappe_utils_stub.now_datetime = lambda: "2026-05-24 00:00:00"
frappe_stub.utils = frappe_utils_stub
frappe_stub.throw = _stub_throw
frappe_stub.StubValidationError = StubValidationError
sys.modules["frappe"] = frappe_stub
sys.modules["frappe.utils"] = frappe_utils_stub
