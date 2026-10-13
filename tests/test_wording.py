"""The denylist over generated text.

Two directions, and the second matters as much as the first. A check that fires
on Drive Fusion's own sentences — which talk about deletion precisely in order
to disclaim it — would be switched off within a week, and then it protects
nothing.
"""

from __future__ import annotations

import pytest

from drivefusion.core import wording


DESTRUCTIVE = [
    "Copy it to ARCHIVE, then delete the original.",
    "Copy it across and then remove the source folder.",
    "Run rm -rf on the old directory once the copy finishes.",
    "rmdir /s the stale tree.",
    "Use robocopy D:\\a E:\\b /MIR to synchronise.",
    "robocopy with /PURGE will tidy the destination.",
    "rsync -av --delete source/ dest/",
    "del /q the duplicates afterwards.",
    "Run diskpart to clean the disk first.",
    "format e: before copying.",
    "erase e:\\archive once the copy is verified.",
    "shred the drive when you are done.",
    "unlink the extra copies.",
    "Once verified, delete the duplicates.",
    "Then remove the old copy to free space.",
]

#: Sentences this tool really does produce. Every one mentions deletion in order
#: to rule it out, and every one must pass.
OUR_OWN_WORDS = [
    "Drive Fusion will not propose deleting anything to make room.",
    (
        "Duplicated within a single drive, so these bytes buy no durability. "
        "This is evidence for you to act on with your own tools; Drive Fusion "
        "does not delete and does not generate a command that would."
    ),
    (
        "This is a document, not an operation. Drive Fusion has no executor: "
        "every step is something you do, with your own tools."
    ),
    "It can be dropped without breaking the policy.",
    (
        "About 327,680 bytes of new storage would close this. Drive Fusion will "
        "not propose deleting anything to make room."
    ),
    "No step kind removes anything; there is no delete step.",
    "Re-hash the copy on ARCHIVE to confirm it landed intact.",
    "every copy would sit on a drive from purchase batch WD|RED|2021-03",
    "In total about 2.5 TB of additional storage would close this.",
]


@pytest.mark.parametrize("text", DESTRUCTIVE)
def test_destructive_instructions_are_refused(text: str) -> None:
    with pytest.raises(wording.DestructiveLanguageError):
        wording.assert_safe(text)


@pytest.mark.parametrize("text", OUR_OWN_WORDS)
def test_the_tools_own_disclaimers_are_allowed(text: str) -> None:
    """A denylist that flags honest disclaimers gets disabled, and then it
    protects nothing. These are the exact sentences Drive Fusion produces."""
    wording.assert_safe(text)


def test_the_failure_says_what_it_found_and_what_to_do() -> None:
    """An assertion that only says "failed" sends whoever hits it to read a
    regex, and the fix — rephrase as an addition — is not obvious from one."""
    with pytest.raises(wording.DestructiveLanguageError) as caught:
        wording.assert_safe("Copy it, then delete the original.", what="plan")

    message = str(caught.value)
    assert "plan" in message
    assert "delete the original" in message
    assert "never proposes removing" in message


def test_every_pattern_is_exercised_by_a_case_above() -> None:
    """A pattern no test reaches is a pattern nobody has checked compiles into
    what its author meant."""
    unmatched = []
    for pattern, reason in wording.PATTERNS:
        if not any(
            f"({reason})" in finding
            for text in DESTRUCTIVE
            for finding in wording.findings(text)
        ):
            unmatched.append(f"{pattern} ({reason})")
    assert not unmatched, f"patterns with no test case: {unmatched}"


def test_findings_reports_every_problem_not_only_the_first() -> None:
    """Fixing one phrase and rerunning to discover the next is a slow way to
    learn there were three."""
    found = wording.findings("rm -rf the source, then rmdir it, with /MIR set")
    assert len(found) >= 3


def test_case_does_not_matter() -> None:
    with pytest.raises(wording.DestructiveLanguageError):
        wording.assert_safe("Use ROBOCOPY /mir to sync.")
