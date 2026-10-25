"""Core tests for haus — stdlib only: python3 -m unittest discover -s tests"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import haus  # noqa: E402


def fake_entity(name, domain="light", state="off", activations=0, last=None):
    return {
        "name": name, "domain": domain, "state": state,
        "name_tokens": sorted(set(haus.tokens(name))),
        "tokens": sorted(set(haus.tokens(name + " " + domain))),
        "activations": activations, "changes": 0, "last": last,
    }


class TestText(unittest.TestCase):
    def test_norm_strips_diacritics(self):
        self.assertEqual(haus.norm("Svjetlo ČŽŠ Đorđe"), "svjetlo czs dorde")

    def test_tokens(self):
        self.assertEqual(haus.tokens("Svjetlo radna soba 2"), ["svjetlo", "radna", "soba", "2"])

    def test_tidy_doubled_name(self):
        self.assertEqual(haus.tidy_name("Blind X Blind X", "cover.blind_x"), "Blind X")
        self.assertEqual(haus.tidy_name("Kitchen light", "light.kitchen"), "Kitchen light")


class TestDedupe(unittest.TestCase):
    def test_prefers_light_over_switch(self):
        ents = {
            "switch.kitchen_light": fake_entity("Kitchen light", "switch", activations=10),
            "light.kitchen_light": fake_entity("Kitchen light", "light", activations=7),
            "cover.kitchen_blind": fake_entity("Kitchen blind", "cover"),
        }
        keep, dropped = haus.dedupe(ents)
        self.assertIn("light.kitchen_light", keep)
        self.assertEqual(dropped["switch.kitchen_light"], "light.kitchen_light")
        self.assertIn("cover.kitchen_blind", keep)
        self.assertEqual(len(keep), 2)

    def test_fold_usage_takes_max_not_sum(self):
        ents = {
            "switch.a": fake_entity("A", "switch", activations=250, last="2026-10-01T00:00:00+00:00"),
            "light.a": fake_entity("A", "light", activations=250, last="2026-10-02T00:00:00+00:00"),
        }
        keep, dropped = haus.dedupe(ents)
        haus.fold_usage(ents, keep, dropped)
        self.assertEqual(keep["light.a"]["activations"], 250)
        self.assertEqual(keep["light.a"]["last"], "2026-10-02T00:00:00+00:00")


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.cache = {
            "entities": {
                "light.desk_lamp": fake_entity("Desk lamp", activations=40),
                "light.living_room": fake_entity("Living room light", activations=5),
                "light.living_room_2": fake_entity("Living room light 2", activations=120),
            },
            "groups": {"light": ["light.desk_lamp", "light.living_room", "light.living_room_2"]},
        }

    def test_token_match(self):
        hits = haus.resolve(self.cache, "desk")
        self.assertEqual(hits[0][1], "light.desk_lamp")

    def test_usage_boost(self):
        hits = haus.resolve(self.cache, "living room")
        self.assertEqual(hits[0][1], "light.living_room_2")

    def test_unavailable_sinks(self):
        # an unavailable, heavily-used device must rank below an available one
        self.cache["entities"]["light.desk_lamp"]["state"] = "unavailable"
        self.cache["entities"]["light.desk_lamp_old"] = fake_entity("Desk lamp old", activations=0)
        hits = haus.resolve(self.cache, "desk")
        self.assertEqual(hits[0][1], "light.desk_lamp_old")
        self.assertEqual(hits[-1][1], "light.desk_lamp")

    def test_number_disambiguates(self):
        hits = haus.resolve(self.cache, "living room 2")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], "light.living_room_2")


class TestGroups(unittest.TestCase):
    def setUp(self):
        self.cache = {
            "entities": {
                "light.a": fake_entity("A", "light"),
                "light.b": fake_entity("B", "light", state="unavailable"),
                "cover.c": fake_entity("C", "cover"),
            },
            "groups": {"light": ["light.a", "light.b"], "cover": ["cover.c"]},
        }

    def test_english_group(self):
        ids = haus.group_targets(self.cache, "lights", "on", haus.LANG["en"])
        self.assertEqual(ids, ["light.a"])

    def test_croatian_group_with_filler(self):
        ids = haus.group_targets(self.cache, "sva svjetla", "on", haus.LANG["hr"])
        self.assertEqual(ids, ["light.a"])

    def test_all(self):
        ids = haus.group_targets(self.cache, "all", "open", haus.LANG["en"])
        self.assertEqual(ids, ["cover.c"])

    def test_unsupported_action(self):
        with self.assertRaises(SystemExit):
            haus.group_targets(self.cache, "covers", "on", haus.LANG["en"])


class TestApiPaths(unittest.TestCase):
    def test_logbook_start_is_in_the_path(self):
        api = haus.Api({"url": "http://example.invalid:8123", "token": "t"})
        captured = {}

        def fake_request(path, method="GET", payload=None, timeout=60):
            captured["path"] = path
            return [], 0

        api.request = fake_request
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        api.logbook(start, start + timedelta(days=3))
        self.assertTrue(captured["path"].startswith("/api/logbook/2026-10-01T00%3A00%3A00%2B00%3A00?end="),
                        captured["path"])


if __name__ == "__main__":
    unittest.main()
