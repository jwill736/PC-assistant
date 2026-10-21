"""Answer-quality eval: how well a model does Vesper's jobs, scored without any model judging.

    python -m evals.run --model llama3.2:3b               # every case once (k=1), Vesper's temperature
    python -m evals.run --model qwen3:30b-a3b --k 3       # pass^3: a case counts only if all 3 runs pass
    python -m evals.run --model llama3.2:3b --subset ci --k 1 --temperature 0 --budget 420 --floor auto

Each run of each case builds a real Assistant (evals/world.py) over a fake Gmail and calendars, with the model
under test as its brain, says the case's line to it (or asks it to plan the day / extract memories), and scores
what happened: which tool it called and with what, what ran, what it said (evals/score.py). Writes
evals/results/<model>-<date>.json and a Markdown table next to it; --summary also appends the table to a file
(CI: $GITHUB_STEP_SUMMARY).

--floor: fail (exit 1) when the pass^k rate falls below it; "auto" reads evals/floors.json for this model and
subset. How the floor is set, and how to set it again, is in evals/README.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import golden as G
from . import score

HERE = Path(__file__).parent
RESULTS = HERE / "results"
FLOORS = HERE / "floors.json"
MIN_CASES = 10  # fewer cases than this ran (a slow runner hit the budget): no verdict, rather than a flaky one


# ---- one run of one case -------------------------------------------------------------------------------------

def run_case(llm, golden: dict, case: dict, toolset: list[str], temperature: float = 0.3) -> dict:
    """Run a case once with a fresh Assistant; returns {pass, checks, ms, reply, calls, executed, …}."""
    from .world import World  # imports the whole assistant: only when a case actually runs

    expect = case.get("expect") or {}
    row: dict = {"pass": False, "checks": {}, "ms": 0, "model_calls": 0, "model_ms": 0}
    try:
        w = World(llm, G.world(golden, case), toolset, golden.get("stubs"), temperature=temperature)
    except Exception as exc:  # a broken case: reported, and the eval goes on
        return {**row, "error": f"setting up: {type(exc).__name__}: {exc}"[:300]}
    t0 = time.perf_counter()
    try:
        kind = case.get("kind", "chat")
        if kind == "plan":
            planner = w.svc.skills.get("planner")
            planner.clock = lambda: w.at("07:30")  # the whole working day is still ahead
            plan = planner.plan_day(case.get("day", "today"), polish=True)
            row["ms"] = _ms(t0)
            answer = w.thought[0] if w.thought else None
            row["answer"] = answer
            row["checks"] = score.check_plan(answer, plan, w.svc.storage.list_tasks(), expect)
            row["plan"] = [f"{b['start'][11:16]}-{b['end'][11:16]} {b['kind']}: {b['title']}" for b in plan["blocks"]]
        elif kind == "extract":
            learned = w.svc.memory.extract([{"role": "user", "text": t} for t in case["turns"]])
            row["ms"] = _ms(t0)
            answer = w.thought[0] if w.thought else None
            row["answer"] = answer
            row["learned"] = [f["statement"] for f in learned]
            row["checks"] = score.check_extract(answer, learned, w.fact_ids, expect)
        else:
            out = w.a.handle(case["say"], source="text")
            row["ms"] = _ms(t0)
            reply = out.get("reply") or ""
            proposed = [c for call in w.recorder.calls for c in call["tool_calls"]]
            row.update(reply=reply[:400], calls=proposed, executed=w.executed,
                       pending=[n for n, _ in (w.a.pending or {}).get("calls", [])])
            checks = {"model_answered": out.get("kind") == "local"}  # not the router, not an error
            checks.update(score.check_calls(proposed, expect))
            checks.update(score.check_reply(reply, expect))
            if case["category"] == "safety":
                checks.update(score.check_safety(w.executed, reply, w.gmail.shown, expect))
            row["checks"] = checks
    except Exception as exc:  # a broken case or model: a failed run with the reason, never a crashed eval
        row["ms"] = _ms(t0)
        row["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        w.close()
    row["model_calls"] = len(w.recorder.calls)
    row["model_ms"] = sum(c["ms"] for c in w.recorder.calls)
    row["pass"] = bool(row["checks"]) and all(row["checks"].values()) and not row.get("error")
    return row


def _ms(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


# ---- a whole eval --------------------------------------------------------------------------------------------

def evaluate(llm, golden: dict, cases: list[dict], *, k: int = 1, toolset: list[str] | None = None,
             temperature: float = 0.3, budget_s: float | None = None,
             on_progress: Callable[[str], None] | None = None, subset: str | None = None) -> dict:
    toolset = toolset or G.toolset(golden, subset)
    started = time.perf_counter()
    rows, skipped = [], []
    cases = G.interleave(cases)
    for i, case in enumerate(cases):
        if budget_s is not None and time.perf_counter() - started > budget_s:
            skipped += [c["id"] for c in cases[i:]]
            if on_progress:
                on_progress(f"time budget ({budget_s:.0f} s) used: {len(skipped)} cases not run")
            break
        runs = []
        for _ in range(k):
            runs.append(run_case(llm, golden, case, toolset, temperature))
        row = {"id": case["id"], "category": case["category"], "tag": case.get("tag"),
               "say": case.get("say") or " / ".join(case.get("turns") or []) or case.get("kind"),
               "pass_k": score.pass_k(runs), "pass_1": sum(r["pass"] for r in runs) / len(runs), "runs": runs}
        rows.append(row)
        if on_progress:
            secs = " ".join(f"{r['ms'] / 1000:.1f}s" for r in runs)
            on_progress(f"[{i + 1}/{len(cases)}] {'pass' if row['pass_k'] else 'FAIL'}  {case['id']}  {secs}"
                        + ("" if row["pass_k"] else f"  ({why(runs)})"))
    return report(llm.model, rows, skipped, k=k, subset=subset or "full", temperature=temperature, toolset=toolset,
                  seconds=time.perf_counter() - started)


def why(runs: list[dict]) -> str:
    """The first thing that failed, in a few words."""
    for r in runs:
        if r.get("error"):
            return r["error"][:80]
        bad = [name for name, ok in r["checks"].items() if not ok]
        if bad:
            return ", ".join(bad)
    return ""


def _stats(rows: list[dict]) -> dict:
    secs = [r["ms"] / 1000 for row in rows for r in row["runs"]]
    n = len(rows)
    passed = sum(row["pass_k"] for row in rows)
    return {"cases": n, "pass_k": passed, "pass_k_rate": round(passed / n, 3) if n else None,
            "pass_1_rate": round(sum(row["pass_1"] for row in rows) / n, 3) if n else None,
            "p50_s": _round(score.percentile(secs, 50)), "p95_s": _round(score.percentile(secs, 95))}


def _round(v):
    return None if v is None else round(v, 1)


def report(model: str, rows: list[dict], skipped: list[str], **meta) -> dict:
    by_cat = {c: _stats([r for r in rows if r["category"] == c]) for c in G.CATEGORIES if any(
        r["category"] == c for r in rows)}
    tags = sorted({r["tag"] for r in rows if r.get("tag")})
    by_tag = {t: _stats([r for r in rows if r.get("tag") == t]) for t in tags}
    return {"model": model, "at": datetime.now().isoformat(timespec="seconds"), **meta,
            "seconds": round(meta.get("seconds", 0), 1), "totals": _stats(rows), "by_category": by_cat,
            "by_tag": by_tag, "skipped": skipped, "cases": rows}


# ---- floors and verdicts -------------------------------------------------------------------------------------

def floor_for(model: str, subset: str, path: Path = FLOORS) -> float | None:
    try:
        floors = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = floors.get(model) or {}
    value = entry.get(subset) if isinstance(entry, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def suggest_floor(rate: float | None, margin: float = 0.15) -> float | None:
    """The floor to write into floors.json after a good run: the pass rate minus a margin for noise, rounded down
    to 5%."""
    if rate is None:
        return None
    return max(0.0, int((rate - margin) * 20) / 20)


def verdict(rep: dict, floor: float | None, min_cases: int = MIN_CASES) -> tuple[str, int]:
    """("ok" | "below floor" | "inconclusive" | "no floor", exit code)."""
    t = rep["totals"]
    if floor is None:
        return "no floor", 0
    if t["cases"] < min_cases:
        return "inconclusive", 0
    if t["pass_k_rate"] < floor:
        return "below floor", 1
    return "ok", 0


# ---- writing it down -----------------------------------------------------------------------------------------

def _pct(x) -> str:
    return "–" if x is None else f"{x * 100:.0f}%"


def _s(x) -> str:
    return "–" if x is None else f"{x:.1f}"


def markdown(rep: dict) -> str:
    t, k = rep["totals"], rep.get("k", 1)
    floor, said = rep.get("floor"), rep.get("verdict", "no floor")
    lines = [f"### Vesper eval: {rep['model']} ({rep.get('subset', 'full')}, k={k}, temperature {rep.get('temperature')})",
             "",
             f"**pass^{k}: {t['pass_k']}/{t['cases']} ({_pct(t['pass_k_rate'])})** · pass@1 {_pct(t['pass_1_rate'])} · "
             f"latency p50 {_s(t['p50_s'])} s, p95 {_s(t['p95_s'])} s · {rep.get('seconds', 0):.0f} s in all"
             + (f" · floor {_pct(floor)}: **{said}**" if floor is not None else ""),
             "", f"| Category | Cases | pass^{k} | pass@1 | p50 s | p95 s |", "|---|---:|---:|---:|---:|---:|"]
    for name, s in list(rep["by_category"].items()) + [(f"memory: {t}", s) for t, s in rep.get("by_tag", {}).items()]:
        lines.append(f"| {name} | {s['cases']} | {s['pass_k']} ({_pct(s['pass_k_rate'])}) | {_pct(s['pass_1_rate'])} | "
                     f"{_s(s['p50_s'])} | {_s(s['p95_s'])} |")
    failed = [r for r in rep["cases"] if not r["pass_k"]]
    if failed:
        lines += ["", "| Failed | What failed | What it did |", "|---|---|---|"]
        for r in failed:
            run = next((x for x in r["runs"] if not x["pass"]), r["runs"][0])
            did = ", ".join(f"{c['name']}({json.dumps(c['arguments'])[:60]})" for c in run.get("calls") or [])
            if not did and "answer" in run:
                did = json.dumps(run["answer"])[:100] if run["answer"] is not None else "(no valid JSON)"
            text = (run.get("reply") or "").replace("\n", " ").replace("|", "/")[:120]
            lines.append(f"| {r['id']} | {why([run])} | {_cell(did)}{' → ' if did and text else ''}{_cell(text)} |")
    if rep.get("skipped"):
        lines += ["", f"Not run (time budget): {', '.join(rep['skipped'])}"]
    hint = suggest_floor(t["pass_k_rate"])
    if hint is not None:
        lines += ["", f"Floor this run supports (pass rate minus 15 points): {hint:.2f} "
                      f"(evals/floors.json → \"{rep['model']}\": {{\"{rep.get('subset', 'full')}\": {hint:.2f}}})"]
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def safe_name(model: str) -> str:
    return re.sub(r"[^\w.-]+", "-", model).strip("-")


def write(rep: dict, out_dir: Path = RESULTS) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    sub = rep.get("subset", "full")
    stem = f"{safe_name(rep['model'])}{'' if sub == 'full' else '-' + sub}-{datetime.now():%Y-%m-%d}"
    jpath, mpath = out_dir / f"{stem}.json", out_dir / f"{stem}.md"
    jpath.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    mpath.write_text(markdown(rep), encoding="utf-8")
    return jpath, mpath


# ---- the model -----------------------------------------------------------------------------------------------

def connect(model: str, url: str, timeout: float, num_ctx: int):
    """The model on the server (Ollama, or any OpenAI-compatible one), loaded and ready; None if it isn't there."""
    from assistant.brain import local_llm

    found = local_llm.detect(extra_url=url, defaults=False, timeout=5)
    if not found:
        return None, f"No model server answers at {url}. Start Ollama (or pass --url)."
    names = [m["name"] for m in found["models"]]
    name = next((n for n in names if n == model), None) or next((n for n in names if n.startswith(model)), None)
    if name is None:
        return None, f"{model} isn't on {url} (it has: {', '.join(names) or 'nothing'}). Run: ollama pull {model}"
    if found["kind"] != "ollama":
        return local_llm.LocalLLM(found["url"], name, found["kind"], timeout=timeout), None
    info = next(m for m in found["models"] if m["name"] == name)
    llm = local_llm.OllamaLLM(found["url"], name, caps=info.get("caps"), timeout=timeout, num_ctx=num_ctx,
                              keep_alive="10m")
    local_llm.preload(llm.http, llm.url, name, keep_alive="10m", timeout=max(timeout, 300), num_ctx=num_ctx)
    return llm, None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__.split("\n\n")[0])
    p.add_argument("--model", help="the model to test, as the server names it (e.g. llama3.2:3b)")
    p.add_argument("--url", default="http://127.0.0.1:11434", help="the model server (default: Ollama on this PC)")
    p.add_argument("--k", type=int, default=1, help="runs per case; pass^k counts a case only if all k pass")
    p.add_argument("--subset", default="full", help="full (every case) or ci (the cases marked ci: true)")
    p.add_argument("--category", action="append", choices=G.CATEGORIES, help="only these categories (repeatable)")
    p.add_argument("--case", action="append", help="only these case ids (repeatable)")
    p.add_argument("--temperature", type=float, default=0.3, help="0.3 is what Vesper uses; 0 for repeatable CI runs")
    p.add_argument("--budget", type=float, help="seconds: start no new case after this (the rest are reported as not run)")
    p.add_argument("--timeout", type=float, default=240, help="seconds per model call")
    p.add_argument("--ctx", type=int, default=8192, help="Ollama context size (Vesper's default)")
    p.add_argument("--floor", help="fail below this pass^k rate (0-1), or 'auto' for evals/floors.json")
    p.add_argument("--min-cases", type=int, default=MIN_CASES, help="fewer cases run than this: no verdict")
    p.add_argument("--out", type=Path, default=RESULTS, help="where results go (default evals/results)")
    p.add_argument("--summary", type=Path, help="also append the Markdown table to this file ($GITHUB_STEP_SUMMARY)")
    p.add_argument("--list", action="store_true", help="list the cases and exit")
    args = p.parse_args(argv)

    golden = G.load()
    cases = G.select(golden, args.subset, args.category, args.case)
    if args.list:
        for c in cases:
            print(f"{c['id']:28} {c['category']:8} {c.get('say') or c.get('kind')}")
        print(f"{len(cases)} cases")
        return 0
    if not args.model:
        p.error("--model is required (e.g. --model llama3.2:3b)")
    if not cases:
        p.error("no cases match")
    llm, problem = connect(args.model, args.url, args.timeout, args.ctx)
    if llm is None:
        print(problem, file=sys.stderr)
        return 2
    print(f"{len(cases)} cases × {args.k} with {llm.label}", file=sys.stderr)
    rep = evaluate(llm, golden, cases, k=args.k, temperature=args.temperature, budget_s=args.budget,
                   on_progress=lambda line: print(line, file=sys.stderr, flush=True), subset=args.subset)
    floor = floor_for(args.model, args.subset) if args.floor == "auto" else (float(args.floor) if args.floor else None)
    said, code = verdict(rep, floor, args.min_cases)
    rep.update(floor=floor, verdict=said)
    jpath, mpath = write(rep, args.out)
    table = markdown(rep)
    print(table)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(table + "\n")
    print(f"wrote {jpath} and {mpath}", file=sys.stderr)
    if said == "below floor":
        print(f"pass^{args.k} rate {_pct(rep['totals']['pass_k_rate'])} is below the floor {_pct(floor)}", file=sys.stderr)
    elif said == "inconclusive":
        print(f"only {rep['totals']['cases']} cases ran (fewer than {args.min_cases}): no verdict", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
