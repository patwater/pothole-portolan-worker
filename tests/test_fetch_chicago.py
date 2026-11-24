"""The fetch guard on Chicago's portal. Run: python3 -I -m unittest discover -s tests -p "test_*.py"

Chicago's 311 data is record-level, like San Francisco's, so it gets the same treatment: metadata and
grouped counts of at least MIN_GROUP_SIZE only, and never a column that locates or identifies a request.
All URLs are synthetic; nothing is fetched.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_source as f  # noqa: E402

BASE = "https://data.cityofchicago.org"
SR = BASE + "/resource/v6vf-nfxy.json?"
GROUPED = ("$select=sr_type,date_extract_y(created_date)%20as%20year,count(*)%20as%20requests"
           "&$group=sr_type,year&$having=count(*)%3E%3D10&$limit=5000")


def both(url):
    return f.aggregate_violation(url) or f.query_policy(url)


class ChicagoGuardTests(unittest.TestCase):
    def test_the_portal_is_aggregate_only(self):
        self.assertIn("data.cityofchicago.org", f.AGGREGATE_ONLY_HOSTS)

    def test_a_grouped_count_with_the_minimum_group_size_passes(self):
        self.assertIsNone(both(SR + GROUPED))

    def test_a_row_download_is_refused(self):
        self.assertIsNotNone(both(SR + "$limit=1000"))
        self.assertIsNotNone(both(BASE + "/resource/v6vf-nfxy.csv"))

    def test_small_groups_are_refused(self):
        self.assertIsNotNone(both(SR + GROUPED.replace("%3E%3D10", "%3E%3D2")))

    def test_location_and_identifier_columns_are_refused(self):
        for field in ("street_address", "latitude", "longitude", "x_coordinate", "location", "sr_number"):
            with self.subTest(field):
                url = SR + f"$select={field},count(*)&$group={field}&$having=count(*)%3E%3D10&$limit=100"
                self.assertIsNotNone(both(url))

    def test_an_unregistered_chicago_dataset_is_refused(self):
        self.assertIsNotNone(both(BASE + "/resource/abcd-1234.json?" + GROUPED))

    def test_metadata_is_allowed(self):
        self.assertIsNone(f.aggregate_violation(BASE + "/api/views/v6vf-nfxy.json"))

    def test_the_historical_pothole_dataset_is_aggregate_only_too(self):
        self.assertTrue(f.DATASET_POLICY["7as2-ds3y"]["aggregate_only"])
        self.assertIsNotNone(both(BASE + "/resource/7as2-ds3y.json?$limit=100"))


if __name__ == "__main__":
    unittest.main()
