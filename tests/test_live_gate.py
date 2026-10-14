"""The live gate must fail closed. These tests exist because the failure mode
of a live gate is opening when nobody meant it to."""

from __future__ import annotations

import os

import pytest

from stratbot.live_gate import ENV_FLAG, EXPECTED_FLAG, LiveGate


@pytest.fixture
def gate(tmp_path) -> LiveGate:
    return LiveGate(project_dir=tmp_path)


@pytest.fixture(autouse=True)
def clear_env():
    original = os.environ.pop(ENV_FLAG, None)
    yield
    if original is None:
        os.environ.pop(ENV_FLAG, None)
    else:
        os.environ[ENV_FLAG] = original


def test_locked_by_default(gate):
    result = gate.check()
    assert not result.unlocked
    assert set(result.missing) == {"env_flag", "marker_file", "cli_flag"}


def test_env_var_alone_does_not_unlock(gate):
    os.environ[ENV_FLAG] = EXPECTED_FLAG
    assert not gate.check().unlocked


def test_marker_alone_does_not_unlock(gate):
    gate.create_marker()
    assert not gate.check().unlocked


def test_cli_flag_alone_does_not_unlock(gate):
    gate.cli_flag = True
    assert not gate.check().unlocked


def test_env_and_marker_without_cli_flag_stays_locked(gate):
    os.environ[ENV_FLAG] = EXPECTED_FLAG
    gate.create_marker()
    result = gate.check()
    assert not result.unlocked
    assert result.missing == ["cli_flag"]


def test_wrong_env_value_does_not_unlock(gate):
    os.environ[ENV_FLAG] = "true"
    gate.create_marker()
    gate.cli_flag = True
    result = gate.check()
    assert not result.unlocked
    assert result.missing == ["env_flag"]


def test_all_three_factors_unlock(gate):
    os.environ[ENV_FLAG] = EXPECTED_FLAG
    gate.create_marker()
    gate.cli_flag = True
    result = gate.check()
    assert result.unlocked, result.reason


def test_removing_the_marker_relocks(gate):
    os.environ[ENV_FLAG] = EXPECTED_FLAG
    gate.create_marker()
    gate.cli_flag = True
    assert gate.check().unlocked
    assert gate.remove_marker() is True
    assert not gate.check().unlocked


def test_removing_a_missing_marker_is_harmless(gate):
    assert gate.remove_marker() is False


def test_marker_is_written_inside_the_project(gate):
    path = gate.create_marker()
    assert path.is_file()
    assert path.parent.name == "data"
    assert path.parent.parent == gate.project_dir


def test_marker_file_does_not_satisfy_itself(gate):
    path = gate.create_marker()
    text = path.read_text(encoding="utf-8")
    assert "does not enable live trading" in text


def test_describe_lists_every_factor(gate):
    text = gate.describe()
    assert "LOCKED" in text
    assert ENV_FLAG in text
    assert "--live" in text


def test_gate_reports_which_factors_are_present(gate):
    os.environ[ENV_FLAG] = EXPECTED_FLAG
    result = gate.check()
    assert result.factors["env_flag"] is True
    assert result.factors["marker_file"] is False
    assert result.factors["cli_flag"] is False


def test_a_plain_flag_value_is_not_the_expected_phrase(gate):
    os.environ[ENV_FLAG] = "1"
    os.environ[ENV_FLAG] = "true"
    gate.create_marker()
    gate.cli_flag = True
    assert not gate.check().unlocked, "only the exact phrase may count"