#!/usr/bin/env python3
"""Focused regressions for the deterministic direct-LaTeX normalizer."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import build_partial_epub3 as core
from epub3 import direct_latex as direct


class SelectorTests(unittest.TestCase):
    def test_nested_tagblock_and_missing_else_are_deterministic(self) -> None:
        diagnostics = direct.SelectorDiagnostics()
        text = (
            r"\begin{tagblock}{tagTrue}A "
            r"\iftag{tagTrue}{B \iftag{probIf}{X}{C}}{D}"
            r"\end{tagblock} "
            r"\iftag{probIf}{NIET}"
        )
        result = direct.select_tagged_content(
            text, direct.initial_tags(), diagnostics=diagnostics
        )
        self.assertIn("A B C", " ".join(result.split()))
        self.assertNotIn("X", result)
        self.assertNotIn("NIET", result)
        self.assertEqual(1, diagnostics.missing_else_defaulted)


class ReferenceTests(unittest.TestCase):
    def test_malformed_conditional_tail_is_repaired_without_losing_tail(self) -> None:
        diagnostics = direct.ReferenceDiagnostics()
        result = direct.transform_references(
            r"\oliflabeldef{aanwezig}{Ja{} en deze staart blijft.}",
            direct.ReferenceState(),
            {"aanwezig"},
            "nl-standard",
            diagnostics,
            unit_id="OLP-0001",
            emitted_labels=set(),
        )
        self.assertEqual("Ja en deze staart blijft.", result)
        self.assertEqual(1, diagnostics.malformed_conditional_tail_repairs)

    def test_duplicate_label_definitions_receive_unique_targets(self) -> None:
        diagnostics = direct.ReferenceDiagnostics()
        emitted: set[str] = set()
        state = direct.ReferenceState()
        first = direct.transform_references(
            r"\label{zelfde}",
            state,
            {"zelfde"},
            "nl-standard",
            diagnostics,
            unit_id="OLP-0001",
            emitted_labels=emitted,
        )
        second = direct.transform_references(
            r"\label{zelfde}",
            state,
            {"zelfde"},
            "nl-standard",
            diagnostics,
            unit_id="OLP-0002",
            emitted_labels=emitted,
        )
        self.assertIn(r"\hypertarget{zelfde}{}", first)
        self.assertIn(r"\hypertarget{zelfde--unit-olp-0002}{}", second)
        self.assertEqual(1, diagnostics.duplicate_label_definitions_renamed)


class ProofAndMathTests(unittest.TestCase):
    def test_empty_premise_and_discharge_remain_explicit(self) -> None:
        result, converted = direct.transform_proof_commands(
            r"\AxiomC{}\DischargeRule{R}{i}\DisplayProof", "nl-gewoon"
        )
        self.assertIn("Premisseplaats zonder ingevulde formule", result)
        self.assertIn("trekt aanname i in", result)
        self.assertEqual(2, converted)

    def test_single_token_arguments_and_repeated_math_passes(self) -> None:
        value = r"\Sat AB \Proves t"
        total = 0
        for _ in range(4):
            value, converted = direct.transform_special_math(value)
            total += converted
            if not converted:
                break
        self.assertEqual(2, total)
        self.assertIn(r"\mathfrak{A} \vDash B", value)
        self.assertIn(r"\vdash{}t", value)
        self.assertNotIn(r"\Sat", value)
        self.assertNotIn(r"\Proves", value)

    def test_align_with_nested_cases_splits_only_top_level_rows(self) -> None:
        source = (
            r"\begin{align}x&=\begin{cases}a\\b\end{cases}"
            "\\\\"
            r"y&=c\end{align}"
        )
        result, converted = direct.normalize_align_environments(source)
        self.assertEqual(1, converted)
        self.assertEqual(2, result.count(r"\["))
        self.assertIn(r"\begin{cases}a\\b\end{cases}", result)

    def test_math_anchor_moves_outside_delimiters(self) -> None:
        result, moved = direct.move_math_anchors_outside(
            r"Voor $x\hypertarget{doel}{}=y$ na."
        )
        self.assertEqual(1, moved)
        self.assertIn(r"\hypertarget{doel}{}$x=y$", result)

    def test_model_theory_and_arithmetic_macros_expand_completely(self) -> None:
        value = (
            r"\Var \substruct \Theory{M} \elemequiv[n] \iso[p] \concat "
            r"\lambd[x][s] \mModel M \mSat/{M}{A}[w] \Expan{M}{R} "
            r"\Part{x}{y} \OCon[\Th{PA}] \OPrf[\Th{PA}] "
            r"\QuantRank{A} \PIso{I} \gn{\bot} \forallx \VDash"
        )
        for _ in range(16):
            value, converted = direct.transform_special_math(value)
            if not converted:
                break
        for command in (
            "Var", "substruct", "Theory", "elemequiv", "iso", "concat",
            "lambd", "mModel", "mSat", "Expan", "Part", "OCon", "OPrf",
            "Th", "QuantRank", "PIso", "gn", "forallx", "VDash",
        ):
            self.assertNotIn("\\" + command, value)
        self.assertIn(r"\mathfrak{M}, w \nVdash A", value)
        self.assertIn(r"\mathsf{Con}_{\mathbf{PA}}", value)

    def test_only_math_hyperlinks_are_flattened(self) -> None:
        value, converted = direct.flatten_math_hyperlinks(
            r"Voor $\text{zie \hyperlink{doel}{de passage}}$ en \hyperlink{buiten}{daar}."
        )
        self.assertEqual(1, converted)
        self.assertIn(r"$\text{zie de passage}$", value)
        self.assertIn(r"\hyperlink{buiten}{daar}", value)


class DiagramTests(unittest.TestCase):
    def test_diagrams_have_unit_unique_names_and_dutch_alt(self) -> None:
        assets: dict[str, bytes] = {}
        first, first_count = direct.replace_diagrams(
            r"\olasset{union.pdf}",
            "nl-standard",
            assets,
            unit_id="OLP-0001",
        )
        second, second_count = direct.replace_diagrams(
            r"\olasset{union.pdf}",
            "nl-gewoon",
            assets,
            unit_id="OLP-0002",
        )
        self.assertEqual((1, 1), (first_count, second_count))
        self.assertEqual(2, len(assets))
        self.assertIn("diagram-olp-0001-001-union.svg", first)
        self.assertIn("diagram-olp-0002-001-union.svg", second)
        self.assertIn("alt={Twee overlappende verzamelingen", first)
        self.assertTrue(all(b"<title" in value and b"<desc" in value for value in assets.values()))


class LiveAcceptedPrefixTests(unittest.TestCase):
    def test_current_accepted192_normalizes_deterministically(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        plan = core.plan_accepted_prefix(repo, tuple(core.REGISTER_CONFIG))
        self.assertEqual(192, len(plan.accepted_units))
        self.assertEqual("OLP-0192", plan.last_id)
        for register in plan.registers:
            config = core.REGISTER_CONFIG[register]
            units = [
                (
                    str(row["unit_id"]),
                    core.stripped_unit_body(
                        (
                            repo / Path(str(row[config["path_key"]]))
                        ).read_text(encoding="utf-8")
                    ),
                )
                for row in plan.accepted_units
            ]
            arguments = {
                "locale_path": (
                    repo
                    / "editions"
                    / register
                    / "support"
                    / "open-logic-reader-locale.tex"
                ),
                "register": register,
                "scope_title": "deterministische test",
                "modified": "2026-09-26",
            }
            first = direct.make_direct_document(units, **arguments)
            second = direct.make_direct_document(units, **arguments)
            self.assertEqual(first.tex, second.tex)
            self.assertEqual(first.assets, second.assets)
            self.assertEqual(first.unit_input_sha256, second.unit_input_sha256)
            self.assertEqual(192, first.metrics["units"])
            self.assertEqual(13, first.metrics["diagrams_converted_to_svg"])
            self.assertEqual(3, first.metrics["selector_missing_else_defaulted"])


if __name__ == "__main__":
    unittest.main()
