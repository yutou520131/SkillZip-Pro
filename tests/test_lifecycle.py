"""Tests for the lifecycle layer: entry contracts, persistent publish, transient views.

Everything here is deterministic and model-free, so these tests are also the
executable specification of the guarantees stated in the README.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from skillzip import lifecycle as lc

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "multi_entry_bundle"


class LifecycleTestCase(unittest.TestCase):
    """Shared scratch workspace with an isolated view cache."""

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="skillzip_lifecycle_test_"))
        self._prev_cache = os.environ.get("SKILLZIP_VIEW_CACHE")
        os.environ["SKILLZIP_VIEW_CACHE"] = str(self.workspace / "view_cache")

    def tearDown(self) -> None:
        if self._prev_cache is None:
            os.environ.pop("SKILLZIP_VIEW_CACHE", None)
        else:
            os.environ["SKILLZIP_VIEW_CACHE"] = self._prev_cache
        shutil.rmtree(self.workspace, ignore_errors=True)

    def out(self, name: str) -> str:
        return str(self.workspace / name)


class TestEntryContracts(LifecycleTestCase):

    def test_labels_are_derived_from_front_matter(self):
        labels = lc.entry_contracts(str(EXAMPLE))
        # Both the root and the nested subskill declare name + description, so a
        # discovery scan would index them: they are public entries.
        self.assertEqual(labels["SKILL.md"], lc.PUBLIC)
        self.assertEqual(labels["sub/SKILL.md"], lc.PUBLIC)
        # Plain references carry no agent-facing description.
        self.assertEqual(labels["references/ci_logs.md"], lc.PRIVATE)

    def test_public_entrypoints_listing(self):
        self.assertEqual(lc.public_entrypoints(str(EXAMPLE)),
                         ["SKILL.md", "sub/SKILL.md"])

    def test_explicit_manifest_overrides_derived_labels(self):
        bundle = self.workspace / "override"
        shutil.copytree(EXAMPLE, bundle)
        (bundle / "entrypoints.json").write_text(
            '{"contracts": {"SKILL.md": "public", "sub/SKILL.md": "private"}}',
            encoding="utf-8")
        labels = lc.entry_contracts(str(bundle))
        self.assertEqual(labels["sub/SKILL.md"], lc.PRIVATE)

    def test_closure_from_a_non_root_entry(self):
        # A direct call to the subskill must not pull in the whole bundle.
        closure = lc.reachable_from(str(EXAMPLE), "sub/SKILL.md")
        self.assertIn("sub/SKILL.md", closure)
        self.assertNotIn("references/coverage.md", closure)


class TestPersistentLifecycle(LifecycleTestCase):

    def test_audited_publish_keeps_every_public_entry_callable(self):
        out = self.out("audited")
        report = lc.compress_persistent(str(EXAMPLE), out)

        self.assertEqual(report["lifecycle"], "persistent")
        self.assertTrue(report["audit_ok"])
        self.assertTrue(report["entry_labels_used"])

        verdict = lc.audit_public_entries(str(EXAMPLE), out)
        self.assertTrue(verdict["ok"], f"failures: {verdict['failures']}")
        self.assertEqual(verdict["worst_public_effective"], 1.0)

    def test_audited_publish_actually_compresses(self):
        out = self.out("audited")
        report = lc.compress_persistent(str(EXAMPLE), out)
        # This example has genuine cross-file duplication, so the kernel must
        # find something; if it ever cannot, it is required to fall back safely.
        self.assertFalse(report["selected_verbatim"])
        self.assertLess(lc.bundle_bytes(out), lc.bundle_bytes(str(EXAMPLE)))

    def test_source_bundle_is_never_modified(self):
        before = lc.bundle_digest(str(EXAMPLE))
        lc.compress_persistent(str(EXAMPLE), self.out("audited"))
        self.assertEqual(lc.bundle_digest(str(EXAMPLE)), before)

    def test_disabling_the_audit_can_lose_a_public_entry(self):
        """The ablation reported in the paper, pinned as a regression test.

        Without the multi-entry audit the kernel renames the nested public entry.
        Its *text* survives, but a direct call can no longer find it, so its
        effective independence is zero.  This is precisely the failure the
        audited default prevents.
        """
        out = self.out("unaudited")
        lc.compress_persistent(str(EXAMPLE), out, audit_entries=False)
        verdict = lc.audit_public_entries(str(EXAMPLE), out)

        self.assertFalse(verdict["ok"])
        rows = {r["entry"]: r for r in verdict["entries"]}
        subskill = rows["sub/SKILL.md"]
        self.assertFalse(subskill["discoverable"])
        self.assertEqual(subskill["effective"], 0.0)
        # Content was not deleted -- this is a discoverability failure only.
        self.assertEqual(subskill["independence"], 1.0)

    def test_audit_restores_what_the_ablation_loses(self):
        audited = self.out("audited")
        report = lc.compress_persistent(str(EXAMPLE), audited)
        # At least one entry filename had to be restored for the audit to pass.
        self.assertGreaterEqual(report["entries_renamed_back"], 1)


class TestTransientLifecycle(LifecycleTestCase):

    def test_view_leaves_the_canonical_bundle_byte_identical(self):
        before = lc.bundle_digest(str(EXAMPLE))
        view = lc.build_execution_view(str(EXAMPLE), "sub/SKILL.md")
        self.assertTrue(view["canonical_unchanged"])
        self.assertEqual(lc.bundle_digest(str(EXAMPLE)), before)

    def test_view_is_runnable_and_rooted_at_the_entry(self):
        view = lc.build_execution_view(str(EXAMPLE), "sub/SKILL.md")
        view_dir = Path(view["view_dir"])
        # The agent is handed an ordinary directory whose root is SKILL.md.
        self.assertTrue((view_dir / "SKILL.md").is_file())
        # The original root is preserved as linked host context.
        self.assertTrue((view_dir / "_host_context.md").is_file())

    def test_view_never_grows_the_run(self):
        view = lc.build_execution_view(str(EXAMPLE), "sub/SKILL.md")
        self.assertLessEqual(view["view_tokens"], view["raw_closure_tokens"])

    def test_second_call_is_served_from_cache(self):
        cold = lc.build_execution_view(str(EXAMPLE), "sub/SKILL.md")
        warm = lc.build_execution_view(str(EXAMPLE), "sub/SKILL.md")
        self.assertFalse(cold["cache_hit"])
        self.assertTrue(warm["cache_hit"])
        self.assertEqual(cold["cache_key"], warm["cache_key"])
        self.assertLess(warm["build_ms"], cold["build_ms"])

    def test_editing_the_bundle_invalidates_the_cache_key(self):
        bundle = self.workspace / "edited"
        shutil.copytree(EXAMPLE, bundle)
        key_before = lc.view_key(str(bundle), "sub/SKILL.md")
        target = bundle / "references" / "ci_logs.md"
        target.write_text(target.read_text(encoding="utf-8") + "\n- A new rule.\n",
                          encoding="utf-8")
        self.assertNotEqual(lc.view_key(str(bundle), "sub/SKILL.md"), key_before)

    def test_publish_transient_ships_the_bundle_unchanged(self):
        out = self.out("transient")
        report = lc.publish_transient(str(EXAMPLE), out)
        self.assertTrue(report["verbatim_canonical"])
        self.assertEqual(report["kernel_calls"], 0)
        # Disk size does not move in this mode, by construction.
        self.assertEqual(lc.bundle_bytes(out), lc.bundle_bytes(str(EXAMPLE)))
        self.assertEqual(lc.bundle_digest(out), lc.bundle_digest(str(EXAMPLE)))

    def test_cache_can_be_cleared(self):
        lc.build_execution_view(str(EXAMPLE), "sub/SKILL.md")
        self.assertGreaterEqual(lc.clear_view_cache(), 1)


class TestIndependenceMeasurement(LifecycleTestCase):

    def test_identical_bundle_is_fully_independent(self):
        copy = self.workspace / "copy"
        shutil.copytree(EXAMPLE, copy)
        report = lc.independence(str(EXAMPLE), str(copy), "sub/SKILL.md")
        self.assertTrue(report["discoverable"])
        self.assertEqual(report["independence"], 1.0)

    def test_missing_entry_scores_zero(self):
        stripped = self.workspace / "stripped"
        shutil.copytree(EXAMPLE, stripped)
        shutil.rmtree(stripped / "sub")
        report = lc.independence(str(EXAMPLE), str(stripped), "sub/SKILL.md")
        self.assertFalse(report["discoverable"])
        self.assertEqual(report["independence"], 0.0)
        self.assertEqual(
            lc.effective_independence(str(EXAMPLE), str(stripped), "sub/SKILL.md"),
            0.0)


if __name__ == "__main__":
    unittest.main()
