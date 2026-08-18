import bisect
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from .config import Config
from .storage import SessionStore


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mx = _mean(xs)
    my = _mean(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    denom = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    return sum(x * y for x, y in zip(dx, dy)) / denom if denom else 0.0


def _movement_agreement(xs: Sequence[float], ys: Sequence[float]) -> float:
    agreements = []
    for i in range(1, len(xs)):
        dx = xs[i] - xs[i - 1]
        dy = ys[i] - ys[i - 1]
        if abs(dx) < 0.5 and abs(dy) < 0.5:
            continue
        agreements.append(1.0 if (dx == 0 and dy == 0) or dx * dy > 0 else 0.0)
    return _mean(agreements) if agreements else 0.0


def _align(field_times: Sequence[float], field_values: Sequence[float],
           cell_samples: Sequence[Tuple[float, float]], lag: float,
           max_hold: float) -> Tuple[List[float], List[float]]:
    observed = []
    expected = []
    for sample_time, cell_mv in cell_samples:
        target = sample_time + lag
        pos = bisect.bisect_right(field_times, target) - 1
        if pos >= 0 and target - field_times[pos] <= max_hold:
            observed.append(field_values[pos])
            expected.append(cell_mv)
    return observed, expected


def _lags(config: Config):
    count = int(round((2 * config.max_lag_s) / config.lag_step_s))
    return [-config.max_lag_s + i * config.lag_step_s for i in range(count + 1)]


def analyze_session(path: Path, config: Config, limit_per_cell: int = 20) -> List[Dict]:
    store = SessionStore(path)
    if store.info().get("status") == "recording":
        store.close()
        raise RuntimeError("stop capture before analysis")
    db = store.db
    cells = defaultdict(list)
    for row in db.execute("SELECT cell_index,wall_ts,voltage_v FROM cell_samples ORDER BY wall_ts"):
        cells[int(row["cell_index"])].append((float(row["wall_ts"]), float(row["voltage_v"]) * 1000.0))

    results_by_cell = {}
    eligible = {}
    lag_values = _lags(config)
    for cell_index in range(1, 17):
        samples = cells.get(cell_index, [])
        movement = (max(v for _, v in samples) - min(v for _, v in samples)) if samples else 0.0
        if len(samples) < config.min_samples:
            results_by_cell[cell_index] = [_placeholder(
                cell_index, "inconclusive",
                "only %d samples; at least %d required" % (len(samples), config.min_samples))]
            continue
        if movement < config.min_movement_mv:
            results_by_cell[cell_index] = [_placeholder(
                cell_index, "inconclusive",
                "voltage movement %.2f mV is below %.2f mV" % (movement, config.min_movement_mv))]
            continue
        eligible[cell_index] = (samples, movement)

    candidate_buckets = defaultdict(list)
    identifiers = list(db.execute(
        "SELECT DISTINCT can_id,is_extended,is_local FROM frames WHERE is_remote=0 AND is_error=0 ORDER BY can_id,is_extended,is_local"))
    for identifier in identifiers:
        can_id = int(identifier["can_id"])
        extended = int(identifier["is_extended"])
        is_local = int(identifier["is_local"])
        frames = [(float(row["wall_ts"]), bytes(row["data"])) for row in db.execute(
            "SELECT wall_ts,data FROM frames WHERE can_id=? AND is_extended=? AND is_local=? AND is_remote=0 AND is_error=0 ORDER BY wall_ts",
            (can_id, extended, is_local))]
        max_length = max((len(data) for _, data in frames), default=0)
        for offset in range(max(0, max_length - 1)):
            points = [(ts, data[offset:offset + 2]) for ts, data in frames if len(data) >= offset + 2]
            if not points:
                continue
            field_times = [point[0] for point in points]
            for endian in ("little", "big"):
                raw_values = [int.from_bytes(point[1], endian, signed=False) for point in points]
                for scale in (0.1, 1.0, 10.0):
                    field_values = [value * scale for value in raw_values]
                    for cell_index, (samples, movement) in eligible.items():
                        best = None
                        for lag in lag_values:
                            observed, expected = _align(field_times, field_values, samples, lag, config.max_hold_s)
                            if len(observed) < config.min_samples:
                                continue
                            errors = [a - b for a, b in zip(observed, expected)]
                            corr = _pearson(observed, expected)
                            rmse = math.sqrt(_mean([e * e for e in errors]))
                            median_error = _median([abs(e) for e in errors])
                            coverage = len(observed) / len(samples)
                            agreement = _movement_agreement(observed, expected)
                            score = 100.0 * (
                                0.45 * max(0.0, corr) +
                                0.20 * coverage +
                                0.20 * (1.0 / (1.0 + rmse / 5.0)) +
                                0.15 * agreement
                            )
                            metrics = (score, len(observed), coverage, corr, rmse,
                                       median_error, agreement, lag)
                            if best is None or metrics[0] > best[0]:
                                best = metrics
                        if best is None:
                            continue
                        score, count, coverage, corr, rmse, median_error, agreement, lag = best
                        verdict = "weak"
                        if coverage >= 0.8 and corr >= 0.95 and rmse <= 10.0:
                            verdict = "convincing"
                        elif coverage >= 0.6 and corr >= 0.8 and rmse <= 25.0:
                            verdict = "possible"
                        candidate_buckets[cell_index].append({
                            "cell_index": cell_index, "can_id": can_id, "is_extended": extended,
                            "is_local": is_local,
                            "byte_offset": offset, "endian": endian, "scale_mv": scale,
                            "sample_count": count, "coverage": coverage, "correlation": corr,
                            "rmse_mv": rmse, "median_error_mv": median_error,
                            "movement_agreement": agreement, "lag_s": lag, "score": score,
                            "verdict": verdict, "detail": "cell movement %.2f mV" % movement,
                        })

    for cell_index in eligible:
        candidates = candidate_buckets[cell_index]
        candidates.sort(key=lambda row: row["score"], reverse=True)
        selected = candidates[:limit_per_cell]
        if not selected:
            results_by_cell[cell_index] = [_placeholder(
                cell_index, "no_candidate", "no comparable CAN fields")]
            continue
        if not any(row["verdict"] == "convincing" for row in selected):
            selected[0]["detail"] += "; no convincing candidate"
        for rank, row in enumerate(selected, 1):
            row["rank"] = rank
        results_by_cell[cell_index] = selected

    all_rows = []
    for cell_index in range(1, 17):
        all_rows.extend(results_by_cell[cell_index])
    store.replace_candidates(all_rows)
    store.close()
    return all_rows


def _placeholder(cell_index: int, verdict: str, detail: str) -> Dict:
    return {
        "cell_index": cell_index, "rank": 1, "can_id": None, "is_extended": None, "is_local": None,
        "byte_offset": None, "endian": None, "scale_mv": None, "sample_count": 0,
        "coverage": None, "correlation": None, "rmse_mv": None,
        "median_error_mv": None, "movement_agreement": None, "lag_s": None,
        "score": None, "verdict": verdict, "detail": detail,
    }


def candidate_series(path: Path, cell_index: int, rank: int) -> Dict:
    store = SessionStore(path)
    row = store.db.execute("SELECT * FROM candidates WHERE cell_index=? AND rank=?", (cell_index, rank)).fetchone()
    if row is None or row["can_id"] is None:
        store.close()
        return {"cell": [], "candidate": []}
    cell = [[r["wall_ts"], r["voltage_v"] * 1000.0] for r in store.db.execute(
        "SELECT wall_ts,voltage_v FROM cell_samples WHERE cell_index=? ORDER BY wall_ts", (cell_index,))]
    candidate = []
    for frame in store.db.execute(
        "SELECT wall_ts,data FROM frames WHERE can_id=? AND is_extended=? AND is_local=? AND is_remote=0 AND is_error=0 ORDER BY wall_ts",
        (row["can_id"], row["is_extended"], row["is_local"])):
        data = bytes(frame["data"])
        offset = int(row["byte_offset"])
        if offset + 2 <= len(data):
            value = int.from_bytes(data[offset:offset + 2], row["endian"]) * row["scale_mv"]
            candidate.append([frame["wall_ts"] - row["lag_s"], value])
    result = {"cell": cell, "candidate": candidate, "candidate_info": dict(row)}
    store.close()
    return result
