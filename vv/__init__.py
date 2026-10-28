"""Drive Fusion V&V: randomized drive datasets and a pass/fail oracle.

This package is test equipment, not part of the product. It *writes* — it builds
mock drives full of files, then mutates and corrupts them between scans — so it
lives outside ``drivefusion/``, is never imported by it, and is not shipped by
``pyproject.toml``. The read-only lint covers ``drivefusion/`` only, and that is
the point: the tool under test must not be able to write, the test bench must.

Three stages, each a subcommand of ``python -m vv`` (see docs/VV.md):

``generate``  build a fresh, randomized dataset across N drives — simulated
              (folders registered as separate drives) or physical (one folder
              per real device) — plus a ground-truth manifest.
``run``       drive a version of Drive Fusion through baseline scan, rescan
              after mutations, and fixity verification, recording what it
              concluded in ``observed.json``.
``evaluate``  compare those conclusions with what the requirements say they
              should have been, and write a pass/fail report traced to
              docs/PLAN.md.
"""

__version__ = "1.0.0"
