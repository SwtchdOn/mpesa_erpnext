"""Registers the canonical frappe stub before any test module imports swdpay code.

Both test files must use THIS stub (sys.modules["frappe"]) — never register their own —
so results don't depend on pytest's collection order.
"""

import sys
import types

import pytest


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
frappe_stub.whitelist = lambda *a, **k: (lambda f: f)
sys.modules["frappe"] = frappe_stub
sys.modules["frappe.utils"] = frappe_utils_stub


# --- Minimal in-memory fakes for the payout callback/reconcile money path ---
#
# Deliberately narrow: only the surface `handle_payout_callback` / `reconcile_payout`
# actually touch (frappe.db.get_value/set_value/exists/commit, frappe.get_doc,
# frappe.get_single, frappe.enqueue, frappe.log_error). Kept in dict/list form —
# not a real ORM — so tests assert real resulting state, not mock call counts.

class FakeDoc:
    """Stand-in for a frappe Document: attribute access mirrors the backing dict,
    and db_set writes straight through (both forms frappe supports: a single
    fieldname/value pair, or a dict of updates)."""

    def __init__(self, name, store):
        self.name = name
        self._store = store
        for key, value in store[name].items():
            setattr(self, key, value)

    def db_set(self, fieldname, value=None):
        updates = fieldname if isinstance(fieldname, dict) else {fieldname: value}
        self._store[self.name].update(updates)
        for key, val in updates.items():
            setattr(self, key, val)


class FakeDB:
    def __init__(self, docs):
        self.docs = docs
        self.commits = 0

    def exists(self, doctype, name):
        return name in self.docs

    def get_value(self, doctype, name, fieldname=None, for_update=False):
        record = self.docs.get(name)
        if record is None:
            return None
        if isinstance(fieldname, (list, tuple)):
            return tuple(record.get(f) for f in fieldname)
        return record.get(fieldname)

    def set_value(self, doctype, name, fieldname, value=None):
        record = self.docs.setdefault(name, {})
        if isinstance(fieldname, dict):
            record.update(fieldname)
        else:
            record[fieldname] = value

    def commit(self):
        self.commits += 1


def _fake_get_doc(doctype, name=None):
    if name not in frappe_stub.db.docs:
        raise frappe_stub.StubValidationError(f"{doctype} {name} not found")
    return FakeDoc(name, frappe_stub.db.docs)


def _fake_get_single(doctype):
    return types.SimpleNamespace(name=doctype)


def _fake_enqueue(method, queue=None, **kwargs):
    frappe_stub.enqueued.append({"method": method, "queue": queue, **kwargs})


def _fake_log_error(title=None, message=None):
    frappe_stub.logged_errors.append({"title": title, "message": message})


def _fake_get_roles():
    # Fail-closed default; approve/reject permission tests monkeypatch this per-test.
    return []


frappe_stub.db = FakeDB({})
frappe_stub.enqueued = []
frappe_stub.logged_errors = []
frappe_stub.get_doc = _fake_get_doc
frappe_stub.get_single = _fake_get_single
frappe_stub.enqueue = _fake_enqueue
frappe_stub.log_error = _fake_log_error
frappe_stub.get_roles = _fake_get_roles


@pytest.fixture
def payout_env():
    """Installs a fresh FakeDB + enqueue/log_error state for the payout tests.

    Some test_reconciliation.py tests permanently overwrite frappe_stub.db with a bare
    types.SimpleNamespace (not restoring it), so this fixture cannot assume
    frappe_stub.db is still a FakeDB when it runs — it installs its own and restores
    whatever was there before, keeping payout tests order-independent either way.
    """
    original_db = frappe_stub.db
    frappe_stub.db = FakeDB({})
    frappe_stub.enqueued = []
    frappe_stub.logged_errors = []
    yield frappe_stub
    frappe_stub.db = original_db
