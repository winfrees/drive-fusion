"""Requirements under test, each traced to the section of docs/PLAN.md it comes from.

The wording is the testable form of the plan's prose. Where the plan states a
rule only implicitly, the derivation is noted, so a reviewer can disagree with
the reading rather than with the code.

``status`` says whether the capability exists yet: ``implemented`` requirements
are verified and can fail a run; ``planned`` ones have test data in every
dataset, so a future version can be measured against them, but report as
DEFERRED until their milestone lands.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Requirement:
    id: str
    section: str
    text: str
    status: str = "implemented"
    milestone: str = ""


REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement("R-RO-01", "§3.1, §3.2, §14",
                "Drive Fusion never modifies, moves, or deletes catalogued data: every "
                "scanned tree is byte- and metadata-identical before and after each "
                "scan, hash, and verify pass."),
    Requirement("R-SC-01", "§4",
                "Nothing outside the user-defined scan scope is catalogued."),
    Requirement("R-SC-02", "§4",
                "Default exclusions ($RECYCLE.BIN, System Volume Information, page/"
                "hibernation files, *.tmp, Office ~$ lock files, .Trash-*) are not "
                "catalogued, and the scan reports how many entries it excluded."),
    Requirement("R-SC-03", "§4, §6.7",
                "Symlinks, junctions, and mount points are reported but never "
                "traversed, so nothing is catalogued through a link and a link loop "
                "cannot make the walk unbounded."),
    Requirement("R-CAT-01", "§2 (use case 1), §7.1",
                "Every in-scope file is catalogued with its drive, path, and size."),
    Requirement("R-CAT-02", "§7.1",
                "Every in-scope directory is catalogued, including empty directories "
                "and chains of nested empty directories."),
    Requirement("R-CAT-03", "§7.1 (derived)",
                "Zero-byte files are catalogued with size 0 and are never presented as "
                "duplicates of one another: they hold no content to protect."),
    Requirement("R-CAT-04", "§6.7",
                "Names are stored as observed: Unicode, spaces, multiple dots, no "
                "extension, leading dots, punctuation, long names, and case variants."),
    Requirement("R-CAT-05", "§6.7",
                "Paths longer than 260 characters are catalogued."),
    Requirement("R-CAT-06", "§6.7",
                "Unreadable files are recorded rather than fatal, and are never given a "
                "content identity that was not established by reading them."),
    Requirement("R-CAT-07", "§2, §5, §8.1",
                "Each drive is catalogued as a distinct device, so copy counts can be "
                "taken over physical drives."),
    Requirement("R-ID-01", "§6.6, §10.4",
                "Byte-identical files share one content identity regardless of name, "
                "extension, case, directory, drive, or modification time."),
    Requirement("R-ID-02", "§6.6",
                "Files whose bytes differ never share a content identity, including "
                "same-name files, same-size files, and files that differ only outside "
                "the regions the quick hash samples (the tier-2 full-hash case)."),
    Requirement("R-ID-03", "§6.6, §8.1",
                "A file is opened for hashing only when its size is shared with another "
                "catalogued file (or it was identified before and is unchanged). Shared-size "
                "files not yet identified are reported as unhashed rather than implied covered."),
    Requirement("R-RED-01", "§8.1",
                "Copies are counted over distinct physical drives, never over paths: two "
                "paths on one drive are one copy."),
    Requirement("R-RED-02", "§6.7, §8.1",
                "Hardlinks are one physical copy, not two."),
    Requirement("R-RED-03", "§8.1, REVIEW SR-12",
                "The under-protected set is exactly the content held on fewer distinct "
                "drives than the policy minimum, including every non-empty file whose size "
                "no other file shares: one copy, known without reading it."),
    Requirement("R-RED-04", "§8.1, §3.3",
                "Reclamation candidates are exactly the content duplicated within a single "
                "drive, with reclaimable bytes = (physical copies - drives) x size; "
                "cross-drive copies are never counted as waste."),
    Requirement("R-RED-05", "§8.1, §11, REVIEW SR-12",
                "Headline numbers (distinct content, catalogued paths, bytes, duplicate "
                "weight, under-protected items and bytes, same-drive waste, single-copy "
                "files, unhashed files) agree with the ground truth."),
    Requirement("R-INC-01", "§6.3, §6.4",
                "A rescan reflects files added and modified since the previous scan; a "
                "modified file does not keep its old identity."),
    Requirement("R-INC-02", "§7.1 (FAIR A2)",
                "Files and directories gone since the previous scan are tombstoned, with "
                "their metadata retained, rather than removed from the catalog."),
    Requirement("R-INC-03", "§6.4",
                "Renamed or moved content is recognized as the same content at a new "
                "path, not as a deletion plus unrelated new content."),
    Requirement("R-FIX-01", "§8.1",
                "Silent corruption (bytes changed, size and modification time not) is "
                "reported for every verifiable file it affects."),
    Requirement("R-FIX-02", "§8.1",
                "Verification produces no false positives, and covers both hash tiers."),
    Requirement("R-FIX-03", "§8.1",
                "Each fixity incident states how many drives hold the content, so a "
                "repair source can be found."),
    Requirement("R-VER-01", "§8.1",
                "Near-duplicates (versions) are grouped into version sets with a proposed "
                "preferred member.", status="planned", milestone="M6"),
)

BY_ID = {r.id: r for r in REQUIREMENTS}
