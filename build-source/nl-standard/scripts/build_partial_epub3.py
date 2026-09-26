#!/usr/bin/env python3
"""Build scope-honest partial Dutch reader PDF, sources, and reflowable EPUB 3.

The only authority for inclusion is ``alignment/UNITS.jsonl``.  The builder
accepts one contiguous prefix whose aggregate and per-register statuses are
all ``accepted``; it refuses an accepted row after the first non-accepted row.
It never follows ``\\olimport`` links, because a translated driver can name
translated-but-pending material.

Real builds use both latexmk/pdfTeX and make4ht only while holding the
machine-wide Windows mutex ``Global\\InterlanguageTeXSlotV1``.  The PDF is
built twice from the exact source tree and must be byte-identical.  Dry runs
and the hermetic regression suite do not launch TeX.  Every packaged EPUB must
then pass the byte-pinned EPUBCheck 5.3.0 runtime.  No reader artifact is
committed until every selected register has passed every check.
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import ctypes
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

from lxml import etree, html


REGISTER_CONFIG = {
    "nl-standard": {
        "path_key": "standard_path",
        "sha_key": "standard_sha256",
        "bytes_key": "standard_bytes",
        "status_key": "standard_status",
        "label": "standaardregister",
        "slug": "Standard",
    },
    "nl-gewoon": {
        "path_key": "ordinary_path",
        "sha_key": "ordinary_sha256",
        "bytes_key": "ordinary_bytes",
        "status_key": "ordinary_status",
        "label": "gewoon register",
        "slug": "Gewoon",
    },
}

TEX_MUTEX_NAME = r"Global\InterlanguageTeXSlotV1"
EPUBCHECK_VERSION = "5.3.0"
EPUBCHECK_JAR_SHA256 = "f7f96617c929371821609b88c8484d6dc9f24fe916499863c46094c5fb778a65"
EPUBCHECK_RUNTIME_TREE_SHA256 = "f5e057f7b81e1a527c53f8fabe63c17681ddc63919343c05eb945a6a1768face"
EPUBCHECK_ENVIRONMENT_VARIABLE = "OPENLOGIC_EPUBCHECK_JAR"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
XHTML_NS = "http://www.w3.org/1999/xhtml"
MATHML_NS = "http://www.w3.org/1998/Math/MathML"
SVG_NS = "http://www.w3.org/2000/svg"
EPUB_NS = "http://www.idpf.org/2007/ops"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
XML_NS = "http://www.w3.org/XML/1998/namespace"
XLINK_NS = "http://www.w3.org/1999/xlink"
NS = {"x": XHTML_NS, "opf": OPF_NS, "dc": DC_NS, "epub": EPUB_NS}

ALLOWED_GENERATED_ASSET_SUFFIXES = {
    ".css": "text/css",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".otf": "font/otf",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
EXTERNAL_LINK_SCHEMES = {"http", "https", "mailto"}
ROOT_SUPPORT_FILES = (
    "open-logic-config.sty",
    "open-logic-envs.sty",
    "open-logic-locale.sty",
)
UPSTREAM_SUPPORT_TREES = ("assets", "bib", "sty")
PDF_DRIVER = "latexmk"
PDF_ENGINE = "pdflatex"
PDF_REPLAY_COUNT = 2
ARTIFACT_ORDER = (
    "pdf",
    "latex",
    "source_zip",
    "epub",
    "epubcheck_receipt",
    "receipt",
)
WINDOWS_JOB_LAUNCH_CAPTURE = (
    "CREATE_SUSPENDED",
    "AssignProcessToJobObject",
    "ResumeThread",
)
WINDOWS_CREATE_SUSPENDED = 0x00000004


class BuildError(RuntimeError):
    """A fail-closed build or audit error."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BuildError(message)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def safe_archive_name(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/"):
        return False
    parts = PurePosixPath(name).parts
    return all(part not in {"", ".", ".."} for part in parts)


def safe_relative_path(value: str, *, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    require(safe_archive_name(path.as_posix()), f"unsafe {label}: {value!r}")
    return path


def file_record(path: Path, *, relative_to: Path | None = None) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.relative_to(relative_to).as_posix() if relative_to else path.name,
        "bytes": len(payload),
        "sha256": sha256_bytes(payload),
    }


def tree_digest(records: Iterable[Mapping[str, object]]) -> str:
    frame = "".join(
        f"{row['sha256']}  {row['bytes']}  {row['path']}\n"
        for row in sorted(records, key=lambda row: str(row["path"]))
    )
    return sha256_bytes(frame.encode("utf-8"))


def byte_tree_identity(entries: Mapping[str, bytes]) -> dict[str, object]:
    """Return a path-sensitive identity for an in-memory source tree."""
    records = [
        {"path": name, "bytes": len(payload), "sha256": sha256_bytes(payload)}
        for name, payload in sorted(entries.items())
    ]
    return {
        "files": len(records),
        "bytes": sum(int(row["bytes"]) for row in records),
        "tree_sha256": tree_digest(records),
    }


def directory_tree_identity(root: Path) -> dict[str, object]:
    """Hash every regular file below ``root`` and reject link/path surprises."""
    require(root.is_dir(), f"missing source tree: {root}")
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), f"source-tree symlink is forbidden: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            require(safe_archive_name(relative), f"unsafe source-tree path: {relative}")
            records.append(file_record(path, relative_to=root))
    require(bool(records), f"source tree is empty: {root}")
    return {
        "files": len(records),
        "bytes": sum(int(row["bytes"]) for row in records),
        "tree_sha256": tree_digest(records),
    }


def canonical_unit_line(row: Mapping[str, object]) -> bytes:
    return (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


@dataclass(frozen=True)
class AcceptedPlan:
    repo_root: Path
    alignment_path: Path
    alignment_sha256: str
    all_units: tuple[dict[str, object], ...]
    accepted_units: tuple[dict[str, object], ...]
    first_excluded: dict[str, object] | None
    registers: tuple[str, ...]
    source_revisions: tuple[str, ...]

    @property
    def first_id(self) -> str:
        return str(self.accepted_units[0]["unit_id"])

    @property
    def last_id(self) -> str:
        return str(self.accepted_units[-1]["unit_id"])

    @property
    def scope_token(self) -> str:
        return f"{self.first_id}--{self.last_id}"

    @property
    def accepted_ids(self) -> tuple[str, ...]:
        return tuple(str(row["unit_id"]) for row in self.accepted_units)

    @property
    def excluded_ids(self) -> tuple[str, ...]:
        accepted = set(self.accepted_ids)
        return tuple(str(row["unit_id"]) for row in self.all_units if row["unit_id"] not in accepted)


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BuildError(f"invalid JSON in {path} line {number}: {exc}") from exc
        require(isinstance(value, dict), f"non-object row in {path} line {number}")
        rows.append(value)
    return rows


def _validate_unit_identity(rows: Sequence[Mapping[str, object]]) -> None:
    require(bool(rows), "alignment/UNITS.jsonl is empty")
    seen_ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        missing = {
            "unit_id",
            "order",
            "status",
            "source_role",
            "source_revision",
            "standard_path",
            "standard_sha256",
            "standard_bytes",
            "standard_status",
            "ordinary_path",
            "ordinary_sha256",
            "ordinary_bytes",
            "ordinary_status",
        } - set(row)
        require(not missing, f"alignment row {index} lacks fields: {sorted(missing)}")
        unit_id = str(row["unit_id"])
        require(bool(re.fullmatch(r"OLP-\d{4}", unit_id)), f"invalid unit id: {unit_id}")
        require(unit_id not in seen_ids, f"duplicate unit id: {unit_id}")
        seen_ids.add(unit_id)
        require(row["order"] == index, f"non-contiguous order at {unit_id}: {row['order']} != {index}")
        if row["status"] == "accepted":
            for key in ("standard_sha256", "ordinary_sha256"):
                require(
                    bool(re.fullmatch(r"[0-9a-f]{64}", str(row[key]))),
                    f"invalid {key} at {unit_id}",
                )
            for key in ("standard_bytes", "ordinary_bytes"):
                require(
                    isinstance(row[key], int) and int(row[key]) >= 0,
                    f"invalid {key} at {unit_id}",
                )


def plan_accepted_prefix(repo_root: Path, registers: Sequence[str]) -> AcceptedPlan:
    repo_root = repo_root.resolve()
    alignment = repo_root / "alignment" / "UNITS.jsonl"
    require(alignment.is_file(), f"missing authoritative alignment: {alignment}")
    requested = tuple(registers)
    require(bool(requested), "at least one register is required")
    require(len(requested) == len(set(requested)), "duplicate register selection")
    require(all(name in REGISTER_CONFIG for name in requested), "unknown register selection")

    rows = _load_jsonl(alignment)
    _validate_unit_identity(rows)
    prefix: list[dict[str, object]] = []
    first_excluded: dict[str, object] | None = None
    encountered_nonaccepted = False
    for row in rows:
        aggregate_accepted = row["status"] == "accepted"
        if aggregate_accepted:
            require(
                not encountered_nonaccepted,
                f"accepted unit {row['unit_id']} occurs after the accepted prefix ended",
            )
            require(
                row["standard_status"] == "accepted" and row["ordinary_status"] == "accepted",
                f"aggregate accepted row lacks per-register acceptance: {row['unit_id']}",
            )
            prefix.append(dict(row))
        else:
            encountered_nonaccepted = True
            if first_excluded is None:
                first_excluded = dict(row)
            require(
                row["standard_status"] != "accepted" and row["ordinary_status"] != "accepted",
                f"per-register accepted row lies outside aggregate prefix: {row['unit_id']}",
            )
    require(bool(prefix), "there is no accepted prefix")

    for row in prefix:
        for register in requested:
            config = REGISTER_CONFIG[register]
            relative = safe_relative_path(str(row[config["path_key"]]), label="translation path")
            require(
                relative.parts[:2] == ("translations", register),
                f"{row['unit_id']} points outside {register}: {relative}",
            )
            path = (repo_root / Path(relative.as_posix())).resolve()
            require(repo_root in path.parents, f"translation path escapes repository: {relative}")
            require(path.is_file(), f"missing accepted translation: {relative}")
            payload = path.read_bytes()
            require(len(payload) == row[config["bytes_key"]], f"byte-count drift: {relative}")
            require(
                sha256_bytes(payload) == row[config["sha_key"]],
                f"digest drift: {relative}",
            )

    source_revisions = tuple(sorted({str(row["source_revision"]) for row in prefix}))
    require(all(source_revisions), "accepted prefix has an empty source revision")
    return AcceptedPlan(
        repo_root=repo_root,
        alignment_path=alignment,
        alignment_sha256=sha256_bytes(alignment.read_bytes()),
        all_units=tuple(rows),
        accepted_units=tuple(prefix),
        first_excluded=first_excluded,
        registers=requested,
        source_revisions=source_revisions,
    )


DOCUMENTCLASS_RE = re.compile(r"^[ \t]*\\documentclass(?:\[[^\]]*\])?\{[^{}]+\}[ \t]*\r?\n?", re.MULTILINE)
BEGIN_DOCUMENT_RE = re.compile(r"^[ \t]*\\begin\{document\}[ \t]*\r?\n?", re.MULTILINE)
END_DOCUMENT_RE = re.compile(r"^[ \t]*\\end\{document\}[ \t]*\r?\n?", re.MULTILINE)
OLIMPORT_RE = re.compile(
    r"^[ \t]*%*[ \t]*\\olimport\*?(?:\[[^\]]*\])?\s*\{[^{}]+\}(?:\[[^\]]*\])?[ \t]*\r?\n?",
    re.MULTILINE,
)
END_HOOK_RE = re.compile(r"^[ \t]*\\OLEnd(?:Chapter|Part)Hook[ \t]*\r?\n?", re.MULTILINE)


def stripped_unit_body(text: str) -> str:
    """Return one source body without wrappers or graph-following imports."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for pattern in (DOCUMENTCLASS_RE, BEGIN_DOCUMENT_RE, END_DOCUMENT_RE, OLIMPORT_RE, END_HOOK_RE):
        normalized = pattern.sub("", normalized)
    return normalized.strip("\n") + "\n"


def _tex_escape_text(value: str) -> str:
    table = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "#": r"\#",
        "$": r"\$",
        "%": r"\%",
        "&": r"\&",
        "_": r"\_",
        "^": r"\textasciicircum{}",
        "~": r"\textasciitilde{}",
    }
    return "".join(table.get(char, char) for char in value)


def output_basename(plan: AcceptedPlan, register: str) -> str:
    return f"OpenLogic-NL-{REGISTER_CONFIG[register]['slug']}-Partial-{plan.scope_token}"


def render_cumulative_tex(plan: AcceptedPlan, register: str, modified: str) -> bytes:
    require(register in plan.registers, f"register absent from plan: {register}")
    config = REGISTER_CONFIG[register]
    title = (
        "The Open Logic Text --- gedeeltelijke Nederlandstalige editie "
        f"({_tex_escape_text(str(config['label']))})"
    )
    scope = f"{plan.first_id}--{plan.last_id}"
    first_excluded = str(plan.first_excluded["unit_id"]) if plan.first_excluded else "geen"
    header = rf"""% Generated deterministically by scripts/build_partial_epub3.py.
% Inclusion authority: alignment/UNITS.jsonl sha256={plan.alignment_sha256}
% Accepted contiguous prefix: {scope} ({len(plan.accepted_units)} units).
% First excluded unit: {first_excluded}.
% This cumulative file contains every accepted source body inline.  It does
% not follow source driver imports and therefore cannot include pending units.
\documentclass[openany]{{memoir}}
\newcommand*{{\olpath}}{{upstream}}
\newcommand*{{\ollangid}}{{nl}}
\newcommand*{{\ollanguage}}{{dutch}}
\usepackage[T1]{{fontenc}}
\usepackage[utf8]{{inputenc}}
\usepackage{{mathpazo}}
\usepackage{{helvet}}
\usepackage{{courier}}
\linespread{{1.05}}
\setlength{{\emergencystretch}}{{1em}}
\input{{upstream/sty/open-logic.sty}}
\input{{upstream/sty/open-logic-defer.sty}}
\includeenv{{editorial}}
\problemsperchapter
\let\cleardoublepage\clearpage
\hypersetup{{
  unicode=true,
  pdflang={{nl-NL}},
  pdftitle={{{title}}},
  pdfauthor={{The Open Logic Project and credited contributors}},
  pdfsubject={{Gedeeltelijke reader met uitsluitend de geaccepteerde aaneengesloten prefix {scope}}},
  pdfkeywords={{logica, verzamelingenleer, Nederlandse vertaling, gedeeltelijke editie}}
}}
\begin{{document}}
\begin{{titlingpage}}
\begin{{raggedleft}}
\HUGE\selectfont\bfseries\sffamily The Open Logic Text
\vskip 3ex
\normalfont\huge Gedeeltelijke Nederlandstalige editie\\
\Large {_tex_escape_text(str(config['label']))}
\vskip 4ex
\large Geaccepteerde broneenheden {scope}\\
{modified}
\end{{raggedleft}}
\vfill
\noindent Gebaseerd op het Open Logic Project. De tekst en broncode worden
beschikbaar gesteld onder Creative Commons Naamsvermelding 4.0 Internationaal.
\end{{titlingpage}}
\chapter*{{Reikwijdte van deze gedeeltelijke editie}}
\addcontentsline{{toc}}{{chapter}}{{Reikwijdte van deze gedeeltelijke editie}}
Deze editie bevat uitsluitend de {len(plan.accepted_units)} opeenvolgende,
in \texttt{{alignment/UNITS.jsonl}} geaccepteerde broneenheden {scope}.
Broneenheid {first_excluded} en alle latere eenheden zijn niet opgenomen.
Een vertaalbestand dat al bestaat maar nog niet als geaccepteerd is gemarkeerd,
maakt uitdrukkelijk geen deel uit van deze editie. Het laatste hoofdstuk kan
daardoor inhoudelijk onvolledig zijn; dat is een eigenschap van deze begrensde
editie en geen claim dat het volledige Open Logic Text al vertaald is.
\tableofcontents*
"""
    pieces = [header]
    chapter_open = False
    part_open = False
    for row in plan.accepted_units:
        role = str(row["source_role"])
        if role == "part_driver":
            if chapter_open:
                pieces.append("\\OLEndChapterHook\n")
                chapter_open = False
            if part_open:
                pieces.append("\\OLEndPartHook\n")
            part_open = True
        elif role == "chapter_driver":
            if chapter_open:
                pieces.append("\\OLEndChapterHook\n")
            chapter_open = True

        relative = safe_relative_path(str(row[config["path_key"]]), label="translation path")
        payload = (plan.repo_root / Path(relative.as_posix())).read_bytes()
        source_text = payload.decode("utf-8")
        body = stripped_unit_body(source_text)
        body_sha256 = sha256_bytes(body.encode("utf-8"))
        source_dir = PurePosixPath(str(row["source_path"])).parent.as_posix()
        unit_id = str(row["unit_id"])
        pieces.append(
            "\n"
            f"% BEGIN ACCEPTED UNIT {unit_id} path={relative.as_posix()} "
            f"source-sha256={sha256_bytes(payload)}\n"
            "\\begingroup\n"
            f"\\def\\olfilename{{{unit_id}}}\n"
            f"\\def\\olfilepath{{{source_dir}/}}\n"
            f"\\hypertarget{{unit-{unit_id}}}{{}}\n"
            f"\\typeout{{PARTIAL-READER-UNIT {unit_id}}}\n"
            f"% BEGIN ACCEPTED BODY {unit_id} body-sha256={body_sha256}\n"
            + body
            + f"% END ACCEPTED BODY {unit_id}\n"
            + "\\endgroup\n"
            f"% END ACCEPTED UNIT {unit_id}\n"
        )
    if chapter_open:
        pieces.append("\\OLEndChapterHook\n")
    if part_open:
        pieces.append("\\OLEndPartHook\n")
    pieces.append(
        r"""
\clearpage
\chapter*{Bron en licentie}
\addcontentsline{toc}{chapter}{Bron en licentie}
De inhoud is gebaseerd op het Open Logic Project. De bronidentiteiten en
SHA-256-hashes van alle opgenomen eenheden staan in het bijbehorende
machineleesbare bronmanifest.
\bibliographystyle{upstream/bib/natbib-oup}
\bibliography{upstream/bib/open-logic}
\end{document}
"""
    )
    result = "".join(pieces).replace("\r\n", "\n").encode("utf-8")
    audit_cumulative_tex(plan, register, result)
    return result


def audit_cumulative_tex(
    plan: AcceptedPlan, register: str, payload: bytes
) -> dict[str, object]:
    """Replay every inline body against its accepted source byte identity."""
    require(register in plan.registers, f"register absent from plan: {register}")
    config = REGISTER_CONFIG[register]
    text = payload.decode("utf-8")
    require(text.count("\\documentclass") == 1, "cumulative TeX has multiple document classes")
    require(text.count("\\begin{document}") == 1, "cumulative TeX lacks one document start")
    require(text.count("\\end{document}") == 1, "cumulative TeX lacks one document end")
    require("\\olimport" not in text, "cumulative TeX retained an import edge")
    body_replays = 0
    for row in plan.accepted_units:
        unit_id = str(row["unit_id"])
        require(
            text.count(f"% BEGIN ACCEPTED UNIT {unit_id} ") == 1,
            f"cumulative TeX does not contain {unit_id} exactly once",
        )
        relative = safe_relative_path(str(row[config["path_key"]]), label="translation path")
        expected = stripped_unit_body(
            (plan.repo_root / Path(relative.as_posix())).read_text(encoding="utf-8")
        )
        expected_sha = sha256_bytes(expected.encode("utf-8"))
        begin = f"% BEGIN ACCEPTED BODY {unit_id} body-sha256={expected_sha}\n"
        end = f"% END ACCEPTED BODY {unit_id}\n"
        require(text.count(begin) == 1 and text.count(end) == 1, f"body markers drifted: {unit_id}")
        actual = text.split(begin, 1)[1].split(end, 1)[0]
        require(actual == expected, f"inline accepted body replay failed: {unit_id}")
        body_replays += 1
    for unit_id in plan.excluded_ids:
        require(
            f"% BEGIN ACCEPTED UNIT {unit_id} " not in text,
            f"excluded unit marker leaked into cumulative TeX: {unit_id}",
        )
        require(
            f"% BEGIN ACCEPTED BODY {unit_id} " not in text,
            f"excluded unit body leaked into cumulative TeX: {unit_id}",
        )
    return {
        "schema": "openlogic-partial-cumulative-tex-audit-v1",
        "status": "PASS",
        "register": register,
        "accepted_unit_bodies_replayed": body_replays,
        "bytes": len(payload),
        "sha256": sha256_bytes(payload),
    }


def dutch_locale_overlay(repo_root: Path, register: str) -> bytes:
    source = repo_root / "editions" / register / "support" / "open-logic-reader-locale.tex"
    require(source.is_file(), f"missing Dutch reader locale overlay: {source}")
    text = source.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("{english}", "{dutch}").replace("\\extrasenglish", "\\extrasdutch")
    prefix = (
        "% Generated locale placement for the partial reader.\n"
        "% Terminology derives byte-for-byte apart from the Babel locale-key\n"
        "% substitution from editions/<register>/support/open-logic-reader-locale.tex.\n"
    )
    return (prefix + text).encode("utf-8")


def _add_tree(entries: dict[str, bytes], source_root: Path, archive_root: str) -> None:
    require(source_root.is_dir(), f"missing source support tree: {source_root}")
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            raise BuildError(f"symlink is not permitted in source support tree: {path}")
        if path.is_file():
            relative = path.relative_to(source_root).as_posix()
            name = f"{archive_root}/{relative}"
            require(safe_archive_name(name), f"unsafe support archive path: {name}")
            entries[name] = path.read_bytes()


def source_tree_entries(
    plan: AcceptedPlan,
    register: str,
    modified: str,
    cumulative_tex: bytes,
) -> dict[str, bytes]:
    config = REGISTER_CONFIG[register]
    base = output_basename(plan, register)
    entries: dict[str, bytes] = {f"{base}.tex": cumulative_tex}
    # Keep the complete authoritative ledger so an extracted tree computes the
    # same prefix boundary and the same authority hash.  Only accepted
    # translation *files* are copied below.
    entries["alignment/UNITS.jsonl"] = plan.alignment_path.read_bytes()
    entries["alignment/ACCEPTED_PREFIX.jsonl"] = b"".join(
        canonical_unit_line(row) for row in plan.accepted_units
    )
    entries["alignment/AUTHORITY.json"] = json_bytes(
        {
            "schema": "openlogic-partial-reader-alignment-authority-v1",
            "authoritative_path": "alignment/UNITS.jsonl",
            "original_alignment_sha256": plan.alignment_sha256,
            "accepted_prefix_first": plan.first_id,
            "accepted_prefix_last": plan.last_id,
            "accepted_units": len(plan.accepted_units),
            "first_excluded_unit": plan.first_excluded["unit_id"] if plan.first_excluded else None,
            "source_revisions": list(plan.source_revisions),
            "register": register,
        }
    )
    for row in plan.accepted_units:
        relative = safe_relative_path(str(row[config["path_key"]]), label="translation path")
        entries[relative.as_posix()] = (plan.repo_root / Path(relative.as_posix())).read_bytes()

    upstream = plan.repo_root / "upstream"
    for name in ROOT_SUPPORT_FILES:
        path = upstream / name
        require(path.is_file(), f"missing upstream build dependency: {path}")
        entries[f"upstream/{name}"] = path.read_bytes()
    for tree in UPSTREAM_SUPPORT_TREES:
        _add_tree(entries, upstream / tree, f"upstream/{tree}")
    for name in ("LICENSE.md", "README.md"):
        path = upstream / name
        if path.is_file():
            entries[f"upstream/{name}"] = path.read_bytes()
    project_license = plan.repo_root / "LICENSE.md"
    require(project_license.is_file(), f"missing project license: {project_license}")
    entries["LICENSE.md"] = project_license.read_bytes()
    locale_source = (
        plan.repo_root
        / "editions"
        / register
        / "support"
        / "open-logic-reader-locale.tex"
    )
    entries[
        f"editions/{register}/support/open-logic-reader-locale.tex"
    ] = locale_source.read_bytes()
    entries["upstream/locale/nl/open-logic-locale.sty"] = dutch_locale_overlay(plan.repo_root, register)

    builder = Path(__file__).resolve()
    entries["scripts/build_partial_epub3.py"] = builder.read_bytes()
    tests = builder.with_name("test_build_partial_epub3.py")
    if tests.is_file():
        entries["scripts/test_build_partial_epub3.py"] = tests.read_bytes()
    support_dir = builder.parent / "epub3"
    if support_dir.is_dir():
        _add_tree(entries, support_dir, "scripts/epub3")

    source_date_epoch = str(calendar.timegm(time.strptime(modified, "%Y-%m-%d")))
    pdf_engine_template = (
        f"{PDF_ENGINE} %O -interaction=nonstopmode -halt-on-error "
        "-file-line-error -recorder %S"
    )
    pdf_commands = [
        [
            PDF_DRIVER,
            "-norc",
            "-gg",
            "-pdf",
            f"-pdflatex={pdf_engine_template}",
            f"-outdir=pdf-replay-{number}",
            f"{base}.tex",
        ]
        for number in range(1, PDF_REPLAY_COUNT + 1)
    ]
    entries["PDF-REBUILD.json"] = json_bytes(
        {
            "schema": "openlogic-partial-reader-pdf-rebuild-v1",
            "input": f"{base}.tex",
            "driver": PDF_DRIVER,
            "engine": PDF_ENGINE,
            "commands": pdf_commands,
            "working_directory": "root of this extracted source tree",
            "environment": {
                "SOURCE_DATE_EPOCH": source_date_epoch,
                "FORCE_SOURCE_DATE": "1",
                "TZ": "UTC",
                "LC_ALL": "C",
                "LANG": "C",
            },
            "repeatability_requirement": {
                "runs": PDF_REPLAY_COUNT,
                "comparison": "exact PDF byte equality",
            },
            "machine_serialization": {
                "platform": "Windows",
                "named_mutex": TEX_MUTEX_NAME,
                "scope": "hold continuously around both complete latexmk process trees",
                "process_capture": list(WINDOWS_JOB_LAUNCH_CAPTURE),
            },
            "bundled_dependencies": {
                "manifest": "SOURCE-MANIFEST.json",
                "project_specific": [
                    f"{base}.tex",
                    "upstream/open-logic-config.sty",
                    "upstream/open-logic-envs.sty",
                    "upstream/open-logic-locale.sty",
                    "upstream/sty/",
                    "upstream/bib/",
                    "upstream/assets/",
                    "upstream/locale/nl/open-logic-locale.sty",
                ],
                "external_tools": [
                    "Python 3.11 or newer with lxml and pypdf",
                    "latexmk",
                    "pdfTeX/pdflatex",
                    "BibTeX as selected by latexmk",
                    "a TeX distribution satisfying the packages imported by the bundled Open Logic styles",
                ],
            },
        }
    )

    build_command = (
        f"python scripts/build_partial_epub3.py --repo-root . --output-dir rebuilt "
        f"--register {register} --modified {modified}\n"
    )
    entries["BUILD.md"] = (
        "# Rebuilding this bounded reader\n\n"
        f"The translated source payload in this tree contains only the accepted contiguous "
        f"prefix {plan.first_id} through {plan.last_id} for `{register}`. It intentionally "
        "contains no later translation file. The complete authoritative `UNITS.jsonl` is "
        "retained as boundary evidence; `ACCEPTED_PREFIX.jsonl` records the selected rows.\n\n"
        "Requirements: Python 3.11+ with lxml and pypdf, latexmk, pdfTeX/pdflatex, "
        "make4ht/TeX4ht, BibTeX, and the LaTeX packages required by the bundled "
        "Open Logic styles; Java; and the byte-pinned EPUBCheck "
        f"{EPUBCHECK_VERSION} runtime (`epubcheck.jar` plus `lib/*.jar`; tree SHA-256 "
        f"`{EPUBCHECK_RUNTIME_TREE_SHA256}`). Pass its JAR with `--epubcheck-jar` or "
        f"set `{EPUBCHECK_ENVIRONMENT_VARIABLE}`. On Windows the builder acquires "
        f"`{TEX_MUTEX_NAME}` continuously around both PDF replay process trees and "
        "separately around the complete make4ht process tree, so those TeX lanes "
        "cannot overlap. Each launcher is created suspended, assigned to the "
        "kill-on-close Windows job, and only then resumed. `PDF-REBUILD.json` "
        "records the exact commands, fixed "
        "environment, bundled dependency roots, and exact-byte replay rule. The "
        "builder checks the PDF header, trailer, parseability, and page count; its "
        "receipt deliberately marks raster rendering and visual QA as pending and "
        "does not claim that such review occurred.\n\n"
        "Run from the root of this extracted tree:\n\n"
        "```text\n"
        + build_command
        + "```\n"
    ).encode("utf-8")

    manifest_records = [
        {"path": name, "bytes": len(payload), "sha256": sha256_bytes(payload)}
        for name, payload in sorted(entries.items())
    ]
    entries["SOURCE-MANIFEST.json"] = json_bytes(
        {
            "schema": "openlogic-partial-reader-source-manifest-v1",
            "register": register,
            "scope": {"first": plan.first_id, "last": plan.last_id, "units": len(plan.accepted_units)},
            "manifest_excludes_itself": True,
            "tree_sha256": tree_digest(manifest_records),
            "records": manifest_records,
        }
    )
    require(len(entries) == len(set(entries)), "duplicate source-tree archive path")
    require(all(safe_archive_name(name) for name in entries), "unsafe source-tree archive path")
    accepted_paths = {
        str(row[config["path_key"]]).replace("\\", "/") for row in plan.accepted_units
    }
    translated_paths = {
        name for name in entries if name.startswith(f"translations/{register}/")
    }
    require(translated_paths == accepted_paths, "source tree translation allowlist drift")
    return entries


def deterministic_zip_bytes(entries: Mapping[str, bytes], *, epub: bool = False) -> bytes:
    require(bool(entries), "cannot create an empty ZIP")
    require(all(safe_archive_name(name) for name in entries), "unsafe ZIP member name")
    require(len(entries) == len(set(entries)), "duplicate ZIP member name")
    if epub:
        require(entries.get("mimetype") == b"application/epub+zip", "wrong EPUB mimetype")

    output = io.BytesIO()

    def info(name: str, compression: int) -> zipfile.ZipInfo:
        value = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
        value.compress_type = compression
        value.create_system = 3
        value.external_attr = 0o100644 << 16
        value.flag_bits |= 0x800
        return value

    with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
        ordered = sorted(entries)
        if epub:
            archive.writestr(info("mimetype", zipfile.ZIP_STORED), entries["mimetype"])
            ordered.remove("mimetype")
        for name in ordered:
            archive.writestr(
                info(name, zipfile.ZIP_DEFLATED),
                entries[name],
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    payload = output.getvalue()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        infos = archive.infolist()
        if epub:
            require(infos[0].filename == "mimetype", "EPUB mimetype is not first")
            require(infos[0].compress_type == zipfile.ZIP_STORED, "EPUB mimetype is compressed")
        offset = 1 if epub else 0
        require(
            [item.filename for item in infos[offset:]]
            == sorted(item.filename for item in infos[offset:]),
            "ZIP entries are not deterministically sorted",
        )
    return payload


class GlobalTexMutex:
    """Bounded Windows named-mutex acquisition with guaranteed release."""

    def __init__(self, timeout_seconds: float, name: str = TEX_MUTEX_NAME):
        require(timeout_seconds > 0, "mutex timeout must be positive")
        self.timeout_ms = min(int(timeout_seconds * 1000), 0xFFFFFFFE)
        self.name = name
        self.handle: int | None = None
        self.owned = False
        self.abandoned = False

    def __enter__(self) -> "GlobalTexMutex":
        require(os.name == "nt", "TeX builds fail closed without the Windows global mutex")
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise BuildError(f"CreateMutexW failed for {self.name}: {ctypes.get_last_error()}")
        self.handle = int(handle)
        result = int(kernel32.WaitForSingleObject(handle, self.timeout_ms))
        if result == 0x00000000:  # WAIT_OBJECT_0
            self.owned = True
        elif result == 0x00000080:  # WAIT_ABANDONED
            self.owned = True
            self.abandoned = True
        else:
            kernel32.CloseHandle(handle)
            self.handle = None
            if result == 0x00000102:  # WAIT_TIMEOUT
                raise BuildError(f"timed out acquiring {self.name}")
            raise BuildError(f"WaitForSingleObject failed for {self.name}: 0x{result:08x}")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.handle is None:
            return
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        try:
            if self.owned and not kernel32.ReleaseMutex(self.handle):
                raise BuildError(f"ReleaseMutex failed for {self.name}: {ctypes.get_last_error()}")
        finally:
            kernel32.CloseHandle(self.handle)
            self.handle = None
            self.owned = False


def _open_suspended_primary_thread(kernel32: object, process_id: int) -> tuple[int, int]:
    """Open the sole (primary) thread of a newly CREATE_SUSPENDED process."""
    from ctypes import wintypes

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or int(snapshot) == invalid_handle:
        raise BuildError(
            f"CreateToolhelp32Snapshot failed for suspended process {process_id}: "
            f"{ctypes.get_last_error()}"
        )
    thread_ids: list[int] = []
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ctypes.set_last_error(0)
        present = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
        if not present:
            error = ctypes.get_last_error()
            require(
                error in (0, 18),  # ERROR_NO_MORE_FILES is an empty snapshot.
                f"Thread32First failed: {error}",
            )
        while present:
            if int(entry.th32OwnerProcessID) == process_id:
                thread_ids.append(int(entry.th32ThreadID))
            ctypes.set_last_error(0)
            present = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
            if not present:
                error = ctypes.get_last_error()
                require(
                    error in (0, 18),
                    f"Thread32Next failed: {error}",
                )
    finally:
        kernel32.CloseHandle(snapshot)
    require(
        len(thread_ids) == 1,
        f"suspended process {process_id} has {len(thread_ids)} threads; refusing to guess the primary thread",
    )
    thread_id = thread_ids[0]
    thread = kernel32.OpenThread(0x0002, False, thread_id)  # THREAD_SUSPEND_RESUME
    if not thread:
        raise BuildError(
            f"OpenThread failed for suspended primary thread {thread_id}: "
            f"{ctypes.get_last_error()}"
        )
    return int(thread), thread_id


def _run_in_windows_job(
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: float,
    log_path: Path,
    environment: Mapping[str, str],
    *,
    process_label: str = "make4ht",
) -> dict[str, object]:
    """Create suspended, assign to a kill-on-close job, resume, and await the tree."""
    require(os.name == "nt", f"Windows job objects are required for the {process_label} process tree")
    require(timeout_seconds > 0, f"{process_label} timeout must be positive")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class BASIC_LIMITS(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMITS),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class BASIC_ACCOUNTING(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise BuildError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
    limits = EXTENDED_LIMITS()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel32.CloseHandle(job)
        raise BuildError(f"SetInformationJobObject failed: {ctypes.get_last_error()}")

    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    process_assigned = False
    primary_thread: int | None = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("wb") as log:
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0) | WINDOWS_CREATE_SUSPENDED
            )  # CREATE_SUSPENDED
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                env=dict(environment),
            )
            process_handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
            if not kernel32.AssignProcessToJobObject(job, process_handle):
                raise BuildError(f"AssignProcessToJobObject failed: {ctypes.get_last_error()}")
            process_assigned = True
            primary_thread, primary_thread_id = _open_suspended_primary_thread(
                kernel32, process.pid
            )
            try:
                previous_suspend_count = int(
                    kernel32.ResumeThread(wintypes.HANDLE(primary_thread))
                )
                if previous_suspend_count == 0xFFFFFFFF:
                    raise BuildError(
                        f"ResumeThread failed for primary thread {primary_thread_id}: "
                        f"{ctypes.get_last_error()}"
                    )
                require(
                    previous_suspend_count == 1,
                    f"primary thread {primary_thread_id} had unexpected suspend count "
                    f"{previous_suspend_count}; process remains fail-closed",
                )
            finally:
                kernel32.CloseHandle(wintypes.HANDLE(primary_thread))
                primary_thread = None
            deadline = started + timeout_seconds
            result = int(
                kernel32.WaitForSingleObject(
                    process_handle,
                    min(max(1, int((deadline - time.monotonic()) * 1000)), 0xFFFFFFFE),
                )
            )
            if result == 0x00000102:
                kernel32.TerminateJobObject(job, 124)
                kernel32.WaitForSingleObject(process_handle, 10000)
                process.wait(timeout=10)
                raise BuildError(f"{process_label} process tree exceeded {timeout_seconds:g} seconds")
            require(result == 0x00000000, f"process wait failed: 0x{result:08x}")
            return_code = process.wait(timeout=10)
            while True:
                accounting = BASIC_ACCOUNTING()
                returned = wintypes.DWORD()
                require(
                    bool(
                        kernel32.QueryInformationJobObject(
                            job,
                            1,  # JobObjectBasicAccountingInformation
                            ctypes.byref(accounting),
                            ctypes.sizeof(accounting),
                            ctypes.byref(returned),
                        )
                    ),
                    f"QueryInformationJobObject failed: {ctypes.get_last_error()}",
                )
                if accounting.ActiveProcesses == 0:
                    break
                if time.monotonic() >= deadline:
                    kernel32.TerminateJobObject(job, 124)
                    raise BuildError(
                        f"{process_label} process tree exceeded {timeout_seconds:g} seconds"
                    )
                time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
            require(
                return_code == 0,
                f"{process_label} failed with exit code {return_code}; see {log_path}",
            )
    finally:
        if process is not None and process.poll() is None:
            if process_assigned:
                kernel32.TerminateJobObject(job, 125)
            else:
                with contextlib.suppress(Exception):
                    process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=10)
        if primary_thread is not None:
            kernel32.CloseHandle(wintypes.HANDLE(primary_thread))
        kernel32.CloseHandle(job)
    return {
        "exit_code": 0,
        "seconds": round(time.monotonic() - started, 3),
        "launch_capture": list(WINDOWS_JOB_LAUNCH_CAPTURE),
    }


def audit_pdf_bytes(payload: bytes) -> dict[str, object]:
    """Perform deterministic structural checks without claiming visual QA."""
    require(bool(payload), "PDF is empty")
    require(
        bool(re.match(br"%PDF-1\.[0-9]\r?\n", payload[:16])),
        "PDF header is malformed",
    )
    require(
        bool(re.search(br"startxref\s+\d+\s+%%EOF\s*\Z", payload, re.DOTALL)),
        "PDF trailer is malformed or missing",
    )
    try:
        import pypdf
    except ImportError as exc:  # pragma: no cover - production dependency failure
        raise BuildError("pypdf is required for the PDF page-count audit") from exc
    try:
        reader = pypdf.PdfReader(io.BytesIO(payload), strict=True)
        require(not reader.is_encrypted, "PDF is unexpectedly encrypted")
        pages = len(reader.pages)
    except BuildError:
        raise
    except Exception as exc:
        raise BuildError(f"PDF parser rejected the candidate: {exc}") from exc
    require(pages > 0, "PDF has no pages")
    return {
        "schema": "openlogic-partial-reader-pdf-audit-v1",
        "status": "PASS_STRUCTURAL_ONLY",
        "bytes": len(payload),
        "sha256": sha256_bytes(payload),
        "header": payload.splitlines()[0].decode("ascii", errors="replace"),
        "trailer_eof": True,
        "pages": pages,
        "parser": {"name": "pypdf", "version": pypdf.__version__},
        "visual_qa_performed": False,
    }


def _first_nonempty_line(payload: bytes) -> str:
    for line in payload.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return line.strip()
    raise BuildError("tool version probe returned an empty log")


def run_pdf_build(
    source_root: Path,
    tex_name: str,
    output_root: Path,
    *,
    modified: str,
    expected_source_tree: Mapping[str, object],
    mutex_timeout_seconds: float,
    pdf_timeout_seconds: float,
) -> tuple[bytes, dict[str, object]]:
    """Build twice under one mutex hold and require byte-identical reader PDFs."""
    modified = parse_modified(modified)
    require(pdf_timeout_seconds > 0, "PDF build timeout must be positive")
    relative_tex = safe_relative_path(tex_name, label="cumulative TeX path")
    require(len(relative_tex.parts) == 1, "cumulative TeX input must be at source-tree root")
    tex_path = source_root / relative_tex.name
    require(tex_path.is_file(), f"missing cumulative TeX input: {tex_path}")
    observed_tree = directory_tree_identity(source_root)
    require(
        observed_tree == dict(expected_source_tree),
        "PDF source tree does not match the exact packaged source tree",
    )
    tex_record = file_record(tex_path, relative_to=source_root)

    driver = shutil.which(PDF_DRIVER)
    engine = shutil.which(PDF_ENGINE)
    require(bool(driver), f"{PDF_DRIVER} is not available on PATH")
    require(bool(engine), f"{PDF_ENGINE} is not available on PATH")
    output_root.mkdir(parents=True, exist_ok=False)
    source_date_epoch = str(calendar.timegm(time.strptime(modified, "%Y-%m-%d")))
    environment = os.environ.copy()
    environment.update(
        {
            "SOURCE_DATE_EPOCH": source_date_epoch,
            "FORCE_SOURCE_DATE": "1",
            "TZ": "UTC",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    fixed_environment = {
        key: environment[key]
        for key in ("SOURCE_DATE_EPOCH", "FORCE_SOURCE_DATE", "TZ", "LC_ALL", "LANG")
    }
    pdf_started = time.monotonic()
    deadline = pdf_started + pdf_timeout_seconds

    def remaining() -> float:
        value = deadline - time.monotonic()
        require(value > 0, f"PDF process trees exceeded {pdf_timeout_seconds:g} seconds")
        return value

    def captured_run(
        command: Sequence[str],
        log_path: Path,
        process_label: str,
        *,
        receipt_command: Sequence[str],
    ) -> dict[str, object]:
        run = _run_in_windows_job(
            command,
            source_root,
            remaining(),
            log_path,
            environment,
            process_label=process_label,
        )
        require(log_path.is_file(), f"{process_label} completed without a captured log")
        log_payload = log_path.read_bytes()
        return {
            **run,
            "command": list(receipt_command),
            "log_bytes": len(log_payload),
            "log_sha256": sha256_bytes(log_payload),
            "log_retained": False,
        }

    engine_executable = str(engine)
    if any(char.isspace() for char in engine_executable):
        engine_executable = f'"{engine_executable}"'
    engine_template = (
        f"{engine_executable} %O -interaction=nonstopmode -halt-on-error "
        "-file-line-error -recorder %S"
    )
    mutex_wait_started = time.monotonic()
    with GlobalTexMutex(mutex_timeout_seconds) as mutex:
        mutex_acquired = time.monotonic()
        driver_version_log = output_root / "latexmk-version.log"
        driver_probe = captured_run(
            [str(driver), "-version"],
            driver_version_log,
            "latexmk version probe",
            receipt_command=[PDF_DRIVER, "-version"],
        )
        driver_probe["version"] = _first_nonempty_line(driver_version_log.read_bytes())
        engine_version_log = output_root / "pdflatex-version.log"
        engine_probe = captured_run(
            [str(engine), "--version"],
            engine_version_log,
            "pdflatex version probe",
            receipt_command=[PDF_ENGINE, "--version"],
        )
        engine_probe["version"] = _first_nonempty_line(engine_version_log.read_bytes())

        pdf_payloads: list[bytes] = []
        pdf_audits: list[dict[str, object]] = []
        runs: list[dict[str, object]] = []
        for number in range(1, PDF_REPLAY_COUNT + 1):
            replay_root = output_root / f"pdf-replay-{number}"
            replay_root.mkdir()
            command = [
                str(driver),
                "-norc",
                "-gg",
                "-pdf",
                f"-pdflatex={engine_template}",
                f"-outdir={replay_root}",
                relative_tex.name,
            ]
            log_path = output_root / f"pdf-replay-{number}.log"
            receipt_engine_template = (
                f"{PDF_ENGINE} %O -interaction=nonstopmode -halt-on-error "
                "-file-line-error -recorder %S"
            )
            receipt_command = [
                PDF_DRIVER,
                "-norc",
                "-gg",
                "-pdf",
                f"-pdflatex={receipt_engine_template}",
                f"-outdir=<PDF_REPLAY_{number}_DIRECTORY>",
                relative_tex.name,
            ]
            run = captured_run(
                command,
                log_path,
                f"PDF replay {number}",
                receipt_command=receipt_command,
            )
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            require(
                not any(
                    marker in log_text
                    for marker in (
                        "! LaTeX Error:",
                        "Emergency stop.",
                        "Fatal error occurred",
                        "No pages of output.",
                    )
                ),
                f"PDF replay {number} returned success but its log contains a fatal TeX marker",
            )
            pdf_path = replay_root / f"{Path(relative_tex.name).stem}.pdf"
            require(pdf_path.is_file(), f"PDF replay {number} produced no PDF")
            payload = pdf_path.read_bytes()
            audit = audit_pdf_bytes(payload)
            run["pdf"] = {
                "bytes": len(payload),
                "sha256": sha256_bytes(payload),
                "pages": audit["pages"],
            }
            pdf_payloads.append(payload)
            pdf_audits.append(audit)
            runs.append(run)
        require(
            all(payload == pdf_payloads[0] for payload in pdf_payloads[1:]),
            "PDF replay was nondeterministic: exact bytes differ",
        )
        require(
            directory_tree_identity(source_root) == observed_tree,
            "PDF build mutated the packaged source tree",
        )
        mutex_hold_seconds = round(time.monotonic() - mutex_acquired, 3)

    payload = pdf_payloads[0]
    audit = pdf_audits[0]
    return payload, {
        "schema": "openlogic-partial-reader-pdf-build-v1",
        "status": "PASS_REPEATABLE_BYTES",
        "driver": {"name": PDF_DRIVER, **driver_probe},
        "engine": {"name": PDF_ENGINE, **engine_probe},
        "input": {
            "cumulative_tex": tex_record,
            "source_tree": observed_tree,
        },
        "environment": fixed_environment,
        "command_path_policy": (
            "Commands retain exact options and relative inputs while local executable/output "
            "paths use portable tool names and explicit directory placeholders."
        ),
        "mutex": TEX_MUTEX_NAME,
        "mutex_abandoned_recovery": mutex.abandoned,
        "mutex_wait_seconds": round(mutex_acquired - mutex_wait_started, 3),
        "mutex_hold_seconds": mutex_hold_seconds,
        "process_tree_guard": "Windows job object, kill on close",
        "launch_capture": list(WINDOWS_JOB_LAUNCH_CAPTURE),
        "timeout_seconds": pdf_timeout_seconds,
        "total_seconds": round(time.monotonic() - pdf_started, 3),
        "runs": runs,
        "repeatability": {
            "runs": PDF_REPLAY_COUNT,
            "comparison": "exact byte equality",
            "status": "PASS",
        },
        "pdf": audit,
    }


def epubcheck_runtime_identity(jar: Path) -> dict[str, object]:
    """Hash the validator JAR and every adjacent runtime dependency JAR."""
    lib = jar.parent / "lib"
    require(lib.is_dir(), "EPUBCheck distribution lacks its lib directory")
    paths = [jar, *sorted(lib.glob("*.jar"))]
    require(len(paths) > 1, "EPUBCheck distribution has no dependency JARs")
    records = [file_record(path, relative_to=jar.parent) for path in paths]
    return {
        "files": len(records),
        "bytes": sum(int(row["bytes"]) for row in records),
        "tree_sha256": tree_digest(records),
    }


def resolve_epubcheck_jar(repo_root: Path, requested: Path | None = None) -> Path:
    """Resolve and byte-pin the already-installed EPUBCheck 5.3.0 distribution."""
    candidates: list[Path]
    if requested is not None:
        candidates = [requested]
    elif os.environ.get(EPUBCHECK_ENVIRONMENT_VARIABLE):
        candidates = [Path(os.environ[EPUBCHECK_ENVIRONMENT_VARIABLE])]
    else:
        candidates = [
            repo_root / "tools" / f"epubcheck-{EPUBCHECK_VERSION}" / "epubcheck.jar",
            Path.home()
            / "Documents"
            / "Codex"
            / "2026-08-13"
            / "read-transcript-for-task"
            / "work"
            / "openlogic-accessible-complete-book"
            / "tools"
            / f"epubcheck-{EPUBCHECK_VERSION}"
            / "epubcheck.jar",
        ]
    jar = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
    require(
        jar is not None,
        f"EPUBCheck {EPUBCHECK_VERSION} is unavailable; pass --epubcheck-jar or set "
        f"{EPUBCHECK_ENVIRONMENT_VARIABLE}",
    )
    payload = jar.read_bytes()
    require(
        sha256_bytes(payload) == EPUBCHECK_JAR_SHA256,
        f"EPUBCheck {EPUBCHECK_VERSION} JAR digest mismatch",
    )
    runtime = epubcheck_runtime_identity(jar)
    require(
        runtime["tree_sha256"] == EPUBCHECK_RUNTIME_TREE_SHA256,
        f"EPUBCheck {EPUBCHECK_VERSION} runtime tree digest mismatch",
    )
    return jar


def run_epubcheck(
    payload: bytes,
    work_root: Path,
    *,
    jar: Path,
    timeout_seconds: float,
) -> dict[str, object]:
    """Validate exact EPUB bytes and return a deterministic, byte-bound receipt."""
    require(bool(payload), "refusing to validate an empty EPUB")
    require(timeout_seconds > 0, "EPUBCheck timeout must be positive")
    jar = jar.resolve()
    require(jar.is_file(), "resolved EPUBCheck JAR disappeared before validation")
    jar_payload = jar.read_bytes()
    require(
        sha256_bytes(jar_payload) == EPUBCHECK_JAR_SHA256,
        f"EPUBCheck {EPUBCHECK_VERSION} JAR changed before validation",
    )
    runtime = epubcheck_runtime_identity(jar)
    require(
        runtime["tree_sha256"] == EPUBCHECK_RUNTIME_TREE_SHA256,
        f"EPUBCheck {EPUBCHECK_VERSION} runtime tree changed before validation",
    )
    java = shutil.which("java")
    require(bool(java), "Java is unavailable for EPUBCheck")

    work_root.mkdir(parents=True, exist_ok=False)
    epub_path = work_root / "candidate.epub"
    report_path = work_root / "epubcheck.json"
    log_path = work_root / "epubcheck.log"
    epub_path.write_bytes(payload)
    expected = {"bytes": len(payload), "sha256": sha256_bytes(payload)}
    command = [
        str(java),
        "-jar",
        str(jar),
        str(epub_path),
        "--profile",
        "default",
        "--failonwarnings",
        "--locale",
        "en",
        "--json",
        str(report_path),
        "--quiet",
    ]
    environment = os.environ.copy()
    environment.update({"TZ": "UTC"})
    _run_in_windows_job(
        command,
        work_root,
        timeout_seconds,
        log_path,
        environment,
        process_label="EPUBCheck",
    )
    require(report_path.is_file(), "EPUBCheck returned success without a JSON report")
    checked_payload = epub_path.read_bytes()
    require(
        checked_payload == payload,
        "EPUB bytes changed while EPUBCheck was running",
    )
    require(
        epubcheck_runtime_identity(jar) == runtime,
        "EPUBCheck runtime tree changed while validation was running",
    )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"EPUBCheck emitted an invalid JSON report: {exc}") from exc
    require(isinstance(report, dict), "EPUBCheck JSON report is not an object")
    checker = report.get("checker")
    require(isinstance(checker, dict), "EPUBCheck JSON report lacks checker metadata")
    require(
        checker.get("checkerVersion") == EPUBCHECK_VERSION,
        f"EPUBCheck version mismatch: {checker.get('checkerVersion')!r}",
    )
    counts: dict[str, int] = {}
    for key, label in (("nFatal", "fatal"), ("nError", "error"), ("nWarning", "warning")):
        value = checker.get(key)
        require(isinstance(value, int) and not isinstance(value, bool), f"invalid EPUBCheck {key}")
        counts[label] = value
    require(counts["fatal"] == 0, f"EPUBCheck reported {counts['fatal']} fatal message(s)")
    require(counts["error"] == 0, f"EPUBCheck reported {counts['error']} error(s)")
    require(counts["warning"] == 0, f"EPUBCheck reported {counts['warning']} warning(s)")
    messages = report.get("messages")
    require(isinstance(messages, list), "EPUBCheck JSON messages field is not a list")
    require(not messages, f"EPUBCheck report contains {len(messages)} message(s)")
    require(
        {"bytes": len(checked_payload), "sha256": sha256_bytes(checked_payload)} == expected,
        "EPUBCheck receipt does not bind the checked EPUB bytes",
    )
    return {
        "schema": "openlogic-partial-epubcheck-receipt-v1",
        "status": "PASS",
        "validator": {
            "name": "EPUBCheck",
            "version": EPUBCHECK_VERSION,
            "jar_bytes": len(jar_payload),
            "jar_sha256": sha256_bytes(jar_payload),
            "runtime_files": runtime["files"],
            "runtime_bytes": runtime["bytes"],
            "runtime_tree_sha256": runtime["tree_sha256"],
        },
        "invocation": {
            "profile": "default",
            "fail_on_warnings": True,
            "locale": "en",
            "quiet": True,
            "timeout_seconds": timeout_seconds,
            "process_tree_guard": "Windows job object, kill on close",
            "launch_capture": list(WINDOWS_JOB_LAUNCH_CAPTURE),
        },
        "checked_epub": expected,
        "result": {**counts, "messages": 0},
    }


def run_make4ht(
    source_root: Path,
    tex_name: str,
    output_root: Path,
    *,
    modified: str,
    mutex_timeout_seconds: float,
    tex_timeout_seconds: float,
) -> dict[str, object]:
    modified = parse_modified(modified)
    executable = shutil.which("make4ht")
    require(bool(executable), "make4ht is not available on PATH")
    build_dir = output_root / "make4ht-build"
    html_dir = output_root / "make4ht-output"
    build_dir.mkdir(parents=True, exist_ok=False)
    html_dir.mkdir(parents=True, exist_ok=False)
    command = [
        str(executable),
        "-a",
        "warning",
        "-f",
        "html5",
        "-B",
        str(build_dir),
        "-d",
        str(html_dir),
        tex_name,
        "mathml",
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "SOURCE_DATE_EPOCH": str(calendar.timegm(time.strptime(modified, "%Y-%m-%d"))),
            "FORCE_SOURCE_DATE": "1",
            "TZ": "UTC",
        }
    )
    log_path = output_root / "make4ht.log"
    with GlobalTexMutex(mutex_timeout_seconds) as mutex:
        run = _run_in_windows_job(
            command,
            source_root,
            tex_timeout_seconds,
            log_path,
            environment,
        )
        require(log_path.is_file(), "make4ht completed without a captured log")
        log_payload = log_path.read_bytes()
        log_text = log_payload.decode("utf-8", errors="replace")
        fatal_markers = (
            "! LaTeX Error:",
            "Emergency stop.",
            "Fatal error occurred",
            "No pages of output.",
        )
        require(
            not any(marker in log_text for marker in fatal_markers),
            "make4ht returned success but its captured log contains a fatal TeX marker",
        )
        run.update({
            "command": [
                "make4ht",
                "-a",
                "warning",
                "-f",
                "html5",
                "-B",
                "<MAKE4HT_BUILD_DIRECTORY>",
                "-d",
                "<MAKE4HT_OUTPUT_DIRECTORY>",
                tex_name,
                "mathml",
            ],
            "command_path_policy": (
                "Exact options and relative input are retained; local executable/output "
                "paths use portable names and explicit directory placeholders."
            ),
            "mutex": TEX_MUTEX_NAME,
            "mutex_abandoned_recovery": mutex.abandoned,
            "source_date_epoch": environment["SOURCE_DATE_EPOCH"],
            "timeout_seconds": tex_timeout_seconds,
            "log_bytes": len(log_payload),
            "log_sha256": sha256_bytes(log_payload),
            "log_retained": False,
            "output": str(html_dir),
        })
        return run


def _local_name(tag: str) -> str:
    return etree.QName(tag).localname.lower() if tag.startswith("{") else tag.lower()


def _qualified_attribute(name: str) -> str:
    if name.startswith("{"):
        return name
    if name == "xml:lang":
        return f"{{{XML_NS}}}lang"
    if name == "xlink:href":
        return f"{{{XLINK_NS}}}href"
    return name


def _clone_namespaced(node: etree._Element, inherited: str = XHTML_NS, *, root: bool = False) -> etree._Element:
    local = _local_name(str(node.tag))
    if local == "math" or inherited == MATHML_NS:
        namespace = MATHML_NS
    elif local == "svg" or inherited == SVG_NS:
        namespace = SVG_NS
    else:
        namespace = XHTML_NS
    if root:
        nsmap = {None: XHTML_NS, "epub": EPUB_NS}
    elif local in {"math", "svg"}:
        nsmap = {None: namespace}
    else:
        nsmap = None
    result = etree.Element(f"{{{namespace}}}{local}", nsmap=nsmap)
    for key, value in node.attrib.items():
        if str(key) == "xmlns" or str(key).startswith("xmlns:"):
            continue
        attr_local = _local_name(str(key))
        require(not attr_local.startswith("on"), f"event-handler attribute is forbidden: {key}")
        result.set(_qualified_attribute(str(key)), value)
    result.text = node.text
    result.tail = node.tail
    for child in node:
        if isinstance(child.tag, str):
            result.append(_clone_namespaced(child, namespace))
    return result


def normalize_generated_document(payload: bytes, *, title: str) -> etree._Element:
    require(b"<!ENTITY" not in payload and b"<!entity" not in payload, "generated HTML declares entities")
    try:
        parsed = etree.fromstring(
            payload,
            parser=etree.XMLParser(resolve_entities=False, no_network=True, recover=False, huge_tree=True),
        )
    except etree.XMLSyntaxError:
        parsed = html.document_fromstring(
            payload,
            parser=html.HTMLParser(encoding="utf-8", remove_comments=False, recover=True, huge_tree=True),
        )
    require(_local_name(str(parsed.tag)) == "html", "generated document has no html root")
    root = _clone_namespaced(parsed, root=True)
    root.set("lang", "nl-NL")
    root.set(f"{{{XML_NS}}}lang", "nl-NL")
    scripts = root.xpath(".//*[local-name()='script']")
    require(not scripts, "generated document contains script")
    heads = root.xpath("./x:head", namespaces=NS)
    bodies = root.xpath("./x:body", namespaces=NS)
    require(len(heads) == 1 and len(bodies) == 1, "generated document lacks one head and body")
    titles = heads[0].xpath("./x:title", namespaces=NS)
    if not titles:
        title_node = etree.SubElement(heads[0], f"{{{XHTML_NS}}}title")
        title_node.text = title
    elif not "".join(titles[0].itertext()).strip():
        titles[0].text = title
    for existing in list(heads[0].xpath("./x:meta", namespaces=NS)):
        if existing.get("charset") is not None or (
            existing.get("http-equiv") or ""
        ).lower() == "content-type":
            heads[0].remove(existing)
    charset = etree.Element(f"{{{XHTML_NS}}}meta")
    charset.set("charset", "utf-8")
    heads[0].insert(0, charset)
    return root


def serialize_xhtml(root: etree._Element) -> bytes:
    return etree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        doctype="<!DOCTYPE html>",
        method="xml",
        pretty_print=False,
    )


def _output_path_for_generated(relative: PurePosixPath) -> PurePosixPath:
    if relative.suffix.lower() in {".html", ".xhtml", ".htm"}:
        return PurePosixPath("OEBPS/generated") / relative.with_suffix(".xhtml")
    return PurePosixPath("OEBPS/generated") / relative


def _discover_generated_files(root: Path) -> dict[PurePosixPath, Path]:
    require(root.is_dir(), f"missing make4ht output: {root}")
    result: dict[PurePosixPath, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BuildError(f"make4ht output symlink is forbidden: {path}")
        if not path.is_file():
            continue
        relative = safe_relative_path(path.relative_to(root).as_posix(), label="make4ht output path")
        suffix = relative.suffix.lower()
        if suffix in {".html", ".xhtml", ".htm"} or suffix in ALLOWED_GENERATED_ASSET_SUFFIXES:
            result[relative] = path
    require(any(path.suffix.lower() in {".html", ".xhtml", ".htm"} for path in result), "make4ht emitted no HTML")
    outputs = [_output_path_for_generated(path) for path in result]
    require(len(outputs) == len(set(outputs)), "make4ht paths collide after XHTML normalization")
    return result


def _rewrite_document_links(
    root: etree._Element,
    source: PurePosixPath,
    output: PurePosixPath,
    path_map: Mapping[PurePosixPath, PurePosixPath],
) -> None:
    for node in root.xpath(".//*[@href or @src]"):
        for attribute in ("href", "src"):
            value = node.get(attribute)
            if not value:
                continue
            split = urlsplit(value)
            if split.scheme or split.netloc or value.startswith("//"):
                require(split.scheme.lower() in EXTERNAL_LINK_SCHEMES, f"forbidden external scheme: {value}")
                continue
            if not split.path:
                continue
            decoded = unquote(split.path)
            candidate = PurePosixPath(posixpath.normpath(posixpath.join(source.parent.as_posix(), decoded)))
            require(safe_archive_name(candidate.as_posix()), f"generated link escapes output tree: {source} -> {value}")
            require(candidate in path_map, f"generated link target is absent: {source} -> {value}")
            mapped = path_map[candidate]
            relative = posixpath.relpath(mapped.as_posix(), output.parent.as_posix())
            node.set(attribute, urlunsplit(("", "", relative, split.query, split.fragment)))


def _add_reader_css(root: etree._Element, output: PurePosixPath) -> None:
    head = root.xpath("./x:head", namespaces=NS)[0]
    target = PurePosixPath("OEBPS/styles/reader.css")
    href = posixpath.relpath(target.as_posix(), output.parent.as_posix())
    node = etree.SubElement(head, f"{{{XHTML_NS}}}link")
    node.set("rel", "stylesheet")
    node.set("href", href)


READER_CSS = b"""/* Deterministic reflowable partial-reader styles. */
html { line-height: 1.55; }
body { margin: 5%; max-width: 72rem; overflow-wrap: anywhere; }
nav, header, footer, main, section, article, aside { display: block; }
a { text-decoration: underline; }
img, svg, math, table { max-width: 100%; }
math { overflow-wrap: normal; }
table { border-collapse: collapse; }
th, td { padding: 0.25em 0.5em; vertical-align: top; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
.scope-note { border: 0.08em solid currentColor; padding: 0.75em; margin: 1em 0; }
@page { margin: 1em; }
"""


def xhtml_element(tag: str, parent: etree._Element | None = None, **attributes: str) -> etree._Element:
    node = etree.Element(f"{{{XHTML_NS}}}{tag}") if parent is None else etree.SubElement(
        parent, f"{{{XHTML_NS}}}{tag}"
    )
    for key, value in attributes.items():
        node.set(key, value)
    return node


def make_navigation(
    plan: AcceptedPlan,
    register: str,
    unit_targets: Mapping[str, tuple[PurePosixPath, str]],
) -> bytes:
    config = REGISTER_CONFIG[register]
    root = etree.Element(f"{{{XHTML_NS}}}html", nsmap={None: XHTML_NS, "epub": EPUB_NS})
    root.set("lang", "nl-NL")
    root.set(f"{{{XML_NS}}}lang", "nl-NL")
    head = xhtml_element("head", root)
    xhtml_element("meta", head, charset="utf-8")
    title = xhtml_element("title", head)
    title.text = "The Open Logic Text — inhoud"
    xhtml_element("link", head, rel="stylesheet", href="styles/reader.css")
    body = xhtml_element("body", root)
    body.set(f"{{{EPUB_NS}}}type", "frontmatter")
    heading = xhtml_element("h1", body)
    heading.text = "The Open Logic Text"
    subtitle = xhtml_element("p", body)
    subtitle.text = (
        f"Gedeeltelijke Nederlandstalige editie — {config['label']} — "
        f"{plan.first_id} tot en met {plan.last_id}"
    )
    note = xhtml_element("p", body, **{"class": "scope-note"})
    note.text = "Alleen de aaneengesloten geaccepteerde prefix is opgenomen."

    toc = xhtml_element("nav", body, id="toc")
    toc.set(f"{{{EPUB_NS}}}type", "toc")
    toc.set("role", "doc-toc")
    toc.set("aria-label", "Inhoudsopgave")
    toc_heading = xhtml_element("h2", toc)
    toc_heading.text = "Inhoud"
    items = xhtml_element("ol", toc)
    for row in plan.accepted_units:
        unit_id = str(row["unit_id"])
        require(unit_id in unit_targets, f"accepted unit anchor missing from make4ht output: {unit_id}")
        path, anchor = unit_targets[unit_id]
        li = xhtml_element("li", items)
        link = xhtml_element("a", li)
        link.set("href", f"{path.relative_to(PurePosixPath('OEBPS')).as_posix()}#{anchor}")
        link.text = f"{unit_id} — {row['source_path']}"

    landmarks = xhtml_element("nav", body)
    landmarks.set(f"{{{EPUB_NS}}}type", "landmarks")
    landmarks.set("aria-label", "Publicatielocaties")
    landmark_heading = xhtml_element("h2", landmarks)
    landmark_heading.text = "Oriëntatiepunten"
    landmark_list = xhtml_element("ol", landmarks)
    li = xhtml_element("li", landmark_list)
    link = xhtml_element("a", li)
    first_path, first_anchor = unit_targets[plan.first_id]
    link.set("href", f"{first_path.relative_to(PurePosixPath('OEBPS')).as_posix()}#{first_anchor}")
    link.set(f"{{{EPUB_NS}}}type", "bodymatter")
    link.text = "Begin van de opgenomen tekst"
    return etree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        doctype="<!DOCTYPE html>",
        method="xml",
        pretty_print=False,
    )


def media_type(path: PurePosixPath) -> str:
    if path.suffix.lower() == ".xhtml":
        return "application/xhtml+xml"
    try:
        return ALLOWED_GENERATED_ASSET_SUFFIXES[path.suffix.lower()]
    except KeyError as exc:
        raise BuildError(f"unsupported EPUB resource type: {path}") from exc


def item_id(path: PurePosixPath) -> str:
    return "item-" + sha256_bytes(path.as_posix().encode("utf-8"))[:20]


def make_package_document(
    plan: AcceptedPlan,
    register: str,
    modified: str,
    resources: Mapping[PurePosixPath, bytes],
    spine_paths: Sequence[PurePosixPath],
) -> bytes:
    config = REGISTER_CONFIG[register]
    package = etree.Element(f"{{{OPF_NS}}}package", nsmap={None: OPF_NS, "dc": DC_NS})
    package.set("version", "3.0")
    package.set("unique-identifier", "pub-id")
    package.set(f"{{{XML_NS}}}lang", "nl-NL")
    package.set(
        "prefix",
        "rendition: http://www.idpf.org/vocab/rendition/# dcterms: http://purl.org/dc/terms/",
    )
    metadata = etree.SubElement(package, f"{{{OPF_NS}}}metadata")

    def dc(name: str, value: str, identifier: str | None = None) -> None:
        node = etree.SubElement(metadata, f"{{{DC_NS}}}{name}")
        node.text = value
        if identifier:
            node.set("id", identifier)

    def meta(property_name: str, value: str) -> None:
        node = etree.SubElement(metadata, f"{{{OPF_NS}}}meta")
        node.set("property", property_name)
        node.text = value

    identity_frame = "\n".join(
        [register, plan.alignment_sha256, *(
            f"{row['unit_id']}:{row[config['sha_key']]}" for row in plan.accepted_units
        )]
    ).encode("utf-8")
    dc("identifier", f"urn:sha256:{sha256_bytes(identity_frame)}", "pub-id")
    dc(
        "title",
        f"The Open Logic Text — gedeeltelijke Nederlandstalige editie ({config['label']}, {plan.first_id}–{plan.last_id})",
    )
    dc("language", "nl-NL")
    dc("creator", "The Open Logic Project and credited contributors")
    dc("publisher", "The Open Logic Project")
    dc("date", modified)
    dc("type", "Textbook")
    dc("subject", "Wiskundige logica")
    dc(
        "description",
        f"Reflowable EPUB 3 met native MathML; uitsluitend de aaneengesloten geaccepteerde prefix {plan.first_id}–{plan.last_id} ({len(plan.accepted_units)} broneenheden).",
    )
    dc("source", "The Open Logic Text, frozen source revision(s): " + ", ".join(plan.source_revisions))
    dc("rights", "Creative Commons Attribution 4.0 International (CC BY 4.0).")
    meta("dcterms:modified", f"{modified}T00:00:00Z")
    meta("rendition:layout", "reflowable")
    meta("rendition:orientation", "auto")
    meta("rendition:spread", "auto")

    manifest = etree.SubElement(package, f"{{{OPF_NS}}}manifest")
    ids: dict[PurePosixPath, str] = {}
    for path, payload in sorted(resources.items(), key=lambda item: item[0].as_posix()):
        require(path.parts[0] == "OEBPS", f"resource outside OEBPS: {path}")
        identifier = item_id(path)
        ids[path] = identifier
        node = etree.SubElement(manifest, f"{{{OPF_NS}}}item")
        node.set("id", identifier)
        node.set("href", path.relative_to(PurePosixPath("OEBPS")).as_posix())
        node.set("media-type", media_type(path))
        properties: list[str] = []
        if path == PurePosixPath("OEBPS/nav.xhtml"):
            properties.append("nav")
        if path.suffix.lower() == ".xhtml":
            root = etree.fromstring(
                payload,
                parser=etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True),
            )
            if root.xpath(".//*[local-name()='math']"):
                properties.append("mathml")
            if root.xpath(".//*[local-name()='svg']"):
                properties.append("svg")
            require(not root.xpath(".//*[local-name()='script']"), f"script in EPUB resource: {path}")
        if properties:
            node.set("properties", " ".join(properties))

    spine = etree.SubElement(package, f"{{{OPF_NS}}}spine")
    for path in spine_paths:
        require(path in ids, f"spine resource is absent from manifest: {path}")
        node = etree.SubElement(spine, f"{{{OPF_NS}}}itemref")
        node.set("idref", ids[path])
        node.set("linear", "yes")
    return etree.tostring(package, encoding="utf-8", xml_declaration=True, pretty_print=True)


def make_container() -> bytes:
    root = etree.Element(f"{{{CONTAINER_NS}}}container", nsmap={None: CONTAINER_NS})
    root.set("version", "1.0")
    rootfiles = etree.SubElement(root, f"{{{CONTAINER_NS}}}rootfiles")
    node = etree.SubElement(rootfiles, f"{{{CONTAINER_NS}}}rootfile")
    node.set("full-path", "OEBPS/package.opf")
    node.set("media-type", "application/oebps-package+xml")
    return etree.tostring(root, encoding="utf-8", xml_declaration=True, pretty_print=True)


def package_make4ht_output(
    plan: AcceptedPlan,
    register: str,
    modified: str,
    generated_root: Path,
) -> tuple[bytes, dict[str, object]]:
    discovered = _discover_generated_files(generated_root)
    path_map = {source: _output_path_for_generated(source) for source in discovered}
    resources: dict[PurePosixPath, bytes] = {}
    unit_targets: dict[str, tuple[PurePosixPath, str]] = {}
    math_roots = 0
    html_paths = [
        source for source in sorted(discovered) if source.suffix.lower() in {".html", ".xhtml", ".htm"}
    ]
    for source in html_paths:
        output = path_map[source]
        root = normalize_generated_document(
            discovered[source].read_bytes(),
            title=f"The Open Logic Text — {REGISTER_CONFIG[register]['label']}",
        )
        _rewrite_document_links(root, source, output, path_map)
        _add_reader_css(root, output)
        ids = [str(value) for value in root.xpath("//@id") if value]
        require(len(ids) == len(set(ids)), f"duplicate ID in generated document: {source}")
        for identifier in ids:
            match = re.fullmatch(r"unit-(OLP-\d{4})", identifier)
            if match:
                unit_id = match.group(1)
                require(unit_id not in unit_targets, f"duplicate accepted-unit anchor: {unit_id}")
                unit_targets[unit_id] = (output, identifier)
        math_roots += len(root.xpath(".//*[local-name()='math']"))
        resources[output] = serialize_xhtml(root)
    require(set(unit_targets) == set(plan.accepted_ids), "make4ht accepted-unit anchor set drift")
    require(math_roots > 0, "make4ht output contains no native MathML")

    for source, path in sorted(discovered.items()):
        if source in html_paths:
            continue
        resources[path_map[source]] = path.read_bytes()
    resources[PurePosixPath("OEBPS/styles/reader.css")] = READER_CSS
    resources[PurePosixPath("OEBPS/nav.xhtml")] = make_navigation(plan, register, unit_targets)
    spine_paths = [PurePosixPath("OEBPS/nav.xhtml"), *(path_map[path] for path in html_paths)]
    require(len(spine_paths) == len(set(spine_paths)), "duplicate spine resource")
    package = make_package_document(plan, register, modified, resources, spine_paths)
    entries: dict[str, bytes] = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": make_container(),
        "OEBPS/package.opf": package,
        **{path.as_posix(): payload for path, payload in resources.items()},
    }
    epub = deterministic_zip_bytes(entries, epub=True)
    audit = audit_epub_bytes(epub, plan=plan, register=register)
    return epub, audit


def _parse_xml(payload: bytes, label: str) -> etree._Element:
    try:
        return etree.fromstring(
            payload,
            parser=etree.XMLParser(resolve_entities=False, no_network=True, recover=False, huge_tree=True),
        )
    except etree.XMLSyntaxError as exc:
        raise BuildError(f"invalid XML in {label}: {exc}") from exc


def _resolve_epub_link(source: PurePosixPath, href: str) -> tuple[PurePosixPath, str]:
    split = urlsplit(href)
    require(not split.query, f"local EPUB link has unsupported query: {source} -> {href}")
    if split.scheme or split.netloc or href.startswith("//"):
        require(split.scheme.lower() in EXTERNAL_LINK_SCHEMES, f"forbidden EPUB link scheme: {href}")
        return source, ""
    target = source if not split.path else PurePosixPath(
        posixpath.normpath(posixpath.join(source.parent.as_posix(), unquote(split.path)))
    )
    require(safe_archive_name(target.as_posix()), f"EPUB link escapes package: {source} -> {href}")
    return target, unquote(split.fragment)


CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(r"@import\s+(['\"])(.*?)\1", re.IGNORECASE)


def _css_links(payload: bytes, source: PurePosixPath) -> list[str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError(f"non-UTF-8 CSS resource: {source}") from exc
    values = [match.group(2).strip() for match in CSS_URL_RE.finditer(text)]
    values.extend(match.group(2).strip() for match in CSS_IMPORT_RE.finditer(text))
    return [value for value in values if value]


def audit_epub_bytes(
    payload: bytes,
    *,
    plan: AcceptedPlan,
    register: str,
) -> dict[str, object]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise BuildError(f"invalid EPUB ZIP: {exc}") from exc
    with archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        require(bool(names), "empty EPUB")
        require(len(names) == len(set(names)), "duplicate EPUB ZIP entry")
        require(all(safe_archive_name(name) for name in names), "unsafe EPUB ZIP path")
        require(names[0] == "mimetype", "mimetype is not first EPUB entry")
        require(infos[0].compress_type == zipfile.ZIP_STORED, "mimetype is compressed")
        require(archive.read("mimetype") == b"application/epub+zip", "wrong EPUB mimetype payload")
        require(names[1:] == sorted(names[1:]), "EPUB members after mimetype are not sorted")
        require("META-INF/container.xml" in names, "EPUB lacks container.xml")
        container = _parse_xml(archive.read("META-INF/container.xml"), "META-INF/container.xml")
        roots = container.xpath(".//*[local-name()='rootfile']/@full-path")
        require(roots == ["OEBPS/package.opf"], "container rootfile drift")
        require("OEBPS/package.opf" in names, "EPUB lacks package.opf")
        package = _parse_xml(archive.read("OEBPS/package.opf"), "OEBPS/package.opf")
        require(package.get("version") == "3.0", "package is not EPUB 3")
        require(package.get(f"{{{XML_NS}}}lang") == "nl-NL", "package language is not nl-NL")
        languages = package.xpath("./opf:metadata/dc:language/text()", namespaces=NS)
        require(languages == ["nl-NL"], "dc:language is not exactly nl-NL")
        titles = package.xpath("./opf:metadata/dc:title/text()", namespaces=NS)
        require(len(titles) == 1 and "gedeeltelijke Nederlandstalige editie" in titles[0], "scope title missing")
        descriptions = package.xpath("./opf:metadata/dc:description/text()", namespaces=NS)
        require(
            len(descriptions) == 1 and plan.first_id in descriptions[0] and plan.last_id in descriptions[0],
            "scope description missing accepted bounds",
        )
        layout = package.xpath(
            "./opf:metadata/opf:meta[@property='rendition:layout']/text()", namespaces=NS
        )
        require(layout == ["reflowable"], "EPUB is not declared reflowable")

        items = package.xpath("./opf:manifest/opf:item", namespaces=NS)
        id_to_path: dict[str, PurePosixPath] = {}
        path_to_item: dict[PurePosixPath, etree._Element] = {}
        for item in items:
            identifier = item.get("id") or ""
            href = unquote(item.get("href") or "")
            require(identifier and identifier not in id_to_path, f"duplicate/empty manifest id: {identifier}")
            relative = safe_relative_path(href, label="manifest href")
            path = PurePosixPath("OEBPS") / relative
            require(path not in path_to_item, f"duplicate manifest href: {path}")
            id_to_path[identifier] = path
            path_to_item[path] = item
            require(item.get("media-type") == media_type(path), f"manifest media type drift: {path}")
        expected_resources = {
            PurePosixPath(name)
            for name in names
            if name not in {"mimetype", "META-INF/container.xml", "OEBPS/package.opf"}
        }
        require(set(path_to_item) == expected_resources, "manifest does not exactly cover EPUB resources")
        nav_items = [item for item in items if "nav" in (item.get("properties") or "").split()]
        require(len(nav_items) == 1, "manifest lacks exactly one navigation item")
        nav_path = id_to_path[nav_items[0].get("id") or ""]
        require(nav_path == PurePosixPath("OEBPS/nav.xhtml"), "navigation path drift")

        spine = package.xpath("./opf:spine/opf:itemref", namespaces=NS)
        spine_paths: list[PurePosixPath] = []
        for item in spine:
            identifier = item.get("idref") or ""
            require(identifier in id_to_path, f"spine idref absent from manifest: {identifier}")
            path = id_to_path[identifier]
            require(path.suffix.lower() == ".xhtml", f"non-XHTML spine resource: {path}")
            spine_paths.append(path)
        require(spine_paths and spine_paths[0] == nav_path, "navigation is not first in spine")
        require(len(spine_paths) == len(set(spine_paths)), "duplicate spine resource")

        roots_by_path: dict[PurePosixPath, etree._Element] = {}
        ids_by_path: dict[PurePosixPath, set[str]] = {}
        math_roots = 0
        for path, item in path_to_item.items():
            if path.suffix.lower() != ".xhtml":
                continue
            root = _parse_xml(archive.read(path.as_posix()), path.as_posix())
            require(_local_name(str(root.tag)) == "html", f"non-HTML XHTML root: {path}")
            require(root.get("lang") == "nl-NL", f"XHTML language drift: {path}")
            require(root.get(f"{{{XML_NS}}}lang") == "nl-NL", f"XHTML xml:lang drift: {path}")
            require(not root.xpath(".//*[local-name()='script']"), f"script in XHTML: {path}")
            ids = [str(value) for value in root.xpath("//@id") if value]
            require(len(ids) == len(set(ids)), f"duplicate XHTML ID: {path}")
            ids_by_path[path] = set(ids)
            roots_by_path[path] = root
            count = len(root.xpath(".//*[namespace-uri()=$ns and local-name()='math']", ns=MATHML_NS))
            math_roots += count
            properties = (item.get("properties") or "").split()
            require((count > 0) == ("mathml" in properties), f"MathML manifest property drift: {path}")
        require(math_roots > 0, "EPUB contains no native MathML")

        local_links = 0
        fragment_links = 0
        for source, root in roots_by_path.items():
            for node in root.xpath(".//*[@href or @src]"):
                for attribute in ("href", "src"):
                    href = node.get(attribute)
                    if not href:
                        continue
                    split = urlsplit(href)
                    if split.scheme or split.netloc or href.startswith("//"):
                        require(split.scheme.lower() in EXTERNAL_LINK_SCHEMES, f"forbidden link: {href}")
                        continue
                    target, fragment = _resolve_epub_link(source, href)
                    require(target in expected_resources, f"broken local EPUB link: {source} -> {href}")
                    if fragment:
                        require(fragment in ids_by_path.get(target, set()), f"broken EPUB fragment: {source} -> {href}")
                        fragment_links += 1
                    local_links += 1

        css_links = 0
        svg_links = 0
        for source, item in path_to_item.items():
            media = item.get("media-type")
            if media == "text/css":
                for href in _css_links(archive.read(source.as_posix()), source):
                    split = urlsplit(href)
                    if split.scheme == "data":
                        continue
                    if split.scheme or split.netloc or href.startswith("//"):
                        require(
                            split.scheme.lower() in EXTERNAL_LINK_SCHEMES,
                            f"forbidden CSS link: {source} -> {href}",
                        )
                        continue
                    if not split.path:
                        continue
                    target, _ = _resolve_epub_link(source, href)
                    require(target in expected_resources, f"broken CSS resource link: {source} -> {href}")
                    css_links += 1
            elif media == "image/svg+xml":
                svg = _parse_xml(archive.read(source.as_posix()), source.as_posix())
                require(
                    not svg.xpath(".//*[local-name()='script']"),
                    f"script in SVG resource: {source}",
                )
                svg_ids = {str(value) for value in svg.xpath("//@id") if value}
                for node in svg.iter():
                    if not isinstance(node.tag, str):
                        continue
                    for key, href in node.attrib.items():
                        require(
                            not _local_name(str(key)).startswith("on"),
                            f"event handler in SVG resource: {source}",
                        )
                        if _local_name(str(key)) != "href" or not href:
                            continue
                        split = urlsplit(href)
                        if split.scheme == "data":
                            continue
                        if split.scheme or split.netloc or href.startswith("//"):
                            require(
                                split.scheme.lower() in EXTERNAL_LINK_SCHEMES,
                                f"forbidden SVG link: {source} -> {href}",
                            )
                            continue
                        if not split.path:
                            require(
                                not split.fragment or unquote(split.fragment) in svg_ids,
                                f"broken SVG fragment: {source} -> {href}",
                            )
                        else:
                            target, _ = _resolve_epub_link(source, href)
                            require(
                                target in expected_resources,
                                f"broken SVG resource link: {source} -> {href}",
                            )
                        svg_links += 1

        nav = roots_by_path[nav_path]
        toc = nav.xpath(".//x:nav[@epub:type='toc']", namespaces=NS)
        require(len(toc) == 1 and toc[0].get("role") == "doc-toc", "EPUB navigation contract drift")
        toc_links = toc[0].xpath(".//x:a/@href", namespaces=NS)
        require(len(toc_links) == len(plan.accepted_units), "TOC does not map every accepted unit")
        toc_units: list[str] = []
        for href in toc_links:
            target, fragment = _resolve_epub_link(nav_path, str(href))
            require(target in roots_by_path and fragment in ids_by_path[target], f"broken TOC target: {href}")
            match = re.fullmatch(r"unit-(OLP-\d{4})", fragment)
            require(bool(match), f"TOC target is not a unit anchor: {href}")
            toc_units.append(match.group(1))
        require(toc_units == list(plan.accepted_ids), "TOC accepted-unit order drift")
        all_unit_anchors = {
            match.group(1)
            for ids in ids_by_path.values()
            for identifier in ids
            if (match := re.fullmatch(r"unit-(OLP-\d{4})", identifier))
        }
        require(all_unit_anchors == set(plan.accepted_ids), "EPUB unit-anchor allowlist drift")
        return {
            "schema": "openlogic-partial-epub3-audit-v1",
            "status": "PASS",
            "register": register,
            "sha256": sha256_bytes(payload),
            "bytes": len(payload),
            "zip_entries": len(names),
            "manifest_items": len(items),
            "spine_items": len(spine_paths),
            "accepted_unit_anchors": len(all_unit_anchors),
            "mathml_roots": math_roots,
            "local_links": local_links,
            "fragment_links": fragment_links,
            "css_links": css_links,
            "svg_links": svg_links,
        }


def plan_report(plan: AcceptedPlan, output_dir: Path, modified: str | None) -> dict[str, object]:
    register_reports = []
    for register in plan.registers:
        base = output_basename(plan, register)
        config = REGISTER_CONFIG[register]
        inputs = [
            {
                "unit_id": row["unit_id"],
                "path": row[config["path_key"]],
                "bytes": row[config["bytes_key"]],
                "sha256": row[config["sha_key"]],
            }
            for row in plan.accepted_units
        ]
        register_reports.append(
            {
                "register": register,
                "label": config["label"],
                "input_tree_sha256": tree_digest(inputs),
                "accepted_translation_files": len(inputs),
                "artifact_order": list(ARTIFACT_ORDER),
                "outputs": {
                    "pdf": str(output_dir / register / f"{base}.pdf"),
                    "latex": str(output_dir / register / f"{base}.tex"),
                    "source_zip": str(output_dir / register / f"{base}-sources.zip"),
                    "epub": str(output_dir / register / f"{base}.epub"),
                    "epubcheck_receipt": str(
                        output_dir / register / f"{base}-EPUBCHECK.json"
                    ),
                    "receipt": str(output_dir / register / f"{base}-BUILD.json"),
                },
            }
        )
    return {
        "schema": "openlogic-partial-reader-build-plan-v1",
        "status": "DRY_RUN" if modified is None else "PLANNED",
        "alignment": {
            "path": str(plan.alignment_path),
            "sha256": plan.alignment_sha256,
        },
        "accepted_prefix": {
            "first": plan.first_id,
            "last": plan.last_id,
            "units": len(plan.accepted_units),
            "contiguous": True,
            "first_excluded": plan.first_excluded["unit_id"] if plan.first_excluded else None,
        },
        "source_revisions": list(plan.source_revisions),
        "modified": modified,
        "artifact_order": list(ARTIFACT_ORDER),
        "pdf": {
            "will_run": modified is not None,
            "driver": PDF_DRIVER,
            "engine": PDF_ENGINE,
            "replays": PDF_REPLAY_COUNT,
            "repeatability_requirement": "exact byte equality",
            "mutex": TEX_MUTEX_NAME,
            "process_tree_guard": "Windows job object, kill on close",
            "launch_capture": list(WINDOWS_JOB_LAUNCH_CAPTURE),
            "structural_checks": ["PDF header", "PDF trailer", "nonzero page count"],
            "render_visual_qa": "PENDING_NOT_PERFORMED",
        },
        "make4ht": {
            "will_run": modified is not None,
            "native_mathml_option": "mathml",
            "mutex": TEX_MUTEX_NAME,
            "process_tree_guard": "Windows job object, kill on close",
            "launch_capture": list(WINDOWS_JOB_LAUNCH_CAPTURE),
        },
        "epubcheck": {
            "will_run": modified is not None,
            "version": EPUBCHECK_VERSION,
            "jar_sha256": EPUBCHECK_JAR_SHA256,
            "runtime_tree_sha256": EPUBCHECK_RUNTIME_TREE_SHA256,
            "profile": "default",
            "fail_on_warnings": True,
            "process_tree_guard": "Windows job object, kill on close",
            "launch_capture": list(WINDOWS_JOB_LAUNCH_CAPTURE),
        },
        "tex_lane_sequence": [
            "PDF replay 1",
            "PDF replay 2",
            "make4ht",
        ],
        "commit": {
            "mode": "single atomic output-directory rename",
            "selected_registers_all_or_nothing": True,
            "existing_partial_output_refused": True,
        },
        "registers": register_reports,
        "publication_performed": False,
    }


def _write_tree(entries: Mapping[str, bytes], root: Path) -> None:
    for name, payload in entries.items():
        target = root / Path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


@dataclass(frozen=True)
class PreparedRegister:
    register: str
    receipt: dict[str, object]
    files: Mapping[str, bytes]


def _artifact_filenames(plan: AcceptedPlan, register: str) -> dict[str, str]:
    base = output_basename(plan, register)
    return {
        "pdf": f"{base}.pdf",
        "latex": f"{base}.tex",
        "source_zip": f"{base}-sources.zip",
        "epub": f"{base}.epub",
        "epubcheck_receipt": f"{base}-EPUBCHECK.json",
        "receipt": f"{base}-BUILD.json",
    }


def _expected_release_paths(
    plan: AcceptedPlan, registers: Sequence[str]
) -> set[str]:
    return {
        f"{register}/{filename}"
        for register in registers
        for filename in _artifact_filenames(plan, register).values()
    }


def _release_file_paths(root: Path) -> set[str]:
    require(root.is_dir(), f"output target is not a directory: {root}")
    result: set[str] = set()
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), f"output symlink is forbidden: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            require(safe_archive_name(relative), f"unsafe output path: {relative}")
            result.add(relative)
    return result


def _release_directory_paths(root: Path) -> set[str]:
    require(root.is_dir(), f"output target is not a directory: {root}")
    result: set[str] = set()
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), f"output symlink is forbidden: {path}")
        if path.is_dir():
            relative = path.relative_to(root).as_posix()
            require(safe_archive_name(relative), f"unsafe output directory: {relative}")
            result.add(relative)
    return result


def _required_directory_paths(file_paths: Iterable[str]) -> set[str]:
    result: set[str] = set()
    for value in file_paths:
        parent = PurePosixPath(value).parent
        while parent != PurePosixPath("."):
            result.add(parent.as_posix())
            parent = parent.parent
    return result


def preflight_output_bundle(output_dir: Path, expected_paths: set[str]) -> None:
    """Reject an already-partial or unexpectedly populated release directory."""
    if not output_dir.exists():
        return
    observed = _release_file_paths(output_dir)
    require(
        observed == expected_paths,
        "refusing partial or unexpected existing output bundle: "
        f"expected {sorted(expected_paths)}, observed {sorted(observed)}",
    )
    observed_directories = _release_directory_paths(output_dir)
    expected_directories = _required_directory_paths(expected_paths)
    require(
        observed_directories == expected_directories,
        "refusing partial or unexpected existing output directories: "
        f"expected {sorted(expected_directories)}, observed {sorted(observed_directories)}",
    )


def _commit_release_bundle(
    output_dir: Path,
    prepared: Sequence[PreparedRegister],
) -> str:
    """Commit every selected-register artifact with one directory rename."""
    require(bool(prepared), "cannot commit an empty release bundle")
    flat: dict[str, bytes] = {}
    for item in prepared:
        for filename, payload in item.files.items():
            relative = f"{item.register}/{filename}"
            require(relative not in flat, f"duplicate release path: {relative}")
            flat[relative] = payload
    require(all(safe_archive_name(name) for name in flat), "unsafe release path")
    expected_paths = set(flat)
    expected_directories = _required_directory_paths(expected_paths)
    preflight_output_bundle(output_dir, expected_paths)
    if output_dir.exists():
        for relative, payload in flat.items():
            path = output_dir / Path(relative)
            require(
                path.read_bytes() == payload,
                f"refusing to overwrite non-identical output: {path}",
            )
        return "EXISTING_IDENTICAL"

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    transaction = uuid.uuid4().hex
    stage_parent = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.transaction-{transaction}-",
            dir=output_dir.parent,
        )
    )
    staged_bundle = stage_parent / "bundle"
    try:
        staged_bundle.mkdir()
        _write_tree(flat, staged_bundle)
        require(
            _release_file_paths(staged_bundle) == expected_paths,
            "staged release path set drifted",
        )
        require(
            _release_directory_paths(staged_bundle) == expected_directories,
            "staged release directory set drifted",
        )
        for relative, payload in flat.items():
            require(
                (staged_bundle / Path(relative)).read_bytes() == payload,
                f"staged release bytes drifted: {relative}",
            )
        require(not output_dir.exists(), "output appeared during atomic commit")
        try:
            os.replace(staged_bundle, output_dir)
        except OSError as exc:
            raise BuildError(f"atomic output-directory commit failed: {exc}") from exc
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)
    require(
        _release_file_paths(output_dir) == expected_paths,
        "committed release path set drifted",
    )
    require(
        _release_directory_paths(output_dir) == expected_directories,
        "committed release directory set drifted",
    )
    for relative, payload in flat.items():
        require(
            (output_dir / Path(relative)).read_bytes() == payload,
            f"committed release bytes drifted: {relative}",
        )
    return "COMMITTED_ATOMICALLY"


def prepare_register(
    plan: AcceptedPlan,
    register: str,
    work_root: Path,
    modified: str,
    *,
    mutex_timeout_seconds: float,
    tex_timeout_seconds: float,
    pdf_timeout_seconds: float,
    epubcheck_jar: Path,
    epubcheck_timeout_seconds: float,
) -> PreparedRegister:
    tex = render_cumulative_tex(plan, register, modified)
    source_entries = source_tree_entries(plan, register, modified, tex)
    source_identity = byte_tree_identity(source_entries)
    source_zip = deterministic_zip_bytes(source_entries)
    with zipfile.ZipFile(io.BytesIO(source_zip)) as source_archive:
        archived_source_entries = {
            item.filename: source_archive.read(item.filename)
            for item in source_archive.infolist()
        }
    require(
        archived_source_entries == source_entries
        and byte_tree_identity(archived_source_entries) == source_identity,
        "source ZIP does not replay the exact PDF input tree",
    )
    base = output_basename(plan, register)
    work_root.mkdir(parents=True, exist_ok=False)
    source_root = work_root / "source"
    source_root.mkdir()
    _write_tree(source_entries, source_root)
    require(
        directory_tree_identity(source_root) == source_identity,
        "materialized source tree differs from the source ZIP payload",
    )
    pdf, pdf_build = run_pdf_build(
        source_root,
        f"{base}.tex",
        work_root / "pdf-build",
        modified=modified,
        expected_source_tree=source_identity,
        mutex_timeout_seconds=mutex_timeout_seconds,
        pdf_timeout_seconds=pdf_timeout_seconds,
    )
    pdf_audit = audit_pdf_bytes(pdf)
    pdf_record = {
        "path": f"{base}.pdf",
        "bytes": len(pdf),
        "sha256": sha256_bytes(pdf),
    }
    require(
        pdf_build.get("schema") == "openlogic-partial-reader-pdf-build-v1"
        and pdf_build.get("status") == "PASS_REPEATABLE_BYTES",
        "PDF compiler receipt lacks the required success schema/status",
    )
    for tool_key, tool_name in (("driver", PDF_DRIVER), ("engine", PDF_ENGINE)):
        tool = pdf_build.get(tool_key)
        require(isinstance(tool, dict), f"PDF compiler receipt lacks {tool_key} metadata")
        require(tool.get("name") == tool_name, f"PDF compiler receipt names the wrong {tool_key}")
        require(bool(tool.get("version")), f"PDF compiler receipt lacks {tool_key} version")
        require(
            isinstance(tool.get("command"), list) and bool(tool["command"]),
            f"PDF compiler receipt lacks {tool_key} version command",
        )
        require(tool.get("exit_code") == 0, f"PDF compiler receipt has nonzero {tool_key} probe")
        require(
            isinstance(tool.get("seconds"), (int, float))
            and not isinstance(tool.get("seconds"), bool)
            and float(tool["seconds"]) >= 0,
            f"PDF compiler receipt lacks {tool_key} runtime",
        )
    pdf_runs = pdf_build.get("runs")
    require(
        isinstance(pdf_runs, list) and len(pdf_runs) == PDF_REPLAY_COUNT,
        "PDF compiler receipt lacks both replay runs",
    )
    for number, replay in enumerate(pdf_runs, start=1):
        require(isinstance(replay, dict), f"PDF replay {number} receipt is not an object")
        require(replay.get("exit_code") == 0, f"PDF replay {number} receipt is nonzero")
        require(
            isinstance(replay.get("command"), list) and bool(replay["command"]),
            f"PDF replay {number} receipt lacks its command",
        )
        require(
            isinstance(replay.get("seconds"), (int, float))
            and not isinstance(replay.get("seconds"), bool)
            and float(replay["seconds"]) >= 0,
            f"PDF replay {number} receipt lacks runtime",
        )
    require(
        pdf_build.get("repeatability")
        == {
            "runs": PDF_REPLAY_COUNT,
            "comparison": "exact byte equality",
            "status": "PASS",
        },
        "PDF compiler receipt lacks the exact-byte replay result",
    )
    require(
        pdf_build.get("mutex") == TEX_MUTEX_NAME
        and pdf_build.get("process_tree_guard") == "Windows job object, kill on close"
        and pdf_build.get("launch_capture") == list(WINDOWS_JOB_LAUNCH_CAPTURE),
        "PDF compiler receipt lacks the mutex/process-tree guard binding",
    )
    require(
        pdf_build.get("pdf") == pdf_audit,
        "PDF compiler receipt does not bind the returned PDF bytes",
    )
    require(
        pdf_build.get("input")
        == {
            "cumulative_tex": {
                "path": f"{base}.tex",
                "bytes": len(tex),
                "sha256": sha256_bytes(tex),
            },
            "source_tree": source_identity,
        },
        "PDF compiler receipt does not bind the exact cumulative TeX/source tree",
    )
    run = run_make4ht(
        source_root,
        f"{base}.tex",
        work_root / "epub-build",
        modified=modified,
        mutex_timeout_seconds=mutex_timeout_seconds,
        tex_timeout_seconds=tex_timeout_seconds,
    )
    require(
        directory_tree_identity(source_root) == source_identity,
        "make4ht mutated the exact PDF/source-ZIP input tree",
    )
    epub, audit = package_make4ht_output(
        plan, register, modified, Path(str(run["output"]))
    )
    require(audit.get("status") == "PASS", "internal EPUB package audit did not pass")
    epubcheck = run_epubcheck(
        epub,
        work_root / "epubcheck",
        jar=epubcheck_jar,
        timeout_seconds=epubcheck_timeout_seconds,
    )
    require(
        epubcheck["checked_epub"]
        == {"bytes": len(epub), "sha256": sha256_bytes(epub)}
        == {"bytes": audit["bytes"], "sha256": audit["sha256"]},
        "internal audit and EPUBCheck did not bind the same EPUB bytes",
    )
    epubcheck_bytes = json_bytes(epubcheck)
    filenames = _artifact_filenames(plan, register)
    receipt = {
        "schema": "openlogic-partial-reader-build-receipt-v1",
        "status": "BUILT_STRUCTURALLY_AUDITED_EPUBCHECKED_VISUAL_QA_PENDING",
        "register": register,
        "scope": {
            "first": plan.first_id,
            "last": plan.last_id,
            "accepted_units": len(plan.accepted_units),
            "first_excluded": plan.first_excluded["unit_id"] if plan.first_excluded else None,
        },
        "alignment_sha256": plan.alignment_sha256,
        "modified": modified,
        "artifact_order": list(ARTIFACT_ORDER),
        "artifacts": {
            "pdf": pdf_record,
            "latex": {"path": filenames["latex"], "bytes": len(tex), "sha256": sha256_bytes(tex)},
            "source_zip": {
                "path": filenames["source_zip"],
                "bytes": len(source_zip),
                "sha256": sha256_bytes(source_zip),
            },
            "epub": {"path": filenames["epub"], "bytes": len(epub), "sha256": sha256_bytes(epub)},
            "epubcheck_receipt": {
                "path": filenames["epubcheck_receipt"],
                "bytes": len(epubcheck_bytes),
                "sha256": sha256_bytes(epubcheck_bytes),
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
                "bytes": len(tex),
                "sha256": sha256_bytes(tex),
            },
            "source_zip": {
                "path": filenames["source_zip"],
                "bytes": len(source_zip),
                "sha256": sha256_bytes(source_zip),
            },
        },
        "pdf_build": pdf_build,
        "pdf_audit": pdf_audit,
        "render_visual_qa": {
            "status": "PENDING_NOT_PERFORMED",
            "performed": False,
            "bound_pdf": pdf_record,
            "statement": "No raster render or visual inspection is claimed by this builder.",
        },
        "make4ht": {key: value for key, value in run.items() if key != "output"},
        "audit": audit,
        "epubcheck": epubcheck,
        "constraints": {
            "pending_translation_files_included": False,
            "native_mathml_required": True,
            "epubcheck_required": True,
            "epubcheck_zero_messages_required": True,
            "pdf_exact_byte_replay_required": True,
            "pdf_visual_qa_performed": False,
            "selected_registers_commit_all_or_nothing": True,
            "publication_performed": False,
        },
    }
    require(
        tuple(receipt["artifacts"]) == ARTIFACT_ORDER,
        "receipt artifact order drift",
    )
    receipt_bytes = json_bytes(receipt)
    files = {
        filenames["pdf"]: pdf,
        filenames["latex"]: tex,
        filenames["source_zip"]: source_zip,
        filenames["epub"]: epub,
        filenames["epubcheck_receipt"]: epubcheck_bytes,
        filenames["receipt"]: receipt_bytes,
    }
    require(tuple(files) == tuple(filenames[key] for key in ARTIFACT_ORDER), "artifact order drift")
    return PreparedRegister(register=register, receipt=receipt, files=files)


def build_release(
    plan: AcceptedPlan,
    output_dir: Path,
    modified: str,
    *,
    mutex_timeout_seconds: float,
    tex_timeout_seconds: float,
    pdf_timeout_seconds: float,
    epubcheck_jar: Path,
    epubcheck_timeout_seconds: float,
) -> tuple[list[dict[str, object]], str]:
    registers = plan.registers
    expected_paths = _expected_release_paths(plan, registers)
    preflight_output_bundle(output_dir, expected_paths)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.build-", dir=output_dir.parent
    ) as temporary_name:
        temporary = Path(temporary_name)
        prepared = [
            prepare_register(
                plan,
                register,
                temporary / register,
                modified,
                mutex_timeout_seconds=mutex_timeout_seconds,
                tex_timeout_seconds=tex_timeout_seconds,
                pdf_timeout_seconds=pdf_timeout_seconds,
                epubcheck_jar=epubcheck_jar,
                epubcheck_timeout_seconds=epubcheck_timeout_seconds,
            )
            for register in registers
        ]
        commit_status = _commit_release_bundle(output_dir, prepared)
    return [item.receipt for item in prepared], commit_status


def build_register(
    plan: AcceptedPlan,
    register: str,
    output_dir: Path,
    modified: str,
    *,
    mutex_timeout_seconds: float,
    tex_timeout_seconds: float,
    pdf_timeout_seconds: float | None = None,
    epubcheck_jar: Path,
    epubcheck_timeout_seconds: float,
) -> dict[str, object]:
    """Compatibility wrapper that atomically commits one selected register."""
    require(register in plan.registers, f"register absent from plan: {register}")
    expected_paths = _expected_release_paths(plan, (register,))
    preflight_output_bundle(output_dir, expected_paths)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.{register}.build-", dir=output_dir.parent
    ) as temporary_name:
        prepared = prepare_register(
            plan,
            register,
            Path(temporary_name) / register,
            modified,
            mutex_timeout_seconds=mutex_timeout_seconds,
            tex_timeout_seconds=tex_timeout_seconds,
            pdf_timeout_seconds=(
                tex_timeout_seconds if pdf_timeout_seconds is None else pdf_timeout_seconds
            ),
            epubcheck_jar=epubcheck_jar,
            epubcheck_timeout_seconds=epubcheck_timeout_seconds,
        )
        _commit_release_bundle(output_dir, (prepared,))
    return prepared.receipt


def parse_modified(value: str) -> str:
    require(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)), "--modified must be YYYY-MM-DD")
    try:
        time.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise BuildError(f"invalid --modified date: {value}") from exc
    return value


def _selected_registers(value: str) -> tuple[str, ...]:
    return tuple(REGISTER_CONFIG) if value == "both" else (value,)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build bounded cumulative PDF/LaTeX/source ZIP/EPUB 3 bundles for "
            "accepted Dutch prefixes."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root (defaults to the parent of scripts/).",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--register",
        choices=("both", *REGISTER_CONFIG),
        default="both",
    )
    parser.add_argument("--modified", help="Fixed publication metadata date, YYYY-MM-DD; required to build.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the plan without writing or invoking TeX.")
    parser.add_argument("--mutex-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--tex-timeout-seconds", type=float, default=1800.0)
    parser.add_argument(
        "--pdf-timeout-seconds",
        type=float,
        default=3600.0,
        help="Total timeout for version probes plus both deterministic PDF replays.",
    )
    parser.add_argument(
        "--epubcheck-jar",
        type=Path,
        help=(
            f"Pinned EPUBCheck {EPUBCHECK_VERSION} JAR; otherwise use "
            f"{EPUBCHECK_ENVIRONMENT_VARIABLE} or the authorized installed distribution."
        ),
    )
    parser.add_argument("--epubcheck-timeout-seconds", type=float, default=600.0)
    args = parser.parse_args(argv)

    try:
        require(args.mutex_timeout_seconds > 0, "mutex timeout must be positive")
        require(args.tex_timeout_seconds > 0, "make4ht timeout must be positive")
        require(args.pdf_timeout_seconds > 0, "PDF build timeout must be positive")
        require(
            args.epubcheck_timeout_seconds > 0,
            "EPUBCheck timeout must be positive",
        )
        registers = _selected_registers(args.register)
        plan = plan_accepted_prefix(args.repo_root, registers)
        output_dir = args.output_dir.resolve()
        protected = [
            (plan.repo_root / name).resolve()
            for name in ("alignment", "translations", "upstream", "editions")
        ]
        require(
            all(
                output_dir != path
                and output_dir not in path.parents
                and path not in output_dir.parents
                for path in protected
            ),
            "output directory may not overlap a protected source tree",
        )
        if args.dry_run:
            report = plan_report(plan, output_dir, None)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        require(args.modified is not None, "--modified is required unless --dry-run is used")
        modified = parse_modified(args.modified)
        epubcheck_jar = resolve_epubcheck_jar(plan.repo_root, args.epubcheck_jar)
        report = plan_report(plan, output_dir, modified)
        receipts, commit_status = build_release(
            plan,
            output_dir,
            modified,
            mutex_timeout_seconds=args.mutex_timeout_seconds,
            tex_timeout_seconds=args.tex_timeout_seconds,
            pdf_timeout_seconds=args.pdf_timeout_seconds,
            epubcheck_jar=epubcheck_jar,
            epubcheck_timeout_seconds=args.epubcheck_timeout_seconds,
        )
        result = {
            **report,
            "status": "BUILT_STRUCTURALLY_AUDITED_EPUBCHECKED_VISUAL_QA_PENDING",
            "commit_status": commit_status,
            "receipts": receipts,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except BuildError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
