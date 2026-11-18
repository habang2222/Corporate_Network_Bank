"""Compare measured service impact without inventing collector measurements."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

CONDITIONS = ("seed", "users", "target_rps", "requested_duration_seconds", "scenario", "focus",
              "failure_ratio_requested", "user_types", "max_output_mib", "session_refresh_seconds")


def difference(before, after):
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return {"baseline": before, "comparison": after, "absolute_delta": None, "percent_delta": None}
    return {"baseline": before, "comparison": after, "absolute_delta": after - before,
            "percent_delta": ((after - before) / before * 100) if before else None}


def compare(baseline, comparison, comparison_label):
    mismatch = {key: {"baseline": baseline.get(key), "comparison": comparison.get(key)}
                for key in CONDITIONS if key not in baseline or key not in comparison or baseline[key] != comparison[key]}
    if mismatch:
        raise ValueError("run conditions differ or are missing: " + json.dumps(mismatch, ensure_ascii=False))
    for name, run in (("baseline", baseline), ("comparison", comparison)):
        if run.get("error") or run.get("stop_reason") != "duration_complete":
            raise ValueError(f"{name} did not complete the requested duration")
        if not isinstance(run.get("total_requests"), int) or run["total_requests"] <= 0:
            raise ValueError(f"{name} has no completed measured requests")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "comparison_label": comparison_label,
        "baseline_run_id": baseline.get("run_id"), "comparison_run_id": comparison.get("run_id"),
        "matched_conditions": {key: baseline[key] for key in CONDITIONS},
        "service_deltas": {"actual_rps": difference(baseline.get("actual_rps"), comparison.get("actual_rps")),
                           "error_rate": difference(baseline.get("error_rate"), comparison.get("error_rate")),
                           "total_requests": difference(baseline.get("total_requests"), comparison.get("total_requests")),
                           "latency_ms": {key: difference(baseline.get("latency_ms", {}).get(key), comparison.get("latency_ms", {}).get(key))
                                          for key in ("mean", "p50", "p95", "p99")}},
        "collector": {"status": "external_measurements_not_verified", "metrics": None},
        "limitations": [
            "The supplied label does not verify that an external collector was running.",
            "Service differences are observations, not proof that a collector caused the change.",
            "Confirm equal host/kernel/image/config versions, starting DB state and concurrent activity separately.",
            "Operation counts are not kernel event counts; retention cannot be computed from HTTP requests.",
            "No performance pass/fail threshold is imposed.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("comparison", type=Path)
    parser.add_argument("--comparison-label", default="external-collector-on (operator supplied, unverified)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = compare(json.loads(args.baseline.read_text(encoding="utf-8")),
                         json.loads(args.comparison.read_text(encoding="utf-8")), args.comparison_label)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
