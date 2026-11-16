"""Date selection as a QUBO, solved classically by our own simulated annealing."""
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from terra.qubo import DEFAULT_TAU_DAYS, SOLVER, anneal, build_qubo, energy, select_dates

rng = np.random.default_rng(3)
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _scenario(m: int = 10):
    days = np.sort(rng.uniform(0.0, 300.0, m))
    quality = rng.uniform(0.0, 1.0, m)
    return days, quality


def _iso(days) -> list[str]:
    return [(BASE + timedelta(days=float(d))).isoformat().replace("+00:00", "Z") for d in days]


def _direct_objective(days, quality, x, k, a, tau=DEFAULT_TAU_DAYS) -> float:
    cardinality = a * (sum(x) - k) ** 2
    spacing = sum(math.exp(-abs(days[i] - days[j]) / tau) for i in range(len(days))
                  for j in range(i + 1, len(days)) if x[i] and x[j])
    reward = sum(quality[i] for i in range(len(days)) if x[i])
    return cardinality + spacing - reward


def test_qubo_matrix_reproduces_the_stated_objective():
    days, quality = _scenario()
    k = 4
    a = 2.0 * (len(days) + 1)
    Q = build_qubo(days, quality, k)
    assert np.allclose(np.triu(Q), Q)  # upper triangular: pairs are counted once
    x = np.array([1, 0, 1, 1, 0, 0, 1, 0, 0, 0])
    direct = _direct_objective(days, quality, x, k, a)
    assert energy(Q, x) + a * k * k == pytest.approx(direct, abs=1e-9)
    assert energy(Q, np.zeros(10)) == pytest.approx(0.0)
    # The constant is what the docstring says it is, so an objective can be rebuilt from a result.
    assert energy(Q, np.zeros(10)) + a * k * k == pytest.approx(a * k * k)


def test_annealer_reaches_the_exhaustive_optimum_for_ten_candidates():
    days, quality = _scenario()
    Q = build_qubo(days, quality, 4)
    patterns = ((np.arange(1 << 10)[:, None] >> np.arange(10)) & 1).astype(np.float64)
    optimum = min(energy(Q, p) for p in patterns)
    for seed in range(5):
        run = anneal(Q, seed=seed)
        assert run["solver"] == SOLVER and run["sweeps"] == 2000
        assert len(run["x"]) == 10 and set(run["x"]) <= {0, 1}
        assert run["energy"] == pytest.approx(optimum, abs=1e-9)
        assert energy(Q, run["x"]) == pytest.approx(run["energy"], abs=1e-9)


def test_annealer_is_deterministic_and_actually_optimises():
    gen = np.random.default_rng(9)  # own generator, so the numbers do not depend on test order
    days = np.sort(gen.uniform(0.0, 300.0, 12))
    quality = gen.uniform(0.0, 1.0, 12)
    Q = build_qubo(days, quality, 4)
    first, second = anneal(Q, sweeps=200, seed=7), anneal(Q, sweeps=200, seed=7)
    assert first == second
    assert energy(Q, first["x"]) == pytest.approx(first["energy"], abs=1e-9)
    one_sweep = anneal(Q, sweeps=1, seed=0)
    assert energy(Q, one_sweep["x"]) == pytest.approx(one_sweep["energy"], abs=1e-9)
    cooled = anneal(Q, seed=7)  # the default 2000-sweep schedule
    draws = np.random.default_rng(5).integers(0, 2, size=(400, 12))
    random_energies = np.array([energy(Q, row) for row in draws])
    assert cooled["energy"] < float(np.median(random_energies))  # far better than a random stack
    assert cooled["energy"] < float(random_energies.min())  # and better than the best of 400 random stacks
    with pytest.raises(ValueError):
        anneal(Q, sweeps=0)
    with pytest.raises(ValueError):
        anneal(Q.T)  # only the upper-triangular convention is accepted


def test_build_qubo_rejects_nonsense():
    days, quality = _scenario(6)
    with pytest.raises(ValueError):
        build_qubo(days, quality, 7)  # more dates than candidates
    with pytest.raises(ValueError):
        build_qubo(days, quality[:-1], 2)  # mismatched lengths
    with pytest.raises(ValueError):
        build_qubo(days, quality, 2, tau_days=0.0)
    with pytest.raises(ValueError):
        select_dates(_iso(days), [1.5] * 6, 2)  # quality outside [0, 1]


def test_select_dates_keeps_exactly_k_and_spreads_them_out():
    days = np.array([0, 1, 2, 3, 60, 61, 62, 63, 120, 121, 122, 123, 180, 181, 182, 183, 240, 241, 242, 243], float)
    quality = np.full(days.size, 0.5)
    result = select_dates(_iso(days), quality.tolist(), 4)
    assert result["solver"] == SOLVER
    assert len(result["chosen"]) == 4 and len(set(result["chosen"])) == 4
    assert result["chosen"] == sorted(result["chosen"])  # sorted by date
    assert result["min_gap_days"] >= DEFAULT_TAU_DAYS  # one date per cluster, not four from one cluster
    assert result["brute_force_optimum_energy"] is None and result["found_optimum"] is None  # M = 20
    assert result["objective"] == pytest.approx(result["energy"] + result["constant"])
    assert result["constant"] == pytest.approx(2 * (days.size + 1) * 16)


def test_select_dates_states_whether_the_annealer_found_the_optimum():
    days, quality = _scenario()
    result = select_dates(_iso(days), quality.tolist(), 4, seed=2)
    assert len(result["chosen"]) == 4
    assert result["brute_force_optimum_energy"] is not None
    assert result["found_optimum"] is True
    assert result["energy"] == pytest.approx(result["brute_force_optimum_energy"], abs=1e-9)
    assert result["objective"] == pytest.approx(result["energy"] + result["constant"])
    assert result["dates"] == sorted(result["dates"])  # kept in chronological order
    assert result["dates"] == [_iso(days)[i] for i in result["chosen"]]


def test_select_dates_follows_quality():
    days = np.array([0.0, 1.0, 60.0, 61.0], float)
    coarse = select_dates(_iso(days), [0.5] * 4, 2)
    assert coarse["min_gap_days"] >= DEFAULT_TAU_DAYS
    # A date that is clearly better than everything near it wins despite the spacing penalty.
    quality = [1.0, 0.0, 0.0, 0.0]
    single = select_dates(_iso(days), quality, 1)
    assert single["chosen"] == [0]
