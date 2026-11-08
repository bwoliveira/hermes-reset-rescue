"""D-025: the masking example is neutral everywhere it is written.

``b***@example.com`` reads like a real address someone's own account produced.
The example is now ``example@example.com -> e***@example.com`` in the
``config_schema`` description, the README and the popup's Settings tab, and no
real-looking sample survives in the repo's user-facing text.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = "example@example.com -> e***@e***.com"

#: ``b***@gmail.com`` and friends: a masked sample that names a real provider
#: and a single-letter local part, which is what a real account looks like.
REAL_LOOKING = re.compile(r"\b[a-z]\*\*\*@(?!(?:example|test)\.)[a-z0-9.-]+\.[a-z]{2,}\b")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class MaskExample(unittest.TestCase):
    def test_the_schema_description_carries_the_neutral_example(self):
        manifest = yaml.safe_load(read(REPO / "plugin.yaml"))
        description = manifest["config_schema"]["mask_labels"]["description"]
        self.assertIn(EXAMPLE, " ".join(description.split()))

    def test_the_readme_carries_the_neutral_example(self):
        text = read(REPO / "README.md")
        self.assertIn(EXAMPLE, text)

    def test_no_real_looking_masked_sample_survives_in_user_facing_text(self):
        for name in ("plugin.yaml", "README.md"):
            text = read(REPO / name)
            found = REAL_LOOKING.findall(text)
            self.assertEqual(found, [], f"{name} still shows {found}")

    def test_the_engine_docstring_example_is_neutral(self):
        text = read(REPO / "reset_rescue" / "accounts.py")
        self.assertNotIn("bob@example.com", text)
        self.assertIn("example@example.com", text)


if __name__ == "__main__":
    unittest.main()
