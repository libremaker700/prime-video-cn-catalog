import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from catalog_exclusions import filter_items, require_filtered


SEASON_ONE = "amzn1.dv.gti.072d4b46-634b-4c99-ab1e-d227f785dd92"
COMPACT_ONE = "0FKWLNTAYMFTSZ7TRZSPLG84NM"
REGISTRY = {SEASON_ONE: ("tv", 1, 2024)}


class ReviewedExclusionTests(unittest.TestCase):
    def test_removes_only_the_confirmed_season_and_its_inherited_poster(self):
        portrait = {"path": "images/one.jpg"}
        replacement = {"path": "images/two.jpg"}
        first = {"season": 1, "year": 2024, "official_compact_id": COMPACT_ONE, "poster": portrait}
        second = {"season": 2, "year": 2026, "poster": replacement}
        series = {"id": "series", "kind": "tv", "seasons": [first, second], "poster": portrait}
        matching_title = {"id": "other", "kind": "tv", "seasons": [{"season": 1, "year": 2024}]}
        self.assertEqual(filter_items([series, matching_title], REGISTRY),
                         [{**series, "seasons": [second], "poster": replacement}, matching_title])
        self.assertEqual(series["seasons"], [first, second])
        with self.assertRaisesRegex(ValueError, "contains reviewed exclusions"):
            require_filtered({"items": [series]}, REGISTRY)

    def test_empty_series_is_removed_and_identity_conflicts_block_publication(self):
        first = {"official_catalog_id": SEASON_ONE, "season": 1, "year": 2024}
        self.assertEqual(filter_items([{"kind": "tv", "seasons": [first]}], REGISTRY), [])
        with self.assertRaisesRegex(ValueError, "Year conflict"):
            filter_items([{"kind": "tv", "seasons": [{**first, "year": 2022}]}], REGISTRY)
