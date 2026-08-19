"""Tests for breast_cancer_hive.sql.

We don't have a Hive cluster here, so we parse the SQL and assert the structural
properties the user requires for the /test/breast_cancer HDFS path:

  1. The table is EXTERNAL and points at LOCATION '/test/breast_cancer'.
  2. There is no LOAD DATA statement (data lives on HDFS already).
  3. The original 31 columns, the CSV delimiter, and the header-skip property
     are preserved.
  4. The drop-table comment notes that dropping the table does NOT delete the
     HDFS file (safety hint, since the file is no longer managed by Hive).

Each test reads the SQL file fresh so the suite always exercises the artefact on
disk, never an in-memory copy.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SQL_PATH = Path(__file__).resolve().parent / "breast_cancer_hive.sql"
SQL_TEXT = SQL_PATH.read_text(encoding="utf-8")

EXPECTED_COLUMNS = [
    "mean_radius", "mean_texture", "mean_perimeter", "mean_area",
    "mean_smoothness", "mean_compactness", "mean_concavity",
    "mean_concave_points", "mean_symmetry", "mean_fractal_dimension",
    "radius_error", "texture_error", "perimeter_error", "area_error",
    "smoothness_error", "compactness_error", "concavity_error",
    "concave_points_error", "symmetry_error", "fractal_dimension_error",
    "worst_radius", "worst_texture", "worst_perimeter", "worst_area",
    "worst_smoothness", "worst_compactness", "worst_concavity",
    "worst_concave_points", "worst_symmetry", "worst_fractal_dimension",
    "target",
]


def _strip_sql_comments(sql: str) -> str:
    """Remove `--` line comments so that phrases inside documentation do not
    count as SQL statements."""
    return re.sub(r"--[^\n]*", "", sql)


def _create_table_block(sql: str) -> str:
    """Return the substring from CREATE TABLE up to (but not including) the
    first semicolon that follows it. Defensive: there should be exactly one
    CREATE TABLE in this file. Allows optional EXTERNAL keyword."""
    match = re.search(
        r"CREATE\s+(?:EXTERNAL\s+)?TABLE\b[^;]*",
        sql,
        flags=re.DOTALL | re.IGNORECASE,
    )
    assert match, "CREATE TABLE statement not found"
    return match.group(0)


class BreastCancerHiveSqlTests(unittest.TestCase):
    def test_uses_external_table(self):
        create = _create_table_block(SQL_TEXT)
        self.assertRegex(create, r"CREATE\s+EXTERNAL\s+TABLE", msg="table must be EXTERNAL")

    def test_location_points_at_hdfs_path(self):
        create = _create_table_block(SQL_TEXT)
        self.assertRegex(
            create,
            r"LOCATION\s*'[^']*?/test/breast_cancer'",
            msg="LOCATION must point at the /test/breast_cancer HDFS path",
        )

    def test_no_load_data_statement(self):
        # Strip comments first so a "don't run LOAD DATA" documentation note
        # doesn't trip the assertion.
        executable = _strip_sql_comments(SQL_TEXT)
        self.assertNotRegex(
            executable,
            r"\bLOAD\s+DATA\b",
            msg="LOAD DATA must be removed; the table reads straight from HDFS",
        )

    def test_preserves_csv_delimiter_and_header_skip(self):
        self.assertIn("FIELDS TERMINATED BY ','", SQL_TEXT)
        self.assertIn("'skip.header.line.count'='1'", SQL_TEXT)

    def test_preserves_all_thirty_one_columns(self):
        # Each expected column must appear in the CREATE TABLE block, in order.
        create = _create_table_block(SQL_TEXT)
        positions = [create.find(f"`{c}`") for c in EXPECTED_COLUMNS]
        self.assertTrue(
            all(p >= 0 for p in positions),
            msg=f"missing columns: {[c for c, p in zip(EXPECTED_COLUMNS, positions) if p < 0]}",
        )
        self.assertEqual(
            positions,
            sorted(positions),
            msg="columns must appear in the original order",
        )
        self.assertEqual(positions.count(-1), 0)

    def test_warns_about_hdfs_file_on_drop(self):
        # The DROP TABLE comment should warn users that dropping the table
        # does NOT delete the underlying HDFS file (it's no longer managed).
        drop_match = re.search(r"DROP\s+TABLE[^;]*", SQL_TEXT, flags=re.IGNORECASE)
        self.assertTrue(drop_match, "DROP TABLE statement missing")
        comment = drop_match.group(0).lower()
        self.assertRegex(
            comment,
            r"hdfs|不会删除|不删除",
            msg="DROP TABLE should warn that the underlying HDFS file is kept",
        )


if __name__ == "__main__":
    unittest.main()