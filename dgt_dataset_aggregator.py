#!/usr/bin/env python3
"""Build reproducible per-project aggregate datasets from Summer School CSVs.

The module intentionally uses only the Python standard library so that the
single file can be copied to another Windows workstation and run directly.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable, Sequence


APP_NAME = "DGT Summer School 2026 Dataset Aggregator"
VERSION = "0.2.1"
SCHEMA_VERSION = 1

OUTPUT_ROOT_NAME = "4-Aggregated-datasets"
BACKUP_ROOT_NAME = "5-automated backups"
LEGACY_BACKUP_ROOT_NAMES = {
    "05-automatic-correction-backups",
    "05-automated-backups",
    "05-automated backups",
    "5-automated-backups",
}
CACHE_NAME = ".aggregator-cache"
MANIFEST_NAME = ".aggregator-manifest.json"
REPORT_CSV_NAME = "aggregation-report.csv"
REPORT_TEXT_NAME = "aggregation-report.txt"
PROVENANCE_NAME = "provenance.csv"
OVERALL_REPORT_CSV_NAME = "overall-aggregation-report.csv"
OVERALL_REPORT_TEXT_NAME = "overall-aggregation-report.txt"

LOCATION_FOLDERS = {
    "production": "41-production",
    "verification": "42-verification",
    "complete": "43-complete",
}

AGGREGATE_FILENAMES = {
    "paired_core_golden": "01-en-(de-fr)-golden.csv",
    "paired_core_modified": "02-en-(de-fr)-modified.csv",
    "paired_other_golden": "03-en-other-golden.csv",
    "paired_other_modified": "04-en-other-modified.csv",
    "orphan_core_golden": "05-en-(de-fr)-golden-orphaned.csv",
    "orphan_core_modified": "06-en-(de-fr)-modified-orphaned.csv",
}

EXCLUDED_FOLDER_NAMES = {
    "3-reference-files",
    "99 - raw resources",
    OUTPUT_ROOT_NAME.casefold(),
    BACKUP_ROOT_NAME.casefold(),
    *(name.casefold() for name in LEGACY_BACKUP_ROOT_NAMES),
    CACHE_NAME.casefold(),
}

GOLDEN_MARKERS = (
    "golden standard",
    "gold-standard",
    "gold standard",
    "_gold",
    "-gold",
    " golden",
)
MODIFIED_MARKERS = (
    "modified",
    "adjusted",
    "with errors",
    "raw mt",
    "comparison",
    "merged",
    "inconsistent",
    "inconsistencified",
)
REFERENCE_MARKERS = (
    "reference act",
    "reference -",
    " - reference",
    " - 3 - reference",
)

SEGMENT_ALIASES = {"segment", "segmentnumber", "segmentid", "row", "line", "unitid"}
ORI_ALIASES = {"ori", "source", "sourcetext", "original", "originaltext"}
COMMON_TRA_ALIASES = {"tra", "target", "targettext", "translation"}
GOLDEN_TRA_ALIASES = COMMON_TRA_ALIASES | {
    "correcttra",
    "goldentra",
    "goldtranslation",
    "expectedtra",
    "trags",
    "tragolden",
    "goldstandard",
}
MODIFIED_TRA_ALIASES = COMMON_TRA_ALIASES | {
    "modifiedtra",
    "actualtra",
    "actualtranslation",
    "modifiedtranslation",
    "traadjusted",
    "adjustedtra",
}

DOCUMENT_ID_RE = re.compile(
    r"(?i)\b([A-Z][A-Z0-9]{1,11}-\d{4}-\d{5}(?:-\d{2}){0,2})"
)
LANGUAGE_PATTERNS = (
    re.compile(r"(?i)(?:^|[^A-Z])EN[-_ ]([A-Z]{2})(?=$|[^A-Z])"),
    re.compile(r"(?i)(?:^|[^A-Z])EN([A-Z]{2})(?=$|[^A-Z])"),
    re.compile(r"(?i)(?:^|[-_ ])([A-Z]{2})-(?:TRA|SDLXLIFF)(?=$|[-_. ])"),
)


class AggregationError(RuntimeError):
    """A user-facing aggregation failure."""


@dataclass
class SourceRow:
    segment: str
    ori: str
    tra: str


@dataclass
class Candidate:
    path: Path
    project_data: Path
    relative_path: str
    role: str
    language: str
    pair_key: str
    sha256: str
    canonical_folder: bool

    def manifest_record(self, used_hash: str, row_count: int, cache_hit: bool) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "name": self.path.name,
            "detected_hash": self.sha256,
            "used_hash": used_hash,
            "role": self.role,
            "language": self.language,
            "pair_key": self.pair_key,
            "rows": row_count,
            "cache_hit": cache_hit,
        }


@dataclass
class LoadedDataset:
    candidate: Candidate
    rows: list[SourceRow]
    used_hash: str
    cache_hit: bool = False


@dataclass
class AggregateRow:
    segment: str
    ori: str
    tra: str
    source_file: str
    source_segment: str
    source_hash: str


@dataclass
class Inspection:
    rows: list[SourceRow] = field(default_factory=list)
    delimiter: str = ""
    encoding: str = ""
    corrections: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def is_standard(self) -> bool:
        return not self.error and not self.corrections

    @property
    def is_fixable(self) -> bool:
        return not self.error and bool(self.corrections)


@dataclass
class AttentionItem:
    severity: str
    path: str
    issue: str
    action: str = ""


@dataclass
class ProjectResult:
    project_data: Path
    status: str
    report_rows: list[dict[str, object]] = field(default_factory=list)
    attention: list[AttentionItem] = field(default_factory=list)
    files_scanned: int = 0
    files_included: int = 0
    duplicates_skipped: int = 0
    cache_hits: int = 0
    corrections_applied: int = 0
    changed_conflicts: int = 0
    paired_file_pairs: int = 0
    paired_files: int = 0
    orphaned_files: int = 0
    orphaned_golden_files: int = 0
    orphaned_modified_files: int = 0
    other_language_orphans_excluded: int = 0
    invalid_pairs_excluded: int = 0
    selection_mode: str = "folder"
    selected_files: int = 0
    carried_forward_files: int = 0
    include_filename: bool = True
    message: str = ""


@dataclass
class PairingStats:
    paired_file_pairs: int = 0
    orphaned_golden_files: int = 0
    orphaned_modified_files: int = 0
    other_language_orphans_excluded: int = 0
    invalid_pairs_excluded: int = 0


DecisionProvider = Callable[[str, dict[str, object]], str]
Logger = Callable[[str], None]


def normalise_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold().lstrip("\ufeff"))


def safe_relative(path: Path, parent: Path) -> str:
    return path.resolve().relative_to(parent.resolve()).as_posix()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def read_json(path: Path, default: object) -> object:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return default


def find_project_data_folders(selected: Sequence[Path]) -> list[Path]:
    found: dict[str, Path] = {}
    for raw in selected:
        root = raw.expanduser().resolve()
        if not root.is_dir():
            raise AggregationError(f"Selected folder does not exist: {root}")
        if root.name.casefold() == "project data":
            found[os.path.normcase(str(root))] = root
            continue
        direct = root / "Project data"
        if direct.is_dir():
            resolved = direct.resolve()
            found[os.path.normcase(str(resolved))] = resolved
            continue
        for current, dirs, _files in os.walk(root):
            dirs[:] = [
                name for name in dirs
                if name.casefold() not in EXCLUDED_FOLDER_NAMES and not name.startswith(".")
            ]
            current_path = Path(current)
            if current_path.name.casefold() == "project data":
                resolved = current_path.resolve()
                found[os.path.normcase(str(resolved))] = resolved
                dirs[:] = []
    if not found:
        raise AggregationError("No folder named 'Project data' was found in the selection.")
    return sorted(found.values(), key=lambda item: str(item).casefold())


def nearest_project_data(path: Path) -> Path | None:
    for parent in path.parents:
        if parent.name.casefold() == "project data":
            return parent.resolve()
    return None


def build_input_plan(
    selected_folders: Sequence[Path],
    selected_files: Sequence[Path],
) -> list[tuple[Path, tuple[Path, ...] | None]]:
    """Return project roots and either an explicit file set or None for full scan."""
    full_projects = find_project_data_folders(selected_folders) if selected_folders else []
    full_keys = {os.path.normcase(str(path.resolve())) for path in full_projects}
    individual: dict[str, tuple[Path, set[Path]]] = {}
    for raw in selected_files:
        path = raw.expanduser().resolve()
        if not path.is_file():
            raise AggregationError(f"Selected input file does not exist: {path}")
        if path.suffix.casefold() not in {".csv", ".tsv"}:
            raise AggregationError(f"Selected input file is not CSV or TSV: {path}")
        project_data = nearest_project_data(path) or path.parent.resolve()
        key = os.path.normcase(str(project_data))
        if key in full_keys:
            continue
        if key not in individual:
            individual[key] = (project_data, set())
        individual[key][1].add(path)
    plan = [(path, None) for path in full_projects]
    plan.extend(
        (project_data, tuple(sorted(files, key=lambda value: str(value).casefold())))
        for project_data, files in individual.values()
    )
    if not plan:
        raise AggregationError("Add at least one project folder or individual CSV/TSV file.")
    return sorted(plan, key=lambda item: str(item[0]).casefold())


def extract_language(value: str) -> str:
    matches: list[str] = []
    for pattern in LANGUAGE_PATTERNS:
        matches.extend(match.upper() for match in pattern.findall(value))
    matches = [match for match in matches if match not in {"EN", "OR"}]
    return matches[-1] if matches else ""


def role_from_path(relative: Path) -> tuple[str, bool, str]:
    parts = [part.casefold() for part in relative.parts]
    name = relative.name.casefold()
    if any(part in EXCLUDED_FOLDER_NAMES for part in parts[:-1]):
        return "excluded", False, "excluded folder"
    if any(marker in name for marker in REFERENCE_MARKERS) or re.search(r"(?i)(?:^|[-_])DWN\d*(?:$|[-_. ])", name):
        return "excluded", False, "reference file"
    if "1-golden-standard-files" in parts:
        return "golden", True, "canonical golden folder"
    if "2-modified-files" in parts:
        return "modified", True, "canonical modified folder"
    golden = any(marker in name for marker in GOLDEN_MARKERS) or name == "golden.csv"
    modified = any(marker in name for marker in MODIFIED_MARKERS) or name == "modified.csv"
    if golden and modified:
        return "ambiguous", False, "both golden and modified markers"
    if golden:
        return "golden", False, "filename marker"
    if modified:
        return "modified", False, "filename marker"
    return "unclassified", False, "no role marker"


def pair_key_for(path: Path, language: str) -> str:
    match = DOCUMENT_ID_RE.search(path.name)
    if match:
        document = match.group(1).upper()
    else:
        stem = path.stem.casefold()
        for marker in GOLDEN_MARKERS + MODIFIED_MARKERS:
            stem = stem.replace(marker, " ")
        stem = re.sub(r"(?i)\b(?:golden|gold|standard|modified|adjusted|raw|mt|errors?)\b", " ", stem)
        stem = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
        document = stem[:120].upper()
    return f"{document}|{language}" if document and language else ""


def scan_candidates(
    project_data: Path,
    selected_files: Sequence[Path] | None = None,
) -> tuple[list[Candidate], list[AttentionItem], int]:
    candidates: list[Candidate] = []
    attention: list[AttentionItem] = []
    scanned = 0
    if selected_files is None:
        paths: list[Path] = []
        for current, dirs, files in os.walk(project_data):
            dirs[:] = [
                name for name in dirs
                if name.casefold() not in EXCLUDED_FOLDER_NAMES and not name.startswith(".")
            ]
            paths.extend(
                Path(current) / name
                for name in sorted(files, key=str.casefold)
                if name.casefold().endswith((".csv", ".tsv"))
            )
    else:
        paths = list(selected_files)
    for path in paths:
        scanned += 1
        try:
            relative = path.relative_to(project_data)
        except ValueError:
            relative = Path(path.name)
        role, canonical, reason = role_from_path(relative)
        if role == "excluded":
            continue
        if role in {"ambiguous", "unclassified"}:
            attention.append(AttentionItem(
                "warning",
                relative.as_posix(),
                f"CSV was not included: {reason}.",
                "Move it into a canonical folder or rename it with a clear role marker.",
            ))
            continue
        language = extract_language(relative.as_posix())
        if not language:
            attention.append(AttentionItem(
                "warning",
                relative.as_posix(),
                "CSV was not included because its target language could not be determined.",
                "Add an EN-XX language marker to its filename or parent folder.",
            ))
            continue
        digest = sha256_file(path)
        candidates.append(Candidate(
            path=path,
            project_data=project_data,
            relative_path=relative.as_posix(),
            role=role,
            language=language,
            pair_key=pair_key_for(path, language),
            sha256=digest,
            canonical_folder=canonical,
        ))
    if selected_files is not None and project_data.name.casefold() != "project data":
        attention.append(AttentionItem(
            "info",
            str(project_data),
            "Selected files are outside a Project data folder; aggregates will be written beside them.",
        ))
    candidates.sort(key=lambda item: (not item.canonical_folder, item.relative_path.casefold()))
    return candidates, attention, scanned


def decode_csv(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    if not raw:
        raise AggregationError("The file is empty.")
    for encoding in ("utf-8-sig", "utf-16", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise AggregationError("The file is not valid UTF-8, UTF-16, or Windows-1252 text.")


def delimiter_score(text: str, delimiter: str, role: str) -> tuple[int, int, int]:
    try:
        rows = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        header = next(rows)
    except (csv.Error, StopIteration):
        return (-1, -1, -1)
    aliases = SEGMENT_ALIASES | ORI_ALIASES | (GOLDEN_TRA_ALIASES if role == "golden" else MODIFIED_TRA_ALIASES)
    normalised = [normalise_header(value) for value in header]
    known = sum(value in aliases for value in normalised)
    structural = int(any(value in ORI_ALIASES for value in normalised)) + int(
        any(value in (GOLDEN_TRA_ALIASES if role == "golden" else MODIFIED_TRA_ALIASES) for value in normalised)
    )
    return structural, known, min(len(header), 20)


def choose_delimiter(text: str, suffix: str, role: str) -> str:
    candidates = ("|", "\t", ",", ";")
    if suffix.casefold() == ".tsv":
        candidates = ("\t", "|", ",", ";")
    return max(candidates, key=lambda value: delimiter_score(text, value, role))


def choose_column(headers: Sequence[str], aliases: set[str]) -> int | None:
    for index, value in enumerate(headers):
        if normalise_header(value) in aliases:
            return index
    return None


def inspect_csv(path: Path, role: str, language: str) -> Inspection:
    try:
        text, encoding = decode_csv(path)
    except AggregationError as exc:
        return Inspection(error=str(exc))
    delimiter = choose_delimiter(text, path.suffix, role)
    try:
        parsed = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    except csv.Error as exc:
        return Inspection(delimiter=delimiter, encoding=encoding, error=f"CSV parsing failed: {exc}")
    while parsed and not any(cell.strip() for cell in parsed[0]):
        parsed.pop(0)
    if not parsed:
        return Inspection(delimiter=delimiter, encoding=encoding, error="The file contains no header or data rows.")
    headers = parsed[0]
    data_rows = [row for row in parsed[1:] if any(cell.strip() for cell in row)]
    normalised_headers = [normalise_header(value) for value in headers]
    segment_index = choose_column(headers, SEGMENT_ALIASES)
    ori_index = choose_column(headers, ORI_ALIASES)
    tra_aliases = GOLDEN_TRA_ALIASES if role == "golden" else MODIFIED_TRA_ALIASES
    tra_index = choose_column(headers, tra_aliases)

    # Two language-code columns are a common converter output.
    if len(headers) == 2 and all(re.fullmatch(r"(?i)[A-Z]{2}", value.strip()) for value in headers):
        ori_index, tra_index = 0, 1
    if ori_index is None or tra_index is None:
        shown = " | ".join(headers)
        return Inspection(
            delimiter=delimiter,
            encoding=encoding,
            error=f"Required ORI and TRA columns could not be identified. Detected header: {shown}",
        )

    trailing_artifact = ""
    artifact_match = re.fullmatch(r"\s*(.*?)\s*([,;]+)\s*", headers[tra_index])
    if artifact_match and normalise_header(artifact_match.group(1)) in tra_aliases:
        possible = artifact_match.group(2)
        artifact_character = possible[-1]
        populated_targets = [
            row[tra_index] for row in data_rows
            if tra_index < len(row) and row[tra_index].strip()
        ]
        matching = sum(value.endswith(artifact_character) for value in populated_targets)
        if populated_targets and matching / len(populated_targets) >= 0.9:
            trailing_artifact = possible
        elif populated_targets:
            return Inspection(
                delimiter=delimiter,
                encoding=encoding,
                error="The TRA header has trailing delimiter artifacts, but data rows do not use them consistently.",
            )

    selected = {index for index in (segment_index, ori_index, tra_index) if index is not None}
    alternate_target_aliases = (GOLDEN_TRA_ALIASES | MODIFIED_TRA_ALIASES) - tra_aliases
    allowed_extra = {
        index for index, value in enumerate(normalised_headers)
        if value in alternate_target_aliases or not value
    }
    meaningful_unselected: list[str] = []
    for index, header in enumerate(headers):
        if index in selected or index in allowed_extra:
            continue
        if header.strip() or any(index < len(row) and row[index].strip() for row in data_rows):
            meaningful_unselected.append(header or f"column {index + 1}")
    if meaningful_unselected:
        return Inspection(
            delimiter=delimiter,
            encoding=encoding,
            error="Unexpected non-standard columns would be discarded: " + ", ".join(meaningful_unselected),
        )

    rows: list[SourceRow] = []
    for row_number, row in enumerate(data_rows, 1):
        if len(row) > len(headers) and any(value.strip() for value in row[len(headers):]):
            return Inspection(
                delimiter=delimiter,
                encoding=encoding,
                error=f"Row {row_number + 1} contains more populated fields than the header.",
            )
        padded = list(row) + [""] * max(0, len(headers) - len(row))
        segment = padded[segment_index].strip() if segment_index is not None else str(row_number)
        target = padded[tra_index]
        if trailing_artifact:
            artifact_character = trailing_artifact[-1]
            run_length = len(target) - len(target.rstrip(artifact_character))
            remove_count = min(run_length, len(trailing_artifact))
            if remove_count:
                target = target[:-remove_count]
        rows.append(SourceRow(
            segment=segment or str(row_number),
            ori=padded[ori_index],
            tra=target,
        ))
    if not rows:
        return Inspection(delimiter=delimiter, encoding=encoding, error="The file contains no data rows.")

    corrections: list[str] = []
    if delimiter != "|":
        corrections.append(f"convert {repr(delimiter)} delimiter to pipe")
    if [value.strip().lstrip("\ufeff") for value in headers] != ["SEGMENT", "ORI", "TRA"]:
        corrections.append("normalise the header to SEGMENT|ORI|TRA")
    if segment_index is None:
        corrections.append("add sequential SEGMENT values")
    if len(headers) != 3:
        corrections.append("remove safely identified alternate or empty columns")
    if trailing_artifact:
        corrections.append(f"remove trailing {trailing_artifact!r} export artifacts")
    if encoding not in {"utf-8", "utf-8-sig"}:
        corrections.append(f"convert {encoding} encoding to UTF-8")
    return Inspection(rows=rows, delimiter=delimiter, encoding=encoding, corrections=corrections)


def csv_bytes(rows: Iterable[Sequence[str]], header: Sequence[str] = ("SEGMENT", "ORI", "TRA")) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter="|", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def backup_and_correct(candidate: Candidate, inspection: Inspection) -> Path:
    backup_root = candidate.project_data / BACKUP_ROOT_NAME
    backup = backup_root / Path(candidate.relative_path)
    if backup.exists():
        try:
            if sha256_file(backup) != candidate.sha256:
                timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                backup = backup.with_name(f"{backup.stem}.{timestamp}{backup.suffix}")
        except OSError:
            timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = backup.with_name(f"{backup.stem}.{timestamp}{backup.suffix}")
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copy2(candidate.path, backup)
    atomic_write_bytes(
        candidate.path,
        csv_bytes((row.segment, row.ori, row.tra) for row in inspection.rows),
    )
    candidate.sha256 = sha256_file(candidate.path)
    return backup


def cache_path(project_data: Path, digest: str) -> Path:
    return project_data / OUTPUT_ROOT_NAME / CACHE_NAME / f"{digest}.json"


def load_cached_rows(project_data: Path, digest: str) -> list[SourceRow] | None:
    data = read_json(cache_path(project_data, digest), {})
    if not isinstance(data, dict) or data.get("sha256") != digest:
        return None
    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list):
        return None
    try:
        return [SourceRow(str(row[0]), str(row[1]), str(row[2])) for row in raw_rows]
    except (IndexError, TypeError):
        return None


def save_cached_rows(project_data: Path, digest: str, rows: Sequence[SourceRow]) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "sha256": digest,
        "rows": [[row.segment, row.ori, row.tra] for row in rows],
    }
    atomic_write_text(cache_path(project_data, digest), json_text(payload))


def previous_input_maps(manifest: object) -> tuple[dict[str, dict[str, object]], dict[str, list[dict[str, object]]]]:
    by_path: dict[str, dict[str, object]] = {}
    by_name: dict[str, list[dict[str, object]]] = {}
    if not isinstance(manifest, dict) or not isinstance(manifest.get("inputs"), list):
        return by_path, by_name
    for raw in manifest["inputs"]:
        if not isinstance(raw, dict):
            continue
        relative = str(raw.get("relative_path", "")).casefold()
        name = str(raw.get("name", "")).casefold()
        if relative:
            by_path[relative] = raw
        if name:
            by_name.setdefault(name, []).append(raw)
    return by_path, by_name


def load_carried_forward_inputs(
    project_data: Path,
    previous: object,
    selected_relative_paths: set[str],
) -> tuple[list[LoadedDataset], list[dict[str, object]]]:
    """Keep cached prior inputs during an incremental individual-file run."""
    if not isinstance(previous, dict) or not isinstance(previous.get("inputs"), list):
        return [], []
    loaded: list[LoadedDataset] = []
    records: list[dict[str, object]] = []
    for raw in previous["inputs"]:
        if not isinstance(raw, dict):
            continue
        relative = str(raw.get("relative_path", ""))
        if not relative or relative.casefold() in selected_relative_paths:
            continue
        used_hash = str(raw.get("used_hash", ""))
        rows = load_cached_rows(project_data, used_hash) if used_hash else None
        if rows is None:
            raise AggregationError(
                "An individual-file run cannot safely preserve a previous input because its cache is missing: "
                f"{relative}. Run a full project-folder scan instead."
            )
        path = project_data / Path(relative)
        candidate = Candidate(
            path=path,
            project_data=project_data,
            relative_path=relative,
            role=str(raw.get("role", "")),
            language=str(raw.get("language", "")),
            pair_key=str(raw.get("pair_key", "")),
            sha256=str(raw.get("detected_hash", used_hash)),
            canonical_folder=any(
                part.casefold() in {"1-golden-standard-files", "2-modified-files"}
                for part in Path(relative).parts
            ),
        )
        loaded.append(LoadedDataset(candidate, rows, used_hash, True))
        record = dict(raw)
        record["cache_hit"] = True
        record["carried_forward"] = True
        records.append(record)
    return loaded, records


def detect_changed_conflicts(
    candidates: Sequence[Candidate], previous: object
) -> dict[str, dict[str, object]]:
    by_path, by_name = previous_input_maps(previous)
    conflicts: dict[str, dict[str, object]] = {}
    for candidate in candidates:
        old = by_path.get(candidate.relative_path.casefold())
        if old and str(old.get("used_hash", old.get("detected_hash", ""))) != candidate.sha256:
            conflicts[candidate.relative_path] = old
            continue
        same_name = by_name.get(candidate.path.name.casefold(), [])
        differing = [
            item for item in same_name
            if str(item.get("used_hash", item.get("detected_hash", ""))) != candidate.sha256
        ]
        if differing and not old:
            conflicts[candidate.relative_path] = differing[0]
    return conflicts


def dataset_similarity(left: LoadedDataset, right: LoadedDataset) -> float:
    def cleaned(value: str) -> str:
        text = value.casefold()
        for marker in GOLDEN_MARKERS + MODIFIED_MARKERS:
            text = text.replace(marker, " ")
        return re.sub(r"[^a-z0-9]+", " ", text).strip()

    return SequenceMatcher(None, cleaned(left.candidate.path.stem), cleaned(right.candidate.path.stem)).ratio()


def role_marker_confidence(dataset: LoadedDataset) -> float:
    name = dataset.candidate.path.name.casefold()
    if dataset.candidate.role == "golden":
        return 0.1 if "golden standard" in name or "gold-standard" in name else 0.0
    if "adjusted" in name or "modified" in name:
        return 0.1
    if "merged" in name or "comparison" in name:
        return -0.1
    return 0.0


def deduplicate_datasets(datasets: Sequence[LoadedDataset]) -> tuple[list[LoadedDataset], list[LoadedDataset]]:
    kept: list[LoadedDataset] = []
    skipped: list[LoadedDataset] = []
    seen: set[tuple[str, str, str]] = set()
    for dataset in sorted(
        datasets,
        key=lambda item: (not item.candidate.canonical_folder, item.candidate.relative_path.casefold()),
    ):
        key = (dataset.candidate.role, dataset.candidate.language, dataset.used_hash)
        if key in seen:
            skipped.append(dataset)
        else:
            kept.append(dataset)
            seen.add(key)
    return kept, skipped


def pair_datasets(
    datasets: Sequence[LoadedDataset], attention: list[AttentionItem]
) -> tuple[list[tuple[LoadedDataset, LoadedDataset]], list[LoadedDataset]]:
    grouped: dict[str, dict[str, list[LoadedDataset]]] = {}
    orphans: list[LoadedDataset] = []
    for dataset in datasets:
        if not dataset.candidate.pair_key:
            orphans.append(dataset)
            continue
        grouped.setdefault(dataset.candidate.pair_key, {"golden": [], "modified": []})[
            dataset.candidate.role
        ].append(dataset)
    pairs: list[tuple[LoadedDataset, LoadedDataset]] = []
    for key in sorted(grouped):
        golden = grouped[key]["golden"]
        modified = grouped[key]["modified"]
        if len(golden) > 1 or len(modified) > 1:
            attention.append(AttentionItem(
                "warning",
                key,
                f"Pair key has {len(golden)} golden and {len(modified)} modified candidates; best filename matches were used.",
                "Review the provenance report.",
            ))
        while golden and modified:
            scored = [
                (
                    dataset_similarity(left, right)
                    + role_marker_confidence(left)
                    + role_marker_confidence(right)
                    + (2.0 if len(left.rows) == len(right.rows) else 0.0),
                    left,
                    right,
                )
                for left in golden for right in modified
            ]
            _score, left, right = max(
                scored,
                key=lambda item: (item[0], item[1].candidate.relative_path, item[2].candidate.relative_path),
            )
            golden.remove(left)
            modified.remove(right)
            pairs.append((left, right))
        orphans.extend(golden)
        orphans.extend(modified)
    return pairs, orphans


def stable_rank(seed: str, *parts: str) -> str:
    material = "\x1f".join((seed, *parts)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def verification_keys(rows: Sequence[AggregateRow], percent: float, seed: str, group: str) -> set[str]:
    if not rows or percent <= 0:
        return set()
    count = int(len(rows) * percent / 100.0 + 0.5)
    if count == 0:
        count = 1
    if percent < 100:
        count = min(count, max(0, len(rows) - 1)) if len(rows) > 1 else 1
    else:
        count = len(rows)
    # Golden and modified partner groups deliberately share a ranking namespace,
    # so exactly the same output segment IDs are reserved on both sides.
    ranking_group = re.sub(r"_(?:golden|modified)$", "", group)
    ranked = sorted(rows, key=lambda row: stable_rank(seed, ranking_group, row.segment))
    return {row.segment for row in ranked[:count]}


def aligned_pair_rows(
    golden: LoadedDataset,
    modified: LoadedDataset,
    attention: list[AttentionItem],
) -> tuple[list[SourceRow], list[SourceRow]] | None:
    if len(golden.rows) != len(modified.rows):
        attention.append(AttentionItem(
            "error",
            f"{golden.candidate.relative_path} <> {modified.candidate.relative_path}",
            f"Paired files have different row counts ({len(golden.rows)} versus {len(modified.rows)}) and were excluded.",
            "Correct or explicitly separate the files, then rerun.",
        ))
        return None
    golden_segments = [row.segment for row in golden.rows]
    modified_segments = [row.segment for row in modified.rows]
    if golden_segments == modified_segments:
        aligned_modified = list(modified.rows)
    elif len(set(golden_segments)) == len(golden_segments) and set(golden_segments) == set(modified_segments):
        lookup = {row.segment: row for row in modified.rows}
        aligned_modified = [lookup[segment] for segment in golden_segments]
        attention.append(AttentionItem(
            "warning",
            f"{golden.candidate.relative_path} <> {modified.candidate.relative_path}",
            "Modified rows were reordered to align matching source segment identifiers.",
            "Review the provenance report if ordering is significant.",
        ))
    else:
        aligned_modified = list(modified.rows)
        attention.append(AttentionItem(
            "warning",
            f"{golden.candidate.relative_path} <> {modified.candidate.relative_path}",
            "Segment identifiers differ; equal-length files were aligned by row position.",
            "Review the pairing and provenance report.",
        ))
    ori_mismatches = sum(
        1 for left, right in zip(golden.rows, aligned_modified)
        if left.ori.strip() != right.ori.strip()
    )
    if ori_mismatches:
        attention.append(AttentionItem(
            "warning",
            f"{golden.candidate.relative_path} <> {modified.candidate.relative_path}",
            f"{ori_mismatches} aligned rows have different ORI text.",
            "Review the source pairing.",
        ))
    return list(golden.rows), aligned_modified


def build_aggregate_groups(
    datasets: Sequence[LoadedDataset],
    attention: list[AttentionItem],
    verification_percent: float,
    seed: str,
) -> tuple[dict[str, dict[str, list[AggregateRow]]], PairingStats]:
    groups: dict[str, list[AggregateRow]] = {key: [] for key in AGGREGATE_FILENAMES}
    pairs, orphans = pair_datasets(datasets, attention)
    stats = PairingStats()

    counters = {key: 0 for key in AGGREGATE_FILENAMES}
    for golden, modified in pairs:
        aligned = aligned_pair_rows(golden, modified, attention)
        if aligned is None:
            stats.invalid_pairs_excluded += 1
            continue
        stats.paired_file_pairs += 1
        golden_rows, modified_rows = aligned
        family = "core" if golden.candidate.language in {"DE", "FR"} else "other"
        golden_group = f"paired_{family}_golden"
        modified_group = f"paired_{family}_modified"
        for left, right in zip(golden_rows, modified_rows):
            counters[golden_group] += 1
            output_segment = str(counters[golden_group])
            counters[modified_group] = counters[golden_group]
            groups[golden_group].append(AggregateRow(
                output_segment, left.ori, left.tra,
                golden.candidate.relative_path, left.segment, golden.used_hash,
            ))
            groups[modified_group].append(AggregateRow(
                output_segment, right.ori, right.tra,
                modified.candidate.relative_path, right.segment, modified.used_hash,
            ))

    for dataset in orphans:
        if dataset.candidate.language not in {"DE", "FR"}:
            stats.other_language_orphans_excluded += 1
            attention.append(AttentionItem(
                "warning",
                dataset.candidate.relative_path,
                f"Unpaired EN-{dataset.candidate.language} {dataset.candidate.role} file has no requested orphan aggregate and was excluded.",
                "Add its counterpart or handle it during later-language verification.",
            ))
            continue
        if dataset.candidate.role == "golden":
            stats.orphaned_golden_files += 1
        else:
            stats.orphaned_modified_files += 1
        group = f"orphan_core_{dataset.candidate.role}"
        for row in dataset.rows:
            counters[group] += 1
            groups[group].append(AggregateRow(
                str(counters[group]), row.ori, row.tra,
                dataset.candidate.relative_path, row.segment, dataset.used_hash,
            ))

    by_location: dict[str, dict[str, list[AggregateRow]]] = {
        location: {key: [] for key in groups} for location in LOCATION_FOLDERS
    }
    for group, complete_rows in groups.items():
        by_location["complete"][group] = complete_rows
        selected = verification_keys(complete_rows, verification_percent, seed, group)
        by_location["verification"][group] = [row for row in complete_rows if row.segment in selected]
        by_location["production"][group] = [row for row in complete_rows if row.segment not in selected]
    return by_location, stats


def write_aggregate_outputs(
    project_data: Path,
    by_location: dict[str, dict[str, list[AggregateRow]]],
    include_filename: bool,
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    output_root = project_data / OUTPUT_ROOT_NAME
    report_rows: list[dict[str, object]] = []
    provenance: list[dict[str, str]] = []
    for location, folder_name in LOCATION_FOLDERS.items():
        folder = output_root / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        for group, filename in AGGREGATE_FILENAMES.items():
            path = folder / filename
            rows = by_location[location][group]
            if rows:
                if include_filename:
                    content = csv_bytes(
                        ((
                            row.source_file,
                            row.segment,
                            row.ori,
                            row.tra,
                        ) for row in rows),
                        header=("FILENAME", "SEGMENT", "ORI", "TRA"),
                    )
                else:
                    content = csv_bytes((row.segment, row.ori, row.tra) for row in rows)
                atomic_write_bytes(path, content)
            elif path.exists():
                path.unlink()
            report_rows.append({
                "location": folder_name,
                "aggregate_file": filename,
                "segments": len(rows),
                "source_files": len({row.source_file for row in rows}),
                "created": "yes" if rows else "no",
            })
            for row in rows:
                provenance.append({
                    "location": folder_name,
                    "aggregate_file": filename,
                    "output_segment": row.segment,
                    "source_file": row.source_file,
                    "source_segment": row.source_segment,
                    "source_sha256": row.source_hash,
                })
    return report_rows, provenance


def write_dict_csv(path: Path, headers: Sequence[str], rows: Sequence[dict[str, object]]) -> None:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers, delimiter="|", lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write_bytes(path, stream.getvalue().encode("utf-8-sig"))


def project_report_text(result: ProjectResult) -> str:
    actionable_attention = sum(
        item.severity.casefold() in {"warning", "error"} for item in result.attention
    )
    lines = [
        f"{APP_NAME} {VERSION}",
        f"Project data: {result.project_data}",
        f"Status: {result.status}",
        f"Input mode: {result.selection_mode}",
        f"Original filename column: {'included' if result.include_filename else 'not included'}",
        "",
        f"CSV/TSV files scanned: {result.files_scanned}",
        f"Individually selected files: {result.selected_files}",
        f"Previous files carried forward: {result.carried_forward_files}",
        f"Source files included: {result.files_included}",
        f"Paired file pairs: {result.paired_file_pairs}",
        f"Paired source files: {result.paired_files}",
        f"Orphaned source files exported: {result.orphaned_files} "
        f"({result.orphaned_golden_files} golden, {result.orphaned_modified_files} modified)",
        f"Other-language orphan files excluded: {result.other_language_orphans_excluded}",
        f"Invalid file pairs excluded: {result.invalid_pairs_excluded}",
        f"Identical duplicate copies skipped: {result.duplicates_skipped}",
        f"Previously processed files loaded from cache: {result.cache_hits}",
        f"Automatic corrections applied: {result.corrections_applied}",
        f"Changed-file conflicts: {result.changed_conflicts}",
        f"Files/items needing attention: {actionable_attention}",
        "",
        "Aggregate contents:",
    ]
    for row in result.report_rows:
        lines.append(
            f"- {row['location']}/{row['aggregate_file']}: "
            f"{row['segments']} segments from {row['source_files']} source files "
            f"(created: {row['created']})"
        )
    lines.extend(["", "Attention items:"])
    if not result.attention:
        lines.append("- None")
    else:
        for item in result.attention:
            suffix = f" Suggested action: {item.action}" if item.action else ""
            lines.append(f"- [{item.severity.upper()}] {item.path}: {item.issue}{suffix}")
    if result.message:
        lines.extend(["", result.message])
    return "\n".join(lines) + "\n"


def default_overall_report_folder(results: Sequence[ProjectResult]) -> Path:
    bases = [
        result.project_data.parent
        if result.project_data.name.casefold() == "project data"
        else result.project_data
        for result in results
    ]
    try:
        common = Path(os.path.commonpath([str(path.resolve()) for path in bases]))
    except ValueError:
        return Path.cwd()
    if common == Path(common.anchor):
        return Path.cwd()
    return common


def segment_total(result: ProjectResult, location: str) -> int:
    return sum(
        int(row.get("segments", 0))
        for row in result.report_rows
        if row.get("location") == location
    )


def overall_report_text(
    results: Sequence[ProjectResult],
    verification_percent: float,
    seed: str,
    include_filename: bool,
) -> str:
    actionable = sum(
        item.severity.casefold() in {"warning", "error"}
        for result in results for item in result.attention
    )
    lines = [
        f"{APP_NAME} {VERSION} — overall aggregation report",
        f"Generated: {dt.datetime.now(dt.timezone.utc).isoformat()}",
        f"Projects/collections: {len(results)}",
        f"Verification percentage: {verification_percent}",
        f"Deterministic seed: {seed}",
        f"Original filename column: {'included' if include_filename else 'not included'}",
        "",
        "Overall totals:",
        f"- Files scanned or selected: {sum(result.files_scanned for result in results)}",
        f"- Source files included: {sum(result.files_included for result in results)}",
        f"- Paired file pairs: {sum(result.paired_file_pairs for result in results)}",
        f"- Paired source files: {sum(result.paired_files for result in results)}",
        f"- Orphaned source files exported: {sum(result.orphaned_files for result in results)}",
        f"- Duplicate copies skipped: {sum(result.duplicates_skipped for result in results)}",
        f"- Files carried forward from cache: {sum(result.carried_forward_files for result in results)}",
        f"- Files/items needing attention: {actionable}",
        "",
        "Per-project results:",
    ]
    for result in results:
        lines.extend([
            "",
            f"[{result.status.upper()}] {result.project_data}",
            f"- Input mode: {result.selection_mode}",
            f"- Included files: {result.files_included}",
            f"- Paired pairs/files: {result.paired_file_pairs}/{result.paired_files}",
            f"- Orphans exported: {result.orphaned_files} "
            f"({result.orphaned_golden_files} golden, {result.orphaned_modified_files} modified)",
            f"- Production segments exported: {segment_total(result, LOCATION_FOLDERS['production'])}",
            f"- Verification segments exported: {segment_total(result, LOCATION_FOLDERS['verification'])}",
            f"- Complete segments exported: {segment_total(result, LOCATION_FOLDERS['complete'])}",
            f"- Attention items: {sum(item.severity.casefold() in {'warning', 'error'} for item in result.attention)}",
        ])
        for row in result.report_rows:
            lines.append(
                f"  - {row['location']}/{row['aggregate_file']}: "
                f"{row['segments']} segments from {row['source_files']} files"
            )
        for item in result.attention:
            suffix = f" Suggested action: {item.action}" if item.action else ""
            lines.append(f"  - [{item.severity.upper()}] {item.path}: {item.issue}{suffix}")
    return "\n".join(lines) + "\n"


def write_overall_reports(
    results: Sequence[ProjectResult],
    folder: Path,
    verification_percent: float,
    seed: str,
    include_filename: bool,
) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    csv_rows: list[dict[str, object]] = []
    for result in results:
        base = {
            "project_data": str(result.project_data),
            "status": result.status,
            "selection_mode": result.selection_mode,
        }
        csv_rows.append(dict(
            base,
            record_type="project",
            files_scanned=result.files_scanned,
            files_included=result.files_included,
            paired_file_pairs=result.paired_file_pairs,
            paired_files=result.paired_files,
            orphaned_files=result.orphaned_files,
            orphaned_golden_files=result.orphaned_golden_files,
            orphaned_modified_files=result.orphaned_modified_files,
            duplicates_skipped=result.duplicates_skipped,
            cache_hits=result.cache_hits,
            carried_forward_files=result.carried_forward_files,
            production_segments=segment_total(result, LOCATION_FOLDERS["production"]),
            verification_segments=segment_total(result, LOCATION_FOLDERS["verification"]),
            complete_segments=segment_total(result, LOCATION_FOLDERS["complete"]),
        ))
        csv_rows.extend(dict(base, record_type="aggregate", **row) for row in result.report_rows)
        csv_rows.extend(
            dict(base, record_type="attention", **asdict(item))
            for item in result.attention
        )
    headers = (
        "record_type", "project_data", "status", "selection_mode",
        "files_scanned", "files_included", "paired_file_pairs", "paired_files",
        "orphaned_files", "orphaned_golden_files", "orphaned_modified_files",
        "duplicates_skipped", "cache_hits", "carried_forward_files",
        "production_segments", "verification_segments", "complete_segments",
        "location", "aggregate_file", "segments", "source_files", "created",
        "severity", "path", "issue", "action",
    )
    csv_path = folder / OVERALL_REPORT_CSV_NAME
    text_path = folder / OVERALL_REPORT_TEXT_NAME
    write_dict_csv(csv_path, headers, csv_rows)
    atomic_write_text(
        text_path,
        overall_report_text(results, verification_percent, seed, include_filename),
    )
    return csv_path, text_path


def resolve_changed_action(
    requested: str,
    decision_provider: DecisionProvider,
    project_data: Path,
    conflicts: dict[str, dict[str, object]],
) -> str:
    if not conflicts:
        return "reprocess"
    if requested != "ask":
        return requested
    return decision_provider("changed_files", {
        "project_data": str(project_data),
        "files": sorted(conflicts),
        "message": "Previously seen filenames now have different content.",
    })


def load_candidate(
    candidate: Candidate,
    previous_record: dict[str, object] | None,
    conflict_action: str,
    auto_fix: str,
    decision_provider: DecisionProvider,
    attention: list[AttentionItem],
    dry_run: bool,
) -> tuple[LoadedDataset | None, bool, bool]:
    """Return dataset, correction_applied, cache_hit."""
    if previous_record and conflict_action == "skip":
        old_hash = str(previous_record.get("used_hash", ""))
        old_rows = load_cached_rows(candidate.project_data, old_hash) if old_hash else None
        if old_rows is None:
            attention.append(AttentionItem(
                "error", candidate.relative_path,
                "The changed file was skipped, but its previous cached rows are unavailable.",
                "Choose reprocess instead.",
            ))
            return None, False, False
        old_candidate = Candidate(
            path=candidate.path,
            project_data=candidate.project_data,
            relative_path=candidate.relative_path,
            role=str(previous_record.get("role", candidate.role)),
            language=str(previous_record.get("language", candidate.language)),
            pair_key=str(previous_record.get("pair_key", candidate.pair_key)),
            sha256=candidate.sha256,
            canonical_folder=candidate.canonical_folder,
        )
        attention.append(AttentionItem(
            "warning", candidate.relative_path,
            "Changed content was skipped; the previous cached version remains in the aggregates.",
            "Reprocess when the new content has been reviewed.",
        ))
        return LoadedDataset(old_candidate, old_rows, old_hash, True), False, True

    cached = load_cached_rows(candidate.project_data, candidate.sha256)
    if cached is not None:
        return LoadedDataset(candidate, cached, candidate.sha256, True), False, True

    inspection = inspect_csv(candidate.path, candidate.role, candidate.language)
    if inspection.error:
        attention.append(AttentionItem(
            "error", candidate.relative_path,
            inspection.error,
            "Correct the source CSV and rerun.",
        ))
        return None, False, False
    correction_applied = False
    if inspection.corrections:
        action = auto_fix
        if action == "ask":
            action = decision_provider("automatic_correction", {
                "project_data": str(candidate.project_data),
                "path": candidate.relative_path,
                "corrections": inspection.corrections,
            })
        if action not in {"yes", "fix", "reprocess"}:
            attention.append(AttentionItem(
                "warning", candidate.relative_path,
                "Fixable non-standard CSV was excluded because automatic correction was declined: "
                + "; ".join(inspection.corrections),
                "Approve automatic correction or fix it manually.",
            ))
            return None, False, False
        if not dry_run:
            backup = backup_and_correct(candidate, inspection)
            correction_applied = True
            attention.append(AttentionItem(
                "info", candidate.relative_path,
                "Automatic correction applied: " + "; ".join(inspection.corrections),
                f"Original backed up to {safe_relative(backup, candidate.project_data)}.",
            ))
        else:
            attention.append(AttentionItem(
                "info", candidate.relative_path,
                "Dry run would apply: " + "; ".join(inspection.corrections),
            ))
    if not dry_run:
        save_cached_rows(candidate.project_data, candidate.sha256, inspection.rows)
    return LoadedDataset(candidate, inspection.rows, candidate.sha256, False), correction_applied, False


def aggregate_project(
    project_data: Path,
    selected_files: Sequence[Path] | None,
    verification_percent: float,
    seed: str,
    include_filename: bool,
    auto_fix: str,
    changed_file_action: str,
    decision_provider: DecisionProvider,
    log: Logger,
    dry_run: bool = False,
) -> ProjectResult:
    result = ProjectResult(
        project_data=project_data,
        status="running",
        selection_mode="individual files" if selected_files is not None else "folder",
        selected_files=len(selected_files or ()),
        include_filename=include_filename,
    )
    log(f"Scanning {project_data}")
    candidates, scan_attention, scanned = scan_candidates(project_data, selected_files)
    result.attention.extend(scan_attention)
    result.files_scanned = scanned

    output_root = project_data / OUTPUT_ROOT_NAME
    manifest_path = output_root / MANIFEST_NAME
    previous = read_json(manifest_path, {})
    conflicts = detect_changed_conflicts(candidates, previous)
    result.changed_conflicts = len(conflicts)
    action = resolve_changed_action(
        changed_file_action, decision_provider, project_data, conflicts
    )
    if action == "abort":
        result.status = "aborted"
        result.message = "No output was changed because processing was cancelled at a changed-file conflict."
        return result
    if action not in {"reprocess", "skip"}:
        raise AggregationError(f"Unsupported changed-file decision: {action}")

    previous_by_path, _previous_by_name = previous_input_maps(previous)
    loaded: list[LoadedDataset] = []
    manifest_inputs: list[dict[str, object]] = []
    for candidate in candidates:
        old = conflicts.get(candidate.relative_path)
        if old is None:
            old = previous_by_path.get(candidate.relative_path.casefold())
        candidate_action = action if candidate.relative_path in conflicts else "reprocess"
        dataset, corrected, cache_hit = load_candidate(
            candidate,
            old,
            candidate_action,
            auto_fix,
            decision_provider,
            result.attention,
            dry_run,
        )
        if dataset is None:
            continue
        loaded.append(dataset)
        result.corrections_applied += int(corrected)
        result.cache_hits += int(cache_hit)
        manifest_inputs.append(candidate.manifest_record(
            dataset.used_hash, len(dataset.rows), cache_hit
        ))

    if selected_files is not None:
        selected_relative_paths: set[str] = set()
        for path in selected_files:
            try:
                relative = path.relative_to(project_data).as_posix()
            except ValueError:
                relative = path.name
            selected_relative_paths.add(relative.casefold())
        carried, carried_records = load_carried_forward_inputs(
            project_data, previous, selected_relative_paths
        )
        loaded.extend(carried)
        manifest_inputs.extend(carried_records)
        result.carried_forward_files = len(carried)
        result.cache_hits += len(carried)

    kept, duplicates = deduplicate_datasets(loaded)
    result.duplicates_skipped = len(duplicates)
    for duplicate in duplicates:
        result.attention.append(AttentionItem(
            "info", duplicate.candidate.relative_path,
            "Identical content already appeared for the same role and language and was not aggregated twice.",
        ))
    result.files_included = len(kept)
    by_location, pairing_stats = build_aggregate_groups(
        kept, result.attention, verification_percent, seed
    )
    result.paired_file_pairs = pairing_stats.paired_file_pairs
    result.paired_files = pairing_stats.paired_file_pairs * 2
    result.orphaned_golden_files = pairing_stats.orphaned_golden_files
    result.orphaned_modified_files = pairing_stats.orphaned_modified_files
    result.orphaned_files = pairing_stats.orphaned_golden_files + pairing_stats.orphaned_modified_files
    result.other_language_orphans_excluded = pairing_stats.other_language_orphans_excluded
    result.invalid_pairs_excluded = pairing_stats.invalid_pairs_excluded

    if dry_run:
        for location, folder in LOCATION_FOLDERS.items():
            for group, filename in AGGREGATE_FILENAMES.items():
                rows = by_location[location][group]
                result.report_rows.append({
                    "location": folder,
                    "aggregate_file": filename,
                    "segments": len(rows),
                    "source_files": len({row.source_file for row in rows}),
                    "created": "dry-run" if rows else "no",
                })
        result.status = "dry-run"
        result.message = "Dry run completed; no source or output files were changed."
        return result

    report_rows, provenance = write_aggregate_outputs(
        project_data, by_location, include_filename
    )
    result.report_rows = report_rows
    result.status = "complete"
    attention_rows = [asdict(item) for item in result.attention]
    summary_row = {
        "record_type": "project",
        "status": result.status,
        "selection_mode": result.selection_mode,
        "files_scanned": result.files_scanned,
        "files_included": result.files_included,
        "paired_file_pairs": result.paired_file_pairs,
        "paired_files": result.paired_files,
        "orphaned_files": result.orphaned_files,
        "orphaned_golden_files": result.orphaned_golden_files,
        "orphaned_modified_files": result.orphaned_modified_files,
        "duplicates_skipped": result.duplicates_skipped,
        "cache_hits": result.cache_hits,
        "carried_forward_files": result.carried_forward_files,
    }
    write_dict_csv(
        output_root / REPORT_CSV_NAME,
        (
            "record_type", "status", "selection_mode", "files_scanned", "files_included",
            "paired_file_pairs", "paired_files", "orphaned_files", "orphaned_golden_files",
            "orphaned_modified_files", "duplicates_skipped", "cache_hits", "carried_forward_files",
            "location", "aggregate_file", "segments", "source_files", "created",
            "severity", "path", "issue", "action",
        ),
        [summary_row]
        + [dict(row, record_type="aggregate") for row in report_rows]
        + [dict(row, record_type="attention") for row in attention_rows],
    )
    write_dict_csv(
        output_root / PROVENANCE_NAME,
        ("location", "aggregate_file", "output_segment", "source_file", "source_segment", "source_sha256"),
        provenance,
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tool": APP_NAME,
        "tool_version": VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project_data": str(project_data),
        "settings": {
            "verification_percent": verification_percent,
            "seed": seed,
            "include_filename": include_filename,
            "selection_mode": result.selection_mode,
            "output_delimiter": "|",
            "output_encoding": "utf-8-sig",
        },
        "inputs": manifest_inputs,
        "summary": {
            "files_scanned": result.files_scanned,
            "files_included": result.files_included,
            "duplicates_skipped": result.duplicates_skipped,
            "cache_hits": result.cache_hits,
            "corrections_applied": result.corrections_applied,
            "paired_file_pairs": result.paired_file_pairs,
            "paired_files": result.paired_files,
            "orphaned_files": result.orphaned_files,
            "orphaned_golden_files": result.orphaned_golden_files,
            "orphaned_modified_files": result.orphaned_modified_files,
            "other_language_orphans_excluded": result.other_language_orphans_excluded,
            "invalid_pairs_excluded": result.invalid_pairs_excluded,
            "carried_forward_files": result.carried_forward_files,
            "attention_items": sum(
                item.severity.casefold() in {"warning", "error"} for item in result.attention
            ),
            "informational_items": sum(
                item.severity.casefold() == "info" for item in result.attention
            ),
        },
    }
    atomic_write_text(manifest_path, json_text(manifest))
    atomic_write_text(output_root / REPORT_TEXT_NAME, project_report_text(result))
    log(f"Completed {project_data}: {result.files_included} source files included")
    return result


def aggregate_projects(
    selected: Sequence[Path],
    verification_percent: float = 15.0,
    seed: str = "2026",
    auto_fix: str = "ask",
    changed_file_action: str = "ask",
    decision_provider: DecisionProvider | None = None,
    log: Logger | None = None,
    dry_run: bool = False,
    individual_files: Sequence[Path] = (),
    include_filename: bool = True,
    overall_report_folder: Path | None = None,
) -> list[ProjectResult]:
    if not 0 <= verification_percent <= 100:
        raise AggregationError("Verification percentage must be between 0 and 100.")
    if decision_provider is None:
        decision_provider = lambda kind, context: "abort" if kind == "changed_files" else "no"
    if log is None:
        log = lambda message: None
    plan = build_input_plan(selected, individual_files)
    results = [
        aggregate_project(
            folder,
            files,
            verification_percent,
            seed,
            include_filename,
            auto_fix,
            changed_file_action,
            decision_provider,
            log,
            dry_run,
        )
        for folder, files in plan
    ]
    if not dry_run:
        report_folder = (
            overall_report_folder.expanduser().resolve()
            if overall_report_folder is not None
            else default_overall_report_folder(results)
        )
        csv_path, text_path = write_overall_reports(
            results,
            report_folder,
            verification_percent,
            seed,
            include_filename,
        )
        log(f"Overall reports: {text_path} and {csv_path}")
    return results


def console_decision_provider(kind: str, context: dict[str, object]) -> str:
    if not sys.stdin.isatty():
        return "abort" if kind == "changed_files" else "no"
    if kind == "changed_files":
        print("\nChanged files detected:")
        for value in context.get("files", []):
            print(f"  - {value}")
        while True:
            answer = input("Reprocess changed files, use cached previous versions, or abort? [r/s/a]: ").strip().casefold()
            if answer in {"r", "reprocess"}:
                return "reprocess"
            if answer in {"s", "skip"}:
                return "skip"
            if answer in {"a", "abort", ""}:
                return "abort"
    if kind == "automatic_correction":
        print(f"\nFixable CSV: {context.get('path')}")
        for value in context.get("corrections", []):
            print(f"  - {value}")
        answer = input("Back up and correct this source file? [y/N]: ").strip().casefold()
        return "yes" if answer in {"y", "yes"} else "no"
    return "no"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-folder", action="append", type=Path, help="Project folder or Project data folder; repeat for multiple projects")
    parser.add_argument("--input-file", action="append", type=Path, help="Individual CSV/TSV file to add; repeat for multiple files")
    parser.add_argument("--verification-percent", type=float, default=15.0, help="Percentage of each aggregate reserved for verification (default: 15)")
    parser.add_argument("--seed", default="2026", help="Stable verification selection seed (default: 2026)")
    parser.add_argument(
        "--include-filename",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Add project-relative original filename as the first aggregate column (default: enabled)",
    )
    parser.add_argument("--overall-report-folder", type=Path, help="Folder for cross-project reports (default: common project parent)")
    parser.add_argument("--auto-fix", choices=("ask", "yes", "no"), default="ask", help="How to handle safely correctable non-standard CSVs")
    parser.add_argument("--changed-file-action", choices=("ask", "reprocess", "skip", "abort"), default="ask", help="How to handle filenames whose content changed since the prior run")
    parser.add_argument("--dry-run", action="store_true", help="Scan and report without changing source or output files")
    parser.add_argument("--headless", action="store_true", help="Use command-line mode instead of the desktop interface")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def run_cli(args: argparse.Namespace) -> int:
    if not args.project_folder and not args.input_file:
        print("error: at least one --project-folder or --input-file is required in headless mode", file=sys.stderr)
        return 2
    try:
        results = aggregate_projects(
            args.project_folder or [],
            verification_percent=args.verification_percent,
            seed=args.seed,
            auto_fix=args.auto_fix,
            changed_file_action=args.changed_file_action,
            decision_provider=console_decision_provider,
            log=print,
            dry_run=args.dry_run,
            individual_files=args.input_file or [],
            include_filename=args.include_filename,
            overall_report_folder=args.overall_report_folder,
        )
    except AggregationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for result in results:
        print("\n" + project_report_text(result))
    return 1 if any(result.status == "aborted" for result in results) else 0


def run_gui(default_folders: Sequence[Path] = (), default_files: Sequence[Path] = ()) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:
        print(f"Tkinter is unavailable: {exc}", file=sys.stderr)
        return 2

    class MultiFolderDialog(tk.Toplevel):
        """Folder browser whose child-folder list supports Ctrl/Shift selection."""

        def __init__(self, parent: tk.Misc, initial_directory: Path) -> None:
            super().__init__(parent)
            self.title("Add project folders")
            self.geometry("760x560")
            self.minsize(580, 420)
            self.transient(parent)
            self.result: list[Path] = []
            self.current_directory = (
                initial_directory.resolve()
                if initial_directory.is_dir()
                else Path.cwd().resolve()
            )
            self.visible_paths: list[Path] = []
            self.path_var = tk.StringVar(value=str(self.current_directory))
            self.status_var = tk.StringVar(value="")
            self._build()
            self.navigate(self.current_directory)
            self.protocol("WM_DELETE_WINDOW", self.cancel)
            self.grab_set()
            self.folder_box.focus_set()

        def _build(self) -> None:
            outer = ttk.Frame(self, padding=12)
            outer.pack(fill=tk.BOTH, expand=True)
            ttk.Label(
                outer,
                text="Select several folders with Ctrl+click or Shift+click, then add them together.",
                font=("Segoe UI", 10, "bold"),
            ).pack(anchor=tk.W)
            ttk.Label(
                outer,
                text="Double-click a folder to browse inside it. Use Add current folder when the folder shown above is the one you need.",
                wraplength=720,
            ).pack(anchor=tk.W, pady=(2, 10))

            path_frame = ttk.Frame(outer)
            path_frame.pack(fill=tk.X)
            ttk.Entry(path_frame, textvariable=self.path_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
            ttk.Button(path_frame, text="Go", command=self.go_to_typed_path).pack(side=tk.LEFT, padx=(6, 0))
            ttk.Button(path_frame, text="Browse…", command=self.choose_parent).pack(side=tk.LEFT, padx=(6, 0))
            ttk.Button(path_frame, text="Up", command=self.go_up).pack(side=tk.LEFT, padx=(6, 0))

            list_frame = ttk.Frame(outer)
            list_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 4))
            self.folder_box = tk.Listbox(
                list_frame,
                selectmode=tk.EXTENDED,
                exportselection=False,
            )
            scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.folder_box.yview)
            self.folder_box.configure(yscrollcommand=scrollbar.set)
            self.folder_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scrollbar.pack(side=tk.LEFT, fill=tk.Y)
            self.folder_box.bind("<Double-Button-1>", self.open_double_clicked)
            self.folder_box.bind("<<ListboxSelect>>", self.update_selection_status)

            ttk.Label(outer, textvariable=self.status_var).pack(anchor=tk.W, pady=(0, 8))
            buttons = ttk.Frame(outer)
            buttons.pack(fill=tk.X)
            ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side=tk.RIGHT)
            ttk.Button(buttons, text="Add selected folders", command=self.accept_selected).pack(side=tk.RIGHT, padx=(0, 8))
            ttk.Button(buttons, text="Add current folder", command=self.accept_current).pack(side=tk.RIGHT, padx=(0, 8))

        def navigate(self, directory: Path) -> None:
            try:
                resolved = directory.expanduser().resolve()
                children = sorted(
                    (path for path in resolved.iterdir() if path.is_dir()),
                    key=lambda path: path.name.casefold(),
                )
            except OSError as exc:
                messagebox.showerror("Cannot open folder", str(exc), parent=self)
                return
            self.current_directory = resolved
            self.path_var.set(str(resolved))
            self.visible_paths = children
            self.folder_box.delete(0, tk.END)
            for child in children:
                self.folder_box.insert(tk.END, child.name)
            self.status_var.set(f"{len(children)} subfolder(s). No folders selected.")

        def go_to_typed_path(self) -> None:
            self.navigate(Path(self.path_var.get().strip()))

        def choose_parent(self) -> None:
            selected = filedialog.askdirectory(
                title="Choose the parent folder to browse",
                initialdir=self.current_directory,
                parent=self,
            )
            if selected:
                self.navigate(Path(selected))

        def go_up(self) -> None:
            self.navigate(self.current_directory.parent)

        def open_double_clicked(self, event: object) -> None:
            selection = self.folder_box.curselection()
            if len(selection) == 1:
                self.navigate(self.visible_paths[selection[0]])

        def update_selection_status(self, event: object | None = None) -> None:
            selected = len(self.folder_box.curselection())
            self.status_var.set(
                f"{len(self.visible_paths)} subfolder(s). {selected} selected."
            )

        def accept_selected(self) -> None:
            selection = self.folder_box.curselection()
            if not selection:
                messagebox.showinfo(
                    "No folders selected",
                    "Select one or more folders, or use Add current folder.",
                    parent=self,
                )
                return
            self.result = [self.visible_paths[index] for index in selection]
            self.destroy()

        def accept_current(self) -> None:
            self.result = [self.current_directory]
            self.destroy()

        def cancel(self) -> None:
            self.result = []
            self.destroy()

    class AggregatorWindow(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title(f"{APP_NAME} {VERSION}")
            self.geometry("980x860")
            self.minsize(800, 700)
            self.percent_var = tk.StringVar(value="15")
            self.seed_var = tk.StringVar(value="2026")
            self.auto_fix_var = tk.StringVar(value="ask")
            self.changed_var = tk.StringVar(value="ask")
            self.include_filename_var = tk.BooleanVar(value=True)
            self.overall_report_var = tk.StringVar(value="")
            self._build()
            for folder in default_folders:
                self.folder_list.insert(tk.END, str(folder))
            for path in default_files:
                self.file_list.insert(tk.END, str(path))

        def _build(self) -> None:
            outer = ttk.Frame(self, padding=12)
            outer.pack(fill=tk.BOTH, expand=True)
            ttk.Label(outer, text="Project folders", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)
            ttk.Label(
                outer,
                text="Add one or more project folders or Project data folders. Each is scanned recursively.",
            ).pack(anchor=tk.W, pady=(2, 6))
            list_frame = ttk.Frame(outer)
            list_frame.pack(fill=tk.X)
            self.folder_list = tk.Listbox(list_frame, height=7, selectmode=tk.EXTENDED)
            self.folder_list.pack(side=tk.LEFT, fill=tk.X, expand=True)
            buttons = ttk.Frame(list_frame)
            buttons.pack(side=tk.LEFT, padx=(8, 0), anchor=tk.N)
            ttk.Button(buttons, text="Add folders…", command=self.add_folder).pack(fill=tk.X)
            ttk.Button(buttons, text="Remove", command=self.remove_folders).pack(fill=tk.X, pady=(6, 0))

            ttk.Label(outer, text="Individual files", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, pady=(12, 0))
            ttk.Label(
                outer,
                text="Optional CSV/TSV files are added incrementally to their nearest Project data collection.",
            ).pack(anchor=tk.W, pady=(2, 6))
            file_frame = ttk.Frame(outer)
            file_frame.pack(fill=tk.X)
            self.file_list = tk.Listbox(file_frame, height=5, selectmode=tk.EXTENDED)
            self.file_list.pack(side=tk.LEFT, fill=tk.X, expand=True)
            file_buttons = ttk.Frame(file_frame)
            file_buttons.pack(side=tk.LEFT, padx=(8, 0), anchor=tk.N)
            ttk.Button(file_buttons, text="Add files…", command=self.add_files).pack(fill=tk.X)
            ttk.Button(file_buttons, text="Remove", command=self.remove_files).pack(fill=tk.X, pady=(6, 0))

            options = ttk.LabelFrame(outer, text="Options", padding=10)
            options.pack(fill=tk.X, pady=12)
            ttk.Label(options, text="Verification percentage").grid(row=0, column=0, sticky=tk.W)
            ttk.Entry(options, textvariable=self.percent_var, width=10).grid(row=0, column=1, padx=(8, 24), sticky=tk.W)
            ttk.Label(options, text="Deterministic seed").grid(row=0, column=2, sticky=tk.W)
            ttk.Entry(options, textvariable=self.seed_var, width=16).grid(row=0, column=3, padx=(8, 0), sticky=tk.W)
            ttk.Label(options, text="Correct fixable CSVs").grid(row=1, column=0, pady=(10, 0), sticky=tk.W)
            ttk.Combobox(options, textvariable=self.auto_fix_var, values=("ask", "yes", "no"), state="readonly", width=9).grid(row=1, column=1, padx=(8, 24), pady=(10, 0), sticky=tk.W)
            ttk.Label(options, text="Changed files").grid(row=1, column=2, pady=(10, 0), sticky=tk.W)
            ttk.Combobox(options, textvariable=self.changed_var, values=("ask", "reprocess", "skip", "abort"), state="readonly", width=14).grid(row=1, column=3, padx=(8, 0), pady=(10, 0), sticky=tk.W)
            ttk.Checkbutton(
                options,
                text="Include original project-relative filename as the first aggregate CSV column",
                variable=self.include_filename_var,
            ).grid(row=2, column=0, columnspan=4, pady=(10, 0), sticky=tk.W)
            ttk.Label(options, text="Overall report folder").grid(row=3, column=0, pady=(10, 0), sticky=tk.W)
            ttk.Entry(options, textvariable=self.overall_report_var).grid(
                row=3, column=1, columnspan=2, padx=(8, 8), pady=(10, 0), sticky=tk.EW
            )
            ttk.Button(options, text="Browse…", command=self.choose_report_folder).grid(
                row=3, column=3, pady=(10, 0), sticky=tk.W
            )
            options.columnconfigure(2, weight=1)

            action_frame = ttk.Frame(outer)
            action_frame.pack(fill=tk.X)
            ttk.Button(action_frame, text="Scan only", command=lambda: self.run(True)).pack(side=tk.LEFT)
            self.run_button = ttk.Button(action_frame, text="Create aggregated datasets", command=lambda: self.run(False))
            self.run_button.pack(side=tk.LEFT, padx=(8, 0))
            ttk.Label(outer, text="Activity log", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, pady=(12, 4))
            self.log_box = tk.Text(outer, height=20, wrap=tk.WORD, state=tk.DISABLED)
            self.log_box.pack(fill=tk.BOTH, expand=True)

        def add_folder(self) -> None:
            existing = list(self.folder_list.get(0, tk.END))
            if existing:
                initial = Path(existing[-1]).parent
            else:
                script_parent = Path(__file__).resolve().parent.parent
                initial = script_parent if script_parent.is_dir() else Path.cwd()
            dialog = MultiFolderDialog(self, initial)
            self.wait_window(dialog)
            known = set(existing)
            for folder in dialog.result:
                value = str(folder)
                if value not in known:
                    self.folder_list.insert(tk.END, value)
                    known.add(value)

        def remove_folders(self) -> None:
            for index in reversed(self.folder_list.curselection()):
                self.folder_list.delete(index)

        def add_files(self) -> None:
            paths = filedialog.askopenfilenames(
                title="Select individual CSV or TSV files",
                filetypes=(("CSV and TSV files", "*.csv *.tsv"), ("All files", "*.*")),
            )
            existing = set(self.file_list.get(0, tk.END))
            for path in paths:
                if path not in existing:
                    self.file_list.insert(tk.END, path)
                    existing.add(path)

        def remove_files(self) -> None:
            for index in reversed(self.file_list.curselection()):
                self.file_list.delete(index)

        def choose_report_folder(self) -> None:
            folder = filedialog.askdirectory(title="Select overall report folder")
            if folder:
                self.overall_report_var.set(folder)

        def log(self, message: str) -> None:
            self.log_box.configure(state=tk.NORMAL)
            self.log_box.insert(tk.END, message + "\n")
            self.log_box.see(tk.END)
            self.log_box.configure(state=tk.DISABLED)
            self.update_idletasks()

        def decide(self, kind: str, context: dict[str, object]) -> str:
            if kind == "changed_files":
                files = list(context.get("files", []))
                shown = "\n".join(f"• {value}" for value in files[:20])
                if len(files) > 20:
                    shown += f"\n• …and {len(files) - 20} more"
                answer = messagebox.askyesnocancel(
                    "Changed source files",
                    "These filenames were processed previously but now have different content:\n\n"
                    + shown
                    + "\n\nYes: reprocess new content\nNo: use cached previous content\nCancel: abort this project",
                    parent=self,
                )
                return "reprocess" if answer is True else "skip" if answer is False else "abort"
            if kind == "automatic_correction":
                corrections = "\n".join(f"• {value}" for value in context.get("corrections", []))
                answer = messagebox.askyesno(
                    "Correct non-standard CSV?",
                    f"{context.get('path')}\n\n{corrections}\n\n"
                    f"The original will be copied to {BACKUP_ROOT_NAME} before correction.",
                    parent=self,
                )
                return "yes" if answer else "no"
            return "no"

        def run(self, dry_run: bool) -> None:
            folders = [Path(value) for value in self.folder_list.get(0, tk.END)]
            files = [Path(value) for value in self.file_list.get(0, tk.END)]
            if not folders and not files:
                messagebox.showerror("No inputs", "Add at least one project folder or individual file.", parent=self)
                return
            try:
                percent = float(self.percent_var.get())
            except ValueError:
                messagebox.showerror("Invalid percentage", "Enter a number from 0 to 100.", parent=self)
                return
            self.run_button.configure(state=tk.DISABLED)
            self.log("Starting scan…" if dry_run else "Starting aggregation…")
            try:
                results = aggregate_projects(
                    folders,
                    verification_percent=percent,
                    seed=self.seed_var.get(),
                    auto_fix=self.auto_fix_var.get(),
                    changed_file_action=self.changed_var.get(),
                    decision_provider=self.decide,
                    log=self.log,
                    dry_run=dry_run,
                    individual_files=files,
                    include_filename=self.include_filename_var.get(),
                    overall_report_folder=(
                        Path(self.overall_report_var.get().strip())
                        if self.overall_report_var.get().strip() else None
                    ),
                )
                for result in results:
                    self.log(project_report_text(result))
                aborted = sum(result.status == "aborted" for result in results)
                attention = sum(
                    item.severity.casefold() in {"warning", "error"}
                    for result in results for item in result.attention
                )
                messagebox.showinfo(
                    "Aggregation finished",
                    f"Processed {len(results)} Project data folder(s).\n"
                    f"Aborted: {aborted}\nAttention items: {attention}\n\n"
                    "See each 4-Aggregated-datasets report and the overall report for details." if not dry_run else
                    f"Scanned {len(results)} Project data folder(s).\nAttention items: {attention}\n\nNo files were changed.",
                    parent=self,
                )
            except Exception as exc:
                self.log(traceback.format_exc())
                messagebox.showerror("Aggregation failed", str(exc), parent=self)
            finally:
                self.run_button.configure(state=tk.NORMAL)

    app = AggregatorWindow()
    app.mainloop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.headless or args.project_folder or args.input_file:
        return run_cli(args)
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
