# DGT Summer School 2026 Dataset Aggregator

A standalone Python tool that scans one or more Summer School project folders—or selected individual CSV/TSV files—and builds reproducible golden-standard and modified aggregate datasets for production, verification, and complete evaluation.

The tool has no third-party dependencies. It supports a Tkinter desktop interface and a headless command-line mode.

## What it creates

For every discovered `Project data` folder:

```text
Project data/
├── 4-Aggregated-datasets/
│   ├── 41-production/
│   ├── 42-verification/
│   ├── 43-complete/
│   ├── aggregation-report.csv
│   ├── aggregation-report.txt
│   ├── provenance.csv
│   ├── .aggregator-manifest.json
│   └── .aggregator-cache/
└── 5-automated backups/  # created only when a correction is applied
```

A multi-project run also creates `overall-aggregation-report.csv` and `overall-aggregation-report.txt` in the projects' common parent folder by default. A different report folder can be selected in the interface or command line.

Each location can contain these files when the corresponding group has content:

1. `01-en-(de-fr)-golden.csv`
2. `02-en-(de-fr)-modified.csv`
3. `03-en-other-golden.csv`
4. `04-en-other-modified.csv`
5. `05-en-(de-fr)-golden-orphaned.csv`
6. `06-en-(de-fr)-modified-orphaned.csv`

All generated datasets are UTF-8 with BOM and pipe-delimited. By default, the project-relative original filename is included as the first column:

```text
FILENAME|SEGMENT|ORI|TRA
```

The filename column can be disabled when a strict `SEGMENT|ORI|TRA` output is required.

## Desktop use

On Windows, double-click `run_aggregator.bat`, or run:

```powershell
py -3 dgt_dataset_aggregator.py
```

Add any number of project folders or `Project data` folders. **Add folders…** opens a folder browser where Ctrl+click or Shift+click selects several sibling folders at once; double-click navigates into a folder. The **Add files…** button accepts multiple CSV/TSV files in one selection. Use **Scan only** to preview the result without changing any source or output file. Use **Create aggregated datasets** to create the outputs.

An individual-file run is incremental: inputs recorded by an earlier full-project run are retained from the content-hash cache. This prevents selecting two new files from accidentally replacing a complete project aggregate with only those two files. A project-folder scan remains authoritative.

## Command-line use

```powershell
py -3 dgt_dataset_aggregator.py --headless `
  --project-folder "C:\Data\Project 1" `
  --project-folder "C:\Data\Project 2\Project data" `
  --input-file "C:\Data\Project 3\Project data\1-golden-standard-files\new-golden.csv" `
  --input-file "C:\Data\Project 3\Project data\2-modified-files\new-modified.csv" `
  --verification-percent 15 `
  --seed 2026 `
  --include-filename `
  --auto-fix ask `
  --changed-file-action ask
```

For unattended runs, replace interactive options with explicit actions:

```powershell
py -3 dgt_dataset_aggregator.py --headless `
  --project-folder "C:\Data\Project 1" `
  --auto-fix yes `
  --changed-file-action reprocess
```

Use `--dry-run` to scan without writing or correcting anything. Run with `--help` for all options.

Use `--no-include-filename` to produce the original three-column aggregate format. Use `--overall-report-folder` to override the automatic common-parent report location.

## Discovery and classification

The scan is recursive. Role classification uses these rules in order:

- CSVs below `1-golden-standard-files` are golden.
- CSVs below `2-modified-files` are modified.
- Elsewhere, clear filename markers such as `golden standard`, `adjusted`, `modified`, `raw MT`, `comparison`, and `merged` are used.
- Reference folders/files, generated outputs, backups, hidden internal folders, and unclassified files are excluded.

Target language is inferred from filename or folder markers such as `EN-FR`, `ENFR`, `-DE-TRA-`, or `-FR-SDLXLIFF-`. Files without a reliable target language are reported for attention instead of guessed.

Golden and modified files are paired using their normalized DGT document identifier and target language, then filename similarity when a key has multiple candidates. Identical copies for the same role and language are aggregated once. A golden and modified file may legitimately have identical content; cross-role files are never deduplicated.

## Verification split

The default verification share is 15% of every aggregate group. Selection is deterministic for a given seed. Matching golden and modified aggregates always reserve the same globally renumbered `SEGMENT` values. The production and verification subsets together equal the complete dataset.

Very small non-empty groups reserve at least one segment when the percentage is above zero. A group with more than one segment retains at least one production segment when the percentage is below 100%.

## CSV validation and correction

The standard input structure is:

```text
SEGMENT|ORI|TRA
```

The scanner validates encoding, delimiter, header mapping, row widths, and meaningful extra columns. Safe corrections include:

- comma, semicolon, or tab delimiter to pipe;
- known header aliases to `SEGMENT|ORI|TRA`;
- adding sequential segment numbers to a two-column file;
- removing clearly empty or alternate golden/modified columns;
- UTF-16 or Windows-1252 to UTF-8;
- removing consistent trailing export delimiter artifacts.

Before an approved correction, the original file is copied under `5-automated backups` with its relative directory structure preserved. Legacy spellings used by earlier versions remain excluded from scans. Ambiguous or destructive transformations are never attempted automatically; the file is excluded and listed in the report.

## Repeat runs and changed files

The manifest records source paths and SHA-256 hashes. Normalized rows are cached by content hash so unchanged files can be reused without duplicate appends or reparsing. Aggregate files are rebuilt atomically on every successful run.

When a previously used filename has different content, the interactive tool asks whether to:

- reprocess the new content;
- keep using the cached previous content; or
- abort that project without changing its outputs.

Headless runs fail safely at an unanswered conflict. Set `--changed-file-action` explicitly for unattended use.

## Reports

- `aggregation-report.txt` is a human-readable summary.
- `aggregation-report.csv` contains counts per output plus every attention item.
- `provenance.csv` maps each output segment in all three locations to its source path, original segment identifier, and source hash.
- `.aggregator-manifest.json` contains repeat-run state and settings.
- `overall-aggregation-report.txt` summarizes paired and orphaned file counts, issues, and exported segment totals across every project in the run.
- `overall-aggregation-report.csv` provides the same cross-project information as structured project, aggregate, and attention records.

## Tests

```powershell
py -3 -m unittest discover -s tests -v
```

## License

MIT License. See [LICENSE](LICENSE).
