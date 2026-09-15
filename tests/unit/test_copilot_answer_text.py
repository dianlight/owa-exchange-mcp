"""Pure-logic tests for `_copilot_answer_text` — no mailbox, no browser, no Playwright.

Copilot has no API, so `tools/copilot.py` reads its answer out of the chat
pane's DOM. `pane.inner_text()` returns the *whole* iframe though — date
separator, role markers, the echoed prompt, the answer, footer disclaimer — and
the tools' documented contract is `{"status": "ok", "text": <answer>}`. The
first successful live run (2026-09-11) returned all of it verbatim when the
answer was the single word "PONG"; LIVE_PANE below is that exact text.

Extraction is marker-first: everything after the last "Copilot said:" is the
newest reply. That wording was *observed* live rather than guessed, but on one
tenant only, so an unrecognised marker falls back to diffing against the pane's
pre-submit text, which needs no knowledge of the transcript's wording.

All of it is ordinary string logic, so none of it needs a mailbox, and two
behaviours here are deliberate choices that are easy to invert by accident:
the *last* marker wins (not the first), and an empty result falls back to the
full text rather than "" — an empty string would read as a successful empty
answer.

Run:
    python -m tests.unit.test_copilot_answer_text
"""

import asyncio
import sys

from exchange_mcp.browser_session import (
    BrowserSession,
    _COPILOT_SETTLE_POLLS,
    _COPILOT_SETTLE_POLLS_NO_MARKER,
    _copilot_answer_marker_index,
    _copilot_answer_text,
    _copilot_has_progress_line,
)

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


# A realistic pane before anything is asked: branding, a greeting, prompt chips
# and a footer disclaimer, all of which are non-empty and already stable.
BASELINE = """Copilot
Ciao! Come posso aiutarti?
Riassumi questa email
Trova i punti chiave
Copilot può commettere errori. Verifica le informazioni importanti."""

PROMPT = "Reply with exactly the word PONG and nothing else."


def test_appended_answer_is_isolated():
    """The ordinary case: pane grows by prompt echo + answer."""
    after = BASELINE + f"\n{PROMPT}\nPONG"
    check("appended", _copilot_answer_text(BASELINE, after, PROMPT), "PONG")


def test_prompt_echo_is_dropped():
    """The pane echoes the submitted prompt; it is not part of the answer."""
    after = BASELINE + f"\n{PROMPT}\nHere is your answer."
    result = _copilot_answer_text(BASELINE, after, PROMPT)
    check("no prompt echo", PROMPT in result, False)
    check("answer kept", result, "Here is your answer.")


def test_multiline_answer_preserves_order():
    after = BASELINE + f"\n{PROMPT}\nFirst point.\nSecond point.\nThird point."
    check("multiline", _copilot_answer_text(BASELINE, after, PROMPT),
          "First point.\nSecond point.\nThird point.")


def test_multiline_prompt_is_dropped_entirely():
    """coach_draft embeds a whole draft in the prompt, over several lines."""
    prompt = "Give me coaching feedback on this draft:\n\nThanks for the update.\nPlease send details."
    after = BASELINE + f"\n{prompt}\nYour tone is clear and polite."
    result = _copilot_answer_text(BASELINE, after, prompt)
    check("draft lines dropped", "Thanks for the update." in result, False)
    check("coaching kept", result, "Your tone is clear and polite.")


def test_chrome_that_persists_is_dropped():
    """Chips/disclaimer usually survive; none of them may reach the caller."""
    after = f"""Copilot
Ciao! Come posso aiutarti?
Copilot può commettere errori. Verifica le informazioni importanti.
{PROMPT}
PONG"""
    check("persistent chrome dropped",
          _copilot_answer_text(BASELINE, after, PROMPT), "PONG")


def test_wholesale_rerender_falls_back_to_full_text():
    """Deliberate: return everything rather than nothing.

    If the pane replaces its content instead of appending, every line is "new"
    only if it differs from the baseline — but if it *matches* the baseline
    exactly while an answer sits elsewhere, the diff is empty. Returning "" would
    look like a successful empty answer; the full text is at least inspectable.
    """
    check("identical text", _copilot_answer_text(BASELINE, BASELINE, PROMPT),
          BASELINE.strip())


# ------------------------------------------------------------------
# The transcript-marker path, from the real 2026-09-11 live run
# ------------------------------------------------------------------

# Verbatim shape of what ask_copilot returned on the first successful live call,
# when the answer was the single word "PONG". Every line except PONG is
# transcript furniture, and all of it reached the caller.
LIVE_PANE = """Oggi
You said:
Copilot said:
Copilot
PONG
Il contenuto generato dall'IA potrebbe non essere corretto"""


def test_live_pane_yields_only_the_answer():
    check("live PONG", _copilot_answer_text("", LIVE_PANE, PROMPT), "PONG")


def test_marker_path_needs_no_baseline():
    """The marker is what makes a first-turn answer extractable at all.

    On the first call the pane's pre-submit text is unrelated to the transcript,
    so diffing alone leaves every furniture line in place.
    """
    check("no baseline, marker present",
          _copilot_answer_text("", LIVE_PANE, PROMPT), "PONG")


def test_last_marker_wins_in_a_multi_turn_pane():
    """A reused pane accumulates turns; only the newest reply is the answer."""
    pane = """Oggi
You said:
Copilot said:
Copilot
First answer.
You said:
Copilot said:
Copilot
Second answer.
Il contenuto generato dall'IA potrebbe non essere corretto"""
    check("newest turn", _copilot_answer_text("", pane, PROMPT), "Second answer.")


def test_multiline_answer_after_marker():
    pane = """Oggi
You said:
Copilot said:
Copilot
Here is a summary:
- First point
- Second point
Il contenuto generato dall'IA potrebbe non essere corretto"""
    check("multiline after marker", _copilot_answer_text("", pane, PROMPT),
          "Here is a summary:\n- First point\n- Second point")


def test_localised_marker_is_recognised():
    pane = "Oggi\nCopilot ha detto:\nCopilot\nEcco la risposta."
    check("it marker", _copilot_answer_text("", pane, PROMPT), "Ecco la risposta.")


def test_english_disclaimer_is_stripped_too():
    pane = ("Today\nYou said:\nCopilot said:\nCopilot\nDone.\n"
            "AI-generated content may be incorrect")
    check("en disclaimer", _copilot_answer_text("", pane, PROMPT), "Done.")


def test_answer_mentioning_copilot_is_not_treated_as_chrome():
    """Chrome lines match exactly, so a sentence about Copilot survives."""
    pane = "Oggi\nCopilot said:\nCopilot\nCopilot can help you draft replies."
    check("mention kept", _copilot_answer_text("", pane, PROMPT),
          "Copilot can help you draft replies.")


def test_unrecognised_marker_falls_back_to_baseline_diff():
    """A redesigned or unlisted transcript must still degrade usefully."""
    pane = BASELINE + "\nKopilot sagte irgendwas:\nThe actual answer."
    result = _copilot_answer_text(BASELINE, pane, PROMPT)
    check("fallback finds answer", "The actual answer." in result, True)
    check("fallback drops baseline chrome", "Riassumi questa email" in result, False)


def test_marker_with_empty_answer_falls_back_to_full_text():
    """An answer marker with nothing usable after it must not return ""."""
    pane = "Oggi\nYou said:\nCopilot said:\nCopilot\nAI-generated content may be incorrect"
    result = _copilot_answer_text("", pane, PROMPT)
    check("non-empty", bool(result.strip()), True)
    check("returns full text", result, pane.strip())


def test_composer_placeholder_below_the_transcript_is_stripped():
    """The composer sits *below* the messages, so the marker cut doesn't remove it.

    Observed live on 2026-09-11: after the marker fix, ask_copilot returned
    'PONG\\nInvia un messaggio a Copilot' — the input box's own placeholder. It
    needs no table of localised placeholder strings, because it was already on
    screen before the prompt was submitted, i.e. already in `baseline`.
    """
    baseline = "Copilot\nInvia un messaggio a Copilot"
    pane = "Oggi\nYou said:\nCopilot said:\nCopilot\nPONG\nInvia un messaggio a Copilot"
    check("placeholder stripped", _copilot_answer_text(baseline, pane, PROMPT), "PONG")


def test_placeholder_without_baseline_is_not_silently_kept_as_answer():
    """Without a baseline the placeholder can't be identified — but it must not
    become the *only* content returned, since the real answer is still there."""
    pane = "Oggi\nCopilot said:\nCopilot\nPONG\nInvia un messaggio a Copilot"
    result = _copilot_answer_text("", pane, PROMPT)
    check("answer present", "PONG" in result, True)


def test_no_baseline_still_strips_chrome():
    """A pane whose pre-submit read failed has no basis to diff against.

    Chrome removal is not conditional on that, though: the bare product name is
    furniture whether or not a baseline exists, so "Copilot\\nPONG" is still an
    answer of "PONG".
    """
    check("no baseline", _copilot_answer_text("", "Copilot\nPONG", PROMPT), "PONG")


def test_whitespace_only_differences_do_not_count_as_answer():
    after = BASELINE + "\n   \n\t\n"
    check("whitespace ignored", _copilot_answer_text(BASELINE, after, PROMPT),
          BASELINE.strip())


def test_repeated_answer_line_matching_chrome_is_dropped():
    """Known limitation, pinned so a future selector fix is measured against it.

    An answer line identical to a chrome line is indistinguishable from chrome
    by diffing alone. This is exactly why the diff is a stopgap and a real
    selector is the fix.
    """
    after = BASELINE + f"\n{PROMPT}\nRiassumi questa email"
    # "Riassumi questa email" is also a prompt chip in BASELINE, so it is lost
    # and the fallback returns the full text rather than a wrong short answer.
    result = _copilot_answer_text(BASELINE, after, PROMPT)
    check("collision falls back", result, after.strip())


def test_prompt_not_supplied_still_strips_chrome():
    """prompt is optional; chrome removal must not depend on it."""
    after = BASELINE + "\nPONG"
    check("no prompt arg", _copilot_answer_text(BASELINE, after), "PONG")


def test_follow_up_suggestion_chips_are_stripped_via_button_labels():
    """Chips render *after* the answer, so nothing positional removes them.

    Observed live 2026-09-11: ask_copilot returned
    'PONG\nStart a new question\nSummarize a topic\nDraft a message'. The
    wording is generated per answer, so a hint table cannot cover it — but they
    are buttons, and the caller passes their labels in.
    """
    pane = """Oggi
You said:
Copilot said:
Copilot
PONG
Start a new question
Summarize a topic
Draft a message"""
    buttons = {"Start a new question", "Summarize a topic", "Draft a message", "Send"}
    check("chips stripped",
          _copilot_answer_text("", pane, PROMPT, buttons), "PONG")


def test_button_labels_are_optional():
    """chrome_lines defaults to None; behaviour must be unchanged without it."""
    pane = "Oggi\nCopilot said:\nCopilot\nPONG"
    check("no buttons arg", _copilot_answer_text("", pane, PROMPT), "PONG")
    check("empty buttons set", _copilot_answer_text("", pane, PROMPT, set()), "PONG")


def test_answer_identical_to_a_button_label_falls_back_not_empty():
    """A one-word answer colliding with a chip must not vanish silently."""
    pane = "Oggi\nCopilot said:\nCopilot\nDraft a message"
    result = _copilot_answer_text("", pane, PROMPT, {"Draft a message"})
    check("non-empty on collision", bool(result.strip()), True)


# ----------------------------------------------------------------------
# Settle-rule helpers: "has Copilot actually finished?"
# ----------------------------------------------------------------------
#
# The 2026-09-15 smoke re-run returned {"status": "ok"} carrying this exact
# text. It is *new* relative to the baseline and *stable* for seconds, so
# neither baseline-diffing nor waiting for stability could reject it.
IN_FLIGHT_PANE = """Copilot
In corso…
Scarica l'app per dispositivi mobili Copilot
Ottieni risposte, analizza documenti e crea contenuti in mobilità."""

# The answer that was hiding behind it, abridged. Note the action-item table:
# inner_text() renders cells tab-separated, so "In corso" here is a *status
# value* inside a line, never a line of its own.
FINISHED_PANE = """Oggi
You said:
Copilot said:
Copilot
Riassunto del thread
Contesto Il thread riguarda la revisione della documentazione tecnica.
Responsabile\tAzione\tStato
Giuseppe Colosimo\tRiesaminare la documentazione\tIn corso
Giuseppe Colosimo\tVerificare i commenti aperti\tDa fare"""


def test_progress_line_is_detected():
    """The literal state that caused the false success must read as in-flight."""
    check("in-flight pane", _copilot_has_progress_line(IN_FLIGHT_PANE), True)
    check("bare marker", _copilot_has_progress_line("In corso"), True)


def test_progress_hint_normalises_trailing_ellipsis():
    """Copilot writes "In corso…"; the ASCII spelling must match too."""
    for variant in ("In corso…", "In corso...", "IN CORSO", "  in corso  ", "Thinking…"):
        check(f"variant {variant!r}", _copilot_has_progress_line(variant), True)


def test_progress_hint_must_be_a_whole_line():
    """A status *value* inside a line is not a progress marker.

    This is the regression that matters most: a substring test would reject
    the correct summary in FINISHED_PANE, whose action-item table legitimately
    contains "In corso" as a cell.
    """
    check("table cell is not progress",
          _copilot_has_progress_line(FINISHED_PANE), False)
    check("prose mentioning it is not progress",
          _copilot_has_progress_line("Il lavoro e' in corso di revisione."), False)
    check("finished answer with no progress text",
          _copilot_has_progress_line("Riassunto del thread\nTutto completato."), False)


def test_progress_line_ignores_empty_and_blank_input():
    check("empty", _copilot_has_progress_line(""), False)
    check("blank lines", _copilot_has_progress_line("\n\n   \n"), False)


def test_answer_marker_index_finds_the_last_turn():
    """Shared by the extractor and the settle check, so pin both behaviours."""
    lines = ["Oggi", "Copilot said:", "first", "You said:", "Copilot said:", "second"]
    check("last marker wins", _copilot_answer_marker_index(lines), 4)
    check("absent marker is None",
          _copilot_answer_marker_index(["Oggi", "In corso…"]), None)
    check("localised marker found",
          _copilot_answer_marker_index(["Copilot ha detto:", "ciao"]), 0)


def test_in_flight_pane_has_no_answer_marker():
    """Why the marker is the discriminator: mid-flight there is no turn yet.

    Together with the previous test this is the whole fix in miniature - the
    in-flight pane has no marker *and* has a progress line, so the settle rule
    refuses it; the finished pane has a marker, so it settles on the short path.
    """
    check("in-flight has no marker",
          _copilot_answer_marker_index(IN_FLIGHT_PANE.splitlines()), None)
    check("finished pane has a marker",
          _copilot_answer_marker_index(FINISHED_PANE.splitlines()) is not None, True)


def test_no_marker_path_demands_more_stability():
    """The weaker evidence path must never be the more trusting one."""
    check("no-marker threshold is stricter",
          _COPILOT_SETTLE_POLLS_NO_MARKER > _COPILOT_SETTLE_POLLS, True)
    check("marker threshold still requires repetition",
          _COPILOT_SETTLE_POLLS >= 2, True)


def test_extractor_still_returns_chrome_when_that_is_all_there_is():
    """The extractor is unchanged: refusing to settle is the polling loop's job.

    Documented so nobody "fixes" this by making the extractor return "" -
    an empty string would read as a successful empty answer, which is exactly
    the failure mode `_copilot_answer_text` already falls back to avoid.
    """
    result = _copilot_answer_text("Copilot", IN_FLIGHT_PANE, PROMPT)
    check("extractor yields something inspectable", bool(result.strip()), True)


def _settle(script, timeout):
    """Drive the real polling loop against a scripted pane, with no browser.

    The helpers above can all pass while the *composition* in
    `_async_copilot_wait_and_read` still settles on chrome - that loop is where
    the marker, progress and stability conditions actually meet, so it gets
    exercised directly. A stub `self` and a fake locator are enough: the only
    things the loop asks of the pane are `inner_text()` and a stop-button count.
    """
    class FakeLocator:
        async def count(self):
            return 0  # no stop control matched - as observed live on 2026-09-15

    class FakePane:
        def __init__(self, frames):
            self.frames = list(frames)
            self.reads = 0

        async def inner_text(self):
            value = self.frames[min(self.reads, len(self.frames) - 1)]
            self.reads += 1
            return value

        def get_by_role(self, *args, **kwargs):
            return FakeLocator()

    class Stub:
        async def _async_copilot_button_labels(self, pane):
            return set()

    return asyncio.run(
        BrowserSession._async_copilot_wait_and_read(
            Stub(), FakePane(script), timeout=timeout,
            baseline="Copilot\nCiao! Come posso aiutarti?",
            prompt="Summarize this thread.",
        )
    )


def test_loop_refuses_to_settle_on_progress_chrome():
    """The 2026-09-15 regression, end to end: no confident answer from chrome."""
    # Short deadline on purpose: the loop polls at ~1s and this suite runs in
    # CI. With a progress line always present `stable_polls` never increments,
    # so 4s proves the same thing 30s would.
    result = _settle([IN_FLIGHT_PANE], timeout=4)
    check("not a success", result["status"] == "ok", False)
    check("honest timeout instead", result["status"], "timeout")
    # partial_text is still populated - a timeout that hands back nothing would
    # lose the only evidence of what the pane was showing.
    check("partial evidence kept", bool(result.get("partial_text", "").strip()), True)


def test_loop_returns_the_answer_once_it_arrives():
    """...and the stricter rule must not cost us the answer that follows it."""
    # Two in-flight polls, then the answer, which the script then holds so the
    # marker path can see its two consecutive identical reads.
    result = _settle([IN_FLIGHT_PANE] * 2 + [FINISHED_PANE], timeout=12)
    check("settles once answered", result["status"], "ok")
    check("real answer returned", "Riassunto del thread" in result["text"], True)
    check("promo block excluded", "Scarica l'app" in result["text"], False)
    check("table status cell preserved", "In corso" in result["text"], True)


TESTS = [
    test_loop_refuses_to_settle_on_progress_chrome,
    test_loop_returns_the_answer_once_it_arrives,
    test_progress_line_is_detected,
    test_progress_hint_normalises_trailing_ellipsis,
    test_progress_hint_must_be_a_whole_line,
    test_progress_line_ignores_empty_and_blank_input,
    test_answer_marker_index_finds_the_last_turn,
    test_in_flight_pane_has_no_answer_marker,
    test_no_marker_path_demands_more_stability,
    test_extractor_still_returns_chrome_when_that_is_all_there_is,
    test_appended_answer_is_isolated,
    test_prompt_echo_is_dropped,
    test_multiline_answer_preserves_order,
    test_multiline_prompt_is_dropped_entirely,
    test_chrome_that_persists_is_dropped,
    test_wholesale_rerender_falls_back_to_full_text,
    test_composer_placeholder_below_the_transcript_is_stripped,
    test_placeholder_without_baseline_is_not_silently_kept_as_answer,
    test_no_baseline_still_strips_chrome,
    test_follow_up_suggestion_chips_are_stripped_via_button_labels,
    test_button_labels_are_optional,
    test_answer_identical_to_a_button_label_falls_back_not_empty,
    test_whitespace_only_differences_do_not_count_as_answer,
    test_repeated_answer_line_matching_chrome_is_dropped,
    test_prompt_not_supplied_still_strips_chrome,
    test_live_pane_yields_only_the_answer,
    test_marker_path_needs_no_baseline,
    test_last_marker_wins_in_a_multi_turn_pane,
    test_multiline_answer_after_marker,
    test_localised_marker_is_recognised,
    test_english_disclaimer_is_stripped_too,
    test_answer_mentioning_copilot_is_not_treated_as_chrome,
    test_unrecognised_marker_falls_back_to_baseline_diff,
    test_marker_with_empty_answer_falls_back_to_full_text,
]


def main() -> bool:
    for test in TESTS:
        try:
            test()
        except Exception as e:  # noqa: BLE001 - report, don't abort the suite
            FAILURES.append(f"{test.__name__}: raised {type(e).__name__}: {e}")

    if FAILURES:
        print(f"FAIL ({len(FAILURES)} problem(s)):")
        for f in FAILURES:
            print(f"  - {f}")
        return False

    print(f"OK - {len(TESTS)} Copilot answer-extraction tests passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
