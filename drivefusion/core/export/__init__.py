"""Export writer. The only package permitted to write user-facing files.

Every write routes through :func:`writer.write`, which asserts the target
resolves inside the user-chosen export root and does not already exist. There
is no delete function here, and no overwrite path — not "none is exposed":
none exists. Lint codes DF009 and DF010 enforce both, because a package allowed
to write was otherwise exempt from every other check.

Modules:

* :mod:`writer` — the single write function, and the export root
* :mod:`bagit` — BagIt packaging (RFC 8493), and a validator for it
* :mod:`metadata` — RO-Crate, DataCite, PREMIS
* :mod:`manifests` — the two per-drive files, written into the bundle
* :mod:`naming` — rename mappings, always with their inverse
* :mod:`reports` — HTML and CSV
* :mod:`bundle` — assembling all of it, in the one order that works
"""
