#!/usr/bin/env python3
"""Hermetic regressions for the bounded Dutch PDF/source/EPUB 3 pipeline."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from unittest import mock

from lxml import etree
from pypdf import PdfWriter

import build_partial_epub3 as builder


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def pdf_fixture(marker: str = "fixture") -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": marker})
    writer.write(output)
    return output.getvalue()


def fake_pdf_build_report(
    pdf: bytes,
    *,
    tex_name: str,
    tex: bytes,
    source_tree: dict[str, object],
) -> dict[str, object]:
    audit = builder.audit_pdf_bytes(pdf)
    return {
        "schema": "openlogic-partial-reader-pdf-build-v1",
        "status": "PASS_REPEATABLE_BYTES",
        "driver": {
            "name": builder.PDF_DRIVER,
            "version": "latexmk fixture 1.0",
            "command": ["latexmk.exe", "-version"],
            "exit_code": 0,
            "seconds": 0.01,
        },
        "engine": {
            "name": builder.PDF_ENGINE,
            "version": "pdfTeX fixture 1.0",
            "command": ["pdflatex.exe", "--version"],
            "exit_code": 0,
            "seconds": 0.01,
        },
        "input": {
            "cumulative_tex": {
                "path": tex_name,
                "bytes": len(tex),
                "sha256": digest(tex),
            },
            "source_tree": source_tree,
        },
        "mutex": builder.TEX_MUTEX_NAME,
        "process_tree_guard": "Windows job object, kill on close",
        "launch_capture": list(builder.WINDOWS_JOB_LAUNCH_CAPTURE),
        "runs": [
            {
                "command": ["latexmk.exe", f"replay-{number}"],
                "exit_code": 0,
                "seconds": 0.01,
            }
            for number in range(1, builder.PDF_REPLAY_COUNT + 1)
        ],
        "repeatability": {
            "runs": builder.PDF_REPLAY_COUNT,
            "comparison": "exact byte equality",
            "status": "PASS",
        },
        "pdf": audit,
    }


class FixtureRepository:
    def __init__(self, root: Path, statuses: tuple[str, ...] = ("accepted", "accepted", "pending")):
        self.root = root
        self.statuses = statuses
        self.rows: list[dict[str, object]] = []
        self.texts: dict[tuple[str, int], bytes] = {}
        self._write()

    def _body(self, register: str, number: int) -> bytes:
        if number == 1:
            value = (
                f"\\chapter*{{Voorwoord {register}}}\n"
                f"ACCEPTED-ONE-{register}\n"
            )
        elif number == 2:
            value = (
                "\\documentclass[../../../include/open-logic-chapter]{subfiles}\n"
                "\\begin{document}\n"
                f"\\olchapter{{sfr}}{{fixture}}{{Hoofdstuk {register}}}\n"
                "\\olimport{pending}\n"
                f"ACCEPTED-TWO-{register} $x=x$.\n"
                "\\OLEndChapterHook\n"
                "\\end{document}\n"
            )
        else:
            value = (
                "\\documentclass[../../../include/open-logic-section]{subfiles}\n"
                "\\begin{document}\n"
                f"PENDING-SECRET-{register}-{number}\n"
                "\\end{document}\n"
            )
        return value.encode("utf-8")

    def _write(self) -> None:
        (self.root / "alignment").mkdir(parents=True)
        for register in builder.REGISTER_CONFIG:
            (self.root / "translations" / register / "content").mkdir(parents=True)
        for number, status in enumerate(self.statuses, start=1):
            unit_id = f"OLP-{number:04d}"
            row: dict[str, object] = {
                "unit_id": unit_id,
                "order": number,
                "status": status,
                "source_role": ("reader_front_matter" if number == 1 else "chapter_driver" if number == 2 else "reader_unit"),
                "source_revision": "a" * 40,
                "source_path": f"content/unit-{number}.tex",
            }
            for register, prefix in (("nl-standard", "standard"), ("nl-gewoon", "ordinary")):
                payload = self._body(register, number)
                self.texts[(register, number)] = payload
                relative = f"translations/{register}/content/unit-{number}.tex"
                # Existing pending files are deliberate: the builder must not read or include them.
                (self.root / Path(relative)).write_bytes(payload)
                row[f"{prefix}_path"] = relative
                row[f"{prefix}_status"] = status
                row[f"{prefix}_bytes"] = len(payload) if status == "accepted" else None
                row[f"{prefix}_sha256"] = digest(payload) if status == "accepted" else None
            self.rows.append(row)
        (self.root / "alignment" / "UNITS.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in self.rows),
            encoding="utf-8",
            newline="\n",
        )

        upstream = self.root / "upstream"
        for directory in builder.UPSTREAM_SUPPORT_TREES:
            (upstream / directory).mkdir(parents=True)
            (upstream / directory / f"fixture-{directory}.txt").write_text(
                directory, encoding="utf-8", newline="\n"
            )
        for name in builder.ROOT_SUPPORT_FILES:
            (upstream / name).write_text(f"% {name}\n", encoding="utf-8", newline="\n")
        (upstream / "LICENSE.md").write_text("upstream license\n", encoding="utf-8")
        (upstream / "README.md").write_text("upstream readme\n", encoding="utf-8")
        (self.root / "LICENSE.md").write_text("project license\n", encoding="utf-8")
        for register in builder.REGISTER_CONFIG:
            locale = self.root / "editions" / register / "support" / "open-logic-reader-locale.tex"
            locale.parent.mkdir(parents=True)
            locale.write_text(
                "\\setlocalecaption{english}{theorem}{Stelling}\n"
                "\\addto\\extrasenglish{}\n",
                encoding="utf-8",
                newline="\n",
            )

    def plan(self, registers: tuple[str, ...] = ("nl-standard", "nl-gewoon")) -> builder.AcceptedPlan:
        return builder.plan_accepted_prefix(self.root, registers)


def generated_book(root: Path, *, broken_fragment: bool = False, math: bool = True) -> Path:
    root.mkdir(parents=True)
    target = "missing-anchor" if broken_fragment else "unit-OLP-0002"
    math_markup = (
        '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi><mo>=</mo><mi>x</mi></math>'
        if math
        else ""
    )
    (root / "book.html").write_text(
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>Fixtureboek</title></head>"
        "<body>"
        '<a id="unit-OLP-0001"></a><h1>Een</h1>'
        f"{math_markup}"
        '<a id="unit-OLP-0002"></a><h2>Twee</h2>'
        f'<p><a href="#{target}">Ga naar twee</a></p>'
        "</body></html>\n",
        encoding="utf-8",
        newline="\n",
    )
    return root


def replace_zip_entry(epub: bytes, name: str, transform) -> bytes:
    with zipfile.ZipFile(io.BytesIO(epub)) as archive:
        entries = {member.filename: archive.read(member.filename) for member in archive.infolist()}
    entries[name] = transform(entries[name])
    return builder.deterministic_zip_bytes(entries, epub=True)


class AcceptedPrefixTests(unittest.TestCase):
    def test_only_contiguous_accepted_prefix_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FixtureRepository(Path(temporary))
            plan = fixture.plan()
            self.assertEqual(("OLP-0001", "OLP-0002"), plan.accepted_ids)
            self.assertEqual("OLP-0003", plan.first_excluded["unit_id"])
            self.assertEqual(("OLP-0003",), plan.excluded_ids)

    def test_accepted_after_pending_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FixtureRepository(Path(temporary), ("accepted", "pending", "accepted"))
            with self.assertRaisesRegex(builder.BuildError, "after the accepted prefix ended"):
                fixture.plan()

    def test_per_register_acceptance_outside_prefix_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FixtureRepository(Path(temporary))
            fixture.rows[2]["standard_status"] = "accepted"
            (fixture.root / "alignment" / "UNITS.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in fixture.rows),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(builder.BuildError, "outside aggregate prefix"):
                fixture.plan()

    def test_accepted_digest_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FixtureRepository(Path(temporary))
            path = fixture.root / str(fixture.rows[0]["standard_path"])
            path.write_bytes(path.read_bytes() + b"mutation")
            with self.assertRaisesRegex(builder.BuildError, "byte-count drift"):
                fixture.plan(("nl-standard",))


class CumulativeSourceTests(unittest.TestCase):
    def test_direct_latex_contains_each_accepted_body_and_no_pending_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FixtureRepository(Path(temporary))
            plan = fixture.plan(("nl-standard",))
            tex = builder.render_cumulative_tex(plan, "nl-standard", "2026-09-19")
            text = tex.decode("utf-8")
            self.assertEqual(1, text.count("% BEGIN ACCEPTED UNIT OLP-0001 "))
            self.assertEqual(1, text.count("% BEGIN ACCEPTED UNIT OLP-0002 "))
            self.assertNotIn("% BEGIN ACCEPTED UNIT OLP-0003 ", text)
            self.assertIn("ACCEPTED-ONE-nl-standard", text)
            self.assertIn("ACCEPTED-TWO-nl-standard", text)
            self.assertNotIn("PENDING-SECRET", text)
            self.assertNotIn("\\olimport", text)
            self.assertEqual(1, text.count("\\documentclass"))
            self.assertEqual(1, text.count("\\begin{document}"))
            self.assertEqual(1, text.count("\\end{document}"))
            self.assertIn("Broneenheid OLP-0003 en alle latere eenheden zijn niet opgenomen", text)

    def test_source_tree_is_allowlisted_complete_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FixtureRepository(Path(temporary))
            plan = fixture.plan(("nl-standard",))
            tex = builder.render_cumulative_tex(plan, "nl-standard", "2026-09-19")
            entries = builder.source_tree_entries(plan, "nl-standard", "2026-09-19", tex)
            translated = sorted(name for name in entries if name.startswith("translations/"))
            self.assertEqual(
                [
                    "translations/nl-standard/content/unit-1.tex",
                    "translations/nl-standard/content/unit-2.tex",
                ],
                translated,
            )
            self.assertNotIn("translations/nl-standard/content/unit-3.tex", entries)
            base = builder.output_basename(plan, "nl-standard")
            self.assertEqual(tex, entries[f"{base}.tex"])
            self.assertIn("SOURCE-MANIFEST.json", entries)
            self.assertIn("BUILD.md", entries)
            self.assertIn("PDF-REBUILD.json", entries)
            self.assertIn("scripts/build_partial_epub3.py", entries)
            self.assertIn(
                "editions/nl-standard/support/open-logic-reader-locale.tex", entries
            )
            self.assertIn("upstream/sty/fixture-sty.txt", entries)
            self.assertIn("upstream/assets/fixture-assets.txt", entries)
            locale = entries["upstream/locale/nl/open-logic-locale.sty"].decode("utf-8")
            self.assertIn("{dutch}", locale)
            self.assertIn("\\extrasdutch", locale)
            pdf_rebuild = json.loads(entries["PDF-REBUILD.json"])
            self.assertEqual("latexmk", pdf_rebuild["driver"])
            self.assertEqual("pdflatex", pdf_rebuild["engine"])
            self.assertEqual(2, len(pdf_rebuild["commands"]))
            self.assertEqual("1789776000", pdf_rebuild["environment"]["SOURCE_DATE_EPOCH"])
            self.assertEqual(
                "exact PDF byte equality",
                pdf_rebuild["repeatability_requirement"]["comparison"],
            )
            self.assertEqual(
                list(builder.WINDOWS_JOB_LAUNCH_CAPTURE),
                pdf_rebuild["machine_serialization"]["process_capture"],
            )
            build_text = entries["BUILD.md"].decode("utf-8")
            self.assertIn(builder.TEX_MUTEX_NAME, build_text)
            self.assertIn("PDF-REBUILD.json", build_text)
            first = builder.deterministic_zip_bytes(entries)
            second = builder.deterministic_zip_bytes(dict(reversed(list(entries.items()))))
            self.assertEqual(first, second)
            with zipfile.ZipFile(io.BytesIO(first)) as archive:
                self.assertEqual(sorted(entries), archive.namelist())
                self.assertEqual(tex, archive.read(f"{base}.tex"))
                extracted = Path(temporary) / "extracted"
                archive.extractall(extracted)
            replay_plan = builder.plan_accepted_prefix(extracted, ("nl-standard",))
            replay_tex = builder.render_cumulative_tex(
                replay_plan, "nl-standard", "2026-09-19"
            )
            replay_entries = builder.source_tree_entries(
                replay_plan, "nl-standard", "2026-09-19", replay_tex
            )
            self.assertEqual(first, builder.deterministic_zip_bytes(replay_entries))

    def test_direct_latex_audit_detects_body_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FixtureRepository(Path(temporary))
            plan = fixture.plan(("nl-standard",))
            tex = builder.render_cumulative_tex(plan, "nl-standard", "2026-09-19")
            corrupted = tex.replace(b"ACCEPTED-TWO-nl-standard", b"BODY-REMOVED", 1)
            with self.assertRaisesRegex(builder.BuildError, "body replay failed"):
                builder.audit_cumulative_tex(plan, "nl-standard", corrupted)

    def test_dry_run_writes_nothing_and_does_not_call_make4ht(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FixtureRepository(Path(temporary) / "repo")
            output = Path(temporary) / "never-created"
            stream = io.StringIO()
            with mock.patch.object(
                builder, "run_make4ht", side_effect=AssertionError("TeX launched")
            ), mock.patch.object(
                builder, "run_epubcheck", side_effect=AssertionError("EPUBCheck launched")
            ), mock.patch.object(
                builder,
                "resolve_epubcheck_jar",
                side_effect=AssertionError("EPUBCheck resolved during dry run"),
            ):
                with contextlib.redirect_stdout(stream):
                    result = builder.main(
                        [
                            "--repo-root",
                            str(fixture.root),
                            "--output-dir",
                            str(output),
                            "--register",
                            "both",
                            "--dry-run",
                        ]
                    )
            self.assertEqual(0, result)
            self.assertFalse(output.exists())
            report = json.loads(stream.getvalue())
            self.assertEqual("DRY_RUN", report["status"])
            self.assertFalse(report["pdf"]["will_run"])
            self.assertFalse(report["make4ht"]["will_run"])
            self.assertFalse(report["epubcheck"]["will_run"])
            self.assertEqual(list(builder.ARTIFACT_ORDER), report["artifact_order"])
            self.assertEqual(
                list(builder.ARTIFACT_ORDER),
                report["registers"][0]["artifact_order"],
            )
            self.assertEqual("OLP-0003", report["accepted_prefix"]["first_excluded"])

    def test_output_may_not_overlap_protected_source_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FixtureRepository(Path(temporary) / "repo")
            output = fixture.root / "translations" / "forbidden-output"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = builder.main(
                    [
                        "--repo-root",
                        str(fixture.root),
                        "--output-dir",
                        str(output),
                        "--register",
                        "nl-standard",
                        "--dry-run",
                    ]
                )
            self.assertEqual(2, result)
            self.assertFalse(output.exists())
            self.assertIn("may not overlap", stderr.getvalue())


class PdfBuildTests(unittest.TestCase):
    def source_tree(self, root: Path) -> tuple[Path, dict[str, object]]:
        source = root / "source"
        source.mkdir(parents=True)
        (source / "book.tex").write_text("fixture", encoding="utf-8", newline="\n")
        return source, builder.directory_tree_identity(source)

    def fake_mutex(self, state: dict[str, object]):
        class FakeMutex:
            abandoned = False

            def __init__(self, timeout: float):
                state["mutex_timeout"] = timeout

            def __enter__(self):
                state["inside"] = True
                return self

            def __exit__(self, *args):
                state["inside"] = False

        return FakeMutex

    def test_pdf_structural_audit_rejects_malformed_candidates(self) -> None:
        good = pdf_fixture()
        with self.assertRaisesRegex(builder.BuildError, "header"):
            builder.audit_pdf_bytes(b"not-a-pdf")
        with self.assertRaisesRegex(builder.BuildError, "trailer"):
            builder.audit_pdf_bytes(good.rsplit(b"%%EOF", 1)[0])
        empty_output = io.BytesIO()
        PdfWriter().write(empty_output)
        with self.assertRaisesRegex(builder.BuildError, "no pages"):
            builder.audit_pdf_bytes(empty_output.getvalue())

    def test_pdf_mutex_failure_launches_no_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, identity = self.source_tree(root)
            job = mock.Mock(side_effect=AssertionError("process launched"))
            with mock.patch.object(
                builder.shutil, "which", side_effect=lambda value: value + ".exe"
            ), mock.patch.object(
                builder,
                "GlobalTexMutex",
                side_effect=builder.BuildError("timed out acquiring mutex fixture"),
            ), mock.patch.object(builder, "_run_in_windows_job", job):
                with self.assertRaisesRegex(builder.BuildError, "timed out acquiring"):
                    builder.run_pdf_build(
                        source,
                        "book.tex",
                        root / "pdf",
                        modified="2026-09-19",
                        expected_source_tree=identity,
                        mutex_timeout_seconds=0.1,
                        pdf_timeout_seconds=2,
                    )
            job.assert_not_called()

    def test_pdf_process_nonzero_and_timeout_fail_closed(self) -> None:
        for error in (
            "PDF replay 1 failed with exit code 7",
            "PDF replay 1 process tree exceeded 0.5 seconds",
        ):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, identity = self.source_tree(root)
                state: dict[str, object] = {"inside": False}

                def fake_job(command, cwd, timeout, log, environment, *, process_label):
                    self.assertTrue(state["inside"])
                    if "version probe" in process_label:
                        log.write_bytes(f"{process_label} 1.0\n".encode())
                        return {"exit_code": 0, "seconds": 0.01}
                    raise builder.BuildError(error)

                with mock.patch.object(
                    builder.shutil, "which", side_effect=lambda value: value + ".exe"
                ), mock.patch.object(
                    builder, "GlobalTexMutex", self.fake_mutex(state)
                ), mock.patch.object(
                    builder, "_run_in_windows_job", side_effect=fake_job
                ):
                    with self.assertRaisesRegex(builder.BuildError, re.escape(error)):
                        builder.run_pdf_build(
                            source,
                            "book.tex",
                            root / "pdf",
                            modified="2026-09-19",
                            expected_source_tree=identity,
                            mutex_timeout_seconds=1,
                            pdf_timeout_seconds=2,
                        )
                self.assertFalse(state["inside"])

    def test_pdf_nondeterministic_replay_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, identity = self.source_tree(root)
            state: dict[str, object] = {"inside": False, "replay": 0}

            def fake_job(command, cwd, timeout, log, environment, *, process_label):
                self.assertTrue(state["inside"])
                log.write_bytes(f"{process_label} success\n".encode())
                if process_label.startswith("PDF replay"):
                    state["replay"] = int(state["replay"]) + 1
                    output = Path(
                        next(value.split("=", 1)[1] for value in command if value.startswith("-outdir="))
                    )
                    (output / "book.pdf").write_bytes(
                        pdf_fixture(f"replay-{state['replay']}")
                    )
                return {"exit_code": 0, "seconds": 0.01}

            with mock.patch.object(
                builder.shutil, "which", side_effect=lambda value: value + ".exe"
            ), mock.patch.object(
                builder, "GlobalTexMutex", self.fake_mutex(state)
            ), mock.patch.object(
                builder, "_run_in_windows_job", side_effect=fake_job
            ):
                with self.assertRaisesRegex(builder.BuildError, "nondeterministic"):
                    builder.run_pdf_build(
                        source,
                        "book.tex",
                        root / "pdf",
                        modified="2026-09-19",
                        expected_source_tree=identity,
                        mutex_timeout_seconds=1,
                        pdf_timeout_seconds=2,
                    )
            self.assertFalse(state["inside"])

    def test_pdf_exact_artifact_and_source_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, identity = self.source_tree(root)
            state: dict[str, object] = {"inside": False, "labels": []}
            expected_pdf = pdf_fixture("stable")

            def fake_job(command, cwd, timeout, log, environment, *, process_label):
                self.assertTrue(state["inside"])
                self.assertEqual("1789776000", environment["SOURCE_DATE_EPOCH"])
                self.assertEqual("1", environment["FORCE_SOURCE_DATE"])
                state["labels"].append(process_label)
                log.write_bytes(f"{process_label} stable\n".encode())
                if process_label.startswith("PDF replay"):
                    output = Path(
                        next(value.split("=", 1)[1] for value in command if value.startswith("-outdir="))
                    )
                    (output / "book.pdf").write_bytes(expected_pdf)
                return {"exit_code": 0, "seconds": 0.01}

            with mock.patch.object(
                builder.shutil, "which", side_effect=lambda value: value + ".exe"
            ), mock.patch.object(
                builder, "GlobalTexMutex", self.fake_mutex(state)
            ), mock.patch.object(
                builder, "_run_in_windows_job", side_effect=fake_job
            ):
                payload, report = builder.run_pdf_build(
                    source,
                    "book.tex",
                    root / "pdf",
                    modified="2026-09-19",
                    expected_source_tree=identity,
                    mutex_timeout_seconds=1,
                    pdf_timeout_seconds=2,
                )
            self.assertEqual(expected_pdf, payload)
            self.assertEqual("PASS_REPEATABLE_BYTES", report["status"])
            self.assertEqual(identity, report["input"]["source_tree"])
            self.assertEqual(digest(b"fixture"), report["input"]["cumulative_tex"]["sha256"])
            self.assertEqual(digest(expected_pdf), report["pdf"]["sha256"])
            self.assertEqual(1, report["pdf"]["pages"])
            self.assertEqual(2, len(report["runs"]))
            self.assertTrue(all(run["exit_code"] == 0 for run in report["runs"]))
            self.assertEqual(["latexmk", "-version"], report["driver"]["command"])
            self.assertEqual(["pdflatex", "--version"], report["engine"]["command"])
            self.assertEqual(
                "-outdir=<PDF_REPLAY_1_DIRECTORY>", report["runs"][0]["command"][-2]
            )
            self.assertNotIn(str(root), json.dumps(report))
            self.assertEqual(
                [
                    "latexmk version probe",
                    "pdflatex version probe",
                    "PDF replay 1",
                    "PDF replay 2",
                ],
                state["labels"],
            )
            self.assertFalse(state["inside"])


class EpubPackagingTests(unittest.TestCase):
    def make_epub(self, temporary: str, **book_options):
        fixture = FixtureRepository(Path(temporary) / "repo")
        plan = fixture.plan(("nl-standard",))
        generated = generated_book(Path(temporary) / "generated", **book_options)
        epub, audit = builder.package_make4ht_output(
            plan, "nl-standard", "2026-09-19", generated
        )
        return plan, epub, audit

    def test_epub_is_deterministic_reflowable_dutch_and_native_mathml(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first_plan, first, first_audit = self.make_epub(first_dir)
            _, second, second_audit = self.make_epub(second_dir)
            self.assertEqual(first, second)
            self.assertEqual("PASS", first_audit["status"])
            self.assertEqual(first_audit["sha256"], second_audit["sha256"])
            self.assertEqual(1, first_audit["mathml_roots"])
            self.assertEqual(2, first_audit["accepted_unit_anchors"])
            with zipfile.ZipFile(io.BytesIO(first)) as archive:
                infos = archive.infolist()
                self.assertEqual("mimetype", infos[0].filename)
                self.assertEqual(zipfile.ZIP_STORED, infos[0].compress_type)
                self.assertEqual(b"application/epub+zip", archive.read("mimetype"))
                self.assertEqual(sorted(info.filename for info in infos[1:]), [info.filename for info in infos[1:]])
                package = etree.fromstring(archive.read("OEBPS/package.opf"))
                ns = {"opf": builder.OPF_NS, "dc": builder.DC_NS}
                self.assertEqual(["nl-NL"], package.xpath("./opf:metadata/dc:language/text()", namespaces=ns))
                self.assertEqual(
                    ["reflowable"],
                    package.xpath("./opf:metadata/opf:meta[@property='rendition:layout']/text()", namespaces=ns),
                )
                items = package.xpath("./opf:manifest/opf:item", namespaces=ns)
                self.assertEqual(1, sum("nav" in (item.get("properties") or "").split() for item in items))
                self.assertEqual(1, sum("mathml" in (item.get("properties") or "").split() for item in items))
                content = etree.fromstring(archive.read("OEBPS/generated/book.xhtml"))
                math = content.xpath(
                    ".//*[namespace-uri()=$ns and local-name()='math']", ns=builder.MATHML_NS
                )
                self.assertEqual(1, len(math))
                nav = etree.fromstring(archive.read("OEBPS/nav.xhtml"))
                hrefs = nav.xpath(
                    ".//x:nav[@epub:type='toc']//x:a/@href",
                    namespaces={"x": builder.XHTML_NS, "epub": builder.EPUB_NS},
                )
                self.assertEqual(
                    [
                        "generated/book.xhtml#unit-OLP-0001",
                        "generated/book.xhtml#unit-OLP-0002",
                    ],
                    hrefs,
                )
            replay = builder.audit_epub_bytes(first, plan=first_plan, register="nl-standard")
            self.assertEqual("PASS", replay["status"])

    def test_no_mathml_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(builder.BuildError, "no native MathML"):
                self.make_epub(temporary, math=False)

    def test_broken_fragment_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(builder.BuildError, "broken EPUB fragment"):
                self.make_epub(temporary, broken_fragment=True)

    def test_manifest_omission_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plan, epub, _ = self.make_epub(temporary)

            def remove_css_item(payload: bytes) -> bytes:
                root = etree.fromstring(payload)
                ns = {"opf": builder.OPF_NS}
                item = root.xpath("./opf:manifest/opf:item[@href='styles/reader.css']", namespaces=ns)[0]
                item.getparent().remove(item)
                return etree.tostring(root, encoding="utf-8", xml_declaration=True, pretty_print=True)

            mutated = replace_zip_entry(epub, "OEBPS/package.opf", remove_css_item)
            with self.assertRaisesRegex(builder.BuildError, "manifest does not exactly cover"):
                builder.audit_epub_bytes(mutated, plan=plan, register="nl-standard")

    def test_broken_css_resource_link_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FixtureRepository(Path(temporary) / "repo")
            plan = fixture.plan(("nl-standard",))
            generated = generated_book(Path(temporary) / "generated")
            path = generated / "book.html"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "</head>", '<link rel="stylesheet" href="book.css"/></head>'
                ),
                encoding="utf-8",
                newline="\n",
            )
            (generated / "book.css").write_text(
                "body { background-image: url(missing.png); }\n",
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaisesRegex(builder.BuildError, "broken CSS resource link"):
                builder.package_make4ht_output(
                    plan, "nl-standard", "2026-09-19", generated
                )

    def test_mimetype_compression_and_order_are_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plan, epub, _ = self.make_epub(temporary)
            with zipfile.ZipFile(io.BytesIO(epub)) as source:
                entries = {item.filename: source.read(item.filename) for item in source.infolist()}
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("OEBPS/package.opf", entries.pop("OEBPS/package.opf"))
                archive.writestr("mimetype", entries.pop("mimetype"))
                for name, payload in entries.items():
                    archive.writestr(name, payload)
            with self.assertRaisesRegex(builder.BuildError, "mimetype is not first"):
                builder.audit_epub_bytes(output.getvalue(), plan=plan, register="nl-standard")


class EpubcheckIntegrationTests(unittest.TestCase):
    def validator_distribution(self, root: Path) -> tuple[Path, bytes]:
        directory = root / "epubcheck-5.3.0"
        (directory / "lib").mkdir(parents=True)
        payload = b"hermetic EPUBCheck 5.3.0 fixture"
        jar = directory / "epubcheck.jar"
        jar.write_bytes(payload)
        (directory / "lib" / "fixture-dependency.jar").write_bytes(
            b"hermetic EPUBCheck dependency fixture"
        )
        return jar, payload

    def write_report(
        self,
        path: Path,
        *,
        fatal: int = 0,
        error: int = 0,
        warning: int = 0,
        messages: list[dict[str, object]] | None = None,
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "checker": {
                        "checkerVersion": builder.EPUBCHECK_VERSION,
                        "nFatal": fatal,
                        "nError": error,
                        "nWarning": warning,
                    },
                    "messages": [] if messages is None else messages,
                }
            ),
            encoding="utf-8",
        )

    def test_epubcheck_receipt_binds_exact_checked_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jar, jar_payload = self.validator_distribution(root)
            runtime_sha = builder.epubcheck_runtime_identity(jar)["tree_sha256"]
            epub = b"exact deterministic EPUB fixture bytes"
            observed: dict[str, object] = {}

            def fake_job(command, cwd, timeout, log, environment, *, process_label):
                self.assertEqual("EPUBCheck", process_label)
                self.assertIn("--failonwarnings", command)
                self.assertEqual("default", command[command.index("--profile") + 1])
                candidate = Path(command[3])
                observed["candidate"] = candidate.read_bytes()
                report = Path(command[command.index("--json") + 1])
                self.write_report(report)
                log.write_bytes(b"")
                return {"seconds": 0.0}

            with mock.patch.object(
                builder, "EPUBCHECK_JAR_SHA256", digest(jar_payload)
            ), mock.patch.object(
                builder, "EPUBCHECK_RUNTIME_TREE_SHA256", runtime_sha
            ), mock.patch.object(
                builder.shutil, "which", return_value="java.exe"
            ), mock.patch.object(
                builder, "_run_in_windows_job", side_effect=fake_job
            ):
                receipt = builder.run_epubcheck(
                    epub,
                    root / "validation",
                    jar=jar,
                    timeout_seconds=2,
                )
            self.assertEqual(epub, observed["candidate"])
            self.assertEqual(
                {"bytes": len(epub), "sha256": digest(epub)},
                receipt["checked_epub"],
            )
            self.assertEqual(digest(jar_payload), receipt["validator"]["jar_sha256"])
            self.assertEqual(
                runtime_sha, receipt["validator"]["runtime_tree_sha256"]
            )
            self.assertEqual("PASS", receipt["status"])

    def test_epubcheck_nonzero_process_result_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jar, jar_payload = self.validator_distribution(root)
            runtime_sha = builder.epubcheck_runtime_identity(jar)["tree_sha256"]

            def failed_job(*args, **kwargs):
                raise builder.BuildError("EPUBCheck failed with exit code 1")

            with mock.patch.object(
                builder, "EPUBCHECK_JAR_SHA256", digest(jar_payload)
            ), mock.patch.object(
                builder, "EPUBCHECK_RUNTIME_TREE_SHA256", runtime_sha
            ), mock.patch.object(
                builder.shutil, "which", return_value="java.exe"
            ), mock.patch.object(
                builder, "_run_in_windows_job", side_effect=failed_job
            ):
                with self.assertRaisesRegex(builder.BuildError, "exit code 1"):
                    builder.run_epubcheck(
                        b"invalid EPUB",
                        root / "validation",
                        jar=jar,
                        timeout_seconds=2,
                    )

    def test_epubcheck_error_report_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jar, jar_payload = self.validator_distribution(root)
            runtime_sha = builder.epubcheck_runtime_identity(jar)["tree_sha256"]

            def error_job(command, cwd, timeout, log, environment, *, process_label):
                report = Path(command[command.index("--json") + 1])
                self.write_report(
                    report,
                    error=1,
                    messages=[{"ID": "PKG-000", "severity": "ERROR"}],
                )
                log.write_bytes(b"fixture error\n")
                return {"seconds": 0.0}

            with mock.patch.object(
                builder, "EPUBCHECK_JAR_SHA256", digest(jar_payload)
            ), mock.patch.object(
                builder, "EPUBCHECK_RUNTIME_TREE_SHA256", runtime_sha
            ), mock.patch.object(
                builder.shutil, "which", return_value="java.exe"
            ), mock.patch.object(
                builder, "_run_in_windows_job", side_effect=error_job
            ):
                with self.assertRaisesRegex(builder.BuildError, "reported 1 error"):
                    builder.run_epubcheck(
                        b"invalid EPUB",
                        root / "validation",
                        jar=jar,
                        timeout_seconds=2,
                    )

    def test_build_invokes_epubcheck_only_after_internal_package_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FixtureRepository(root / "repo")
            plan = fixture.plan(("nl-standard",))
            generated = generated_book(root / "generated")
            output = root / "output"
            events: list[str] = []
            original_package = builder.package_make4ht_output
            reader_pdf = pdf_fixture()

            def fake_pdf(source_root, tex_name, output_root, **kwargs):
                self.assertEqual([], events)
                events.append("pdf-pass")
                tex = (source_root / tex_name).read_bytes()
                return reader_pdf, fake_pdf_build_report(
                    reader_pdf,
                    tex_name=tex_name,
                    tex=tex,
                    source_tree=dict(kwargs["expected_source_tree"]),
                )

            def fake_make4ht(*args, **kwargs):
                self.assertEqual(["pdf-pass"], events)
                events.append("make4ht-pass")
                return {"output": str(generated), "seconds": 0.0}

            def audited_package(*args, **kwargs):
                self.assertEqual(["pdf-pass", "make4ht-pass"], events)
                epub, audit = original_package(*args, **kwargs)
                self.assertEqual("PASS", audit["status"])
                events.append("package-audit-pass")
                return epub, audit

            def checked_epub(epub, work_root, *, jar, timeout_seconds):
                self.assertEqual(
                    ["pdf-pass", "make4ht-pass", "package-audit-pass"], events
                )
                events.append("epubcheck-pass")
                return {
                    "schema": "openlogic-partial-epubcheck-receipt-v1",
                    "status": "PASS",
                    "validator": {
                        "name": "EPUBCheck",
                        "version": builder.EPUBCHECK_VERSION,
                        "jar_bytes": 1,
                        "jar_sha256": "0" * 64,
                    },
                    "invocation": {"profile": "default", "fail_on_warnings": True},
                    "checked_epub": {"bytes": len(epub), "sha256": digest(epub)},
                    "result": {"fatal": 0, "error": 0, "warning": 0, "messages": 0},
                }

            with mock.patch.object(
                builder, "run_pdf_build", side_effect=fake_pdf
            ), mock.patch.object(
                builder, "run_make4ht", side_effect=fake_make4ht
            ), mock.patch.object(
                builder, "package_make4ht_output", side_effect=audited_package
            ), mock.patch.object(
                builder, "run_epubcheck", side_effect=checked_epub
            ):
                receipt = builder.build_register(
                    plan,
                    "nl-standard",
                    output,
                    "2026-09-19",
                    mutex_timeout_seconds=1,
                    tex_timeout_seconds=2,
                    epubcheck_jar=Path("unused-epubcheck.jar"),
                    epubcheck_timeout_seconds=3,
                )
            self.assertEqual(
                ["pdf-pass", "make4ht-pass", "package-audit-pass", "epubcheck-pass"],
                events,
            )
            self.assertEqual(
                "BUILT_STRUCTURALLY_AUDITED_EPUBCHECKED_VISUAL_QA_PENDING",
                receipt["status"],
            )
            self.assertEqual(list(builder.ARTIFACT_ORDER), receipt["artifact_order"])
            self.assertEqual(list(builder.ARTIFACT_ORDER), list(receipt["artifacts"]))
            self.assertEqual("PENDING_NOT_PERFORMED", receipt["render_visual_qa"]["status"])
            self.assertEqual(
                receipt["artifacts"]["epub"]["sha256"],
                receipt["epubcheck"]["checked_epub"]["sha256"],
            )
            pdf_path = output / "nl-standard" / receipt["artifacts"]["pdf"]["path"]
            self.assertEqual(reader_pdf, pdf_path.read_bytes())
            self.assertEqual(digest(reader_pdf), receipt["artifacts"]["pdf"]["sha256"])
            self.assertEqual(
                receipt["artifacts"]["pdf"],
                receipt["render_visual_qa"]["bound_pdf"],
            )
            self.assertEqual(
                receipt["source_binding"]["tree"],
                receipt["pdf_build"]["input"]["source_tree"],
            )
            self.assertTrue(receipt["source_binding"]["source_zip_replays_tree_exactly"])
            self.assertEqual(
                receipt["artifacts"]["latex"],
                receipt["pdf_build"]["input"]["cumulative_tex"],
            )
            check_path = (
                output
                / "nl-standard"
                / receipt["artifacts"]["epubcheck_receipt"]["path"]
            )
            check_payload = check_path.read_bytes()
            self.assertEqual(
                receipt["artifacts"]["epubcheck_receipt"]["sha256"],
                digest(check_payload),
            )
            self.assertEqual(receipt["epubcheck"], json.loads(check_payload))

    def test_build_commits_nothing_when_epubcheck_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FixtureRepository(root / "repo")
            plan = fixture.plan(("nl-standard",))
            generated = generated_book(root / "generated")
            output = root / "output"
            reader_pdf = pdf_fixture()

            def fake_pdf(source_root, tex_name, output_root, **kwargs):
                tex = (source_root / tex_name).read_bytes()
                return reader_pdf, fake_pdf_build_report(
                    reader_pdf,
                    tex_name=tex_name,
                    tex=tex,
                    source_tree=dict(kwargs["expected_source_tree"]),
                )

            def fake_make4ht(*args, **kwargs):
                return {"output": str(generated), "seconds": 0.0}

            with mock.patch.object(
                builder, "run_pdf_build", side_effect=fake_pdf
            ), mock.patch.object(
                builder, "run_make4ht", side_effect=fake_make4ht
            ), mock.patch.object(
                builder,
                "run_epubcheck",
                side_effect=builder.BuildError("EPUBCheck failed with exit code 1"),
            ):
                with self.assertRaisesRegex(builder.BuildError, "exit code 1"):
                    builder.build_register(
                        plan,
                        "nl-standard",
                        output,
                        "2026-09-19",
                        mutex_timeout_seconds=1,
                        tex_timeout_seconds=2,
                        epubcheck_jar=Path("unused-epubcheck.jar"),
                        epubcheck_timeout_seconds=3,
                    )
            self.assertFalse((output / "nl-standard").exists())


class AtomicCommitTests(unittest.TestCase):
    def test_partial_existing_output_is_refused_before_any_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FixtureRepository(root / "repo")
            plan = fixture.plan()
            output = root / "release"
            partial = output / "nl-standard"
            partial.mkdir(parents=True)
            (partial / f"{builder.output_basename(plan, 'nl-standard')}.pdf").write_bytes(
                pdf_fixture()
            )
            with mock.patch.object(
                builder,
                "prepare_register",
                side_effect=AssertionError("build ran despite partial output"),
            ):
                with self.assertRaisesRegex(builder.BuildError, "partial or unexpected"):
                    builder.build_release(
                        plan,
                        output,
                        "2026-09-19",
                        mutex_timeout_seconds=1,
                        tex_timeout_seconds=2,
                        pdf_timeout_seconds=2,
                        epubcheck_jar=Path("unused.jar"),
                        epubcheck_timeout_seconds=2,
                    )
            self.assertEqual(1, len(list(output.rglob("*.*"))))

    def test_paired_release_failure_commits_neither_register(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FixtureRepository(root / "repo")
            plan = fixture.plan()
            output = root / "release"
            first = builder.PreparedRegister(
                register="nl-standard",
                receipt={"status": "fixture"},
                files={"fixture": b"not committed"},
            )

            def fake_prepare(plan_arg, register, work_root, modified, **kwargs):
                if register == "nl-standard":
                    work_root.mkdir(parents=True)
                    return first
                raise builder.BuildError("second register fixture failure")

            with mock.patch.object(
                builder, "prepare_register", side_effect=fake_prepare
            ):
                with self.assertRaisesRegex(builder.BuildError, "second register"):
                    builder.build_release(
                        plan,
                        output,
                        "2026-09-19",
                        mutex_timeout_seconds=1,
                        tex_timeout_seconds=2,
                        pdf_timeout_seconds=2,
                        epubcheck_jar=Path("unused.jar"),
                        epubcheck_timeout_seconds=2,
                    )
            self.assertFalse(output.exists())

    def test_atomic_commit_writes_exact_selected_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = (
                builder.PreparedRegister(
                    register="nl-standard",
                    receipt={"register": "nl-standard"},
                    files={"a.pdf": b"standard-pdf", "a.tex": b"standard-tex"},
                ),
                builder.PreparedRegister(
                    register="nl-gewoon",
                    receipt={"register": "nl-gewoon"},
                    files={"b.pdf": b"ordinary-pdf", "b.tex": b"ordinary-tex"},
                ),
            )
            output = root / "release"
            self.assertEqual(
                "COMMITTED_ATOMICALLY",
                builder._commit_release_bundle(output, prepared),
            )
            self.assertEqual(
                {
                    "nl-standard/a.pdf",
                    "nl-standard/a.tex",
                    "nl-gewoon/b.pdf",
                    "nl-gewoon/b.tex",
                },
                builder._release_file_paths(output),
            )
            self.assertEqual(
                "EXISTING_IDENTICAL",
                builder._commit_release_bundle(output, prepared),
            )
            mutated = (
                prepared[0],
                builder.PreparedRegister(
                    register="nl-gewoon",
                    receipt={"register": "nl-gewoon"},
                    files={"b.pdf": b"changed", "b.tex": b"ordinary-tex"},
                ),
            )
            with self.assertRaisesRegex(builder.BuildError, "non-identical"):
                builder._commit_release_bundle(output, mutated)
            self.assertEqual(b"ordinary-pdf", (output / "nl-gewoon" / "b.pdf").read_bytes())


class MutexAndInvocationTests(unittest.TestCase):
    def mocked_windows_job_runtime(
        self, *, assign_result: bool = True, resume_result: int = 1
    ):
        events: list[str] = []

        class FakeProcess:
            _handle = 202
            pid = 404

            def __init__(self):
                self.returncode = None

            def poll(self):
                return self.returncode

            def kill(self):
                events.append("kill-primary")
                self.returncode = -9

            def wait(self, timeout=None):
                events.append("wait-primary")
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

        process = FakeProcess()

        class FakeKernel32:
            def __init__(self):
                self.CreateJobObjectW = mock.Mock(return_value=101)
                self.SetInformationJobObject = mock.Mock(return_value=True)
                self.AssignProcessToJobObject = mock.Mock(side_effect=self.assign)
                self.QueryInformationJobObject = mock.Mock(side_effect=self.query)
                self.WaitForSingleObject = mock.Mock(return_value=0)
                self.TerminateJobObject = mock.Mock(side_effect=self.terminate)
                self.ResumeThread = mock.Mock(side_effect=self.resume)
                self.CloseHandle = mock.Mock(side_effect=self.close)

            def assign(self, job, process_handle):
                events.append("assign-job")
                return assign_result

            def query(self, job, info_class, accounting, size, returned):
                events.append("query-job-empty")
                accounting._obj.ActiveProcesses = 0
                return True

            def terminate(self, job, exit_code):
                events.append("terminate-job")
                return True

            def resume(self, thread):
                events.append("resume-primary")
                return resume_result

            def close(self, handle):
                value = handle.value if hasattr(handle, "value") else int(handle)
                events.append(f"close-{value}")
                return True

        kernel32 = FakeKernel32()
        observed_popen: dict[str, object] = {}

        def fake_popen(*args, **kwargs):
            events.append("popen-suspended")
            observed_popen.update(kwargs)
            return process

        def fake_open_primary(kernel, process_id):
            self.assertIs(kernel32, kernel)
            self.assertEqual(process.pid, process_id)
            events.append("open-primary")
            return 303, 405

        return events, process, kernel32, observed_popen, fake_popen, fake_open_primary

    @unittest.skipUnless(os.name == "nt", "Windows named mutex contract")
    def test_mutex_times_out_for_another_thread_and_releases_in_finally(self) -> None:
        name = "Local\\OpenLogicPartialReaderTest-" + uuid.uuid4().hex
        acquired = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def holder() -> None:
            try:
                with builder.GlobalTexMutex(2, name=name):
                    acquired.set()
                    release.wait(2)
            except BaseException as exc:  # pragma: no cover - assertion aid
                errors.append(exc)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(acquired.wait(1))
        with self.assertRaisesRegex(builder.BuildError, "timed out acquiring"):
            with builder.GlobalTexMutex(0.05, name=name):
                pass
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        with self.assertRaises(RuntimeError):
            with builder.GlobalTexMutex(1, name=name):
                raise RuntimeError("exercise finally release")
        with builder.GlobalTexMutex(1, name=name):
            pass

    def test_make4ht_planning_occurs_inside_mutex_without_launching_tex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "book.tex").write_text("fixture", encoding="utf-8")
            state = {"inside": False, "job_called": False}

            class FakeMutex:
                abandoned = False

                def __init__(self, timeout: float):
                    self.timeout = timeout

                def __enter__(self):
                    state["inside"] = True
                    return self

                def __exit__(self, *args):
                    state["inside"] = False

            def fake_job(command, cwd, timeout, log, environment):
                self.assertTrue(state["inside"])
                self.assertIn("mathml", command)
                self.assertEqual(source, cwd)
                self.assertEqual("1789776000", environment["SOURCE_DATE_EPOCH"])
                self.assertEqual("1", environment["FORCE_SOURCE_DATE"])
                log.write_bytes(b"make4ht fixture success\n")
                state["job_called"] = True
                return {"seconds": 0.0}

            with mock.patch.object(builder.shutil, "which", return_value="make4ht.exe"), mock.patch.object(
                builder, "GlobalTexMutex", FakeMutex
            ), mock.patch.object(builder, "_run_in_windows_job", side_effect=fake_job):
                report = builder.run_make4ht(
                    source,
                    "book.tex",
                    root / "output",
                    modified="2026-09-19",
                    mutex_timeout_seconds=1,
                    tex_timeout_seconds=2,
                )
            self.assertTrue(state["job_called"])
            self.assertFalse(state["inside"])
            self.assertEqual(builder.TEX_MUTEX_NAME, report["mutex"])
            self.assertEqual("make4ht", report["command"][0])
            self.assertIn("<MAKE4HT_BUILD_DIRECTORY>", report["command"])
            self.assertNotIn(str(root), json.dumps(report))
            self.assertEqual(
                digest(b"make4ht fixture success\n"), report["log_sha256"]
            )

    @unittest.skipUnless(os.name == "nt", "Windows job-object contract")
    def test_windows_job_nonzero_epubcheck_process_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(
                builder.BuildError, "EPUBCheck failed with exit code 7"
            ):
                builder._run_in_windows_job(
                    [sys.executable, "-c", "raise SystemExit(7)"],
                    root,
                    5,
                    root / "epubcheck.log",
                    os.environ.copy(),
                    process_label="EPUBCheck",
                )

    @unittest.skipUnless(os.name == "nt", "Windows suspended-process contract")
    def test_job_launch_assigns_suspended_process_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                events,
                _,
                kernel32,
                observed_popen,
                fake_popen,
                fake_open_primary,
            ) = self.mocked_windows_job_runtime()
            with mock.patch.object(
                builder.ctypes, "WinDLL", return_value=kernel32
            ), mock.patch.object(
                builder.subprocess, "Popen", side_effect=fake_popen
            ), mock.patch.object(
                builder, "_open_suspended_primary_thread", side_effect=fake_open_primary
            ):
                result = builder._run_in_windows_job(
                    ["fixture.exe"],
                    root,
                    2,
                    root / "fixture.log",
                    {},
                    process_label="fixture",
                )
            self.assertEqual(0, result["exit_code"])
            self.assertEqual(
                list(builder.WINDOWS_JOB_LAUNCH_CAPTURE), result["launch_capture"]
            )
            self.assertTrue(
                int(observed_popen["creationflags"])
                & builder.WINDOWS_CREATE_SUSPENDED
            )
            self.assertLess(events.index("assign-job"), events.index("open-primary"))
            self.assertLess(events.index("open-primary"), events.index("resume-primary"))
            self.assertIn("close-303", events)
            self.assertIn("close-101", events)
            self.assertNotIn("terminate-job", events)
            self.assertNotIn("kill-primary", events)

    @unittest.skipUnless(os.name == "nt", "Windows suspended-process contract")
    def test_job_assignment_failure_never_resumes_and_kills_primary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                events,
                _,
                kernel32,
                observed_popen,
                fake_popen,
                fake_open_primary,
            ) = self.mocked_windows_job_runtime(assign_result=False)
            open_primary = mock.Mock(side_effect=fake_open_primary)
            with mock.patch.object(
                builder.ctypes, "WinDLL", return_value=kernel32
            ), mock.patch.object(
                builder.subprocess, "Popen", side_effect=fake_popen
            ), mock.patch.object(
                builder, "_open_suspended_primary_thread", open_primary
            ):
                with self.assertRaisesRegex(builder.BuildError, "AssignProcessToJobObject"):
                    builder._run_in_windows_job(
                        ["fixture.exe"],
                        root,
                        2,
                        root / "fixture.log",
                        {},
                        process_label="fixture",
                    )
            self.assertTrue(
                int(observed_popen["creationflags"])
                & builder.WINDOWS_CREATE_SUSPENDED
            )
            open_primary.assert_not_called()
            kernel32.ResumeThread.assert_not_called()
            self.assertIn("kill-primary", events)
            self.assertIn("wait-primary", events)
            self.assertIn("close-101", events)
            self.assertNotIn("terminate-job", events)

    @unittest.skipUnless(os.name == "nt", "Windows suspended-process contract")
    def test_resume_failure_closes_thread_and_terminates_assigned_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                events,
                _,
                kernel32,
                observed_popen,
                fake_popen,
                fake_open_primary,
            ) = self.mocked_windows_job_runtime(resume_result=0xFFFFFFFF)
            with mock.patch.object(
                builder.ctypes, "WinDLL", return_value=kernel32
            ), mock.patch.object(
                builder.subprocess, "Popen", side_effect=fake_popen
            ), mock.patch.object(
                builder, "_open_suspended_primary_thread", side_effect=fake_open_primary
            ):
                with self.assertRaisesRegex(builder.BuildError, "ResumeThread failed"):
                    builder._run_in_windows_job(
                        ["fixture.exe"],
                        root,
                        2,
                        root / "fixture.log",
                        {},
                        process_label="fixture",
                    )
            self.assertTrue(
                int(observed_popen["creationflags"])
                & builder.WINDOWS_CREATE_SUSPENDED
            )
            self.assertLess(events.index("assign-job"), events.index("resume-primary"))
            self.assertLess(events.index("resume-primary"), events.index("close-303"))
            self.assertLess(events.index("close-303"), events.index("terminate-job"))
            self.assertIn("wait-primary", events)
            self.assertIn("close-101", events)
            self.assertNotIn("kill-primary", events)

    def test_fatal_tex_marker_fails_before_mutex_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "book.tex").write_text("fixture", encoding="utf-8")
            state = {"inside": False, "released": False}

            class FakeMutex:
                abandoned = False

                def __init__(self, timeout: float):
                    self.timeout = timeout

                def __enter__(self):
                    state["inside"] = True
                    return self

                def __exit__(self, *args):
                    state["inside"] = False
                    state["released"] = True

            def fake_job(command, cwd, timeout, log, environment):
                self.assertTrue(state["inside"])
                log.write_bytes(b"! LaTeX Error: fixture failure\n")
                return {"seconds": 0.0}

            with mock.patch.object(builder.shutil, "which", return_value="make4ht.exe"), mock.patch.object(
                builder, "GlobalTexMutex", FakeMutex
            ), mock.patch.object(builder, "_run_in_windows_job", side_effect=fake_job):
                with self.assertRaisesRegex(builder.BuildError, "fatal TeX marker"):
                    builder.run_make4ht(
                        source,
                        "book.tex",
                        root / "output",
                        modified="2026-09-19",
                        mutex_timeout_seconds=1,
                        tex_timeout_seconds=2,
                    )
            self.assertTrue(state["released"])
            self.assertFalse(state["inside"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
