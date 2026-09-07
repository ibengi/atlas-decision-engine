"""Regression tests for PROD READ_ONLY observation through drawdown.

These tests prove the patch cannot turn an execution blocker into a CAPITAL
bypass: only the exact ``equity_drawdown`` result is converted to observation
continuation, and only in PROD READ_ONLY.
"""

from types import SimpleNamespace

import read_only_dashboard_bootstrap as bootstrap


def _engine(env="prod"):
    return SimpleNamespace(client=SimpleNamespace(env=env))


def test_prod_read_only_equity_drawdown_continues_observation(monkeypatch):
    monkeypatch.setenv("PROD_ACCESS_MODE", "READ_ONLY")
    monkeypatch.setattr(
        bootstrap, "_original_post_balance_gates",
        lambda self: (False, "equity_drawdown"),
    )
    eng = _engine("prod")

    assert bootstrap._post_balance_gates_observation_aware(eng) == (True, None)
    assert eng._read_only_capital_guard == "equity_drawdown"


def test_capital_equity_drawdown_remains_blocking(monkeypatch):
    monkeypatch.setenv("PROD_ACCESS_MODE", "CAPITAL")
    monkeypatch.setattr(
        bootstrap, "_original_post_balance_gates",
        lambda self: (False, "equity_drawdown"),
    )
    eng = _engine("prod")

    assert bootstrap._post_balance_gates_observation_aware(eng) == (
        False, "equity_drawdown"
    )
    assert not hasattr(eng, "_read_only_capital_guard")


def test_demo_equity_drawdown_remains_blocking(monkeypatch):
    monkeypatch.setenv("PROD_ACCESS_MODE", "READ_ONLY")
    monkeypatch.setattr(
        bootstrap, "_original_post_balance_gates",
        lambda self: (False, "equity_drawdown"),
    )
    eng = _engine("demo")

    assert bootstrap._post_balance_gates_observation_aware(eng) == (
        False, "equity_drawdown"
    )


def test_other_fail_closed_guards_remain_blocking_in_read_only(monkeypatch):
    monkeypatch.setenv("PROD_ACCESS_MODE", "READ_ONLY")
    eng = _engine("prod")

    for guard in (
        "persistence_failure",
        "contract_cap_invalid",
        "reconciliation_mismatch",
        "daily_loss_stop",
        "consecutive_loss_breaker",
        "max_open_positions",
    ):
        monkeypatch.setattr(
            bootstrap, "_original_post_balance_gates",
            lambda self, guard=guard: (False, guard),
        )
        assert bootstrap._post_balance_gates_observation_aware(eng) == (
            False, guard
        )


def test_observed_drawdown_is_carried_into_cycle_and_dashboard(monkeypatch):
    eng = _engine("prod")
    eng._read_only_capital_guard = "equity_drawdown"
    res = {"report": {"cycle_id": "cycle-test"}}
    saved = {}

    def fake_finish(self, n, cycle_res, execution_path):
        assert cycle_res["report"]["capital_blocking_guard"] == "equity_drawdown"
        assert cycle_res["report"]["capital_eligible"] is False
        return 0

    monkeypatch.setattr(bootstrap, "_original_finish_cycle", fake_finish)
    monkeypatch.setattr(bootstrap.JsonStore, "load", lambda path, default: {})
    monkeypatch.setattr(
        bootstrap.JsonStore, "save",
        lambda path, value: saved.update(value) or True,
    )

    assert bootstrap._finish_cycle_with_capital_guard(
        eng, 123, res, "sequential"
    ) == 0
    assert saved["read_only"] is True
    assert saved["capital_blocking_guard"] == "equity_drawdown"
    assert saved["capital_eligible"] is False
    assert eng._read_only_capital_guard is None
