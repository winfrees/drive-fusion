# Testing Drive Fusion on real drives

This is the procedure for running the V&V bench (docs/VV.md) on real, physically separate
drives, with data the bench generates, and for reading and sending back the results.
Everything here runs from the **V&V dashboard** (`python -m vv gui`), or the same steps from
the command line.

**What happens to your drives.** The bench writes test data, and only test data, into a new
folder it creates on each drive, `DriveFusion-VV`. Between Drive Fusion's scans it changes
some of those files on purpose: it adds, edits, moves, deletes and silently corrupts them, to
see whether Drive Fusion notices. After each run it removes what it created. It refuses to
delete anything it did not create, and refuses any folder that already holds something.
Drive Fusion itself only reads. Every run checks that with a byte-for-byte snapshot of each
test folder before and after each of Drive Fusion's phases (VV-RO-01).

## 1. What you need

- **Windows 10 or 11.** Linux and macOS also work, for the parts that don't need NTFS.
- **Python 3.11 or newer**, from python.org; tick "Add python.exe to PATH" when installing.
- **This repository**, downloaded or cloned. Then, in a terminal in its folder:

  ```
  python -m pip install -e ".[gui]"
  ```

  The bench tests Drive Fusion's own code from this folder: the same code the Windows build
  packages. CI checks separately that the build runs.

- **Drives: at least two separate physical devices, ideally three or more.** Two folders on
  one drive, or two partitions of one disk, count as one copy, and the bench refuses them.
  Aim for a mix:

  | Drive | Why |
  |---|---|
  | An **NTFS** drive (an external HDD or SSD) | Hardlinks, and symlinks if Developer Mode is on (§5) |
  | An **exFAT** drive (most USB sticks and SD cards) | Large clusters, no links: Drive Fusion's other filesystem |
  | A third device of either kind | Copies across three drives, as the planner and the NDSA checks need |

  Each device must be one Windows can identify, by serial number or partition table. A few
  USB enclosures hide both, and the bench says so and stops. Use another enclosure or port.

- **Free space:** about 100–300 MB on each drive for the default size. The bench checks
  before writing and refuses if a drive would drop below a 10 % reserve.

The results go on the **system drive** (`C:`) by default. Keep them off the drives under
test: Drive Fusion won't keep its catalog on a drive it scans.

## 2. Running from the dashboard

```
python -m vv gui
```

1. **Drives:** choose **Real drives**. For each drive press **Add drive folder…** and pick
   its root (for example `E:\`) or any folder on it. The bench makes its own new
   `DriveFusion-VV` folder inside.
2. **Suites:** tick what to test. All of them is the default, and a full run of all suites
   on three drives takes a few minutes.

   | Suite | What it checks |
   |---|---|
   | Read-only and scope | Nothing changes on a scanned drive; only folders in scope are catalogued |
   | Catalog and rescans | Every file is catalogued; a rescan sees additions, edits, deletions and moves |
   | Content identity | Copies are recognised as one content, near-misses are not |
   | Copies and duplicates | Copies are counted per physical device; under-protected items and headline numbers are right |
   | Fixity | Silent corruption is found, and a normal edit isn't mistaken for it |
   | Drive health and risk | Failure rates, P(loss), expected loss a year |
   | Collections, versions, scorecards | Collection proposals, membership, version sets, FAIR and NDSA |
   | Planner | Plans only add copies, fit each drive and meet their targets |
   | Exports | Manifests, packages, mappings and reports are complete and true |

   Hover a suite for its requirement IDs. Each suite runs on its own: tick one to test just
   that area.
3. **Runs:** how many times to repeat, each on a fresh, differently seeded dataset. Start
   with **1** to check the setup, then use **5–10** for a real result. Leave **First seed**
   empty for random seeds; each run's seed is recorded so any run can be replayed.
4. **Results folder:** keep the default, `C:\Users\<you>\DriveFusion-VV\campaign-…`.
5. Press **Run.** The log shows each step; the grid fills in one column per run.
   **Stop after this run** finishes the current run cleanly. Don't unplug a drive mid-run;
   if one is unplugged, that run is marked ERROR and the next ones continue.

When it finishes, the top line shows the campaign verdict. **Open dashboard** shows the
full report in a browser, and **Open folder** shows the files. **Open campaign…** reads any
earlier campaign back.

## 3. Running from the command line

The same campaign, without the window:

```
python -m vv campaign --out C:\vv\real-1 --drive-path E:\ --drive-path F:\ --drive-path G:\ --iterations 5
```

Add `--suite fixity --suite exports` (repeatable) to choose suites; `python -m vv suites`
lists them. Add `--keep` to leave each run's data on the drives for inspection, then remove
it later with `python -m vv clean <dataset>`. The exit status is 0 when everything passes.

## 4. Reading the results

```
campaign-…/
  dashboard.html     open this first
  summary.json       the same, for tools
  junit.xml          every run's tests, for CI tools
  campaign.json      what was asked: drives, suites, runs, versions, this computer
  runs/001/
    report.md        this run: every requirement and its tests, discrepancies, the seed
    results.json     each test's verdict and details
    observed.json    what Drive Fusion concluded
    dataset.json     the seed and the drives
    log.txt          everything the run printed
  runs/002/ …
```

- **Verdicts:**
  - **PASS:** checked and correct.
  - **FAIL:** a discrepancy, listed under *Discrepancies* with the files involved.
  - **ERROR:** a phase crashed; the log has the traceback.
  - **n/a:** this dataset couldn't test it, for example no NTFS drive for the hardlink
    test. That is not a pass.
- **"All runs"** is the worst verdict across runs, so a requirement that fails once shows as
  FAIL.
- **A FAIL is what this is for.** Each run's `report.md` gives the seed and the command that
  rebuilds the identical dataset.

## 5. Getting the most coverage

| To test | Do |
|---|---|
| Hardlinks (VV-RED-02) | Include an NTFS drive |
| Symlinks (VV-SC-03) | NTFS, and either run the terminal as administrator or turn on Developer Mode (Settings → For developers) |
| Partitions of one disk (R-RED-06, R-EXP-02) | Covered by the simulated drives (`python -m vv campaign --out … --drives 4`); on real drives use whole devices |
| Large clusters | An exFAT drive bigger than 32 GB (128 KiB clusters) |

## 6. Sending results back

Zip the campaign folder and send it. It holds no file contents from your own data: only
the bench's generated files are ever listed, and those have been removed from the drives.
`campaign.json` records the computer's name and Windows version.

## 7. If something goes wrong

| Message | Meaning |
|---|---|
| *is on the same physical device as* | Two folders share a drive. Use one folder per device |
| *can't identify this device* | The enclosure hides the serial. Try another port or enclosure |
| *would leave less than … free* | Not enough space. Free some, or lower **Files per drive** |
| *already holds something* | The folder isn't empty. The bench never writes into a used folder |
| A run is **ERROR** after unplugging | Reconnect the drive. The remaining runs continue; leftover test data can be removed with `python -m vv clean` on that run's dataset (kept only with `--keep`) |
