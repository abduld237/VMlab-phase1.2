#!/usr/bin/env python3
"""Measure analysis latency over the benchmark image set.

The PRD targets a result inside 60 seconds. Getting there means changing one
thing at a time and re-measuring, which needs a harness that runs the real
pipeline without the API or the database in the way -- no analysis rows, no
auth, no polling. Retrieval still hits the live knowledge base, because
retrieval latency is part of what we are measuring.

    # baseline, then again after a change, then compare
    .venv/bin/python scripts/benchmark_latency.py --limit 8 --label baseline
    .venv/bin/python scripts/benchmark_latency.py --limit 8 --label routing
    .venv/bin/python scripts/benchmark_latency.py --compare baseline routing

Runs are written to data/benchmark-runs/<label>.json so a comparison can be made
days apart. Images run sequentially by default: concurrent runs contend for the
same providers and the timings stop meaning anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from vmlab.api.routes.analyses import PerQueryRetriever  # noqa: E402
from vmlab.config import get_settings  # noqa: E402
from vmlab.graph.nodes.validate import ImageRejected, validate_and_normalise  # noqa: E402
from vmlab.graph.pipeline import run_analysis  # noqa: E402
from vmlab.models.openrouter import OpenRouterClient  # noqa: E402
from vmlab.tenancy.session import close_pool, open_pool  # noqa: E402

BENCHMARK_DIR = REPO_ROOT / "data" / "benchmark"
RESULTS_DIR = REPO_ROOT / "data" / "benchmark-runs"

# The stages worth reporting separately. The three specialists run in parallel,
# so their sum is meaningless -- only the slowest one is on the critical path.
SPECIALISTS = ("creative_vm", "retail_psychology", "commercial")


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. n is small enough that interpolation is noise."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


async def _run_one(path: Path, client: OpenRouterClient) -> dict:
    """One image through the real graph. Never raises: a failure is a data point."""
    raw = path.read_bytes()
    settings = get_settings()

    try:
        image = validate_and_normalise(
            raw, long_edge=settings.image_long_edge_px, max_bytes=settings.max_upload_bytes
        )
    except ImageRejected as exc:
        return {"image": path.name, "ok": False, "stage": "validate", "error": str(exc)}

    started = time.monotonic()
    try:
        result, telemetry = await run_analysis(
            client=client,
            retriever=PerQueryRetriever(tenant_id=None),
            image_bytes=image.data,
            mime_type=image.mime_type,
            quality_flags=image.quality_flags,
        )
    except Exception as exc:  # noqa: BLE001 - a failed run is a measurement
        return {
            "image": path.name,
            "ok": False,
            "stage": "analysis",
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "wall_s": round(time.monotonic() - started, 1),
        }

    timings = telemetry["stage_timings_ms"]
    slowest = max((timings.get(name, 0) for name in SPECIALISTS), default=0)

    return {
        "image": path.name,
        "ok": True,
        "total_s": round(timings.get("total", 0) / 1000, 1),
        "evidence_s": round(timings.get("evidence", 0) / 1000, 1),
        "retrieval_s": round(timings.get("retrieval", 0) / 1000, 1),
        "specialists_s": round(slowest / 1000, 1),
        "synthesis_s": round(timings.get("synthesis", 0) / 1000, 1),
        "attempts": telemetry.get("stage_attempts", {}),
        "providers": telemetry.get("model_versions", {}),
        "cost_usd": telemetry.get("cost_usd", 0.0),
        "prompt_tokens": telemetry.get("prompt_tokens", 0),
        "completion_tokens": telemetry.get("completion_tokens", 0),
        # Quality signals, so a change that buys speed by thinning the analysis
        # is visible here rather than being discovered by the client.
        "items_per_perspective": {
            finding.perspective.value: len(finding.items) for finding in result.findings
        },
        "citations": sum(
            len(item.supporting_rule_ids)
            for finding in result.findings
            for item in finding.items
        ),
        "actions": len(result.synthesis.actions),
        "errors": telemetry.get("errors", []),
    }


def _summarise(runs: list[dict]) -> dict:
    ok = [r for r in runs if r["ok"]]
    totals = [r["total_s"] for r in ok]
    summary = {
        "images": len(runs),
        "completed": len(ok),
        "completion_rate": round(100 * len(ok) / len(runs), 1) if runs else 0.0,
        "p50_s": round(_percentile(totals, 0.5), 1),
        "p90_s": round(_percentile(totals, 0.9), 1),
        "max_s": round(max(totals), 1) if totals else 0.0,
        "mean_cost_usd": round(statistics.mean([r["cost_usd"] for r in ok]), 6) if ok else 0.0,
        "under_60s": sum(1 for t in totals if t <= 60),
    }
    for stage in ("evidence_s", "retrieval_s", "specialists_s", "synthesis_s"):
        values = [r[stage] for r in ok]
        summary[f"p50_{stage}"] = round(_percentile(values, 0.5), 1)
    # Retries are the usual reason two runs of the same work differ wildly.
    summary["total_attempts"] = sum(sum(r["attempts"].values()) for r in ok)
    summary["retried_stages"] = sum(
        1 for r in ok for count in r["attempts"].values() if count > 1
    )
    return summary


def _print_table(runs: list[dict], summary: dict) -> None:
    print()
    header = f"{'image':22}{'total':>7}{'evid':>7}{'retr':>7}{'spec':>7}{'synth':>7}{'cost':>9}"
    print(f"{header}  notes")
    print("-" * 92)
    for run in runs:
        if not run["ok"]:
            print(f"{run['image'][:22]:22}{'FAILED':>7}{'':>28}{'':>9}  {run['error'][:34]}")
            continue
        retried = [f"{k}x{v}" for k, v in run["attempts"].items() if v > 1]
        note = ",".join(retried) if retried else ""
        print(
            f"{run['image'][:22]:22}{run['total_s']:7.1f}{run['evidence_s']:7.1f}"
            f"{run['retrieval_s']:7.1f}{run['specialists_s']:7.1f}{run['synthesis_s']:7.1f}"
            f"{run['cost_usd']:9.5f}  {note}"
        )
    print("-" * 92)
    print(
        f"  completed {summary['completed']}/{summary['images']}"
        f" ({summary['completion_rate']}%)   under 60s:"
        f" {summary['under_60s']}/{summary['completed']}"
    )
    print(
        f"  p50 {summary['p50_s']}s   p90 {summary['p90_s']}s   max {summary['max_s']}s"
        f"   mean cost ${summary['mean_cost_usd']:.5f}"
    )
    print(
        f"  stage p50: evidence {summary['p50_evidence_s']}s  retrieval"
        f" {summary['p50_retrieval_s']}s  specialists {summary['p50_specialists_s']}s"
        f"  synthesis {summary['p50_synthesis_s']}s"
    )
    print(
        f"  model calls {summary['total_attempts']},"
        f" stages needing a retry: {summary['retried_stages']}"
    )

    providers: dict[str, int] = {}
    for run in runs:
        for value in (run.get("providers") or {}).values():
            name = value.split(" via ")[-1] if " via " in value else "(unreported)"
            providers[name] = providers.get(name, 0) + 1
    if providers:
        ranked = ", ".join(f"{k} x{v}" for k, v in sorted(providers.items(), key=lambda p: -p[1]))
        print(f"  providers served: {ranked}")
    print()


def _compare(labels: list[str]) -> None:
    print()
    print(f"{'label':16}{'p50':>8}{'p90':>8}{'max':>8}{'<60s':>8}{'cost':>10}{'calls':>7}")
    print("-" * 65)
    for label in labels:
        path = RESULTS_DIR / f"{label}.json"
        if not path.exists():
            print(f"{label:16}  (no run recorded)")
            continue
        s = json.loads(path.read_text())["summary"]
        print(
            f"{label[:16]:16}{s['p50_s']:8.1f}{s['p90_s']:8.1f}{s['max_s']:8.1f}"
            f"{s['under_60s']:>5}/{s['completed']:<2}{s['mean_cost_usd']:10.5f}{s['total_attempts']:7}"
        )
    print()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=8, help="how many images to run")
    parser.add_argument("--label", default="run", help="name this run for later comparison")
    parser.add_argument("--images", nargs="*", help="specific filenames instead of the first N")
    parser.add_argument("--compare", nargs="*", help="print a table of saved runs and exit")
    args = parser.parse_args()

    if args.compare:
        _compare(args.compare)
        return 0

    if args.images:
        paths = [BENCHMARK_DIR / name for name in args.images]
        missing = [p.name for p in paths if not p.exists()]
        if missing:
            print(f"not in {BENCHMARK_DIR}: {', '.join(missing)}")
            return 1
    else:
        # Sorted so "the first 8" means the same 8 every time; comparing a run
        # against a different subset would measure the images, not the change.
        paths = sorted(BENCHMARK_DIR.iterdir())[: args.limit]

    if not paths:
        print(f"no images in {BENCHMARK_DIR}")
        return 1

    settings = get_settings()
    print(f"vision={settings.vision_model}  reasoning={settings.reasoning_model}")
    print(f"{len(paths)} images, sequentially, label={args.label!r}")

    await open_pool()
    runs: list[dict] = []
    try:
        async with OpenRouterClient() as client:
            for index, path in enumerate(paths, start=1):
                print(f"  [{index}/{len(paths)}] {path.name} ...", end="", flush=True)
                run = await _run_one(path, client)
                runs.append(run)
                print(f" {run['total_s']}s" if run["ok"] else f" FAILED ({run['error'][:60]})")
    finally:
        await close_pool()

    summary = _summarise(runs)
    _print_table(runs, summary)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output = RESULTS_DIR / f"{args.label}.json"
    output.write_text(
        json.dumps(
            {
                "label": args.label,
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "config": {
                    "vision_model": settings.vision_model,
                    "reasoning_model": settings.reasoning_model,
                    "image_long_edge_px": settings.image_long_edge_px,
                    "retrieval_top_k": settings.retrieval_top_k,
                },
                "summary": summary,
                "runs": runs,
            },
            indent=2,
        )
    )
    print(f"written to {output.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
