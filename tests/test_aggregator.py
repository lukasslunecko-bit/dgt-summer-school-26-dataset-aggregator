import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import dgt_dataset_aggregator as aggregator


def write_csv(path: Path, rows, delimiter="|", header=("SEGMENT", "ORI", "TRA")):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def read_csv(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle, delimiter="|"))


class AggregatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "Example project"
        self.data = self.project / "Project data"

    def tearDown(self):
        self.temporary.cleanup()

    def decision(self, kind, context):
        return "reprocess" if kind == "changed_files" else "yes"

    def create_pair(self, document="TEST-2026-00001-00-00", language="DE", count=10):
        golden = self.data / "1-golden-standard-files" / f"user - {document}-00-{language}-TRA-00 - Golden standard.csv"
        modified = self.data / "2-modified-files" / f"user - {document}-00-{language}-TRA-00 - Adjusted.csv"
        rows = [(str(index), f"Source {index}", f"Golden {index}") for index in range(1, count + 1)]
        changed = [(str(index), f"Source {index}", f"Modified {index}") for index in range(1, count + 1)]
        write_csv(golden, rows)
        write_csv(modified, changed)
        return golden, modified

    def test_builds_aligned_production_verification_and_complete(self):
        self.create_pair(count=10)
        self.create_pair(document="TEST-2026-00002-00-00", language="IT", count=4)
        orphan = self.data / "1-golden-standard-files" / "user - TEST-2026-00003-00-00-00-FR-TRA-00 - Golden standard.csv"
        write_csv(orphan, [(str(i), f"O {i}", f"T {i}") for i in range(1, 5)])
        reference = self.data / "3-reference-files" / "TEST-2026-99999_EN-DE-DWN.csv"
        write_csv(reference, [("1", "ignored", "ignored")])

        results = aggregator.aggregate_projects(
            [self.project], verification_percent=15, seed="test-seed",
            auto_fix="yes", changed_file_action="reprocess",
            decision_provider=self.decision,
        )
        self.assertEqual(results[0].status, "complete")
        output = self.data / aggregator.OUTPUT_ROOT_NAME
        core_golden = aggregator.AGGREGATE_FILENAMES["paired_core_golden"]
        core_modified = aggregator.AGGREGATE_FILENAMES["paired_core_modified"]
        verification_golden = read_csv(output / "42-verification" / core_golden)
        verification_modified = read_csv(output / "42-verification" / core_modified)
        production_golden = read_csv(output / "41-production" / core_golden)
        complete_golden = read_csv(output / "43-complete" / core_golden)

        self.assertEqual(len(verification_golden) - 1, 2)
        self.assertEqual(len(production_golden) - 1, 8)
        self.assertEqual(len(complete_golden) - 1, 10)
        self.assertEqual(complete_golden[0], ["FILENAME", "SEGMENT", "ORI", "TRA"])
        self.assertEqual(
            [row[1] for row in verification_golden[1:]],
            [row[1] for row in verification_modified[1:]],
        )
        other_name = aggregator.AGGREGATE_FILENAMES["paired_other_golden"]
        self.assertEqual(len(read_csv(output / "43-complete" / other_name)) - 1, 4)
        orphan_name = aggregator.AGGREGATE_FILENAMES["orphan_core_golden"]
        self.assertEqual(len(read_csv(output / "43-complete" / orphan_name)) - 1, 4)
        self.assertTrue((output / aggregator.PROVENANCE_NAME).is_file())
        self.assertTrue((output / aggregator.REPORT_TEXT_NAME).is_file())
        self.assertTrue((self.project / aggregator.OVERALL_REPORT_CSV_NAME).is_file())
        self.assertTrue((self.project / aggregator.OVERALL_REPORT_TEXT_NAME).is_file())
        overall_text = (self.project / aggregator.OVERALL_REPORT_TEXT_NAME).read_text(encoding="utf-8")
        self.assertIn("Paired file pairs: 2", overall_text)
        self.assertIn("Complete segments exported:", overall_text)
        self.assertEqual(results[0].paired_file_pairs, 2)
        self.assertEqual(results[0].orphaned_golden_files, 1)

    def test_identical_same_role_copy_is_aggregated_once(self):
        golden, modified = self.create_pair(count=3)
        duplicate = golden.parent / "converted" / golden.name
        duplicate.parent.mkdir(parents=True)
        duplicate.write_bytes(golden.read_bytes())

        result = aggregator.aggregate_projects(
            [self.data], auto_fix="yes", changed_file_action="reprocess",
            decision_provider=self.decision,
        )[0]
        self.assertEqual(result.duplicates_skipped, 1)
        name = aggregator.AGGREGATE_FILENAMES["paired_core_golden"]
        complete = read_csv(self.data / aggregator.OUTPUT_ROOT_NAME / "43-complete" / name)
        self.assertEqual(len(complete) - 1, 3)

    def test_fixable_semicolon_csv_is_backed_up_and_normalised(self):
        golden, modified = self.create_pair(count=2)
        write_csv(golden, [("1", "One", "Un"), ("2", "Two", "Deux")], delimiter=";")

        result = aggregator.aggregate_projects(
            [self.data], auto_fix="yes", changed_file_action="reprocess",
            decision_provider=self.decision,
        )[0]
        self.assertEqual(result.corrections_applied, 1)
        self.assertEqual(read_csv(golden)[0], ["SEGMENT", "ORI", "TRA"])
        backup = self.data / aggregator.BACKUP_ROOT_NAME / golden.relative_to(self.data)
        self.assertTrue(backup.is_file())
        self.assertIn(b";", backup.read_bytes().splitlines()[0])

    def test_two_language_columns_gain_segments(self):
        golden = self.data / "1-golden-standard-files" / "TEST-2026-00004_ENFR - Golden standard.csv"
        modified = self.data / "2-modified-files" / "TEST-2026-00004_ENFR - Modified.csv"
        write_csv(golden, [("Hello", "Bonjour"), ("Bye", "Au revoir")], header=("EN", "FR"))
        write_csv(modified, [("Hello", "Bon jour"), ("Bye", "Salut")], header=("EN", "FR"))

        result = aggregator.aggregate_projects(
            [self.data], auto_fix="yes", changed_file_action="reprocess",
            decision_provider=self.decision,
        )[0]
        self.assertEqual(result.corrections_applied, 2)
        self.assertEqual(read_csv(golden)[1][0], "1")

    def test_trailing_export_columns_are_removed_without_losing_real_punctuation(self):
        golden, modified = self.create_pair(count=2)
        golden.write_text(
            "SEGMENT|ORI|TRA,,,,\n1|One|Target,,,,\n2|Two|Ends with comma,,,,,\n",
            encoding="utf-8",
        )
        modified.write_text(
            "SEGMENT|ORI|TRA;\n1|One|Changed;\n2|Two|Changed again;;\n",
            encoding="utf-8",
        )
        result = aggregator.aggregate_projects(
            [self.data], auto_fix="yes", changed_file_action="reprocess",
            decision_provider=self.decision,
        )[0]
        self.assertEqual(result.corrections_applied, 2)
        self.assertEqual(read_csv(golden)[2][2], "Ends with comma,")
        self.assertEqual(read_csv(modified)[2][2], "Changed again;")

    def test_changed_file_can_keep_cached_previous_content(self):
        _golden, modified = self.create_pair(count=2)
        first = aggregator.aggregate_projects(
            [self.data], auto_fix="yes", changed_file_action="reprocess",
            decision_provider=self.decision,
        )[0]
        self.assertEqual(first.status, "complete")
        output_name = aggregator.AGGREGATE_FILENAMES["paired_core_modified"]
        output_path = self.data / aggregator.OUTPUT_ROOT_NAME / "43-complete" / output_name
        original_output = output_path.read_bytes()

        write_csv(modified, [("1", "Source 1", "New content"), ("2", "Source 2", "New content")])
        second = aggregator.aggregate_projects(
            [self.data], auto_fix="yes", changed_file_action="skip",
            decision_provider=self.decision,
        )[0]
        self.assertEqual(second.changed_conflicts, 1)
        self.assertEqual(output_path.read_bytes(), original_output)

        third = aggregator.aggregate_projects(
            [self.data], auto_fix="yes", changed_file_action="skip",
            decision_provider=self.decision,
        )[0]
        self.assertEqual(third.changed_conflicts, 1)
        self.assertEqual(output_path.read_bytes(), original_output)

    def test_structural_problem_is_reported_and_excluded(self):
        bad = self.data / "2-modified-files" / "TEST-2026-00005-00-00-00-DE-TRA-00 - Merged.csv"
        write_csv(bad, [("1", "A", "B", "C")], header=("SEGMENT", "ORI", "TRA", "COMMENT"))
        result = aggregator.aggregate_projects(
            [self.data], auto_fix="yes", changed_file_action="reprocess",
            decision_provider=self.decision,
        )[0]
        self.assertEqual(result.files_included, 0)
        self.assertTrue(any("Unexpected non-standard columns" in item.issue for item in result.attention))

    def test_dry_run_does_not_create_output_or_modify_source(self):
        golden, _modified = self.create_pair(count=2)
        before = hashlib.sha256(golden.read_bytes()).hexdigest()
        results = aggregator.aggregate_projects(
            [self.project], auto_fix="yes", changed_file_action="reprocess",
            decision_provider=self.decision, dry_run=True,
        )
        self.assertEqual(results[0].status, "dry-run")
        self.assertFalse((self.data / aggregator.OUTPUT_ROOT_NAME).exists())
        self.assertEqual(hashlib.sha256(golden.read_bytes()).hexdigest(), before)

    def test_filename_column_can_be_disabled(self):
        self.create_pair(count=2)
        aggregator.aggregate_projects(
            [self.project], auto_fix="yes", changed_file_action="reprocess",
            decision_provider=self.decision, include_filename=False,
        )
        name = aggregator.AGGREGATE_FILENAMES["paired_core_golden"]
        output = read_csv(self.data / aggregator.OUTPUT_ROOT_NAME / "43-complete" / name)
        self.assertEqual(output[0], ["SEGMENT", "ORI", "TRA"])

    def test_individual_files_incrementally_extend_existing_project(self):
        self.create_pair(count=2)
        aggregator.aggregate_projects(
            [self.project], auto_fix="yes", changed_file_action="reprocess",
            decision_provider=self.decision,
        )
        new_golden, new_modified = self.create_pair(
            document="TEST-2026-00009-00-00", language="FR", count=3
        )
        result = aggregator.aggregate_projects(
            [], auto_fix="yes", changed_file_action="reprocess",
            decision_provider=self.decision,
            individual_files=[new_golden, new_modified],
        )[0]
        self.assertEqual(result.selection_mode, "individual files")
        self.assertEqual(result.selected_files, 2)
        self.assertEqual(result.carried_forward_files, 2)
        self.assertEqual(result.paired_file_pairs, 2)
        name = aggregator.AGGREGATE_FILENAMES["paired_core_golden"]
        output = read_csv(self.data / aggregator.OUTPUT_ROOT_NAME / "43-complete" / name)
        self.assertEqual(len(output) - 1, 5)

    def test_multi_project_run_writes_common_overall_report(self):
        self.create_pair(count=2)
        second_data = self.root / "Second project" / "Project data"
        write_csv(
            second_data / "1-golden-standard-files" / "TEST-2026-00020-00-00-00-DE-TRA-00 - Golden standard.csv",
            [("1", "A", "B")],
        )
        write_csv(
            second_data / "2-modified-files" / "TEST-2026-00020-00-00-00-DE-TRA-00 - Modified.csv",
            [("1", "A", "C")],
        )
        results = aggregator.aggregate_projects(
            [self.root], auto_fix="yes", changed_file_action="reprocess",
            decision_provider=self.decision,
        )
        self.assertEqual(len(results), 2)
        overall = self.root / aggregator.OVERALL_REPORT_TEXT_NAME
        self.assertTrue(overall.is_file())
        text = overall.read_text(encoding="utf-8")
        self.assertIn("Projects/collections: 2", text)
        self.assertIn("Second project", text)


if __name__ == "__main__":
    unittest.main()
