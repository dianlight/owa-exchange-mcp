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

import sys

from exchange_mcp.browser_session import _copilot_answer_text

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


TESTS = [
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
