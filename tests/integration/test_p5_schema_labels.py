"""P5: the settings form's labels are not localizable by a plugin — proven, not assumed.

The maintainer asked for pt-BR labels in Desktop → Capabilities → Plugins →
Reset Rescue when Hermes is in Portuguese. Hermes does **not** support that, and
this module is the evidence, so the answer is "keep English" rather than a
guess:

* ``hermes_cli/plugins_settings.py`` builds each field's ``label`` from the
  manifest verbatim (``str(spec.get("label") or spec.get("title") or key)``) —
  there is no locale parameter, no lookup and no translation hook;
* ``hermes_cli/plugins_manifest.py`` validates ``config_schema`` entries for
  type/choices and never for a localized label (``_config_schema_entries``
  copies the mapping through);
* the Desktop form renders ``fieldLabel(field)`` — the manifest label, or a
  humanized key — and ``field.description`` raw; neither goes through
  ``useI18n`` (``apps/desktop/src/app/settings/plugin-settings-form.tsx``);
* ``agent/i18n_layers.py`` (the plugin language-pack layer) has no
  ``config_schema`` hook at all.

If a future Hermes adds one of these, the corresponding test fails and the
labels can be localized then. Until then the manifest stays English, which is
also what every other plugin's form shows in a pt-BR Desktop.
"""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path

HERMES = Path("/usr/local/lib/hermes-agent")

#: The three surfaces a localized label would have to travel through.
SETTINGS_MODULE = HERMES / "hermes_cli" / "plugins_settings.py"
MANIFEST_MODULE = HERMES / "hermes_cli" / "plugins_manifest.py"
DESKTOP_FORM = HERMES / "apps" / "desktop" / "src" / "app" / "settings" / "plugin-settings-form.tsx"
I18N_LAYERS = HERMES / "agent" / "i18n_layers.py"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


class NoLocalizedSchemaLabels(unittest.TestCase):
    """The finding, pinned against the installed Hermes."""

    def setUp(self) -> None:
        if not SETTINGS_MODULE.is_file():
            self.skipTest("no installed Hermes to inspect")

    def test_the_field_label_is_the_manifest_string_verbatim(self):
        from hermes_cli import plugins_settings

        source = inspect.getsource(plugins_settings.plugin_settings_fields)
        self.assertIn('str(spec.get("label") or spec.get("title") or key)', source)
        # A locale-aware builder would need a locale argument; there is none.
        self.assertNotIn(
            "locale", inspect.signature(plugins_settings.plugin_settings_fields).parameters
        )
        self.assertNotIn(
            "language", inspect.signature(plugins_settings.plugin_settings_fields).parameters
        )

    def test_the_manifest_parser_has_no_label_translation(self):
        source = read(MANIFEST_MODULE)
        self.assertIn("def _config_schema_entries", source)
        # It copies the entry through; nothing looks a label up.
        self.assertNotIn("label_by_locale", source)
        self.assertNotIn("config_schema_locale", source)

    def test_the_desktop_form_does_not_translate_the_label_or_description(self):
        source = read(DESKTOP_FORM)
        self.assertIn("export function fieldLabel", source)
        # The label is the manifest string (or a humanized key), never t(...).
        self.assertIn("const label = field.label.trim()", source)
        self.assertNotRegex(source, r"t\([^)]*field\.label")
        self.assertNotRegex(source, r"t\([^)]*field\.description")
        self.assertIn("joinSentences(field.description", source)

    def test_the_plugin_language_pack_layer_has_no_config_schema_hook(self):
        source = read(I18N_LAYERS)
        if not source:
            self.skipTest("no i18n layer module to inspect")
        self.assertNotIn("config_schema", source)
        self.assertNotIn("plugin_settings", source)

    def test_our_manifest_therefore_stays_english(self):
        """And it does: labels are the English table, not a pt-BR variant."""
        import yaml

        repo = Path(__file__).resolve().parents[2]
        manifest = yaml.safe_load((repo / "plugin.yaml").read_text(encoding="utf-8"))
        for key, spec in manifest["config_schema"].items():
            label = str(spec.get("label") or "")
            self.assertTrue(label.strip(), key)
            self.assertNotIn("ã", label.lower(), f"{key} label looks localized")
            self.assertNotIn("ç", label.lower(), f"{key} label looks localized")
            self.assertNotIn("Rótulo", label, key)
