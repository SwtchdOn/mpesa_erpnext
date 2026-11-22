"""The C2B and payout jobs run as a permitted user when Guest (Safaricom's callback) queued them."""

import sys
import types

import pytest

frappe_stub = sys.modules["frappe"]

from swdpay import outbound, reconciliation


@pytest.fixture
def session(monkeypatch):
    """frappe.session / set_user, and what reconcile_transaction saw."""
    state = types.SimpleNamespace(user="Guest", calls=[], error=None)
    monkeypatch.setattr(frappe_stub, "session", state, raising=False)
    monkeypatch.setattr(frappe_stub, "set_user", lambda user: setattr(state, "user", user), raising=False)

    def reconcile(name, manual=False, source=None):
        state.calls.append((name, manual, source, state.user))
        if state.error:
            raise state.error
        return "transaction"

    monkeypatch.setattr(reconciliation, "reconcile_transaction", reconcile)
    return state


def test_a_callback_job_runs_as_administrator_and_switches_back(session):
    assert reconciliation.reconcile_from_callback("TX1") == "transaction"
    assert session.calls == [("TX1", False, "Background Job", "Administrator")]
    assert session.user == "Guest"


def test_it_switches_back_when_reconciliation_raises(session):
    session.error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        reconciliation.reconcile_from_callback("TX1")
    assert session.user == "Guest"


def test_a_persons_job_keeps_their_permissions(session):
    session.user = "office@example.com"
    reconciliation.reconcile_from_callback("TX1")
    assert session.calls == [("TX1", False, "Background Job", "office@example.com")]
    assert session.user == "office@example.com"


def test_the_callback_queues_that_job_after_commit(monkeypatch):
    queued = []
    monkeypatch.setattr(frappe_stub, "enqueue", lambda method, **kw: queued.append((method, kw)))
    reconciliation.enqueue_reconciliation("TX1")
    assert queued == [(
        "swdpay.reconciliation.reconcile_from_callback",
        {"queue": "short", "transaction_name": "TX1", "enqueue_after_commit": True},
    )]


def test_a_payout_job_runs_as_administrator_and_switches_back(session, monkeypatch):
    seen = []
    monkeypatch.setattr(outbound, "reconcile_payout", lambda *a, **kw: seen.append((a, kw, session.user)))
    outbound.reconcile_payout_from_callback("MPPR-1", {"result_code": 0}, verify_conversation=False)
    assert seen == [(("MPPR-1", {"result_code": 0}), {"timeout": False, "verify_conversation": False}, "Administrator")]
    assert session.user == "Guest"


def test_a_payout_job_switches_back_when_it_raises(session, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(outbound, "reconcile_payout", fail)
    with pytest.raises(RuntimeError):
        outbound.reconcile_payout_from_callback("MPPR-1", {})
    assert session.user == "Guest"


def test_a_persons_payout_job_keeps_their_permissions(session, monkeypatch):
    seen = []
    session.user = "office@example.com"
    monkeypatch.setattr(outbound, "reconcile_payout", lambda *a, **kw: seen.append(session.user))
    outbound.reconcile_payout_from_callback("MPPR-1", {}, timeout=True)
    assert seen == ["office@example.com"]
