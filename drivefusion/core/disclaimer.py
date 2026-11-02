"""The scope disclaimer, and its acknowledgement (docs/PLAN.md §1, REVIEW SR-25).

The text is shown before the tool first reads a drive, and must be accepted.
Acceptance is an entry in the hash-chained audit log that carries the SHA-256
of the exact text accepted: if the wording ever changes, the new text has to
be accepted again, and the log shows which text was accepted when, by whom.

Scripts and tests accept it with ``--accept-disclaimer`` on the command line
or ``DRIVEFUSION_ACCEPT_DISCLAIMER=1`` in the environment. That acceptance is
recorded the same way, with how it was given.
"""

from __future__ import annotations

import hashlib
import json
import os

from drivefusion.core.store import audit

TEXT = (
    "Drive Fusion is not intended for, and has not been validated for, identifiable "
    "human-subjects data, protected health information, or otherwise regulated data. "
    "Do not add such data to its scan scope. The tool assists with documentation and "
    "preservation planning. Its scorecards report how far your records are consistent with "
    "FAIR and NDSA criteria; they do not establish that any requirement is met. "
    "Responsibility for meeting institutional, funder, and legal requirements remains with you. "
    "Drive Fusion is provided as is, without warranty of any kind, for no particular purpose; "
    "you use it at your own risk, and should verify its results before acting on them. "
    "The full terms, including limitation of liability and indemnification, are in "
    "DISCLAIMER.md, which comes with the program."
)

ENVIRONMENT_VARIABLE = "DRIVEFUSION_ACCEPT_DISCLAIMER"
ACTION = "disclaimer.accepted"


def text_sha256() -> str:
    return hashlib.sha256(TEXT.encode("utf-8")).hexdigest()


def accepted(catalog) -> bool:
    """Whether this catalog's log holds an acceptance of the current text."""
    for row in catalog.conn.execute(
        "SELECT detail_json FROM audit WHERE action = ? ORDER BY id DESC", (ACTION,)
    ):
        try:
            if json.loads(row["detail_json"]).get("text_sha256") == text_sha256():
                return True
        except (TypeError, ValueError):
            continue
    return False


def accept(catalog, how: str) -> None:
    """Record acceptance of the current text. ``how``: prompt, flag, env, dialog."""
    audit.append(catalog.conn, ACTION, {"text_sha256": text_sha256(), "how": how})


def accepted_by_environment() -> bool:
    return os.environ.get(ENVIRONMENT_VARIABLE, "").strip().lower() in ("1", "yes", "true")
