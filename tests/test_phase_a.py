from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from skillzip import BundleCompressionError, compress_bundle, compress_oneshot
from skillzip.bundle import resolve_bundle
from skillzip.capsules import plan_capsules
from skillzip.contract import Contract, Unit
from skillzip.environment import (EnvironmentContract, EnvironmentGuarantee,
                                  apply_environment_contract)
from skillzip.scanner import file_references, reference_occurrences
from skillzip.skill import Skill


ROOT = Path(__file__).resolve().parents[1]


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def sample_bundle(root: Path) -> Path:
    write(root / "SKILL.md", """---
name: phase-a-demo
description: Process CSV or JSON records and delegate specialized recovery.
---

# Phase A demo

Always keep an audit record for every completed operation.

- When processing CSV, read [CSV workflow](references/csv.md).
- When processing JSON, read [JSON workflow](references/json.md).
- When troubleshooting, read [Recovery guide](references/recovery.md) only when needed.
- For specialist recovery, read [subskill](sub/SKILL.md).
- Run `scripts/check.py` to validate the produced file.
""")
    shared = (
        "Always preserve the original identifier, timestamp, provenance label, "
        "source checksum, source URI, immutable record version, tenant identifier, "
        "schema revision, ingestion timestamp, collection identifier, access label, "
        "retention class, regional boundary, consent marker, parent record identifier, "
        "transformation history, validation digest, producer version, and lineage chain "
        "in every transformed record."
    )
    write(root / "references/csv.md", f"""# CSV workflow

## Rules

{shared}

Always validate each record against the declared schema.

## Workflow

1. Read the CSV header and required columns.
2. Validate every row against the declared schema.
3. Return the normalized records and validation summary.
""")
    write(root / "references/json.md", f"""# JSON workflow

## Rules

{shared}

Always validate each record against the declared schema.

## Workflow

1. Read the JSON object and required fields.
2. Validate every object against the declared schema.
3. Return the normalized records and validation summary.
""")
    long_a = " ".join(["Always collect the validation code and preserve the failing field name."] * 18)
    long_b = " ".join(["Always record the source URI and return a recoverable diagnostic."] * 18)
    write(root / "references/recovery.md", f"""# Recovery guide

This guide contains mutually exclusive recovery branches.

## When validation fails

{long_a}

## When the source is unavailable

{long_b}
""")
    write(root / "sub/SKILL.md", """---
name: recovery-specialist
description: Handle recovery cases delegated by the root skill.
---

# Recovery specialist

Always return the recovery status and the source identifier.
""")
    write(root / "scripts/check.py", "print('validated')\n")
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "assets/blob.bin").write_bytes(b"\x00\x01\x02")
    return root


class ScannerTests(unittest.TestCase):
    def test_structured_references_keep_conditions_and_ignore_code(self):
        text = """When validation fails, read [guide](references/fail.md#retry).

```md
[example](not-a-dependency.md)
```
Run scripts/check.py only when needed.
"""
        refs = reference_occurrences(text)
        self.assertEqual([r.target for r in refs],
                         ["references/fail.md", "scripts/check.py"])
        self.assertEqual(refs[0].fragment, "retry")
        self.assertEqual(refs[0].condition, "validation fails")
        self.assertEqual(file_references(text),
                         ["references/fail.md#retry", "scripts/check.py"])


class ResolverTests(unittest.TestCase):
    def test_recursive_resolution_cycles_and_unreferenced_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "SKILL.md", "Read [a](refs/a.md).\n")
            write(root / "refs/a.md", "Read [b](b.md).\n")
            write(root / "refs/b.md", "Read [a](a.md).\n")
            write(root / "assets/unreferenced.txt", "keep me\n")
            graph = resolve_bundle(str(root))
            self.assertTrue(graph.nodes["refs/a.md"].reachable)
            self.assertTrue(graph.nodes["refs/b.md"].reachable)
            self.assertFalse(graph.nodes["assets/unreferenced.txt"].reachable)
            self.assertEqual(len(graph.cycles), 1)

    def test_path_escape_is_reported_and_never_read_or_copied(self):
        """An out-of-root reference is preserved verbatim, never followed.

        Real skill bundles legitimately point at package-level files outside their
        own directory, so refusing the whole bundle is not an option.  The security
        requirement is that the compressor never reads or copies the outside file and
        never claims it as bundle content.
        """
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            root = outer / "bundle"
            write(outer / "outside.md", "SECRET-OUTSIDE-CONTENT\n")
            write(root / "SKILL.md",
                  "Read [outside](../outside.md).\nAlways return a result.\n")
            graph = resolve_bundle(str(root))
            self.assertTrue(any("path_escape" in item for item in graph.errors))
            # the escaping target is never admitted as a bundle node
            self.assertNotIn("../outside.md", graph.nodes)

            output = outer / "output"
            _, report = compress_bundle(
                str(root), str(output), cli=None,
                cfg={"extract_llm": False, "audit_llm": False})
            self.assertTrue(report["audit"]["ok"])
            # reported, but attributed to the source rather than to compression
            self.assertEqual(report["audit"]["dangling_or_unsafe_references"], [])
            self.assertTrue(report["audit"]["preexisting_source_reference_defects"])
            # no content from outside the bundle root leaked into the output
            emitted = "\n".join(p.read_text(encoding="utf-8")
                                for p in output.rglob("*.md"))
            self.assertNotIn("SECRET-OUTSIDE-CONTENT", emitted)
            self.assertFalse((output / "outside.md").exists())
            self.assertFalse((output / ".." / "outside.md").resolve()
                             .is_relative_to(output.resolve())
                             if hasattr(Path, "is_relative_to") else False)

    def test_preexisting_missing_reference_does_not_block_compression(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            write(root / "SKILL.md",
                  "Read [documentation](https://example.com/manual.pdf).\n"
                  "Read [missing](references/missing.md).\n"
                  "Always return a validated result.\n")
            graph = resolve_bundle(str(root))
            self.assertEqual([e.status for e in graph.edges], ["external", "missing"])
            output = Path(tmp) / "output"
            _, report = compress_bundle(
                str(root), str(output), cli=None,
                cfg={"extract_llm": False, "audit_llm": False})
            self.assertTrue(report["audit"]["ok"])
            self.assertEqual(report["audit"]["dangling_or_unsafe_references"], [])
            # the author's stale link is still visible in the report
            self.assertTrue(report["audit"]["preexisting_source_reference_defects"])

    def test_strict_mode_still_refuses_defective_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            root = outer / "bundle"
            write(outer / "outside.md", "secret\n")
            write(root / "SKILL.md", "Read [outside](../outside.md).\n")
            with self.assertRaises(BundleCompressionError):
                compress_bundle(str(root), str(outer / "output"), cli=None,
                                cfg={"extract_llm": False, "audit_llm": False,
                                     "tolerate_source_reference_defects": False})


class EnvironmentTests(unittest.TestCase):
    def test_only_exact_typed_guarantee_is_removed(self):
        unit = Unit("C1", "rule", ("root",), "Always validate every record.",
                    modality="must")
        contract = Contract(units=[unit])
        env = EnvironmentContract(guarantees=[EnvironmentGuarantee(
            id="host-1", type="rule", modality="must",
            content="Always validate every record.", resources=("refs/*.md",),
        )])
        reduced, drops = apply_environment_contract(contract, "refs/a.md", env)
        self.assertEqual(reduced.units, [])
        self.assertEqual(drops[0]["guarantee_id"], "host-1")

        mismatch = Contract(units=[Unit(
            "C2", "rule", ("root",), "Always validate every record.",
            modality="must_not")])
        reduced, drops = apply_environment_contract(mismatch, "refs/a.md", env)
        self.assertEqual(len(reduced.units), 1)
        self.assertEqual(drops, [])


class CapsuleTests(unittest.TestCase):
    def test_only_explicit_conditional_sections_are_split(self):
        repeated = " ".join(["Always preserve the diagnostic code."] * 20)
        text = f"""# Recovery

Common instructions stay in the dispatcher.

## When validation fails

{repeated}

## When the source is unavailable

{repeated}
"""
        plan = plan_capsules("references/recovery.md", text, min_tokens=20)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.capsules), 2)
        self.assertIn("Common instructions stay", plan.dispatcher)
        self.assertIn("do not load unrelated modules", plan.dispatcher)


class BundleIntegrationTests(unittest.TestCase):
    def test_bundle_compression_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = sample_bundle(base / "source")
            env_path = base / "environment.json"
            write(env_path, json.dumps({
                "version": 1,
                "name": "test-host",
                "guarantees": [{
                    "id": "schema-validator",
                    "type": "output",
                    "modality": "must",
                    "content": "Always validate each record against the declared schema.",
                    "validation": "verify before finishing",
                    "resources": ["references/*.md"],
                    "source": "tool-schema:validator",
                }],
            }))
            output = base / "output"
            _, report = compress_bundle(
                str(source), str(output), cli=None,
                environment_contract=str(env_path),
                cfg={
                    "extract_llm": False,
                    "audit_llm": False,
                    "capsule_min_tokens": 40,
                    "capsule_min_sections": 2,
                },
            )
            self.assertTrue(report["audit"]["ok"])
            before = report["original_cost"]
            after = report["compressed_cost"]
            before_obj = before["expected_execution_tokens"] + 0.05 * before["deployment_text_tokens"]
            after_obj = after["expected_execution_tokens"] + 0.05 * after["deployment_text_tokens"]
            self.assertLessEqual(after_obj, before_obj)
            self.assertTrue((output / "SKILL.md").is_file())
            if not report["selected_verbatim_bundle_baseline"]:
                self.assertTrue((output / "sub/SUBSKILL.md").is_file())
                self.assertFalse((output / "sub/SKILL.md").exists())
                self.assertGreaterEqual(len(report["capsules"]), 1)
                self.assertGreaterEqual(len(report["environment_drops"]), 2)
                shared = list((output / "references/.skillzip_shared").glob("*.md"))
                self.assertEqual(len(shared), 1)
                self.assertNotIn(".skillzip_shared", (output / "SKILL.md").read_text())
                self.assertIn(".skillzip_shared", (output / "references/csv.md").read_text())
            self.assertEqual((output / "scripts/check.py").read_text(), "print('validated')\n")
            self.assertEqual((output / "assets/blob.bin").read_bytes(), b"\x00\x01\x02")

    def test_original_single_file_api_regression(self):
        original = Skill("demo", """# Demo

Always validate the input before continuing.
Always validate the input before continuing.
1. Read the input.
2. Return the result.
""")
        compressed, report = compress_oneshot(
            original, cli=None,
            cfg={"extract_llm": False, "audit_llm": False})
        self.assertLessEqual(compressed.tokens, original.tokens)
        self.assertIn("remaining_ratio", report)

    def test_tiny_bundle_selects_exact_verbatim_feasibility_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            original = "---\nname: tiny\ndescription: Tiny skill.\n---\n\nReturn the result.\n"
            write(source / "SKILL.md", original)
            output = base / "output"
            _, report = compress_bundle(
                str(source), str(output), cli=None,
                cfg={"extract_llm": False, "audit_llm": False},
            )
            self.assertTrue(report["selected_verbatim_bundle_baseline"])
            self.assertEqual((output / "SKILL.md").read_text(encoding="utf-8"), original)

    def test_bundle_output_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = sample_bundle(base / "source")
            out_a, out_b = base / "out-a", base / "out-b"
            cfg = {"extract_llm": False, "audit_llm": False,
                   "capsule_min_tokens": 40}
            compress_bundle(str(source), str(out_a), cli=None, cfg=cfg)
            compress_bundle(str(source), str(out_b), cli=None, cfg=cfg)
            files_a = {p.relative_to(out_a): p.read_bytes()
                       for p in out_a.rglob("*") if p.is_file()}
            files_b = {p.relative_to(out_b): p.read_bytes()
                       for p in out_b.rglob("*") if p.is_file()}
            self.assertEqual(files_a, files_b)

    def test_cli_end_to_end_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = sample_bundle(base / "source")
            output = base / "output"
            report = base / "report.json"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env["PYTHONPYCACHEPREFIX"] = str(base / "pycache")
            result = subprocess.run(
                [sys.executable, "-m", "skillzip.cli", "--no-llm",
                 "compress-bundle", str(source), "--output", str(output),
                 "--report", str(report)],
                cwd=str(ROOT), env=env, text=True, capture_output=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(data["audit"]["ok"])
            self.assertTrue((output / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
