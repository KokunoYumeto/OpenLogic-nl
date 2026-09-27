#!/usr/bin/env python3
"""Hermetic regressions for the no-TeX Dutch source/EPUB builder."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_partial_epub3 as core
import build_partial_source_epub3 as source_builder
from epub3.direct_latex import DirectDocument, known_diagram_svg
from test_build_partial_epub3 import FixtureRepository


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def fixture_document(plan: core.AcceptedPlan, *, image: bool = False) -> DirectDocument:
    assets = {}
    if image:
        assets["diagram.svg"], _description = known_diagram_svg("union", "nl-standard")
    return DirectDocument(
        tex=b"\\documentclass{article}\n\\begin{document}fixture\\end{document}\n",
        assets=assets,
        unit_input_sha256={unit_id: digest(unit_id.encode()) for unit_id in plan.accepted_ids},
        metrics={
            "units": len(plan.accepted_units),
            "diagrams_converted_to_svg": len(assets),
        },
    )


def write_html_fixture(
    root: Path,
    plan: core.AcceptedPlan,
    document: DirectDocument,
    *,
    warning: bool = False,
    broken_fragment: bool = False,
    visible_tex: bool = False,
    empty_alt: bool = False,
) -> None:
    root.mkdir(parents=True)
    for name, payload in document.assets.items():
        (root / name).write_bytes(payload)
    pieces = [
        "<!DOCTYPE html><html lang=\"nl-NL\"><head><meta charset=\"utf-8\"><title>Test</title></head><body>"
    ]
    for number, unit_id in enumerate(plan.accepted_ids, start=1):
        pieces.append(f'<p><span id="unit-{unit_id}"></span></p>')
        pieces.append(
            "<p>Tekst "
            '<math xmlns="http://www.w3.org/1998/Math/MathML">'
            "<semantics><mi>x</mi>"
            '<annotation encoding="application/x-tex">x</annotation>'
            "</semantics></math>."
            "</p>"
        )
        if visible_tex and number == 1:
            pieces.append(r"<p>\UnknownMacro zichtbaar.</p>")
        if number == 1 and document.assets:
            alt = "" if empty_alt else "Twee verzamelingen met een gekleurd gebied."
            pieces.append(f'<p><img src="diagram.svg" alt="{alt}"></p>')
        pieces.append(f'<p><span id="unit-end-{unit_id}"></span></p>')
    target = "ontbreekt" if broken_fragment else f"unit-{plan.accepted_ids[-1]}"
    pieces.append(f'<p><a href="#{target}">Ga verder</a></p></body></html>')
    (root / "book.html").write_text("".join(pieces), encoding="utf-8", newline="\n")
    (root / "pandoc.log").write_bytes(b"warning\n" if warning else b"")
    (root / "direct.tex").write_bytes(document.tex)


class PlanTests(unittest.TestCase):
    def test_dry_run_declares_no_pdf_no_tex_and_source_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FixtureRepository(Path(temporary))
            plan = fixture.plan()
            report = source_builder.plan_report(plan, Path(temporary) / "out", None)
            self.assertEqual("DRY_RUN_VALIDATED", report["status"])
            self.assertEqual(
                ["latex", "source_zip", "epub", "epubcheck_receipt", "receipt"],
                report["artifact_order"],
            )
            self.assertEqual(0, report["conversion"]["tex_processes"])
            self.assertFalse(report["conversion"]["tex_mutex_used"])
            self.assertFalse(report["conversion"]["pdf_included"])
            self.assertTrue(all(not path.lower().endswith(".pdf") for path in report["expected_paths"]))


class HtmlAuditTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[core.AcceptedPlan, DirectDocument, Path]:
        fixture = FixtureRepository(root / "repo")
        plan = fixture.plan(("nl-standard",))
        document = fixture_document(plan)
        generated = root / "generated"
        write_html_fixture(generated, plan, document)
        return plan, document, generated

    def test_valid_html_binds_every_unit_and_math(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plan, document, generated = self._fixture(Path(temporary))
            audit = source_builder.audit_pandoc_html(
                plan, "nl-standard", generated, document
            )
            self.assertEqual("PASS", audit["status"])
            self.assertEqual(2, audit["units"])
            self.assertEqual(2, audit["mathml_roots"])
            self.assertEqual(2, len(audit["segment_records"]))

    def test_warning_log_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FixtureRepository(root / "repo")
            plan = fixture.plan(("nl-standard",))
            document = fixture_document(plan)
            generated = root / "generated"
            write_html_fixture(generated, plan, document, warning=True)
            with self.assertRaisesRegex(core.BuildError, "warnings"):
                source_builder.audit_pandoc_html(plan, "nl-standard", generated, document)

    def test_broken_fragment_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FixtureRepository(root / "repo")
            plan = fixture.plan(("nl-standard",))
            document = fixture_document(plan)
            generated = root / "generated"
            write_html_fixture(generated, plan, document, broken_fragment=True)
            with self.assertRaisesRegex(core.BuildError, "broken Pandoc internal links"):
                source_builder.audit_pandoc_html(plan, "nl-standard", generated, document)

    def test_visible_raw_tex_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FixtureRepository(root / "repo")
            plan = fixture.plan(("nl-standard",))
            document = fixture_document(plan)
            generated = root / "generated"
            write_html_fixture(generated, plan, document, visible_tex=True)
            with self.assertRaisesRegex(core.BuildError, "visible raw TeX"):
                source_builder.audit_pandoc_html(plan, "nl-standard", generated, document)

    def test_missing_alt_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FixtureRepository(root / "repo")
            plan = fixture.plan(("nl-standard",))
            document = fixture_document(plan, image=True)
            generated = root / "generated"
            write_html_fixture(generated, plan, document, empty_alt=True)
            with self.assertRaisesRegex(core.BuildError, "missing or generic Dutch image"):
                source_builder.audit_pandoc_html(plan, "nl-standard", generated, document)


class NoTexPreparationTests(unittest.TestCase):
    def test_prepare_never_enters_pdf_make4ht_or_mutex_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FixtureRepository(root / "repo")
            plan = fixture.plan(("nl-standard",))
            document = fixture_document(plan)
            epub = b"epub-fixture"
            epub_audit = {
                "schema": "openlogic-partial-epub3-audit-v1",
                "status": "PASS",
                "bytes": len(epub),
                "sha256": digest(epub),
            }
            html_record = {"path": "book.html", "bytes": 10, "sha256": "a" * 64}
            replay = {
                "command": ["pandoc"],
                "exit_code": 0,
                "seconds": 0.1,
                "launch_capture": list(core.WINDOWS_JOB_LAUNCH_CAPTURE),
                "audit": {"html": html_record},
                "output_tree": {"files": 1, "bytes": 10, "tree_sha256": "b" * 64},
            }
            epubcheck = {
                "schema": "openlogic-partial-epubcheck-receipt-v1",
                "status": "PASS",
                "checked_epub": {"bytes": len(epub), "sha256": digest(epub)},
                "result": {"fatal": 0, "error": 0, "warning": 0, "messages": 0},
            }
            with (
                mock.patch.object(source_builder, "direct_document", return_value=document),
                mock.patch.object(source_builder.shutil, "which", return_value=r"C:\pandoc.exe"),
                mock.patch.object(source_builder, "_pandoc_version", return_value="pandoc fixture"),
                mock.patch.object(source_builder, "run_pandoc_once", side_effect=[replay, replay]),
                mock.patch.object(core, "package_make4ht_output", return_value=(epub, epub_audit)),
                mock.patch.object(core, "run_epubcheck", return_value=epubcheck),
                mock.patch.object(core, "run_pdf_build", side_effect=AssertionError("PDF path entered")) as pdf,
                mock.patch.object(core, "run_make4ht", side_effect=AssertionError("make4ht path entered")) as make4ht,
                mock.patch.object(core, "GlobalTexMutex", side_effect=AssertionError("mutex path entered")) as mutex,
            ):
                prepared = source_builder.prepare_register(
                    plan,
                    "nl-standard",
                    root / "work",
                    "2026-09-26",
                    pandoc_timeout_seconds=30,
                    epubcheck_jar=root / "fixture.jar",
                    epubcheck_timeout_seconds=30,
                )
            pdf.assert_not_called()
            make4ht.assert_not_called()
            mutex.assert_not_called()
            self.assertNotIn("pdf", prepared.receipt["artifacts"])
            self.assertEqual(0, prepared.receipt["constraints"]["tex_processes_launched"])
            self.assertFalse(prepared.receipt["constraints"]["tex_mutex_acquired"])
            self.assertEqual(
                tuple(source_builder.no_pdf_filenames(plan, "nl-standard")[key] for key in source_builder.ARTIFACT_ORDER),
                tuple(prepared.files),
            )


if __name__ == "__main__":
    unittest.main()
