"""The memory recall eval (evals/memory_recall.py): 41 facts, 32 questions in J's words, 15 that nothing answers.
Floors, so a change to recall that makes it worse fails here. The numbers before and after the fixes it found are in
docs/VESPER-2.3-phone-memory.md."""

import pytest

from evals import memory_recall as M


@pytest.fixture(scope="module")
def results():
    return M.run()


def test_the_set_is_the_size_it_says():
    assert len(M.FACTS) == 41 and len(M.QUESTIONS) == 32 and len(M.NEGATIVES) == 15
    keys = {k for k, *_ in M.FACTS}
    assert all(want <= keys for _, want in M.QUESTIONS) and set(M.CHANGED) | set(M.CHANGED.values()) <= keys


def test_words_only(results):
    r = results["words only"]
    assert r["recall@3"] >= 0.84 and r["wrong_recall"] == 0 and r["old_answer"] == 0


def test_with_embeddings(results):
    r = results["stand-in embeddings"]
    assert r["recall@3"] >= 0.93 and r["wrong_recall"] <= 0.07 and r["newest_first"] == 1


def test_a_change_that_was_not_linked(results):
    r = results["stand-in embeddings, change not linked"]
    assert r["newest_first"] == 1  # the old version can still show, but below the new one (and dated)
    # every change gets retired one way or the other: the extractor sees the old fact, or the tidy-up asks J
    assert r["extractor_sees_old"] + r["tidy_up_asks"] >= 1


def test_the_table_and_the_command_line(capsys):
    assert M.main(["-v"]) == 0
    out = capsys.readouterr().out
    assert "| words only |" in out and "tidy-up asks" in out
