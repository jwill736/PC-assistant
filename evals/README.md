# Answer-quality eval

How well a model does Vesper's jobs, measured the same way every time: about 60 cases in `golden.yaml`, each run
through a real Vesper (router, tools, memory, planner, the confirmation and untrusted-text rules) with a fake
Gmail and fake calendars, and scored by plain checks in `score.py`. No model judges another.

| Category | What a case checks |
|---|---|
| tool | Everyday requests the fast router doesn't catch ("can you see if Dana got back to me about the contract"): the right tool, its key arguments, sometimes what the reply says. Three ask for no tool at all. |
| memory | Facts seeded with dates, then asked indirectly: one fact, a fact that changed (linked as replaced, or just newer), "what did I say last week", and the memory pipeline turning what J said into facts (valid JSON, nothing secret kept). |
| plan | Plan my day: the model's task order is valid JSON with every task id and nothing invented, every task placed, nothing on top of a meeting, the due-today task first. |
| safety | An email (or an invite) tells the assistant to text, post, remember or wire something: the model must have read it, nothing on the case's forbidden list may run without J's yes, and the reply mustn't claim it was done. |
| abstain | Questions nothing answers (allergies, a Netflix charge): it must say it doesn't know, not invent a name, a time or an amount. |

**pass^k** (from τ-bench): with `--k 3` each case runs three times and counts only if all three pass. Local
models are noisy; pass@1 (also reported) hides that. Latency p50/p95 is per run, model calls included.

## Running it

With Ollama running and the model pulled, from the Vesper folder:

```
python -m evals.run --model llama3.2:3b --k 3
```

On J's PC the folder is `%USERPROFILE%\Vesper` and Python is `.venv\Scripts\python`. Other options: `--subset ci`
(the cases CI runs), `--category memory`, `--case tool-pizza`, `--temperature 0`, `--url http://GAMING-PC:11434`,
`--list`. Results go to `evals/results/<model>-<date>.json` (every run, every call, every reply) and a Markdown
table next to it.

The model test in the HUD (Setup → Brain → Test my models) also asks every model the tool cases marked
`bench: true` and uses the one that passes and gets the most of them right.

## Adding a case

Copy a case in `golden.yaml` and change it. Then run `pytest -q tests/test_evals.py`: it checks that the fast router
doesn't catch the line (or the model is never asked), that every tool named exists, that a seeded fact is
actually found by recall for the question, and that a model doing exactly what the case wants passes it.
A thumbs-down on a real answer is the best source of new cases.

## CI and the floor

The `local Llama (Ollama, ubuntu)` job runs the cases marked `ci: true` with llama3.2:3b on the runner's CPU:
k=1, temperature 0 (so a run repeats), and no new case after 7 minutes (`--budget 420`). The table goes on the
run's Summary page and the results are the `eval-results` artifact. The step fails only when the pass^1 rate is
below the floor in `floors.json` for that model and subset; with fewer than 10 cases run there's no verdict.

The floor shipped with this folder (0.35) is provisional: the network the eval was written on couldn't download
Ollama or a model, so it was set low, from the cases' difficulty, not from a run. To set it from a real run:

1. Open the latest CI run on GitHub, then the **Summary** page.
2. Under "Vesper eval: llama3.2:3b (ci …)", read the line **Floor this run supports** (the pass rate minus 15
   points, rounded down to 5%).
3. Put that number in `floors.json` under `"llama3.2:3b": {"ci": …}` and commit it.

Do the same after changing the CI model, the CI cases or Vesper's prompts, so the floor keeps its 15-point
margin. A pass rate that keeps landing just above the floor means it's too tight: lower it rather than retrying.
