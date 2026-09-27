#!/usr/bin/env python3
"""Build the bounded Dutch cumulative source and EPUB 3 without TeX.

Inclusion is governed only by the contiguous accepted prefix in
``alignment/UNITS.jsonl``.  The script creates the directly downloadable
cumulative LaTeX, a complete deterministic source ZIP, and a genuine
reflowable EPUB 3 with native MathML.  Pandoc conversion is replayed twice and
must be byte-identical.  EPUBCheck must then report zero messages.  This path
does not launch a TeX engine and does not acquire ``Global\\InterlanguageTeXSlotV1``.
"""

from __future__ import annotations

import argparse
import calendar
import copy
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence
from urllib.parse import unquote, urlsplit

from lxml import etree, html

import build_partial_epub3 as core
from epub3.direct_latex import AI_DISCLOSURE_NL as DIRECT_AI_DISCLOSURE_NL
from epub3.direct_latex import DirectDocument, make_direct_document


PANDOC_REPLAY_COUNT = 2
PANDOC_FROM = "latex"
PANDOC_TO = "html5"
ARTIFACT_ORDER = (
    "latex",
    "source_zip",
    "epub",
    "epubcheck_receipt",
    "receipt",
)
VISIBLE_TEX_COMMAND_RE = re.compile(r"\\[A-Za-z@]+")
GENERIC_IMAGE_ALT = {"", "image", "afbeelding", "diagram"}


def accepted_unit_bodies(
    plan: core.AcceptedPlan, register: str
) -> list[tuple[str, str]]:
    """Read the exact accepted target bodies in authoritative unit order."""
    core.require(register in plan.registers, f"register absent from plan: {register}")
    config = core.REGISTER_CONFIG[register]
    result: list[tuple[str, str]] = []
    for row in plan.accepted_units:
        relative = core.safe_relative_path(
            str(row[config["path_key"]]), label="translation path"
        )
        path = plan.repo_root / Path(relative.as_posix())
        result.append(
            (
                str(row["unit_id"]),
                core.stripped_unit_body(path.read_text(encoding="utf-8")),
            )
        )
    return result


def direct_document(
    plan: core.AcceptedPlan,
    register: str,
    modified: str,
) -> DirectDocument:
    core.require(
        DIRECT_AI_DISCLOSURE_NL == core.AI_DISCLOSURE_NL,
        "reader AI disclosure drifted between cumulative and direct sources",
    )
    config = core.REGISTER_CONFIG[register]
    locale = (
        plan.repo_root
        / "editions"
        / register
        / "support"
        / "open-logic-reader-locale.tex"
    )
    core.require(locale.is_file(), f"missing Dutch reader locale: {locale}")
    return make_direct_document(
        accepted_unit_bodies(plan, register),
        locale_path=locale,
        register=register,
        scope_title=(
            "gedeeltelijke Nederlandstalige editie "
            f"({config['label']}, {plan.first_id}–{plan.last_id})"
        ),
        modified=modified,
    )


def _pandoc_version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise core.BuildError(f"Pandoc version probe failed: {exc}") from exc
    core.require(result.returncode == 0, "Pandoc version probe returned nonzero")
    try:
        lines = result.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise core.BuildError("Pandoc version output is not UTF-8") from exc
    core.require(bool(lines and lines[0].strip()), "Pandoc version output is empty")
    return lines[0].strip()


def _visible_copy(document: etree._Element) -> etree._Element:
    result = copy.deepcopy(document)
    for node in result.xpath(
        ".//*[local-name()='annotation' and @encoding='application/x-tex']"
    ):
        parent = node.getparent()
        core.require(parent is not None, "orphan TeX annotation in Pandoc output")
        parent.remove(node)
    return result


def audit_pandoc_html(
    plan: core.AcceptedPlan,
    register: str,
    generated_root: Path,
    document: DirectDocument,
) -> dict[str, object]:
    """Bind one Pandoc HTML output to all accepted inputs and accessibility gates."""
    html_path = generated_root / "book.html"
    log_path = generated_root / "pandoc.log"
    core.require(html_path.is_file(), "Pandoc emitted no book.html")
    core.require(log_path.is_file(), "Pandoc process log is missing")
    log = log_path.read_bytes()
    if log:
        try:
            rendered_log = log.decode("utf-8")
        except UnicodeDecodeError:
            rendered_log = repr(log[-4000:])
        raise core.BuildError(
            "Pandoc emitted warnings or other process output: " + rendered_log[-4000:]
        )
    payload = html_path.read_bytes()
    try:
        raw = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise core.BuildError("Pandoc HTML is not UTF-8") from exc
    parsed = html.document_fromstring(
        payload,
        parser=html.HTMLParser(
            encoding="utf-8", remove_comments=False, recover=True, huge_tree=True
        ),
    )
    core.require(core._local_name(str(parsed.tag)) == "html", "Pandoc output lacks html root")
    core.require(parsed.get("lang") == "nl-NL", "Pandoc HTML language is not nl-NL")
    core.require(not parsed.xpath(".//*[local-name()='script']"), "script in Pandoc HTML")

    identifiers = [str(value) for value in parsed.xpath("//@id") if value]
    identifier_set = set(identifiers)
    core.require(len(identifiers) == len(identifier_set), "duplicate ID in Pandoc HTML")
    start_units = [
        match.group(1)
        for identifier in identifiers
        if (match := re.fullmatch(r"unit-(OLP-\d{4})", identifier))
    ]
    end_units = [
        match.group(1)
        for identifier in identifiers
        if (match := re.fullmatch(r"unit-end-(OLP-\d{4})", identifier))
    ]
    expected = list(plan.accepted_ids)
    core.require(start_units == expected, "Pandoc start-anchor order drift")
    core.require(end_units == expected, "Pandoc end-anchor order drift")

    segment_records: list[dict[str, object]] = []
    previous_end = -1
    for unit_id in expected:
        start_marker = f'id="unit-{unit_id}"'
        end_marker = f'id="unit-end-{unit_id}"'
        start = raw.find(start_marker)
        end = raw.find(end_marker, start + len(start_marker))
        core.require(start > previous_end and end > start, f"raw anchor order drift: {unit_id}")
        previous_end = end
        fragment = raw[start + len(start_marker) : end]
        try:
            text = " ".join(
                html.fromstring("<div>" + fragment + "</div>").text_content().split()
            )
        except (etree.ParserError, etree.XMLSyntaxError) as exc:
            raise core.BuildError(f"cannot parse HTML segment {unit_id}: {exc}") from exc
        core.require(bool(text), f"empty rendered unit segment: {unit_id}")
        segment_records.append(
            {
                "unit_id": unit_id,
                "normalized_input_sha256": document.unit_input_sha256[unit_id],
                "rendered_html_bytes": len(fragment.encode("utf-8")),
                "rendered_html_sha256": core.sha256_bytes(fragment.encode("utf-8")),
                "rendered_text_bytes": len(text.encode("utf-8")),
                "rendered_text_sha256": core.sha256_bytes(text.encode("utf-8")),
            }
        )

    internal_links = [
        str(value) for value in parsed.xpath(".//a[starts-with(@href, '#')]/@href")
    ]
    broken = sorted(
        {
            unquote(value[1:])
            for value in internal_links
            if unquote(value[1:]) not in identifier_set
        }
    )
    core.require(not broken, f"broken Pandoc internal links: {broken[:10]}")

    math_roots = parsed.xpath(".//*[local-name()='math']")
    core.require(bool(math_roots), "Pandoc HTML contains no native MathML")
    for number, math in enumerate(math_roots, start=1):
        annotations = math.xpath(
            ".//*[local-name()='annotation' and @encoding='application/x-tex']"
        )
        core.require(
            len(annotations) == 1 and bool("".join(annotations[0].itertext()).strip()),
            f"MathML expression {number} lacks exactly one nonempty TeX annotation",
        )

    visible = _visible_copy(parsed)
    visible_text = "\n".join(
        str(value) for value in visible.xpath("//text()") if str(value).strip()
    )
    leaks = sorted(set(VISIBLE_TEX_COMMAND_RE.findall(visible_text)))
    core.require(not leaks, f"visible raw TeX command leakage: {leaks[:10]}")
    core.require("!!" not in visible_text, "visible unresolved Dutch token marker")

    images = parsed.xpath(".//img")
    core.require(
        len(images) == int(document.metrics["diagrams_converted_to_svg"]),
        "Pandoc image count differs from generated diagram count",
    )
    image_records: list[dict[str, object]] = []
    observed_assets: set[str] = set()
    for image in images:
        source_value = image.get("src") or ""
        split = urlsplit(source_value)
        core.require(
            not split.scheme and not split.netloc and not source_value.startswith("//"),
            f"nonlocal Pandoc image: {source_value}",
        )
        source = unquote(split.path)
        relative = core.safe_relative_path(source, label="Pandoc image source")
        core.require(len(relative.parts) == 1, f"nested generated image path: {source}")
        name = relative.as_posix()
        core.require(name not in observed_assets, f"duplicate rendered image: {name}")
        observed_assets.add(name)
        core.require(name in document.assets, f"unbound rendered image: {name}")
        path = generated_root / Path(name)
        core.require(path.is_file(), f"missing rendered image: {name}")
        svg_payload = path.read_bytes()
        core.require(svg_payload == document.assets[name], f"rendered image bytes drifted: {name}")
        svg = core._parse_xml(svg_payload, name)
        core.require(core._local_name(str(svg.tag)) == "svg", f"non-SVG diagram: {name}")
        titles = svg.xpath(".//*[local-name()='title' and normalize-space()]")
        descriptions = svg.xpath(".//*[local-name()='desc' and normalize-space()]")
        core.require(len(titles) == 1, f"SVG lacks exactly one title: {name}")
        core.require(len(descriptions) == 1, f"SVG lacks exactly one description: {name}")
        core.require(not svg.xpath(".//*[local-name()='script']"), f"script in SVG: {name}")
        for node in svg.iter():
            if not isinstance(node.tag, str):
                continue
            core.require(
                all(not core._local_name(str(key)).startswith("on") for key in node.attrib),
                f"event handler in SVG: {name}",
            )
        alt = (image.get("alt") or "").strip()
        core.require(
            len(alt) >= 12 and alt.lower() not in GENERIC_IMAGE_ALT,
            f"missing or generic Dutch image description: {name}",
        )
        description = " ".join(descriptions[0].itertext()).strip()
        core.require(alt == description, f"HTML alt and SVG description drift: {name}")
        image_records.append(
            {
                "path": name,
                "bytes": len(svg_payload),
                "sha256": core.sha256_bytes(svg_payload),
                "alt": alt,
                "title": " ".join(titles[0].itertext()).strip(),
                "description": description,
            }
        )
    core.require(
        observed_assets == set(document.assets),
        "Pandoc image allowlist differs from generated diagram assets",
    )

    return {
        "schema": "openlogic-direct-pandoc-html-audit-v1",
        "status": "PASS",
        "register": register,
        "html": {
            "path": "book.html",
            "bytes": len(payload),
            "sha256": core.sha256_bytes(payload),
        },
        "pandoc_log": {
            "path": "pandoc.log",
            "bytes": len(log),
            "sha256": core.sha256_bytes(log),
        },
        "units": len(segment_records),
        "ordered_start_anchors": len(start_units),
        "ordered_end_anchors": len(end_units),
        "all_segments_nonempty": True,
        "ids": len(identifiers),
        "duplicate_ids": 0,
        "internal_links": len(internal_links),
        "broken_internal_links": 0,
        "mathml_roots": len(math_roots),
        "tex_annotations": len(math_roots),
        "visible_raw_tex_commands": 0,
        "images": len(image_records),
        "images_with_meaningful_dutch_alt": len(image_records),
        "svg_titles": len(image_records),
        "svg_descriptions": len(image_records),
        "segment_records": segment_records,
        "image_records": image_records,
    }


def run_pandoc_once(
    plan: core.AcceptedPlan,
    register: str,
    document: DirectDocument,
    work_root: Path,
    *,
    modified: str,
    timeout_seconds: float,
    pandoc: str,
) -> dict[str, object]:
    core.require(not work_root.exists(), f"Pandoc work root already exists: {work_root}")
    work_root.mkdir(parents=True)
    (work_root / "direct.tex").write_bytes(document.tex)
    for name, payload in document.assets.items():
        target = work_root / Path(name)
        core.require(target.parent == work_root, f"unsafe direct asset name: {name}")
        target.write_bytes(payload)
    bibliography = plan.repo_root / "upstream" / "bib" / "open-logic.bib"
    core.require(bibliography.is_file(), f"missing Pandoc bibliography: {bibliography}")
    command = [
        pandoc,
        "direct.tex",
        f"--from={PANDOC_FROM}",
        f"--to={PANDOC_TO}",
        "--standalone",
        "--toc",
        "--number-sections",
        "--mathml",
        "--citeproc",
        f"--bibliography={bibliography}",
        "--metadata=lang:nl-NL",
        "--output=book.html",
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "SOURCE_DATE_EPOCH": str(calendar.timegm(time.strptime(modified, "%Y-%m-%d"))),
            "TZ": "UTC",
        }
    )
    process = core._run_in_windows_job(
        command,
        work_root,
        timeout_seconds,
        work_root / "pandoc.log",
        environment,
        process_label="Pandoc",
    )
    core.require(
        (work_root / "direct.tex").read_bytes() == document.tex,
        "Pandoc mutated the direct LaTeX input",
    )
    for name, payload in document.assets.items():
        core.require(
            (work_root / Path(name)).read_bytes() == payload,
            f"Pandoc mutated generated diagram: {name}",
        )
    audit = audit_pandoc_html(plan, register, work_root, document)
    return {
        "command": command,
        "exit_code": process["exit_code"],
        "seconds": process["seconds"],
        "launch_capture": process["launch_capture"],
        "audit": audit,
        "output_tree": core.directory_tree_identity(work_root),
    }


def no_pdf_filenames(plan: core.AcceptedPlan, register: str) -> dict[str, str]:
    base = core.output_basename(plan, register)
    return {
        "latex": f"{base}.tex",
        "source_zip": f"{base}-sources.zip",
        "epub": f"{base}.epub",
        "epubcheck_receipt": f"{base}-EPUBCHECK.json",
        "receipt": f"{base}-BUILD.json",
    }


def expected_release_paths(
    plan: core.AcceptedPlan, registers: Sequence[str]
) -> set[str]:
    return {
        f"{register}/{filename}"
        for register in registers
        for filename in no_pdf_filenames(plan, register).values()
    }


def prepare_register(
    plan: core.AcceptedPlan,
    register: str,
    work_root: Path,
    modified: str,
    *,
    pandoc_timeout_seconds: float,
    epubcheck_jar: Path,
    epubcheck_timeout_seconds: float,
) -> core.PreparedRegister:
    cumulative_tex = core.render_cumulative_tex(plan, register, modified)
    source_entries = core.source_tree_entries(
        plan, register, modified, cumulative_tex
    )
    source_identity = core.byte_tree_identity(source_entries)
    source_zip = core.deterministic_zip_bytes(source_entries)
    with zipfile.ZipFile(io.BytesIO(source_zip)) as archive:
        replayed = {
            item.filename: archive.read(item.filename) for item in archive.infolist()
        }
    core.require(
        replayed == source_entries
        and core.byte_tree_identity(replayed) == source_identity,
        "source ZIP does not replay the exact source tree",
    )

    document = direct_document(plan, register, modified)
    pandoc = shutil.which("pandoc")
    core.require(bool(pandoc), "Pandoc is unavailable")
    pandoc = str(Path(pandoc).resolve())
    pandoc_version = _pandoc_version(pandoc)
    work_root.mkdir(parents=True, exist_ok=False)
    replays = [
        run_pandoc_once(
            plan,
            register,
            document,
            work_root / f"pandoc-replay-{number}",
            modified=modified,
            timeout_seconds=pandoc_timeout_seconds,
            pandoc=pandoc,
        )
        for number in range(1, PANDOC_REPLAY_COUNT + 1)
    ]
    first_tree = replays[0]["output_tree"]
    core.require(
        all(replay["output_tree"] == first_tree for replay in replays[1:]),
        "Pandoc replay output trees are not byte-identical",
    )
    first_html = replays[0]["audit"]["html"]
    core.require(
        all(replay["audit"]["html"] == first_html for replay in replays[1:]),
        "Pandoc replay HTML bytes are not identical",
    )

    generated_root = work_root / "pandoc-replay-1"
    epub, epub_audit = core.package_make4ht_output(
        plan, register, modified, generated_root
    )
    core.require(epub_audit.get("status") == "PASS", "internal EPUB audit failed")
    epubcheck = core.run_epubcheck(
        epub,
        work_root / "epubcheck",
        jar=epubcheck_jar,
        timeout_seconds=epubcheck_timeout_seconds,
    )
    core.require(
        epubcheck["checked_epub"]
        == {"bytes": len(epub), "sha256": core.sha256_bytes(epub)}
        == {"bytes": epub_audit["bytes"], "sha256": epub_audit["sha256"]},
        "internal audit and EPUBCheck did not bind the same EPUB bytes",
    )

    filenames = no_pdf_filenames(plan, register)
    epubcheck_bytes = core.json_bytes(epubcheck)
    receipt = {
        "schema": "openlogic-partial-source-epub-build-receipt-v1",
        "status": "BUILT_REPRODUCIBLY_STRUCTURALLY_AUDITED_EPUBCHECKED_VISUAL_QA_PENDING",
        "register": register,
        "scope": {
            "first": plan.first_id,
            "last": plan.last_id,
            "accepted_units": len(plan.accepted_units),
            "first_excluded": (
                plan.first_excluded["unit_id"] if plan.first_excluded else None
            ),
        },
        "alignment_sha256": plan.alignment_sha256,
        "modified": modified,
        "artifact_order": list(ARTIFACT_ORDER),
        "artifacts": {
            "latex": {
                "path": filenames["latex"],
                "bytes": len(cumulative_tex),
                "sha256": core.sha256_bytes(cumulative_tex),
            },
            "source_zip": {
                "path": filenames["source_zip"],
                "bytes": len(source_zip),
                "sha256": core.sha256_bytes(source_zip),
            },
            "epub": {
                "path": filenames["epub"],
                "bytes": len(epub),
                "sha256": core.sha256_bytes(epub),
            },
            "epubcheck_receipt": {
                "path": filenames["epubcheck_receipt"],
                "bytes": len(epubcheck_bytes),
                "sha256": core.sha256_bytes(epubcheck_bytes),
            },
            "receipt": {
                "path": filenames["receipt"],
                "self_digest_recorded": False,
                "reason": "A receipt cannot embed a stable digest of its own complete bytes.",
            },
        },
        "source_binding": {
            "tree": source_identity,
            "source_zip_replays_tree_exactly": True,
            "cumulative_tex": {
                "path": filenames["latex"],
                "bytes": len(cumulative_tex),
                "sha256": core.sha256_bytes(cumulative_tex),
            },
        },
        "direct_conversion": {
            "schema": "openlogic-direct-pandoc-conversion-v1",
            "status": "PASS_EXACT_BYTE_REPLAY",
            "pandoc": {
                "path": pandoc,
                "version": pandoc_version,
                "from": PANDOC_FROM,
                "to": PANDOC_TO,
            },
            "input": {
                "bytes": len(document.tex),
                "sha256": core.sha256_bytes(document.tex),
                "assets": len(document.assets),
                "unit_input_sha256": dict(document.unit_input_sha256),
                "metrics": dict(document.metrics),
            },
            "replays": replays,
            "repeatability": {
                "runs": PANDOC_REPLAY_COUNT,
                "comparison": "exact generated-tree byte equality",
                "status": "PASS",
            },
        },
        "audit": epub_audit,
        "epubcheck": epubcheck,
        "render_visual_qa": {
            "status": "PENDING_NOT_PERFORMED",
            "performed": False,
            "bound_epub": {
                "bytes": len(epub),
                "sha256": core.sha256_bytes(epub),
            },
        },
        "constraints": {
            "tex_processes_launched": 0,
            "tex_mutex_acquired": False,
            "pdf_included": False,
            "pending_translation_files_included": False,
            "native_mathml_required": True,
            "meaningful_dutch_image_alt_required": True,
            "svg_title_and_description_required": True,
            "epubcheck_required": True,
            "epubcheck_zero_messages_required": True,
            "selected_registers_commit_all_or_nothing": True,
            "publication_performed": False,
        },
    }
    core.require(tuple(receipt["artifacts"]) == ARTIFACT_ORDER, "artifact order drift")
    receipt_bytes = core.json_bytes(receipt)
    files = {
        filenames["latex"]: cumulative_tex,
        filenames["source_zip"]: source_zip,
        filenames["epub"]: epub,
        filenames["epubcheck_receipt"]: epubcheck_bytes,
        filenames["receipt"]: receipt_bytes,
    }
    core.require(
        tuple(files) == tuple(filenames[key] for key in ARTIFACT_ORDER),
        "release file order drift",
    )
    return core.PreparedRegister(register=register, receipt=receipt, files=files)


def plan_report(
    plan: core.AcceptedPlan, output_dir: Path, modified: str | None
) -> dict[str, object]:
    return {
        "schema": "openlogic-partial-source-epub-build-plan-v1",
        "status": "DRY_RUN_VALIDATED" if modified is None else "READY",
        "alignment": {
            "path": str(plan.alignment_path),
            "bytes": plan.alignment_path.stat().st_size,
            "sha256": plan.alignment_sha256,
        },
        "scope": {
            "first": plan.first_id,
            "last": plan.last_id,
            "accepted_units": len(plan.accepted_units),
            "first_excluded": (
                plan.first_excluded["unit_id"] if plan.first_excluded else None
            ),
        },
        "registers": list(plan.registers),
        "output_dir": str(output_dir.resolve()),
        "artifact_order": list(ARTIFACT_ORDER),
        "expected_paths": sorted(expected_release_paths(plan, plan.registers)),
        "conversion": {
            "pandoc_direct": True,
            "replays": PANDOC_REPLAY_COUNT,
            "native_mathml": True,
            "epubcheck": core.EPUBCHECK_VERSION,
            "tex_processes": 0,
            "tex_mutex_used": False,
            "pdf_included": False,
        },
        "modified": modified,
        "publication_performed": False,
    }


def build_release(
    plan: core.AcceptedPlan,
    output_dir: Path,
    modified: str,
    *,
    pandoc_timeout_seconds: float,
    epubcheck_jar: Path,
    epubcheck_timeout_seconds: float,
) -> tuple[list[dict[str, object]], str]:
    expected = expected_release_paths(plan, plan.registers)
    core.preflight_output_bundle(output_dir, expected)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.source-epub-build-", dir=output_dir.parent
    ) as temporary_name:
        temporary = Path(temporary_name)
        prepared = [
            prepare_register(
                plan,
                register,
                temporary / register,
                modified,
                pandoc_timeout_seconds=pandoc_timeout_seconds,
                epubcheck_jar=epubcheck_jar,
                epubcheck_timeout_seconds=epubcheck_timeout_seconds,
            )
            for register in plan.registers
        ]
        commit = core._commit_release_bundle(output_dir, prepared)
    return [item.receipt for item in prepared], commit


def _selected_registers(value: str) -> tuple[str, ...]:
    return tuple(core.REGISTER_CONFIG) if value == "both" else (value,)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--register", choices=("both", *core.REGISTER_CONFIG), default="both"
    )
    parser.add_argument("--modified", default=time.strftime("%Y-%m-%d"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pandoc-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--epubcheck-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--epubcheck-jar", type=Path)
    args = parser.parse_args(argv)
    try:
        registers = _selected_registers(args.register)
        plan = core.plan_accepted_prefix(args.repo_root, registers)
        output_dir = args.output_dir.resolve()
        core.require(args.pandoc_timeout_seconds > 0, "Pandoc timeout must be positive")
        core.require(
            args.epubcheck_timeout_seconds > 0,
            "EPUBCheck timeout must be positive",
        )
        if args.dry_run:
            print(
                json.dumps(
                    plan_report(plan, output_dir, None),
                    ensure_ascii=True,
                    sort_keys=True,
                    indent=2,
                )
            )
            return 0
        modified = core.parse_modified(args.modified)
        jar = core.resolve_epubcheck_jar(plan.repo_root, args.epubcheck_jar)
        report = plan_report(plan, output_dir, modified)
        receipts, commit = build_release(
            plan,
            output_dir,
            modified,
            pandoc_timeout_seconds=args.pandoc_timeout_seconds,
            epubcheck_jar=jar,
            epubcheck_timeout_seconds=args.epubcheck_timeout_seconds,
        )
        report.update(
            {
                "status": "PASS_BUILT_ATOMICALLY",
                "commit": commit,
                "receipts": receipts,
            }
        )
        print(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2))
        return 0
    except (core.BuildError, OSError, UnicodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
