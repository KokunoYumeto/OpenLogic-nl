"""Maak browser-HTML naast de ongewijzigde XHTML-bestanden van reader 192.

Gebruik vanuit de publicatierepository:
    python scripts/build-web-html-compat-192.py

Vereist Python 3 en lxml. Er wordt geen TeX gestart en er wordt niets vertaald.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import json

from lxml import etree, html


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "docs" / "readers" / "192-r1"
PROVENANCE = ROOT / "provenance" / "DUTCH_HTML_COMPAT_192_20260927.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def elements(node: etree._Element) -> list[etree._Element]:
    return [item for item in node.iter() if isinstance(item.tag, str)]


def convert(src: Path) -> dict[str, object]:
    raw = src.read_bytes()
    tree = etree.fromstring(
        raw,
        etree.XMLParser(resolve_entities=False, no_network=True),
    )
    before_text = "".join(tree.itertext())
    before_math = sum(local(item.tag) == "math" for item in elements(tree))
    before_ids = [item.get("id") for item in elements(tree) if item.get("id")]
    before_count = len(elements(tree))

    for node in elements(tree):
        if node.tag.startswith("{http://www.w3.org/1999/xhtml}"):
            node.tag = local(node.tag)
        href = node.get("href")
        if href:
            node.set(
                "href",
                href.replace("book.xhtml", "book.html").replace(
                    "nav.xhtml", "nav.html"
                ),
            )

    output = etree.tostring(
        tree,
        method="html",
        encoding="utf-8",
        doctype="<!DOCTYPE html>",
    )
    parsed = html.fromstring(output)
    assert "".join(parsed.itertext()) == before_text, "Tekstwijziging"
    assert [item.get("id") for item in elements(parsed) if item.get("id")] == before_ids
    assert sum(local(item.tag) == "math" for item in elements(parsed)) == before_math
    assert len(elements(parsed)) == before_count

    target = src.with_suffix(".html")
    target.write_bytes(output)
    return {
        "path": target.relative_to(ROOT).as_posix(),
        "bytes": len(output),
        "sha256": digest(output),
        "source_path": src.relative_to(ROOT).as_posix(),
        "source_bytes": len(raw),
        "source_sha256": digest(raw),
        "text_identical": True,
        "anchors_identical": True,
        "math_elements": before_math,
        "element_count": before_count,
    }


def main() -> None:
    assert WEB.is_dir(), WEB
    rows: list[dict[str, object]] = []
    for register in ("nl-standard", "nl-gewoon"):
        for relative in ("OEBPS/generated/book.xhtml", "OEBPS/nav.xhtml"):
            rows.append(convert(WEB / register / relative))

    landing = WEB / "index.html"
    original = landing.read_bytes()
    text = original.decode("utf-8")
    text = text.replace("book.xhtml", "book.html").replace(
        "nav.xhtml", "nav.html"
    )
    published = text.encode("utf-8")
    landing.write_bytes(published)
    assert b"book.xhtml" not in published and b"nav.xhtml" not in published

    report = {
        "schema": "openlogic-html-compat/2",
        "status": "PASS",
        "scope": "OLP-0001–OLP-0192; 192/722 per register; geen vertaalwijzigingen",
        "files": rows,
        "landing": {
            "path": landing.relative_to(ROOT).as_posix(),
            "source_bytes": len(original),
            "source_sha256": digest(original),
            "published_bytes": len(published),
            "published_sha256": digest(published),
            "change": "Alleen de vier lokale XHTML-ingangen zijn naar hun inhoudelijk gelijke HTML-ingangen omgezet.",
        },
        "epub_and_source_archives_changed": False,
    }
    PROVENANCE.parent.mkdir(parents=True, exist_ok=True)
    PROVENANCE.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "status": "PASS",
                "html_files": len(rows),
                "math_elements": [row["math_elements"] for row in rows],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
