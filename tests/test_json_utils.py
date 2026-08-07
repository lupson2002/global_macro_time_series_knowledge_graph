import unittest

from src.json_utils import parse_json_list


class ParseJsonListTests(unittest.TestCase):
    def test_parses_only_json_arrays(self):
        self.assertEqual(parse_json_list('["a", 2]'), ["a", 2])
        for raw in (None, "", "not-json", '{"a": 1}', 7):
            with self.subTest(raw=raw):
                self.assertEqual(parse_json_list(raw), [])

    def test_native_list_contract_is_explicit(self):
        value = ["already", "parsed"]
        self.assertIs(parse_json_list(value), value)
        self.assertEqual(parse_json_list(value, accept_native=False), [])


if __name__ == "__main__":
    unittest.main()
