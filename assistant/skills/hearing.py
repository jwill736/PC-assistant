"""Hearing: the voice test (Setup → Voice test) and the words Vesper learns to hear right.

The voice test records 30–40 of your own commands and a few minutes of only background, replays them through the
live capture chain and suggests settings from your numbers (voice/bench.py). Nothing changes until you click Apply.

Words: say or type "you misheard Balatro as Belatro" (or "I said Niamh, not Niam"), and it asks before it learns
the fix; retype in the HUD what you just said with a word fixed, and it offers the fix with a button. Learned fixes
go to data/settings.yaml (voice.corrections) and are listed on the panel with Remove (voice/vocab.py).

Settings (config.yaml, voice): corrections, hotwords_score (2.0; 0 = don't expect names), end_of_turn
(silence | smart), turn_pause_ms, turn_max_silence_ms.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid

from ..brain.router import Intent, clean
from ..brain.tools import Tool, obj
from ..voice import bench, vocab
from . import Skill

log = logging.getLogger(__name__)

RETYPE_S = 90       # typing what you said this soon after saying it counts as fixing it
KEEP_SUGGESTIONS = 5
RECENT_S = 120      # "it's Niamh, not Niam" counts when "Niam" was in something heard this recently


class Hearing(Skill):
    name, title = "hearing", "Voice test & words"

    def __init__(self, svc, clock=time.time, threads: bool = True, recorder=None):
        super().__init__(svc)
        self.clock, self.threads = clock, threads
        self.words = vocab.Corrections(svc.cfg)
        self.suggestions: list[dict] = []
        self._heard: tuple[str, float] | None = None  # the last voice command: (text, when)
        self._retyped: tuple[str, float] | None = None  # the one a retype was already checked against
        self._notes: list[str] = []
        self.runtime = None
        self._recorder = recorder  # (cfg, bus, lines, background_s, speak) -> Recorder-like (tests)
        self._active = None        # the Recorder while recording (Cancel stops it)
        self.bench = {"state": "idle"}  # idle | recording | analysing | done | error | cancelled
        self.folder = svc.cfg.data_dir / "bench" / "voice-test"
        self.result_file = svc.cfg.data_dir / "bench" / "voice-test.json"
        self.report = self._load_report()
        self._said = ""  # J's last "you misheard Balatro as …", with his capitals
        svc.bus.on(self.on_event)

    def start(self, runtime) -> None:
        self.runtime = runtime
        self.publish()

    # ---- words: "you misheard X as Y", retyping ------------------------------------------------------------------
    def route(self, t: str):
        found = vocab.parse_correction(t)
        if found is None:
            return None
        heard, meant, near = found
        if near and not (vocab.alike(heard, meant) >= 0.6 and self._recently_heard(heard)):
            return None  # "it's Tuesday, not Monday": he's changing what he asked for, not correcting my ears
        return Intent("tool", "learn_word", {"heard": heard, "meant": meant})

    def _recently_heard(self, phrase: str) -> bool:
        if not self._heard or self.clock() - self._heard[1] > RECENT_S:
            return False
        return f" {vocab.norm(phrase)} " in f" {vocab.norm(self._heard[0])} "

    def _as_typed(self, meant: str) -> str:
        """The words as J wrote them ("Balatro"): the router hands skills lower case."""
        if meant != meant.lower():
            return meant
        original = vocab.words(self._said)
        want = meant.split()
        for i in range(len(original) - len(want) + 1):
            if [w.lower() for w in original[i:i + len(want)]] == [w.lower() for w in want]:
                return " ".join(original[i:i + len(want)])
        return meant

    def on_event(self, ev: dict) -> None:
        if ev.get("type") != "user_said":
            return
        d = ev.get("data") or {}
        text, source = d.get("text"), d.get("source")
        if not isinstance(text, str):
            return
        if vocab.parse_correction(clean(text)):
            self._said = text  # kept for the yes that follows
        if source == "voice":
            self._heard = (text, self.clock())
        elif source == "text" and self._heard and self._heard is not self._retyped \
                and self.clock() - self._heard[1] <= RETYPE_S:
            self._retyped = self._heard  # one retype per sentence said
            self._suggest(self._heard[0], text)

    def _suggest(self, said: str, typed: str) -> None:
        known = {i["heard"] for i in self.words.items()}
        new = []
        for heard, meant in vocab.retype_fixes(said, typed):
            if heard in known or vocab.refuse(heard, meant, self.svc.cfg["assistant"].get("wake_words") or ()):
                continue
            item = {"id": uuid.uuid4().hex[:8], "heard": heard, "meant": meant, "said": said, "at": self.clock()}
            self.suggestions = [s for s in self.suggestions if s["heard"] != heard][-(KEEP_SUGGESTIONS - 1):] + [item]
            new.append(item)
        if new:
            self.publish()
            self.svc.bus.publish("hearing_suggestion", {"items": new})  # the HUD shows a "Remember" button

    def learn_word(self, heard: str, meant: str) -> dict:
        out = self.words.add(heard, self._as_typed(meant))
        if out.get("ok"):
            self.suggestions = [s for s in self.suggestions if s["heard"] != out["heard"]]
            self.publish()
        return out

    def tools(self):
        def tier(a):  # nothing to ask about when it can't be learned anyway
            return 0 if vocab.refuse(a.get("heard", ""), a.get("meant", ""),
                                     self.svc.cfg["assistant"].get("wake_words") or ()) else 2

        return [Tool("learn_word", "Remember that a word the speech recognition keeps mishearing (heard) should be "
                     "written as another (meant), e.g. heard 'belatro', meant 'Balatro'. Only when the user says so.",
                     obj({"heard": {"type": "string"}, "meant": {"type": "string"}}, ["heard", "meant"]),
                     self.learn_word, tier=tier,
                     confirm_text=lambda a: f"always write “{self._as_typed(str(a.get('meant', '')))}” when I hear "
                                          f"“{a.get('heard')}”")]

    def say(self, tool, args, result):
        if tool != "learn_word":
            return None
        if not result.get("ok"):
            return result.get("error") or "I couldn't learn that one."
        return f"Got it. When I hear {result['heard']}, I'll write {result['meant']}."

    # ---- the voice test ----------------------------------------------------------------------------------------------
    def _load_report(self) -> dict | None:
        try:
            return json.loads(self.result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _emit(self, **data) -> None:
        self.svc.bus.publish("voice_bench", data, sticky=data.get("step") in ("done", "error", "cancelled"))

    def _set(self, state: str, **extra) -> None:
        self.bench = {"state": state, **extra}
        self.publish()

    def suggested_phrases(self) -> list[str]:
        rows = [r for r in self.svc.storage.recent_log(600) if r.get("role") == "user" and r.get("source") == "voice"]
        return bench.phrases(str(self.svc.cfg["assistant"]["name"]), [r["text"] for r in rows])

    def _blocked(self) -> str | None:
        rt = self.runtime
        if rt is None or getattr(rt, "listener", None) is None:
            return "Voice is off (voice.enabled: false)."
        if self.bench["state"] in ("recording", "analysing"):
            return "The voice test is already running."
        if getattr(rt, "calibrator", None) is not None:
            return "Calibration is running: finish it first."
        return None

    def start_test(self, lines: list[str], background_s: float) -> dict:
        why = self._blocked()
        if why:
            return {"ok": False, "error": why}
        lines = [" ".join(str(x).split()) for x in lines if str(x).strip()][:60]
        if len(lines) < 5:
            return {"ok": False, "error": "Give it at least 5 commands to read."}
        background_s = max(30.0, min(600.0, float(background_s or 180)))
        self._set("recording")
        self._run(self._record_then_analyse, lines, background_s)
        return {"ok": True}

    def rerun(self) -> dict:
        why = self._blocked()
        if why:
            return {"ok": False, "error": why}
        saved = bench.load_recordings(self.folder)
        if saved is None:
            return {"ok": False, "error": "No saved recordings: run the test first."}
        self._set("analysing")
        self._run(self._analyse, *saved)
        return {"ok": True}

    def cancel(self) -> dict:
        """Stops the recording (the minute of analysis after it isn't stopped)."""
        if self._active is not None:
            self._active.cancelled.set()
        return {"ok": True}

    def _run(self, fn, *args) -> None:
        if self.threads:
            threading.Thread(target=fn, args=args, name="voice-test", daemon=True).start()
        else:
            fn(*args)

    def _speak(self, text: str) -> None:
        live = getattr(self.svc, "live", None)
        if live and live():
            return  # not on air
        speaker = getattr(self.runtime, "speaker", None)
        if speaker is not None:
            speaker.say(text, expects_reply=False)

    def _record_then_analyse(self, lines: list[str], background_s: float) -> None:
        from ..voice import calibrate as calib

        lst = self.runtime.listener
        lst.pause("voice test")  # it owns the mic: nothing it hears is a command
        try:
            make = self._recorder or (lambda cfg, bus, ln, bg, speak: bench.Recorder(
                cfg, bus, calib.mic_recorder((cfg.get("voice") or {}).get("input_device")), ln, bg, speak))
            self._active = make(self.svc.cfg, self.svc.bus, lines, background_s, self._speak)
            got = self._active.run()
        except Exception as exc:
            log.exception("voice test recording failed")
            lst.resume("voice test")
            self._fail(exc)
            return
        finally:
            self._active = None
        if got.get("cancelled"):
            lst.resume("voice test")
            self._set("cancelled")
            return
        try:
            bench.save_recordings(self.folder, got["commands"], got["background"])
        except OSError as exc:  # still analysed, just not kept for a re-run
            log.warning("voice test recordings not saved: %s", exc)
        self._analyse(got["commands"], got["background"])  # gives the mic back when it's done

    def _fail(self, exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"
        self._emit(step="error", error=error)
        self._set("error", error=error)

    def _chain(self, progress) -> bench.Chain:
        from ..voice import turn, vad, wakeword

        lst, ver = self.runtime.listener, getattr(self.runtime, "verifier", None)
        models_dir = lst.models_dir
        engine = lst.ensure_stt()
        notes = []
        try:
            progress("Getting the end-of-turn model ready (9 MB, the first time)…")
            turn_model = turn.load(models_dir)
        except Exception as exc:
            turn_model = None
            notes.append(f"Natural end of turn couldn't be tried: {type(exc).__name__}: {exc}")
        try:
            wake = wakeword.load(lst.cfg, models_dir)  # its own copy: the live one keeps its state
        except Exception as exc:
            wake = None
            notes.append(f"The name model didn't load: {exc}")
        name = next((n for n in (wake.names if wake else []) if lst.trigger_kind(n) == "name"), None)
        emb = ver.embedder() if ver is not None and ver.active else None
        profile = ver.profile if emb is not None else None
        cfg = dict(lst.cfg)
        self._notes = notes
        return bench.Chain(
            cfg=cfg, wake_words=list(lst.wake_words), transcribe=engine.transcribe, hints=lst._hints(),
            segmenter=lambda c: vad.make_segmenter(c if turn_model is not None else {**c, "end_of_turn": "silence"},
                                                   models_dir, turn_model=turn_model),
            correct=lst.correct, wake=wake, wake_name=name, wake_mode=lst.wake_mode(),
            score=(lambda v: profile.score(emb.embed(v))) if profile is not None else None,
            threshold=profile.threshold if profile is not None else None, turn_model=turn_model)

    def _analyse(self, commands: list, background) -> None:
        lst = self.runtime.listener
        lst.pause("voice test")  # one speech engine, used by one thread at a time
        try:
            def progress(text: str) -> None:
                self._emit(step="analysing", prompt=text)
                self.bench = {"state": "analysing", "detail": text}

            self._set("analysing", detail="Starting…")
            self._notes = []
            report = bench.RealBench(self._chain(progress), progress, clock=self.clock).run(commands, background)
            report["notes"] = self._notes
            self.result_file.parent.mkdir(parents=True, exist_ok=True)
            self.result_file.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
            self.report = report
            self._emit(step="done")
            self._set("done")
        except Exception as exc:
            log.exception("voice test analysis failed")
            self._fail(exc)
        finally:
            lst.resume("voice test")

    def apply(self, keys: list[str]) -> dict:
        """J's click: save the recommendations he ticked, then restart the listener so they're live."""
        if not self.report or not self.report.get("recommendations"):
            return {"ok": False, "error": "Nothing to apply: run the voice test first."}
        rt = self.runtime
        done = bench.apply(self.svc.cfg, self.report["recommendations"], [str(k) for k in keys or []],
                           getattr(rt, "verifier", None))
        if not done:
            return {"ok": False, "error": "Tick at least one suggestion."}
        self.report["applied"] = sorted(set(self.report.get("applied") or []) | set(done))
        self.result_file.write_text(json.dumps(self.report, indent=1, default=str), encoding="utf-8")
        audit = getattr(getattr(getattr(rt, "assistant", None), "tools", None), "audit", None)
        if audit is not None:
            audit.write(tool="voice_test_apply", tier=1, source="hud", utterance="", outcome="ok",
                        args={r["key"]: f"{r['current']} → {r['value']}" for r in self.report["recommendations"]
                              if r["key"] in done})
        self._restart_listener()
        self.publish()
        return {"ok": True, "applied": done}

    def set_end_of_turn(self, mode: str) -> dict:
        """The panel's switch: silence | smart (natural end of turn), saved and live at once."""
        from ..config import save_setting

        if mode not in ("silence", "smart"):
            return {"ok": False, "error": "End of turn is silence or smart."}
        save_setting(self.svc.cfg, "voice.end_of_turn", mode)
        self._restart_listener()
        self.publish()
        return {"ok": True, "end_of_turn": mode}

    def _restart_listener(self) -> None:
        rt = self.runtime
        lst = getattr(rt, "listener", None)
        if lst is not None:
            lst.restart()  # a fresh loop reads the new voice detector, end of turn and name settings
        if rt is not None and hasattr(rt, "voice_status"):
            self.svc.bus.publish("voice_profile", rt.voice_status(), sticky=True)

    def delete_recordings(self) -> dict:
        if self.bench["state"] in ("recording", "analysing"):
            return {"ok": False, "error": "Wait for the test to finish (or cancel it)."}
        shutil.rmtree(self.folder, ignore_errors=True)
        self.publish()
        return {"ok": True}

    # ---- HUD ---------------------------------------------------------------------------------------------------------
    def actions(self):
        return {
            "add": lambda b: self.learn_word(str(b.get("heard", "")), str(b.get("meant", ""))),
            "remove": lambda b: self._publish_after(self.words.remove(str(b.get("heard", "")))),
            "dismiss": lambda b: self._dismiss(str(b.get("id", ""))),
            "phrases": lambda b: {"ok": True, "phrases": self.suggested_phrases()},
            "start": lambda b: self.start_test(list(b.get("phrases") or []), b.get("background_s", 180)),
            "rerun": lambda b: self.rerun(),
            "cancel": lambda b: self.cancel(),
            "apply": lambda b: self.apply(list(b.get("keys") or [])),
            "delete": lambda b: self.delete_recordings(),
            "end_of_turn": lambda b: self.set_end_of_turn(str(b.get("mode", ""))),
        }

    def _publish_after(self, out: dict) -> dict:
        self.publish()
        return out

    def _dismiss(self, sid: str) -> dict:
        self.suggestions = [s for s in self.suggestions if s["id"] != sid]
        self.publish()
        return {"ok": True}

    def status(self) -> dict:
        rep = self.report
        return {"words": self.words.items(), "suggestions": list(self.suggestions), "bench": dict(self.bench),
                "end_of_turn": (self.svc.cfg.get("voice") or {}).get("end_of_turn", "silence"),
                "recordings": (self.folder / "commands.json").exists(),
                "report": {k: v for k, v in rep.items() if k != "tried"} if rep else None}
