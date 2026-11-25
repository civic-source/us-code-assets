"""Strict JSON and cross-language generation identity controls."""
import json
from pathlib import Path
import unittest
from publisher import protocol as p


class ProtocolControls(unittest.TestCase):
    def test_rejects_duplicate_fields_noninteger_numbers_and_unknown_keys(self):
        for value in [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1.0}']:
            with self.assertRaises(ValueError):
                p.strict_json(value)
        with self.assertRaises(ValueError):
            p.exact({'known': 1, 'extra': 2}, {'known'})

    def test_refuses_unsafe_noncanonical_member_paths(self):
        for path in ['', '.', '..', '../a', '/a', 'a//b', 'a/./b', 'a\\b', 'a\nb', 'a/%2e%2e/b', 'ü']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                p.safe_path(path)

    def test_type_and_whole_project_bounds_do_not_accept_boolean_counts(self):
        with self.assertRaises(ValueError):
            p.file_table([{'path': 'a', 'byteLength': True, 'sha256': 'a' * 64}])
        with self.assertRaises(ValueError):
            p.file_table([{'path': 'a', 'byteLength': 900000000, 'sha256': 'a' * 64},
                          {'path': 'b', 'byteLength': 1, 'sha256': 'b' * 64}])

    def test_matches_actual_typescript_generation_golden(self):
        fixture = json.loads((Path(__file__).parent / 'data/generation-v1.json').read_text())
        rows = {row['path']: row for row in fixture['payload']}
        self.assertEqual(p.generation_id(rows), fixture['generationId'])
