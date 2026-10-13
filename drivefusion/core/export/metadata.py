"""Machine-readable descriptions: RO-Crate, DataCite, PREMIS.

Three formats because they answer three different questions, and the plan names
all three (§8.3): RO-Crate says *what this is* in a way a repository or a
crawler can read, DataCite says *how to cite it* in the shape a deposit form
expects, and PREMIS says *what happened to it* — the fixity checks and scans
that are the only evidence anyone has that the bytes are still the bytes.

**What is in the crate, and what is not.** The data entities here are
collections, not files. A crate listing 50 million `File` entities would be
unusable by anything, and the files are not in the bundle anyway: they are on
the user's drives, which is the whole point of the tool. So each collection is
a `Dataset` with its counts, size, checksummed-content proportion and the
drives holding it, and the file-level inventory goes to CSV alongside, where a
spreadsheet can open it.

**Nothing is invented to satisfy a schema.** DataCite requires `creators` and
`publisher`; a catalog of someone's drives frequently knows neither. The honest
answer is DataCite's own `(:unav)` placeholder, which means "value unavailable",
and a matching finding in the FAIR scorecard. Filling in the account name would
make the record validate and make it wrong, and a deposit-ready record that
attributes the work to whoever ran the tool is worse than one that admits it
does not know.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from drivefusion.core.store.catalog import Catalog

#: DataCite's own placeholder for a value that is genuinely unavailable. Used
#: rather than a guess, and reported as a gap rather than hidden.
UNAVAILABLE = "(:unav)"

#: What each fixity result means, spelled out. "mismatch" in a column is a
#: database value; "the bytes on disk have changed since they were catalogued"
#: is what someone reading an export needs to be told.
FIXITY_OUTCOMES = {
    "ok": "the file re-read to the hash recorded for it",
    "mismatch": (
        "the file re-read to a different hash: the bytes on disk have changed "
        "since they were catalogued, and nothing in Drive Fusion changed them"
    ),
    "unreadable": "the file could not be read when the check ran",
    "missing": "the file was no longer at its recorded path",
}

RO_CRATE_CONTEXT = "https://w3id.org/ro/crate/1.1/context"
RO_CRATE_PROFILE = "https://w3id.org/ro/crate/1.1"
DATACITE_SCHEMA = "http://datacite.org/schema/kernel-4"

#: The tool as an agent, named identically in every format so provenance can be
#: followed between them.
def _tool_entity(version: str) -> dict:
    return {
        "@id": "#drive-fusion",
        "@type": "SoftwareApplication",
        "name": "Drive Fusion",
        "version": version,
        "description": (
            "Read-only drive cataloging and consolidation planning. Opens files "
            "for reading only; has no code that can write, move or delete them."
        ),
    }


def _creators(collection) -> list[dict]:
    """Creators from the catalog, or the unavailable placeholder.

    ``creators_json`` holds a list of names when someone has said who made the
    data. When nobody has, this returns the placeholder: a record naming the
    person who happened to run the tool would validate and be wrong.
    """
    raw = collection.row.get("creators_json")
    if raw:
        try:
            names = json.loads(raw)
        except (TypeError, ValueError):
            names = []
        if isinstance(names, str):
            names = [names]
        entries = [
            {"@type": "Person", "name": str(name).strip()}
            for name in names
            if str(name).strip()
        ]
        if entries:
            return entries
    return [{"@type": "Person", "name": UNAVAILABLE}]


def ro_crate(
    catalog: Catalog,
    *,
    version: str,
    collections: list,
    stats: dict,
    generated_at: str | None = None,
) -> str:
    """An RO-Crate metadata document describing this export.

    The root entity is the *export*, not the archive: it is the thing that
    exists at a path and has a date. The archive is described by it.
    """
    from drivefusion.core import standards

    generated = generated_at or datetime.now(timezone.utc).isoformat()
    graph: list[dict] = [
        {
            "@id": "ro-crate-metadata.json",
            "@type": "CreativeWork",
            "conformsTo": {"@id": RO_CRATE_PROFILE},
            "about": {"@id": "./"},
        },
        {
            "@id": "./",
            "@type": "Dataset",
            "name": "Drive Fusion catalog export",
            "description": (
                "A description of a personal or lab storage archive: the "
                "collections found in it, how many copies of each exist, and "
                "how durable those copies are. The described files are not "
                "included in this bundle; they remain on the drives this "
                "records. Drive Fusion did not modify any of them."
            ),
            "datePublished": generated,
            "license": {"@id": "#license-unspecified"},
            "creator": {"@id": "#drive-fusion"},
            "hasPart": [
                {"@id": f"#collection-{item.local_pid}"} for item in collections
            ],
            "measurementTechnique": (
                f"files catalogued: {stats.get('files', 0)}; "
                f"bytes: {stats.get('bytes', 0)}; "
                f"volumes: {stats.get('volumes', 0)}"
            ),
        },
        _tool_entity(version),
        {
            "@id": "#license-unspecified",
            "@type": "CreativeWork",
            "name": (
                "No licence is asserted for this description. Each collection "
                "carries its own licence, or records that it has none."
            ),
        },
    ]

    for item in collections:
        entity = {
            "@id": f"#collection-{item.local_pid}",
            "@type": "Dataset",
            "identifier": item.local_pid,
            "name": item.title,
            "creator": _creators(item),
        }
        if item.description:
            entity["description"] = item.description
        if item.data_type:
            entity["additionalType"] = item.data_type
        if item.row.get("external_pid"):
            entity["sameAs"] = {"@id": str(item.row["external_pid"])}
        if item.license:
            entity["license"] = {"@id": _licence_id(item.license)}
        else:
            # Recorded as a stated absence rather than omitted: FAIR R1.1 is a
            # blocking criterion, and a missing key reads as an oversight while
            # "none asserted" reads as the finding it is.
            entity["license"] = {"@id": "#licence-none-asserted"}

        measures = item.row.get("_measures") or {}
        if measures:
            entity["size"] = measures.get("bytes")
            entity["distribution"] = {
                "@type": "DataDownload",
                "name": "held on local drives, not included in this bundle",
                "contentSize": measures.get("bytes"),
            }
            entity["variableMeasured"] = [
                {"@type": "PropertyValue", "name": name, "value": value}
                for name, value in (
                    ("files", measures.get("files")),
                    ("physical_copies", measures.get("copies")),
                    ("checksummed_files", measures.get("hashed")),
                    ("drives", measures.get("drives")),
                )
                if value is not None
            ]
        graph.append(entity)

        for relation in item.row.get("_relations", []):
            graph.append(
                {
                    "@id": f"#relation-{item.local_pid}-{relation['id']}",
                    "@type": "Relationship",
                    "relationType": relation["relation_type"],
                    "source": {"@id": f"#collection-{item.local_pid}"},
                    "target": {"@id": str(relation["to_ref"])},
                }
            )

    if any(not item.license for item in collections):
        graph.append(
            {
                "@id": "#licence-none-asserted",
                "@type": "CreativeWork",
                "name": "No licence asserted",
                "description": (
                    "Nobody has recorded reuse terms for this collection. "
                    "Unlicensed data is not reusable by anyone else, which is "
                    "why the FAIR scorecard treats it as blocking rather than "
                    "as a minor gap."
                ),
            }
        )

    for licence in sorted({item.license for item in collections if item.license}):
        graph.append(
            {
                "@id": _licence_id(licence),
                "@type": "CreativeWork",
                "name": licence,
                "description": _licence_note(licence),
            }
        )

    rubric = standards.fair_rubric()
    graph.append(
        {
            "@id": "#fair-rubric",
            "@type": "CreativeWork",
            "name": rubric.citation,
            "description": (
                "The rubric this export's scorecards were produced against. "
                "Drive Fusion's operationalisation of the published "
                "principles, not the published text itself."
            ),
        }
    )

    return json.dumps(
        {"@context": RO_CRATE_CONTEXT, "@graph": graph}, indent=2, sort_keys=False
    )


def _licence_id(licence: str) -> str:
    if licence.lower().startswith(("http://", "https://")):
        return licence
    if licence == "not-for-sharing":
        return "#licence-not-for-sharing"
    return f"https://spdx.org/licenses/{licence}"


def _licence_note(licence: str) -> str:
    if licence == "not-for-sharing":
        return (
            "Deliberately not shared. A recorded decision not to share is a "
            "complete answer to the licence question; an empty field is not."
        )
    return f"SPDX identifier {licence}."


def datacite(collection, *, stats: dict | None = None) -> str:
    """A DataCite record for one collection, in the JSON serialisation.

    Deposit-ready in shape, and honest about what is missing: required fields
    with no value in the catalog carry DataCite's ``(:unav)`` rather than a
    plausible guess, so a submission fails review for the right reason instead
    of succeeding with a fabricated creator.
    """
    measures = (collection.row.get("_measures") or {}) if collection.row else {}
    stats = stats or {}

    record = {
        "schemaVersion": DATACITE_SCHEMA,
        "types": {
            "resourceTypeGeneral": "Dataset",
            "resourceType": collection.data_type or UNAVAILABLE,
        },
        "identifiers": [
            {"identifier": collection.local_pid, "identifierType": "Local accession"}
        ],
        "creators": [
            {"name": entry["name"], "nameType": "Personal"}
            for entry in _creators(collection)
        ],
        "titles": [{"title": collection.title}],
        "publisher": collection.project or UNAVAILABLE,
        "publicationYear": str(_year(collection)),
        "dates": [
            {
                "date": collection.row.get("created_at", ""),
                "dateType": "Created",
                "dateInformation": "catalogued by Drive Fusion",
            }
        ],
        "sizes": (
            [f"{measures['bytes']} bytes", f"{measures['files']} files"]
            if measures.get("bytes") is not None
            else []
        ),
        "descriptions": [
            {
                "description": collection.description or UNAVAILABLE,
                "descriptionType": "Abstract",
            },
            {
                "description": (
                    "Described by Drive Fusion from local storage. The files "
                    "themselves are not deposited and are not included in this "
                    "bundle; this record describes where they are and how many "
                    "copies exist."
                ),
                "descriptionType": "TechnicalInfo",
            },
        ],
    }

    if collection.row.get("external_pid"):
        record["identifiers"].insert(
            0,
            {"identifier": str(collection.row["external_pid"]), "identifierType": "DOI"},
        )

    if collection.license:
        record["rightsList"] = [
            {
                "rights": collection.license,
                "rightsIdentifier": collection.license,
                "rightsIdentifierScheme": "SPDX",
            }
        ]
    else:
        # DataCite's rightsList is optional, so an absent licence could simply
        # be omitted. It is stated instead, because a deposit form that shows
        # nothing in the rights box looks like a form nobody filled in rather
        # than data nobody licensed.
        record["rightsList"] = [{"rights": UNAVAILABLE}]

    relations = collection.row.get("_relations", [])
    if relations:
        record["relatedIdentifiers"] = [
            {
                "relatedIdentifier": str(relation["to_ref"]),
                "relatedIdentifierType": _identifier_type(str(relation["to_ref"])),
                "relationType": relation["relation_type"],
            }
            for relation in relations
        ]

    record["_drive_fusion_note"] = (
        "Fields reading (:unav) are DataCite's placeholder for a value that is "
        "genuinely unavailable. Drive Fusion does not guess them: a record that "
        "names whoever ran the tool as the creator would validate and be wrong."
    )
    return json.dumps(record, indent=2)


def _identifier_type(reference: str) -> str:
    lowered = reference.lower()
    if lowered.startswith("10.") or "doi.org" in lowered:
        return "DOI"
    if lowered.startswith(("http://", "https://")):
        return "URL"
    return "Local accession"


def _year(collection) -> int:
    created = collection.row.get("created_at") or ""
    if len(created) >= 4 and created[:4].isdigit():
        return int(created[:4])
    return datetime.now(timezone.utc).year


def premis_events(catalog: Catalog, *, version: str, limit: int = 5000) -> str:
    """Fixity and provenance events, as PREMIS-shaped JSON.

    These are the only evidence that the bytes have not rotted. An export
    without them describes an archive whose integrity is asserted rather than
    checked, so they are a first-class part of the bundle rather than a
    diagnostic.

    Serialised as JSON rather than PREMIS XML: the bundle's other metadata is
    JSON-LD and JSON, the element names and vocabulary are PREMIS's, and a
    consumer that needs XML can transform it. Claiming to emit PREMIS XML would
    mean emitting a schema-valid document, which this is not.
    """
    rows = catalog.conn.execute(
        "SELECT id, checked_at, result, content_id, file_id "
        "FROM fixity_check ORDER BY checked_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()

    events = []
    for row in rows:
        entry = dict(row)
        result = entry.get("result") or "unknown"
        events.append(
            {
                "eventIdentifier": {
                    "eventIdentifierType": "local",
                    "eventIdentifierValue": f"fixity-{entry['id']}",
                },
                "eventType": "fixity check",
                "eventDateTime": entry.get("checked_at"),
                "eventDetailInformation": {
                    "eventDetail": (
                        "file re-read and its hash compared with the one "
                        "recorded in the catalog"
                    )
                },
                "eventOutcomeInformation": {
                    "eventOutcome": result,
                    "eventOutcomeDetail": {
                        "eventOutcomeDetailNote": FIXITY_OUTCOMES.get(
                            result, "outcome not recognised by this version"
                        )
                    },
                },
                "linkingAgentIdentifier": {
                    "linkingAgentIdentifierType": "local",
                    "linkingAgentIdentifierValue": "#drive-fusion",
                },
                "linkingObjectIdentifier": {
                    "linkingObjectIdentifierType": "local file id",
                    "linkingObjectIdentifierValue": entry.get("file_id"),
                },
            }
        )

    counts = catalog.conn.execute(
        "SELECT result, COUNT(*) AS n FROM fixity_check GROUP BY result"
    ).fetchall()

    return json.dumps(
        {
            "agent": {
                "agentIdentifier": {
                    "agentIdentifierType": "local",
                    "agentIdentifierValue": "#drive-fusion",
                },
                "agentName": f"Drive Fusion {version}",
                "agentType": "software",
                "agentNote": (
                    "Reads only. Every event here is a read of a file and a "
                    "comparison of hashes; none of them changed anything."
                ),
            },
            "summary": {
                str(dict(row)["result"]): int(dict(row)["n"]) for row in counts
            },
            "events": events,
            "note": (
                "PREMIS vocabulary in a JSON serialisation, not schema-valid "
                "PREMIS XML. Events are capped at the most recent "
                f"{limit}; the catalog holds the full history."
            ),
        },
        indent=2,
    )
