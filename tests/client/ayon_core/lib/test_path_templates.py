import os
import unittest
from importlib import util
from pathlib import Path


_MODULE_PATH = (
    Path(__file__).resolve().parents[4]
    / "client"
    / "ayon_core"
    / "lib"
    / "path_templates.py"
)
_SPEC = util.spec_from_file_location("path_templates", _MODULE_PATH)
_MODULE = util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)

StringTemplate = _MODULE.StringTemplate
DefaultKeysDict = _MODULE.DefaultKeysDict
TemplateUnsolved = _MODULE.TemplateUnsolved


class TestStringTemplateOptionalAlternatives(unittest.TestCase):
    def test_format_uses_first_matching_branch(self):
        template = StringTemplate("asset/<{root[publish]}|{root[work]}>")

        result = template.format({
            "root": {
                "publish": "publish",
                "work": "work",
            }
        })

        self.assertEqual(str(result), "asset/publish")
        self.assertTrue(result.solved)
        self.assertEqual(result.missing_keys, [])
        self.assertEqual(result.invalid_types, {})

    def test_format_falls_back_without_leaking_missing_keys(self):
        template = StringTemplate("asset/<{root[publish]}|{root[work]}>")

        result = template.format({"root": {"work": "work"}})

        self.assertEqual(str(result), "asset/work")
        self.assertTrue(result.solved)
        self.assertEqual(result.missing_keys, [])
        self.assertEqual(result.invalid_types, {})

    def test_format_falls_back_without_leaking_invalid_types(self):
        template = StringTemplate("asset/<{root[publish]}|{root[work]}>")

        result = template.format({
            "root": {
                "publish": {"name": "publish"},
                "work": "work",
            }
        })

        self.assertEqual(str(result), "asset/work")
        self.assertTrue(result.solved)
        self.assertEqual(result.missing_keys, [])
        self.assertEqual(result.invalid_types, {})

    def test_format_silently_removes_optional_when_no_branch_matches(self):
        template = StringTemplate("asset/<{root[publish]}|{root[work]}>")

        result = template.format({"root": {}})

        self.assertEqual(str(result), "asset/")
        self.assertTrue(result.solved)
        self.assertEqual(result.missing_keys, [])
        self.assertEqual(result.invalid_types, {})

    def test_format_keeps_branch_with_missing_nested_optional(self):
        template = StringTemplate(
            "asset/<{root[publish]}<-{variant}>|{root[work]}>"
        )

        result = template.format({"root": {"publish": "publish"}})

        self.assertEqual(str(result), "asset/publish")
        self.assertTrue(result.solved)
        self.assertEqual(result.missing_keys, [])
        self.assertEqual(result.invalid_types, {})

    def test_remove_optional_parts_uses_fallback_branch(self):
        template = StringTemplate(
            "asset/<{root[publish]}<-{variant}>|{root[work]}>"
        )

        self.assertEqual(
            template.remove_optional_parts_for_data({
                "root": {
                    "publish": "publish",
                },
            }),
            "asset/{root[publish]}",
        )

        self.assertEqual(
            template.remove_optional_parts_for_data({
                "root": {},
            }),
            "asset/",
        )

    def test_remove_optional_parts_uses_required_fallback_key(self):
        template = StringTemplate("asset/<{root[publish]|root[work]}>")

        self.assertEqual(
            template.remove_optional_parts_for_data({
                "root": {
                    "work": "work",
                },
            }),
            "asset/{root[work]}",
        )


class TestStringTemplateRequiredAlternatives(unittest.TestCase):
    def test_required_alternative_uses_first_matching_branch(self):
        template = StringTemplate("asset/{root[publish]|root[work]}")

        result = template.format({
            "root": {
                "publish": "publish",
                "work": "work",
            },
        })

        self.assertEqual(str(result), "asset/publish")
        self.assertTrue(result.solved)
        self.assertEqual(result.missing_keys, [])
        self.assertEqual(result.invalid_types, {})

    def test_required_alternative_falls_back_to_second_branch(self):
        template = StringTemplate("asset/{root[publish]|root[work]}")

        result = template.format({
            "root": {
                "work": "work",
            },
        })

        self.assertEqual(str(result), "asset/work")
        self.assertTrue(result.solved)
        self.assertEqual(result.missing_keys, [])
        self.assertEqual(result.invalid_types, {})

    def test_required_alternative_fails_when_no_branch_matches(self):
        template = StringTemplate("asset/{root[publish]|root[work]}")

        result = template.format({"root": {}})

        self.assertEqual(str(result), "asset/{root[publish]|root[work]}")
        self.assertFalse(result.solved)
        self.assertTrue(len(result.missing_keys) > 0)

    def test_required_alternative_strict_raises_when_no_branch_matches(self):
        template = StringTemplate("asset/{root[publish]|root[work]}")

        with self.assertRaises(TemplateUnsolved):
            template.format_strict({"root": {}})


class TestStringTemplateRegression(unittest.TestCase):
    def test_format_keeps_existing_plain_formatting(self):
        template = StringTemplate("asset/{project[name]}/{version:03}")

        result = template.format({
            "project": {"name": "demo"},
            "version": 7,
        })

        self.assertEqual(str(result), "asset/demo/007")
        self.assertTrue(result.solved)
        self.assertEqual(result.missing_keys, [])
        self.assertEqual(result.invalid_types, {})

    def test_format_supports_list_indexes(self):
        template = StringTemplate("asset/{items[1][name]}")

        result = template.format({
            "items": [
                {"name": "first"},
                {"name": "second"},
            ]
        })

        self.assertEqual(str(result), "asset/second")
        self.assertTrue(result.solved)
        self.assertEqual(result.missing_keys, [])
        self.assertEqual(result.invalid_types, {})

    def test_format_supports_default_keys_dict(self):
        template = StringTemplate("asset/{folder}")

        result = template.format({
            "folder": DefaultKeysDict("name", {"name": "FolderName"})
        })

        self.assertEqual(str(result), "asset/FolderName")
        self.assertTrue(result.solved)
        self.assertEqual(result.missing_keys, [])
        self.assertEqual(result.invalid_types, {})

    def test_format_strict_raises_on_missing_key(self):
        template = StringTemplate("asset/{project[name]}")

        with self.assertRaises(TemplateUnsolved):
            template.format_strict({})

    def test_normalized_returns_clean_path(self):
        template = StringTemplate(r"asset\folder\..\shot")

        result = template.format({})
        normalized = result.normalized()

        self.assertEqual(str(normalized), os.path.normpath("asset/folder/../shot"))
        self.assertTrue(normalized.solved)

    def test_remove_optional_parts_without_alternatives(self):
        template = StringTemplate("asset/<{folder}>/shot")

        self.assertEqual(
            template.remove_optional_parts_for_data({}),
            "asset//shot",
        )

        self.assertEqual(
            template.remove_optional_parts_for_data({"folder": "folder"}),
            "asset/{folder}/shot",
        )



if __name__ == "__main__":
    unittest.main()

