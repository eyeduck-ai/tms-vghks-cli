import os
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def requirement_names(requirements):
    names = []
    for requirement in requirements:
        name = requirement.split(";", 1)[0].strip()
        for separator in ("[", "<", ">", "=", "~", "!", " "):
            name = name.split(separator, 1)[0]
        names.append(name.lower())
    return names


class DependencyConfigTests(unittest.TestCase):
    def load_pyproject(self):
        return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def read_readme(self):
        return (ROOT / "README.md").read_text(encoding="utf-8")

    def read_cli_docs(self):
        return (ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")

    def test_playwright_is_optional_not_base_dependency(self):
        data = self.load_pyproject()

        base_dependencies = requirement_names(data["project"]["dependencies"])
        self.assertIn("requests", base_dependencies)
        self.assertNotIn("playwright", base_dependencies)

        optional_dependencies = data["project"]["optional-dependencies"]
        self.assertIn(
            "playwright",
            requirement_names(optional_dependencies["playwright"]),
        )
        self.assertIn("playwright", requirement_names(optional_dependencies["full"]))

    def test_uv_default_install_is_requests_only(self):
        data = self.load_pyproject()

        self.assertEqual(data["tool"]["uv"]["default-groups"], [])
        self.assertIn("dev", data["dependency-groups"])

    def test_console_scripts_include_only_formal_command(self):
        project = self.load_pyproject()["project"]
        scripts = project["scripts"]

        self.assertEqual(project["name"], "tms-vghks-cli")
        self.assertEqual(scripts["tms-vghks-cli"], "tms_vghks.cli:main")
        self.assertNotIn("tms-vghks", scripts)

    def test_docs_use_formal_cli_command_and_json_default(self):
        docs = self.read_readme() + "\n" + self.read_cli_docs()

        self.assertIn("uv run tms-vghks-cli", docs)
        self.assertNotIn("uv run tms-vghks ", docs)
        self.assertNotIn("--json", docs)
        self.assertNotIn("--text", docs)
        self.assertNotIn("--non-interactive", docs)

    def test_daily_docs_use_short_commands_without_accounts_flag(self):
        readme_quick_start = self.read_readme().split("## Quick Start", 1)[1].split("```", 2)[1]
        cli_daily = self.read_cli_docs().split("## Daily Commands", 1)[1].split("```", 2)[1]
        daily_docs = readme_quick_start + "\n" + cli_daily

        for command in (
            "uv run tms-vghks-cli sign-in",
            "uv run tms-vghks-cli pending",
            "uv run tms-vghks-cli completed",
            "uv run tms-vghks-cli course <course-id-or-url>",
            "uv run tms-vghks-cli go --quiz auto",
        ):
            self.assertIn(command, daily_docs)
        self.assertNotIn("--accounts", daily_docs)

    def test_top_level_import_does_not_require_optional_backend_packages(self):
        script = """
import builtins

blocked_roots = {"playwright", "paddleocr", "paddle"}
real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name.split(".", 1)[0] in blocked_roots:
        raise AssertionError(f"optional package imported at top level: {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import

import tms_vghks

session = tms_vghks.TmsSession()
assert session.backend == tms_vghks.OperationBackend.REQUESTS
assert type(session.requests_tools()).__name__ == "RequestsBackendTools"
assert callable(tms_vghks.compare_backend_read_paths)
print("ok")
"""
        env = dict(os.environ)
        src_path = str(ROOT / "src")
        env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("ok", result.stdout)

    def test_readme_documents_requests_default_without_playwright_fallback(self):
        readme = self.read_readme()

        self.assertIn("`pending`, `completed`, `course`, and `go` use the requests backend by", readme)
        self.assertIn("requests-default commands continue through requests login", readme)
        self.assertNotIn("default `auto` uses a saved `.tms_session` bundle when valid, then falls back to interactive Playwright login", readme)
