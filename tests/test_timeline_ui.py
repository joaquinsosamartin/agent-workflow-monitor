"""Regression checks for the functional workflow vs. technical audit UI."""
import unittest
from pathlib import Path


class TimelineUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui=(Path(__file__).resolve().parents[1]/"monitor"/"static"/"index.html").read_text(encoding="utf-8")

    def test_tools_are_collapsible_and_distinct_from_phases(self):
        self.assertIn("function isToolStep(node)",self.ui)
        self.assertIn("actionOpened.has(node.id)",self.ui)
        self.assertIn('Mostrar herramientas (',self.ui)
        self.assertIn("const phaseChildren=children.filter(n=>!isToolStep(n))",self.ui)

    def test_event_audit_is_collapsed_and_filterable(self):
        self.assertIn("Registro técnico · auditoría de eventos",self.ui)
        self.assertIn('audit.open=historyExpanded',self.ui)
        self.assertIn('["node.status","Cambios de pasos"]',self.ui)
        self.assertIn('["usage","Consumo de tokens"]',self.ui)

    def test_generic_cli_root_not_counted_as_completed_phase(self):
        self.assertIn('["Codex CLI session","Claude Code session"]',self.ui)
        self.assertIn("Turno finalizado · sesión abierta",self.ui)

    def test_readme_references_real_screenshots(self):
        root=Path(__file__).resolve().parents[1]
        readme=(root/"README.md").read_text(encoding="utf-8")
        for name in ("dashboard","claude-mod"):
            filename=name+"-real.webp"
            image=root/"docs"/"screenshots"/filename
            self.assertTrue(image.is_file(),image)
            self.assertIn("docs/screenshots/"+filename,readme)
            with image.open("rb") as f:
                self.assertEqual(f.read(4),b"RIFF")
                f.seek(8)
                self.assertEqual(f.read(4),b"WEBP")


if __name__=="__main__":
    unittest.main()
