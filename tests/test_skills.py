import tempfile
import unittest
from pathlib import Path

from app.services.skills import MindBridgeSkillRegistry, SkillLoadError


def write_skill(root: Path, name: str, text: str) -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


class SkillRegistryTests(unittest.TestCase):
    def test_skill_registry_loads_valid_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(
                root,
                "demo_skill",
                """---\nname: demo_skill\ndescription: Use for a clear and sufficiently described demo scenario.\nversion: 1.2.0\nenabled: true\npriority: 20\nallowed_tools: EXCEL_REPORT, CASE_CREATE\n---\n\n# Demo\n\n## Workflow\n\n- Do one thing.\n""",
            )

            skill = MindBridgeSkillRegistry(root).get_required("demo_skill")

            self.assertEqual(skill.name, "demo_skill")
            self.assertEqual(skill.version, "1.2.0")
            self.assertTrue(skill.enabled)
            self.assertEqual(skill.priority, 20)
            self.assertEqual(skill.allowed_tools, ("EXCEL_REPORT", "CASE_CREATE"))
            self.assertEqual(skill.validation_issues(), [])

    def test_disabled_skill_is_visible_in_status_but_cannot_be_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(
                root,
                "demo_skill",
                """---\nname: demo_skill\ndescription: Use for a disabled but otherwise valid demo scenario.\nversion: 1.0.0\nenabled: false\npriority: 100\n---\n\n# Demo\n\n## Workflow\n\n- Do one thing.\n""",
            )

            registry = MindBridgeSkillRegistry(root)

            self.assertEqual(registry.list_skills(), [])
            self.assertEqual(registry.status_items()[0]["status"], "DISABLED")
            with self.assertRaises(SkillLoadError):
                registry.get_required("demo_skill")

    def test_skill_loads_and_validates_output_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(
                root,
                "demo_skill",
                """---\nname: demo_skill\ndescription: Use for validating a structured output contract in tests.\n---\n\n# Demo\n\n## Workflow\n\n- Answer safely.\n""",
            )
            (root / "demo_skill" / "output_schema.json").write_text(
                '{"minLength": 5, "maxQuestions": 1, "requiredTerms": ["安全"], "forbiddenTerms": ["诊断"]}',
                encoding="utf-8",
            )

            skill = MindBridgeSkillRegistry(root).get_required("demo_skill")

            self.assertEqual(skill.validate_output("请先保证安全。"), [])
            issues = skill.validate_output("诊断？为什么？")
            self.assertTrue(any("缺少必需内容" in issue for issue in issues))
            self.assertTrue(any("包含禁止内容" in issue for issue in issues))
            self.assertTrue(any("问题数量超过" in issue for issue in issues))

    def test_skill_status_reports_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(
                root,
                "demo_skill",
                """---\nname: demo_skill\ndescription: short\n---\n\n# Demo\n""",
            )

            status = MindBridgeSkillRegistry(root).status_items()[0]

            self.assertEqual(status["status"], "WARN")
            self.assertTrue(status["issues"])

    def test_skill_requires_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "bad", "# Missing metadata")

            with self.assertRaises(SkillLoadError):
                MindBridgeSkillRegistry(root).get_required("bad")


if __name__ == "__main__":
    unittest.main()
