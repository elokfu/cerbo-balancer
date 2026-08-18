#!/usr/bin/env python3
"""Replay the cell-16 estimator against a schema-12/13 balancer CSV."""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path
from typing import Any

from dyness_rs485_service import Cell16Estimator


def number(row: dict[str, str], key: str) -> float | None:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def percentile_95(values: list[float]) -> float:
    ordered = sorted(abs(value) for value in values)
    return ordered[int(0.95 * (len(ordered) - 1))]


def metrics(values: list[float], common: list[float]) -> dict[str, float]:
    steps = [
        ((values[index] - common[index]) - (values[index - 1] - common[index - 1])) * 1000.0
        for index in range(1, len(values))
    ]
    return {
        "relativeStepSigmaMv": statistics.pstdev(steps),
        "relativeStep95Mv": percentile_95(steps),
        "relativeStepMaxMv": max(abs(value) for value in steps),
    }


def replay(path: Path) -> dict[int, dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(line for line in stream if not line.startswith("#")))
    results: dict[int, dict[str, Any]] = {}
    for address in (2, 3, 4):
        estimator = Cell16Estimator()
        prefix = f"battery_{address:02d}_"
        common_values: list[float] = []
        existing_values: list[float] = []
        estimated_values: list[float] = []
        residuals: list[float] = []
        constrained = 0
        for sequence, row in enumerate(rows, 1):
            cells = [number(row, prefix + f"cell_{index:02d}_v") for index in range(1, 16)]
            pack_voltage = number(row, prefix + "voltage_v")
            existing = number(row, prefix + "cell_16_v")
            minimum = number(row, "vmin_v")
            maximum = number(row, "vmax_v")
            if any(value is None for value in cells) or None in (pack_voltage, existing, minimum, maximum):
                continue
            numeric_cells = [float(value) for value in cells if value is not None]
            extrema = {
                "valid": True,
                "vmin": minimum,
                "vmax": maximum,
                "minCellAddress": 1,
                "minCellIndex": 1,
                "maxCellAddress": 1,
                "maxCellIndex": 2,
            }
            result = estimator.estimate(
                {"address": address, "reportedCells": numeric_cells, "voltage": pack_voltage},
                extrema,
                sequence * 8000,
            )
            if result is None:
                continue
            common_values.append(result["cell16CommonVoltage"])
            existing_values.append(existing)
            estimated_values.append(result["calculatedCellVoltage"])
            residuals.append(result["cell16PackResidualMv"])
            constrained += int(result["cell16ConstraintApplied"])
        existing_metrics = metrics(existing_values, common_values)
        improved_metrics = metrics(estimated_values, common_values)
        results[address] = {
            "samples": len(estimated_values),
            "existing": existing_metrics,
            "improved": improved_metrics,
            "constraintPercent": constrained * 100.0 / len(estimated_values),
            "meanPackResidualMv": statistics.mean(residuals),
        }
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--no-fail", action="store_true")
    args = parser.parse_args()
    results = replay(args.csv_path)
    failed = False
    for address, result in results.items():
        improved = result["improved"]
        print(
            f"battery {address}: samples={result['samples']} "
            f"sigma={improved['relativeStepSigmaMv']:.3f} mV "
            f"p95={improved['relativeStep95Mv']:.3f} mV "
            f"max={improved['relativeStepMaxMv']:.3f} mV "
            f"constraints={result['constraintPercent']:.2f}% "
            f"mean_residual={result['meanPackResidualMv']:.3f} mV"
        )
        failed |= (
            result["samples"] != 3059 or
            improved["relativeStepSigmaMv"] > 0.5 or
            improved["relativeStep95Mv"] > 1.0 or
            improved["relativeStepMaxMv"] > 2.0 or
            result["constraintPercent"] > 2.0 or
            abs(result["meanPackResidualMv"]) > 0.1
        )
    return 0 if args.no_fail or not failed else 1


if __name__ == "__main__":
    sys.exit(main())
