from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_partial_epub3 as core
import build_partial_pdf_pair as pair_builder


class PairedPdfBuilderTests(unittest.TestCase):
    def make_bundle(self, root: Path) -> tuple[Path, Path]:
        bundle = root / "bundle"
        exact_epubs: dict[str, dict[str, object]] = {}
        for register, slug in (("nl-standard", "Standard"), ("nl-gewoon", "Gewoon")):
            register_root = bundle / register
            register_root.mkdir(parents=True)
            stem = f"OpenLogic-NL-{slug}-Partial-OLP-0001--OLP-0160"
            tex_name = f"{stem}.tex"
            tex = f"% {register}\n".encode()
            entries = {tex_name: tex, "BUILD.md": b"fixture\n"}
            source_zip = core.deterministic_zip_bytes(entries)
            epub = f"epub-{register}".encode()
            (register_root / tex_name).write_bytes(tex)
            (register_root / f"{stem}-sources.zip").write_bytes(source_zip)
            (register_root / f"{stem}.epub").write_bytes(epub)
            receipt = {
                "schema": "openlogic-partial-source-epub-build-receipt-v1",
                "status": "BUILT_REPRODUCIBLY_STRUCTURALLY_AUDITED_EPUBCHECKED_VISUAL_QA_PENDING",
                "register": register,
                "modified": "2026-09-27",
                "scope": {
                    "accepted_units": 160,
                    "first": "OLP-0001",
                    "last": "OLP-0160",
                    "first_excluded": "OLP-0161",
                },
                "artifacts": {
                    "latex": {
                        "path": tex_name,
                        "bytes": len(tex),
                        "sha256": core.sha256_bytes(tex),
                    },
                    "source_zip": {
                        "path": f"{stem}-sources.zip",
                        "bytes": len(source_zip),
                        "sha256": core.sha256_bytes(source_zip),
                    },
                    "epub": {
                        "path": f"{stem}.epub",
                        "bytes": len(epub),
                        "sha256": core.sha256_bytes(epub),
                    },
                },
                "source_binding": {
                    "tree": core.byte_tree_identity(entries),
                    "source_zip_replays_tree_exactly": True,
                },
            }
            (register_root / f"{stem}-BUILD.json").write_bytes(core.json_bytes(receipt))
            exact_epubs[register] = {
                "bytes": len(epub),
                "sha256": core.sha256_bytes(epub),
            }
        visual = root / "visual.json"
        visual.write_text(
            json.dumps(
                {
                    "schema": "openlogic-nl-accepted160-epub-visual-qa-v1",
                    "status": "PASS",
                    "exact_epubs": exact_epubs,
                }
            ),
            encoding="utf-8",
        )
        return bundle, visual

    def test_one_outer_mutex_covers_both_register_builds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, visual = self.make_bundle(root)
            state: dict[str, object] = {"enters": 0, "calls": []}

            class FakeMutex:
                def __init__(self, timeout_seconds: float):
                    self.timeout_seconds = timeout_seconds
                    self.name = core.TEX_MUTEX_NAME
                    self.owned = False
                    self.abandoned = False

                def __enter__(self):
                    state["enters"] = int(state["enters"]) + 1
                    self.owned = True
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    self.owned = False

            def fake_audit(payload: bytes) -> dict[str, object]:
                return {
                    "schema": "fixture-pdf-audit",
                    "status": "PASS",
                    "bytes": len(payload),
                    "sha256": core.sha256_bytes(payload),
                    "pages": 1,
                }

            def fake_pdf_build(
                source_root,
                tex_name,
                output_root,
                *,
                modified,
                expected_source_tree,
                mutex_timeout_seconds,
                pdf_timeout_seconds,
                held_mutex,
            ):
                self.assertTrue(held_mutex.owned)
                register = source_root.parent.name
                state["calls"].append((register, held_mutex))
                payload = f"%PDF fixture {register}".encode()
                audit = fake_audit(payload)
                return payload, {
                    "status": "PASS_REPEATABLE_BYTES",
                    "mutex_scope": "caller-held",
                    "pdf": audit,
                }

            output = root / "output"
            with (
                mock.patch.object(pair_builder.shutil, "which", return_value=r"C:\\tool.exe"),
                mock.patch.object(core, "GlobalTexMutex", FakeMutex),
                mock.patch.object(core, "run_pdf_build", side_effect=fake_pdf_build),
                mock.patch.object(core, "audit_pdf_bytes", side_effect=fake_audit),
            ):
                receipt = pair_builder.build_pair(
                    bundle,
                    output,
                    visual,
                    mutex_timeout_seconds=3,
                    pdf_timeout_seconds=10,
                )
            self.assertEqual(1, state["enters"])
            self.assertEqual(["nl-standard", "nl-gewoon"], [row[0] for row in state["calls"]])
            self.assertIs(state["calls"][0][1], state["calls"][1][1])
            self.assertEqual(
                "PASS_PAIRED_PDF_BUILT_REPRODUCIBLY_VISUAL_QA_PENDING",
                receipt["status"],
            )
            self.assertEqual(1, receipt["mutex"]["bounded_acquisition_attempts"])
            self.assertEqual(5, len([path for path in output.rglob("*") if path.is_file()]))


if __name__ == "__main__":
    unittest.main()
