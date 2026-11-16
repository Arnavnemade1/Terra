"""Which acquisitions enter the trend stack, posed as a QUBO and solved classically.

Picking dates is a binary choice per acquisition, so it is written as a QUBO: minimise
x^T Q x + constant over x in {0,1}^M, the form quantum annealers accept. The objective is

    A * (sum_i x_i - k)^2  +  sum_{i<j} exp(-|d_i - d_j| / tau_days) * x_i * x_j  -  sum_i quality_i * x_i

where dates close together in time are penalised (a stack should be spread out, not clustered)
and quality in [0, 1] is rewarded. A defaults to 2 * (M + 1), which makes the cardinality
constraint dominate: leaving or adding one date costs A, while spacing and quality together move
the objective by at most a few.

The solver is **classical simulated annealing implemented here in numpy**, run on ordinary CPUs. It
is not a quantum annealer and it is not claimed to be one; every result states
"solver": "classical simulated annealing". For small stacks (M <= 16) the true optimum is found by
exhausting all 2^M choices, so a result also states whether the annealer matched it.

Q is upper triangular: energy(x) = x^T Q x counts the diagonal once and each pair once, and the
constant A * k^2 is not included. A quantum annealer that expects a symmetric matrix can be given
Q + Q^T - diag(Q) without changing the energy per its own convention; here the convention is fixed
and documented so the numbers are reproducible.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from collections.abc import Sequence

import numpy as np

SOLVER = "classical simulated annealing"
DEFAULT_TAU_DAYS = 24.0
RESTARTS = 8
EXHAUSTIVE_LIMIT = 16


def _penalty(m: int) -> float:
    """Default cardinality weight A = 2 * (M + 1): one date too many or too few costs more than spacing."""
    return 2.0 * (m + 1)


def build_qubo(days: list[float], quality: list[float], k: int,
               tau_days: float = DEFAULT_TAU_DAYS, a: float | None = None) -> np.ndarray:
    """Upper-triangular Q for the date-selection QUBO; energy(x) = x^T Q x + a * k^2.

    `days` is the acquisition time in days (any consistent origin, only differences matter) and
    `quality` is each date's usefulness in [0, 1]. `k` is the number of dates to keep and `tau_days`
    the time scale over which two dates count as close.

    Expanding A * (sum x - k)^2 for binary x gives a constant A * k^2, a diagonal term
    A * (1 - 2k) * x_i, and A over every pair. The pair coefficient is therefore 2A plus the
    Gaussian spacing penalty exp(-|d_i - d_j| / tau_days), and the diagonal is
    A * (1 - 2k) - quality_i. Spacing and quality are both order one, so among selections with
    exactly k dates they decide the choice, while the cardinality term keeps the count at k.
    """
    d = np.asarray(days, dtype=np.float64).ravel()
    q = np.asarray(quality, dtype=np.float64).ravel()
    m = d.size
    if m != q.size:
        raise ValueError("days and quality must have the same length.")
    if m < 1:
        raise ValueError("At least one candidate date is required.")
    if not isinstance(k, (int, np.integer)) or not 1 <= int(k) <= m:
        raise ValueError("k must be a whole number of dates between 1 and the number of candidates.")
    if not np.isfinite(tau_days) or tau_days <= 0:
        raise ValueError("tau_days must be positive and finite.")
    if np.any(~np.isfinite(d)):
        raise ValueError("days must be finite.")
    if np.any(~np.isfinite(q)):
        raise ValueError("quality must be finite.")
    weight = float(a) if a is not None else _penalty(m)
    if not np.isfinite(weight) or weight <= 0:
        raise ValueError("a must be positive and finite.")

    matrix = np.zeros((m, m), dtype=np.float64)
    gaps = np.abs(d[:, None] - d[None, :])
    upper = np.triu_indices(m, 1)
    matrix[upper] = 2.0 * weight + np.exp(-gaps[upper] / float(tau_days))
    matrix[np.diag_indices(m)] = weight * (1.0 - 2.0 * int(k)) - q
    return matrix


def energy(Q: np.ndarray, x: Sequence[int]) -> float:
    """x^T Q x for the upper-triangular Q from build_qubo; the constant a * k^2 is not included."""
    matrix = np.asarray(Q, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1]:
        raise ValueError("Q must be a square matrix.")
    bits = np.asarray(x, dtype=np.float64).ravel()
    if bits.size != matrix.shape[0]:
        raise ValueError("x must have one entry per row of Q.")
    if np.any((bits != 0.0) & (bits != 1.0)):
        raise ValueError("x must be binary.")
    return float(bits @ matrix @ bits)


def _fields(rows: list[list[float]], x: list[int]) -> list[float]:
    """dE/dx_i for every bit: the diagonal plus the contributions of the other bits."""
    m = len(rows)
    local = [rows[i][i] for i in range(m)]
    for i in range(m):
        for j in range(i + 1, m):
            if x[j]:
                local[i] += rows[i][j]
        for j in range(i):
            if x[j]:
                local[i] += rows[j][i]
    return local


def anneal(Q: np.ndarray, sweeps: int = 2000, seed: int = 0,
           t0: float | None = None, t1: float = 1e-3) -> dict:
    """Single-bit-flip Metropolis simulated annealing over a classical schedule.

    Each sweep visits every bit in a random order and flips it with probability
    exp(-dE / T) when the flip raises the energy, T falling geometrically from `t0` to `t1` across
    the run. The lowest energy seen is returned, with the bit vector that produced it.

    This is a classical optimiser written in Python and numpy. It runs on a CPU and says nothing
    about quantum hardware. `t0` defaults to a quarter of the largest entry of Q, which is roughly
    the energy of one bit flip and therefore hot enough to leave a poor start.
    """
    matrix = np.asarray(Q, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1] or matrix.shape[0] < 1:
        raise ValueError("Q must be a non-empty square matrix.")
    if np.any(np.tril(matrix, -1) != 0.0):
        raise ValueError("anneal expects the upper-triangular Q from build_qubo.")
    if int(sweeps) < 1:
        raise ValueError("sweeps must be at least one.")
    if t1 <= 0 or not np.isfinite(t1):
        raise ValueError("t1 must be positive and finite.")
    m = matrix.shape[0]
    rows = matrix.tolist()
    rng = np.random.default_rng(seed)
    start = float(t0) if t0 is not None else max(1e-3, 0.25 * float(np.max(np.abs(matrix))))
    if start <= 0 or not np.isfinite(start):
        raise ValueError("t0 must be positive and finite.")
    if start < t1:
        start = t1

    x = [int(v) for v in rng.integers(0, 2, m)]
    current = float(sum(rows[i][i] * x[i] for i in range(m))
                    + sum(rows[i][j] * x[i] * x[j] for i in range(m) for j in range(i + 1, m)))
    best_x, best = list(x), current
    local = _fields(rows, x)
    steps = max(1, int(sweeps) - 1)
    for sweep in range(int(sweeps)):
        temperature = start * (t1 / start) ** (sweep / steps)
        for bit in rng.permutation(m):
            i = int(bit)
            change = 1 - 2 * x[i]
            delta = change * local[i]
            if delta <= 0.0 or rng.random() < math.exp(-delta / temperature):
                x[i] = 1 - x[i]
                current += delta
                if current < best:
                    best, best_x = current, list(x)
                # dE/dx_j moves with x_i for every other bit; dE/dx_i does not depend on x_i.
                for j in range(i + 1, m):
                    local[j] += change * rows[i][j]
                for j in range(i):
                    local[j] += change * rows[j][i]
    return {"x": best_x, "energy": float(best), "solver": SOLVER, "sweeps": int(sweeps)}


def _brute_force(Q: np.ndarray) -> float:
    m = Q.shape[0]
    patterns = ((np.arange(1 << m)[:, None] >> np.arange(m)) & 1).astype(np.float64)
    energies = np.einsum("bi,ij,bj->b", patterns, Q, patterns)
    return float(np.min(energies))


def _days(value: str) -> float:
    """Days since the epoch for an ISO date or timestamp; naive values are read as UTC."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Not an ISO date: {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp() / 86400.0


def select_dates(dates_iso: list[str], quality: list[float], k: int, seed: int = 0) -> dict:
    """Choose k acquisitions spread over time, by classical simulated annealing (8 restarts).

    Returns the chosen indices sorted by date, with the dates themselves, the QUBO energy
    x^T Q x, the constant A * k^2 and their sum (the objective), the smallest gap in days between
    chosen dates, and, for M <= 16, the exhaustive optimum energy and whether the annealer matched
    it. `found_optimum` is None when the stack is too large to exhaust.

    The spread test is honest about the trade-off: dates are pushed apart by the spacing term and
    pulled by quality, so a high-quality date can still sit close to another chosen date when it is
    worth more than the spacing penalty.
    """
    days = np.array([_days(d) for d in dates_iso], dtype=np.float64)
    scores = np.asarray(quality, dtype=np.float64).ravel()
    m = days.size
    if m != scores.size:
        raise ValueError("dates_iso and quality must have the same length.")
    if m < 1:
        raise ValueError("At least one candidate date is required.")
    if not isinstance(k, (int, np.integer)) or not 1 <= int(k) <= m:
        raise ValueError("k must be a whole number of dates between 1 and the number of candidates.")
    if np.any((scores < 0.0) | (scores > 1.0)):
        raise ValueError("quality must lie in [0, 1].")
    weight = _penalty(m)
    matrix = build_qubo(days, scores, int(k), a=weight)

    rng = np.random.default_rng(seed)
    best = None
    for _ in range(RESTARTS):
        run = anneal(matrix, seed=int(rng.integers(0, 2 ** 31 - 1)))
        if best is None or run["energy"] < best["energy"]:
            best = run
    chosen = sorted((i for i, v in enumerate(best["x"]) if v), key=lambda i: float(days[i]))
    gap = float(np.min(np.diff(days[chosen]))) if len(chosen) > 1 else None
    constant = weight * int(k) ** 2
    result = {
        "chosen": chosen,
        "dates": [dates_iso[i] for i in chosen],
        "energy": float(best["energy"]),
        "constant": float(constant),
        "objective": float(best["energy"] + constant),
        "min_gap_days": gap,
        "solver": SOLVER,
        "restarts": RESTARTS,
        "sweeps": int(best["sweeps"]),
        "k": int(k),
        "quality_sum": float(scores[chosen].sum()),
        "brute_force_optimum_energy": None,
        "found_optimum": None,
    }
    if m <= EXHAUSTIVE_LIMIT:
        optimum = _brute_force(matrix)
        result["brute_force_optimum_energy"] = optimum
        result["found_optimum"] = bool(np.isclose(best["energy"], optimum, rtol=0.0, atol=1e-9))
    return result
