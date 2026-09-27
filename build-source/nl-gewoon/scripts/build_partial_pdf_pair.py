#!/usr/bin/env python3
"""Build the paired accepted-prefix Dutch PDFs under one global TeX mutex hold.

The input is an already-built paired source/EPUB bundle.  Each PDF is compiled
twice from the exact source ZIP bytes and must replay byte-for-byte.  One outer
``Global\\InterlanguageTeXSlotV1`` acquisition covers all version probes and all
four compiler process trees; nothing is committed unless both registers pass.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import build_partial_epub3 as core


REGISTERS = ("nl-standard", "nl-gewoon")


@dataclass(frozen=True)
class RegisterInput:
    register: str
    receipt_path: Path
    receipt_bytes: bytes
    receipt: dict[str, object]
    source_zip_path: Path
    source_zip_bytes: bytes
    direct_tex_path: Path
    direct_tex_bytes: bytes
    epub_path: Path
    epub_bytes: bytes
    source_entries: dict[str, bytes]
    source_identity: dict[str, object]
    tex_name: str


def _artifact_path(register_root: Path, record: object, label: str) -> Path:
    core.require(isinstance(record, dict), f"{label} artifact record is not an object")
    relative = core.safe_relative_path(str(record.get("path") or ""), label=label)
    core.require(len(relative.parts) == 1, f"{label} artifact must be at register root")
    path = register_root / relative.name
    core.require(path.is_file(), f"missing {label} artifact: {path}")
    payload = path.read_bytes()
    core.require(
        record.get("bytes") == len(payload)
        and record.get("sha256") == core.sha256_bytes(payload),
        f"{label} artifact identity differs from its build receipt",
    )
    return path


def _read_source_zip(payload: bytes) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for info in archive.infolist():
            core.require(not info.is_dir(), f"source ZIP contains a directory entry: {info.filename}")
            relative = core.safe_relative_path(info.filename, label="source ZIP entry")
            name = relative.as_posix()
            core.require(name not in entries, f"duplicate source ZIP entry: {name}")
            unix_type = (info.external_attr >> 16) & 0o170000
            core.require(unix_type != 0o120000, f"source ZIP contains a symbolic link: {name}")
            entries[name] = archive.read(info)
    core.require(bool(entries), "source ZIP is empty")
    return entries


def load_register_input(bundle_root: Path, register: str) -> RegisterInput:
    core.require(register in REGISTERS, f"unsupported register: {register}")
    register_root = bundle_root / register
    core.require(register_root.is_dir(), f"missing register bundle: {register_root}")
    receipts = sorted(register_root.glob("*-BUILD.json"))
    core.require(len(receipts) == 1, f"expected one build receipt in {register_root}")
    receipt_path = receipts[0]
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    core.require(
        receipt.get("schema") == "openlogic-partial-source-epub-build-receipt-v1"
        and receipt.get("status")
        == "BUILT_REPRODUCIBLY_STRUCTURALLY_AUDITED_EPUBCHECKED_VISUAL_QA_PENDING"
        and receipt.get("register") == register,
        f"unexpected source/EPUB build receipt for {register}",
    )
    scope = receipt.get("scope")
    core.require(
        isinstance(scope, dict)
        and scope.get("accepted_units") == 160
        and scope.get("first") == "OLP-0001"
        and scope.get("last") == "OLP-0160"
        and scope.get("first_excluded") == "OLP-0161",
        f"source/EPUB scope drift for {register}",
    )
    artifacts = receipt.get("artifacts")
    core.require(isinstance(artifacts, dict), f"build receipt lacks artifacts for {register}")
    source_zip_path = _artifact_path(register_root, artifacts.get("source_zip"), "source ZIP")
    direct_tex_path = _artifact_path(register_root, artifacts.get("latex"), "direct LaTeX")
    epub_path = _artifact_path(register_root, artifacts.get("epub"), "EPUB")
    source_zip_bytes = source_zip_path.read_bytes()
    direct_tex_bytes = direct_tex_path.read_bytes()
    epub_bytes = epub_path.read_bytes()
    source_entries = _read_source_zip(source_zip_bytes)
    source_identity = core.byte_tree_identity(source_entries)
    source_binding = receipt.get("source_binding")
    core.require(
        isinstance(source_binding, dict)
        and source_binding.get("source_zip_replays_tree_exactly") is True
        and source_binding.get("tree") == source_identity,
        f"source ZIP tree identity differs from its build receipt for {register}",
    )
    tex_name = direct_tex_path.name
    core.require(
        source_entries.get(tex_name) == direct_tex_bytes,
        f"direct cumulative LaTeX is not byte-identical to the source ZIP root for {register}",
    )
    return RegisterInput(
        register=register,
        receipt_path=receipt_path,
        receipt_bytes=receipt_bytes,
        receipt=receipt,
        source_zip_path=source_zip_path,
        source_zip_bytes=source_zip_bytes,
        direct_tex_path=direct_tex_path,
        direct_tex_bytes=direct_tex_bytes,
        epub_path=epub_path,
        epub_bytes=epub_bytes,
        source_entries=source_entries,
        source_identity=source_identity,
        tex_name=tex_name,
    )


def verify_visual_receipt(path: Path, inputs: list[RegisterInput]) -> dict[str, object]:
    core.require(path.is_file(), f"missing EPUB visual-QA receipt: {path}")
    payload = path.read_bytes()
    receipt = json.loads(payload.decode("utf-8"))
    core.require(
        receipt.get("schema") == "openlogic-nl-accepted160-epub-visual-qa-v1"
        and receipt.get("status") == "PASS",
        "EPUB visual-QA receipt is not a passing accepted-160 receipt",
    )
    exact = receipt.get("exact_epubs")
    core.require(isinstance(exact, dict), "EPUB visual-QA receipt lacks exact EPUB bindings")
    for item in inputs:
        record = exact.get(item.register)
        core.require(
            isinstance(record, dict)
            and record.get("bytes") == len(item.epub_bytes)
            and record.get("sha256") == core.sha256_bytes(item.epub_bytes),
            f"EPUB visual-QA receipt does not bind the current {item.register} bytes",
        )
    return {
        "path": str(path),
        "bytes": len(payload),
        "sha256": core.sha256_bytes(payload),
        "status": "PASS_EXACT_EPUB_BYTES_BOUND",
    }


def _write_source_tree(entries: dict[str, bytes], root: Path) -> None:
    core.require(not root.exists(), f"source materialization root already exists: {root}")
    root.mkdir(parents=True)
    for name, payload in sorted(entries.items()):
        relative = core.safe_relative_path(name, label="source materialization path")
        target = root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _write_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    core.require(not path.exists(), f"staged output already exists: {path}")
    path.write_bytes(payload)


def build_pair(
    bundle_root: Path,
    output_dir: Path,
    visual_receipt_path: Path,
    *,
    mutex_timeout_seconds: float,
    pdf_timeout_seconds: float,
) -> dict[str, object]:
    core.require(not output_dir.exists(), f"refusing to overwrite output: {output_dir}")
    core.require(mutex_timeout_seconds > 0, "mutex timeout must be positive")
    core.require(pdf_timeout_seconds > 0, "PDF timeout must be positive")
    core.require(bool(shutil.which(core.PDF_DRIVER)), f"{core.PDF_DRIVER} is unavailable")
    core.require(bool(shutil.which(core.PDF_ENGINE)), f"{core.PDF_ENGINE} is unavailable")
    inputs = [load_register_input(bundle_root, register) for register in REGISTERS]
    modified_values = {str(item.receipt.get("modified")) for item in inputs}
    core.require(len(modified_values) == 1, "paired source bundles have different modified dates")
    modified = core.parse_modified(modified_values.pop())
    visual_receipt = verify_visual_receipt(visual_receipt_path, inputs)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.build-", dir=output_dir.parent
    ) as temporary_name:
        temporary = Path(temporary_name)
        materialized: dict[str, Path] = {}
        for item in inputs:
            source_root = temporary / item.register / "source"
            _write_source_tree(item.source_entries, source_root)
            core.require(
                core.directory_tree_identity(source_root) == item.source_identity,
                f"materialized source tree drift for {item.register}",
            )
            materialized[item.register] = source_root

        mutex_wait_started = time.monotonic()
        with core.GlobalTexMutex(mutex_timeout_seconds) as mutex:
            mutex_acquired = time.monotonic()
            results: dict[str, dict[str, object]] = {}
            pdf_payloads: dict[str, bytes] = {}
            for item in inputs:
                pdf, build = core.run_pdf_build(
                    materialized[item.register],
                    item.tex_name,
                    temporary / item.register / "pdf-build",
                    modified=modified,
                    expected_source_tree=item.source_identity,
                    mutex_timeout_seconds=mutex_timeout_seconds,
                    pdf_timeout_seconds=pdf_timeout_seconds,
                    held_mutex=mutex,
                )
                audit = core.audit_pdf_bytes(pdf)
                core.require(
                    build.get("status") == "PASS_REPEATABLE_BYTES"
                    and build.get("mutex_scope") == "caller-held"
                    and build.get("pdf") == audit,
                    f"paired PDF build receipt failed validation for {item.register}",
                )
                pdf_payloads[item.register] = pdf
                results[item.register] = {
                    "source_bundle": {
                        "build_receipt": {
                            "path": str(item.receipt_path),
                            "bytes": len(item.receipt_bytes),
                            "sha256": core.sha256_bytes(item.receipt_bytes),
                        },
                        "source_zip": {
                            "path": str(item.source_zip_path),
                            "bytes": len(item.source_zip_bytes),
                            "sha256": core.sha256_bytes(item.source_zip_bytes),
                        },
                        "direct_latex": {
                            "path": str(item.direct_tex_path),
                            "bytes": len(item.direct_tex_bytes),
                            "sha256": core.sha256_bytes(item.direct_tex_bytes),
                        },
                    },
                    "pdf": audit,
                    "build": build,
                    "visual_qa": "PENDING_NOT_PERFORMED",
                }
            mutex_hold_seconds = round(time.monotonic() - mutex_acquired, 3)
            mutex_abandoned = mutex.abandoned
        pair_receipt: dict[str, object] = {
            "schema": "openlogic-accepted160-paired-pdf-build-v1",
            "status": "PASS_PAIRED_PDF_BUILT_REPRODUCIBLY_VISUAL_QA_PENDING",
            "scope": {
                "accepted_units_each_register": 160,
                "first": "OLP-0001",
                "last": "OLP-0160",
                "first_excluded": "OLP-0161",
                "registers": list(REGISTERS),
            },
            "source_bundle_root": str(bundle_root),
            "modified": modified,
            "epub_visual_qa": visual_receipt,
            "mutex": {
                "name": core.TEX_MUTEX_NAME,
                "bounded_acquisition_attempts": 1,
                "timeout_seconds": mutex_timeout_seconds,
                "wait_seconds": round(mutex_acquired - mutex_wait_started, 3),
                "hold_seconds": mutex_hold_seconds,
                "abandoned_recovery": mutex_abandoned,
                "continuous_scope": "both registers, both exact-byte PDF replays, and all tool probes",
            },
            "process_tree_guard": "Windows job object, kill on close",
            "registers": results,
            "publication_performed": False,
            "next_gate": "representative rendered-page visual QA for both exact PDFs",
        }
        staged = temporary / "bundle"
        staged.mkdir()
        expected_paths: set[str] = {"PAIR-PDF-BUILD.json"}
        for item in inputs:
            stem = Path(item.tex_name).stem
            pdf_name = f"{stem}.pdf"
            build_name = f"{stem}-PDF-BUILD.json"
            _write_file(staged / item.register / pdf_name, pdf_payloads[item.register])
            _write_file(
                staged / item.register / build_name,
                core.json_bytes(results[item.register]),
            )
            expected_paths.update(
                {f"{item.register}/{pdf_name}", f"{item.register}/{build_name}"}
            )
        _write_file(staged / "PAIR-PDF-BUILD.json", core.json_bytes(pair_receipt))
        observed_paths = {
            path.relative_to(staged).as_posix()
            for path in staged.rglob("*")
            if path.is_file()
        }
        core.require(observed_paths == expected_paths, "staged paired-PDF path set drifted")
        os.replace(staged, output_dir)
    core.require(output_dir.is_dir(), "paired PDF output was not committed")
    return pair_receipt


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--source-bundle-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--visual-receipt", type=Path, required=True)
    parser.add_argument("--mutex-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--pdf-timeout-seconds", type=float, default=600.0)
    return parser.parse_args(argv)


def _resolve(root: Path, value: Path) -> Path:
    return value.resolve() if value.is_absolute() else (root / value).resolve()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        repo_root = args.repo_root.resolve()
        receipt = build_pair(
            _resolve(repo_root, args.source_bundle_root),
            _resolve(repo_root, args.output_dir),
            _resolve(repo_root, args.visual_receipt),
            mutex_timeout_seconds=args.mutex_timeout_seconds,
            pdf_timeout_seconds=args.pdf_timeout_seconds,
        )
    except (core.BuildError, OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
