"""The denylist over generated text.

Drive Fusion never deletes anything. The code cannot: there is no writer in the
gateway, handles are opened read-only, and the lint refuses the calls. None of
that constrains what the tool *says*. A plan is a checklist a person follows by
hand, and several of the words below are also executable if pasted into a shell,
so a rationale phrased as "copy it there, then rmdir the original" would turn a
planning tool into a destructive one without anybody deciding to make it one.

So every document this tool generates is read before it is handed over, and
refused if it contains instructions to remove data. Checked in the product
rather than only in a test, because a test only covers the wordings its fixture
happens to reach — the first version of this check lived in a test and never
once ran against the branch that needed it most.

The list is deliberately narrow and anchored on word boundaries. Drive Fusion's
own honest sentences talk about deletion in order to disclaim it — "Drive Fusion
will not propose deleting anything to make room" — and a check that fired on
those would have to be switched off, which is worse than not having it.
"""

from __future__ import annotations

import re


class DestructiveLanguageError(AssertionError):
    """Raised when generated text tells the user to remove data.

    An ``AssertionError`` because it reports an internal contradiction, not bad
    input: something in this codebase composed a sentence the tool promises
    never to produce, and the only correct response is to not hand it over.
    """


#: Each entry is a pattern and what it would mean if it appeared. The reason is
#: carried so the failure names the problem rather than echoing a regex.
PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+-[a-z]*r", "a recursive shell delete"),
    (r"\brmdir\b", "a directory removal"),
    (r"\bdel\s+/[a-z]", "a Windows del with switches"),
    (r"\berase\s+[a-z/\\.]", "an erase command"),
    (r"\bdiskpart\b", "a partition tool"),
    (r"\bformat\s+[a-z]:", "a volume format"),
    (r"\bunlink\b", "an unlink call"),
    (r"\bshred\b", "a secure-erase tool"),
    (r"/MIR\b", "robocopy's mirror switch, which deletes from the destination"),
    (r"/PURGE\b", "robocopy's purge switch, which deletes from the destination"),
    (r"--delete\b", "rsync's delete switch"),
    (r"\bremove the (original|source|old copy)", "an instruction to remove data"),
    (r"\bdelete the (original|source|old copy|duplicates?)",
     "an instruction to delete data"),
    (r"\bthen (delete|remove|rm)\b", "a delete-after-copy instruction"),
)

_COMPILED = tuple(
    (re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in PATTERNS
)


def findings(text: str) -> list[str]:
    """Every destructive phrase in this text, described in words."""
    found = []
    for pattern, reason in _COMPILED:
        match = pattern.search(text)
        if match:
            found.append(f"{match.group(0)!r} ({reason})")
    return found


def assert_safe(text: str, *, what: str = "document") -> None:
    """Refuse text that tells the user to remove data.

    Raises rather than sanitising. A sanitiser would quietly change the meaning
    of a sentence that someone wrote deliberately, and the resulting document
    would be neither what was written nor obviously wrong.
    """
    found = findings(text)
    if found:
        raise DestructiveLanguageError(
            f"this {what} tells the user to remove data: {'; '.join(found)}. "
            "Drive Fusion plans additions and never proposes removing anything; "
            "rephrase so the step says what to copy, not what to delete."
        )
