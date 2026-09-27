"""Deterministic, non-TeX normalization for the bounded Dutch EPUB lane.

The PDF source remains the authoritative editable LaTeX.  This module makes a
second, conversion-only LaTeX stream that Pandoc can read without executing a
TeX engine.  It resolves Open Logic's text tokens and selection tags, expands
the small amount of xparse-only notation used by the accepted prefix, and
turns proof/tableau graphics into explicit accessible sequences.  Every
conversion is fail-closed: an unknown diagram, unresolved token, or raw
Open-Logic selector is an error rather than silently omitted content.
"""

from __future__ import annotations

import hashlib
import html as html_module
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, MutableMapping, Sequence


class DirectConversionError(RuntimeError):
    """A deterministic source-normalization failure."""


AI_DISCLOSURE_NL = (
    "Vertaling en eerdere controles: OpenAI Codex; de huidige eigenaar gebruikt "
    "GPT-5.6 Sol, Ultra effort. De exacte historische model- en inspanningsinstelling "
    "van ieder tekstgedeelte zijn nog niet afzonderlijk bewezen; de huidige instelling "
    "wordt daarom niet aan alle eerdere vertaalbytes toegeschreven. Samenstelling van "
    "deze reader, directe EPUB-conversie en deterministische bouwcontroles: OpenAI "
    "Codex — GPT-5.6 Sol, Ultra effort. Geen menselijke redactie of beoordeling wordt "
    "geclaimd."
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DirectConversionError(message)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _skip_space(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def _group(text: str, position: int, opener: str = "{", closer: str = "}") -> tuple[str, int]:
    position = _skip_space(text, position)
    require(position < len(text) and text[position] == opener, f"expected {opener!r} at byte {position}")
    depth = 1
    cursor = position + 1
    start = cursor
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start:cursor], cursor + 1
        cursor += 1
    raise DirectConversionError(f"unclosed {opener!r} group beginning at byte {position}")


def _optional(text: str, position: int) -> tuple[str | None, int]:
    position = _skip_space(text, position)
    if position < len(text) and text[position] == "[":
        value, position = _group(text, position, "[", "]")
        return value, position
    return None, position


def _command_end(text: str, position: int) -> int:
    require(text[position] == "\\", "command parser is not on a backslash")
    cursor = position + 1
    if cursor < len(text) and text[cursor].isalpha():
        while cursor < len(text) and (text[cursor].isalpha() or text[cursor] == "@"):
            cursor += 1
        return cursor
    return min(cursor + 1, len(text))


def _strip_comments(text: str) -> str:
    result: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True):
        cut = None
        for index, char in enumerate(line):
            if char != "%":
                continue
            backslashes = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                cut = index
                break
        if cut is None:
            result.append(line)
        else:
            suffix = "\n" if line.endswith("\n") else ""
            result.append(line[:cut] + suffix)
    return "".join(result)


def _normal_key(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _capitalize(value: str) -> str:
    for index, char in enumerate(value):
        if char.isalpha():
            return value[:index] + char.upper() + value[index + 1 :]
    return value


@dataclass(frozen=True)
class TextToken:
    singular: str
    plural: str
    capital_singular: str
    capital_plural: str


def load_text_tokens(locale_path: Path) -> dict[str, TextToken]:
    """Parse the locale's actual ``settexttoken`` assignments."""
    text = _strip_comments(locale_path.read_text(encoding="utf-8"))
    result: dict[str, TextToken] = {}
    cursor = 0
    marker = "\\settexttoken"
    while True:
        start = text.find(marker, cursor)
        if start < 0:
            break
        position = start + len(marker)
        key, position = _group(text, position)
        position = _skip_space(text, position)
        if position < len(text) and text[position] == "*":
            position += 1
        singular, position = _group(text, position)
        plural, position = _group(text, position)
        capital_singular, position = _optional(text, position)
        capital_plural, position = _optional(text, position)
        key = _normal_key(key)
        result[key] = TextToken(
            singular=singular.strip(),
            plural=plural.strip(),
            capital_singular=(capital_singular.strip() if capital_singular is not None else _capitalize(singular.strip())),
            capital_plural=(capital_plural.strip() if capital_plural is not None else _capitalize(plural.strip())),
        )
        cursor = position
    require(bool(result), f"locale contains no text-token assignments: {locale_path}")
    return result


def expand_text_tokens(text: str, tokens: Mapping[str, TextToken]) -> str:
    """Resolve shorthand and explicit token access using the Dutch locale."""
    output: list[str] = []
    cursor = 0
    while cursor < len(text):
        if text.startswith("!!", cursor):
            position = cursor + 2
            capital = False
            article = False
            if position < len(text) and text[position] == "^":
                capital = True
                position += 1
            if position < len(text) and text[position] == "a":
                article = True
                position += 1
            key, position = _group(text, position)
            plural = position < len(text) and text[position] == "s"
            if plural:
                position += 1
            normalized = _normal_key(key)
            require(normalized in tokens, f"undefined Dutch text token: {normalized!r}")
            token = tokens[normalized]
            value = (
                token.capital_plural if capital and plural else
                token.capital_singular if capital else
                token.plural if plural else token.singular
            )
            if article:
                value = ("Een " if capital else "een ") + value
            output.append(value)
            cursor = position
            continue
        matched = None
        for name in ("\\usetoken", "\\printtoken"):
            if text.startswith(name, cursor):
                matched = name
                break
        if matched:
            switch, position = _group(text, cursor + len(matched))
            key, position = _group(text, position)
            normalized = _normal_key(key)
            require(normalized in tokens, f"undefined explicit Dutch text token: {normalized!r}")
            token = tokens[normalized]
            values = {
                "s": token.singular,
                "p": token.plural,
                "S": token.capital_singular,
                "P": token.capital_plural,
                "a": "een",
                "A": "Een",
            }
            require(switch in values, f"unsupported text-token switch: {switch!r}")
            output.append(values[switch])
            cursor = position
            continue
        matched = None
        for name in ("\\article", "\\Article"):
            if text.startswith(name, cursor):
                matched = name
                break
        if matched:
            _key, position = _group(text, cursor + len(matched))
            output.append("Een" if matched == "\\Article" else "een")
            cursor = position
            continue
        output.append(text[cursor])
        cursor += 1
    result = "".join(output)
    require("!!" not in result, "unresolved Open Logic text-token shorthand")
    return result


DEFAULT_TRUE_TAGS = {
    "prvNot", "prvOr", "prvAnd", "prvIf", "prvIff", "prvTrue", "prvFalse",
    "prvEx", "prvAll", "prvBox", "prvDiamond", "limitClause", "tagTrue", "TMs",
    "lambda", "prfSC", "prfND", "prfAX", "prfTab", "FOL", "cmplCCS", "cmplMCS",
    "novice", "math", "compsci", "phil",
}
DEFAULT_FALSE_TAGS = {
    "defNot", "defOr", "defAnd", "defIf", "defIff", "defTrue", "defFalse",
    "defEx", "defAll", "defBox", "defDiamond", "probNot", "probOr", "probAnd",
    "probIf", "probIff", "probEx", "probAll", "probBox", "probDiamond",
}


def initial_tags() -> dict[str, bool]:
    result = {name: True for name in DEFAULT_TRUE_TAGS}
    result.update({name: False for name in DEFAULT_FALSE_TAGS})
    return result


def _tag_value(name: str, tags: Mapping[str, bool]) -> bool:
    name = name.strip()
    if name.startswith("not") and len(name) > 3:
        return not bool(tags.get(name[3:], False))
    return bool(tags.get(name, False))


def _tag_any(value: str, tags: Mapping[str, bool]) -> bool:
    return any(_tag_value(name, tags) for name in value.split(",") if name.strip())


def _environment_body_from(text: str, body_start: int, environment: str) -> tuple[str, int]:
    begin = f"\\begin{{{environment}}}"
    end = f"\\end{{{environment}}}"
    cursor = body_start
    depth = 1
    while depth:
        next_begin = text.find(begin, cursor)
        next_end = text.find(end, cursor)
        require(next_end >= 0, f"unclosed {environment} environment")
        if next_begin >= 0 and next_begin < next_end:
            depth += 1
            cursor = next_begin + len(begin)
        else:
            depth -= 1
            if depth == 0:
                return text[body_start:next_end], next_end + len(end)
            cursor = next_end + len(end)
    raise AssertionError("unreachable")


def _environment_body(text: str, start: int, environment: str) -> tuple[str, int]:
    begin = f"\\begin{{{environment}}}"
    require(text.startswith(begin, start), f"expected {begin} at byte {start}")
    return _environment_body_from(text, start + len(begin), environment)


@dataclass
class SelectorDiagnostics:
    missing_else_defaulted: int = 0


def select_tagged_content(
    text: str,
    tags: MutableMapping[str, bool],
    *,
    single_item: bool = False,
    diagnostics: SelectorDiagnostics | None = None,
) -> str:
    """Evaluate the exact Open Logic selection constructs without TeX."""
    output: list[str] = []
    cursor = 0
    while cursor < len(text):
        if text.startswith("\\tagtrue", cursor) or text.startswith("\\tagfalse", cursor):
            enabled = text.startswith("\\tagtrue", cursor)
            name = "\\tagtrue" if enabled else "\\tagfalse"
            values, cursor = _group(text, cursor + len(name))
            for value in values.split(","):
                if value.strip():
                    tags[value.strip()] = enabled
            continue
        if text.startswith("\\iftag", cursor):
            names, position = _group(text, cursor + len("\\iftag"))
            yes, position = _group(text, position)
            if _skip_space(text, position) < len(text) and text[_skip_space(text, position)] == "{":
                no, position = _group(text, position)
            else:
                no = ""
                if diagnostics is not None:
                    diagnostics.missing_else_defaulted += 1
            chosen = yes if _tag_any(names, tags) else no
            output.append(
                select_tagged_content(
                    chosen,
                    tags,
                    single_item=single_item,
                    diagnostics=diagnostics,
                )
            )
            cursor = position
            continue
        if text.startswith("\\tagitem", cursor):
            names, position = _group(text, cursor + len("\\tagitem"))
            yes, position = _group(text, position)
            if _skip_space(text, position) < len(text) and text[_skip_space(text, position)] == "{":
                no, position = _group(text, position)
            else:
                no = ""
                if diagnostics is not None:
                    diagnostics.missing_else_defaulted += 1
            chosen = yes if _tag_any(names, tags) else no
            if chosen.strip():
                output.append(
                    ("" if single_item else "\\item ")
                    + select_tagged_content(
                        chosen,
                        tags,
                        single_item=single_item,
                        diagnostics=diagnostics,
                    )
                )
            cursor = position
            continue
        if text.startswith("\\tagrefs", cursor):
            pairs, cursor = _group(text, cursor + len("\\tagrefs"))
            labels: list[str] = []
            for pair in pairs.split(","):
                if "/" not in pair:
                    continue
                tag, label = pair.split("/", 1)
                if _tag_value(tag, tags):
                    labels.append(label.strip())
            if labels:
                output.append("\\cref{" + ",".join(labels) + "}")
            continue
        if text.startswith("\\tagprob", cursor):
            position = cursor + len("\\tagprob")
            master, position = _optional(text, position)
            names, position = _group(text, position)
            end = text.find("\\tagendprob", position)
            require(end >= 0, "tagprob has no matching tagendprob")
            include = _tag_value(master or "tagTrue", tags) and not _tag_any(names, tags)
            if include:
                output.append(
                    select_tagged_content(
                        text[position:end],
                        tags,
                        single_item=single_item,
                        diagnostics=diagnostics,
                    )
                )
            cursor = end + len("\\tagendprob")
            continue
        if text.startswith("\\begin{tagblock}", cursor):
            prefix_end = cursor + len("\\begin{tagblock}")
            names, body_start = _group(text, prefix_end)
            body, actual_end = _environment_body_from(text, body_start, "tagblock")
            if _tag_any(names, tags):
                output.append(
                    select_tagged_content(
                        body,
                        tags,
                        single_item=single_item,
                        diagnostics=diagnostics,
                    )
                )
            cursor = actual_end
            continue
        if text.startswith("\\begin{tagenumerate}", cursor):
            prefix_end = cursor + len("\\begin{tagenumerate}")
            names, body_start = _group(text, prefix_end)
            body, end_at = _environment_body_from(text, body_start, "tagenumerate")
            count = sum(1 for name in names.split(",") if name.strip() and _tag_value(name, tags))
            selected = select_tagged_content(
                body,
                tags,
                single_item=count <= 1,
                diagnostics=diagnostics,
            )
            output.append(("\\begin{enumerate}\n" + selected + "\n\\end{enumerate}") if count > 1 else selected)
            cursor = end_at
            continue
        if single_item and text.startswith("\\item", cursor):
            cursor += len("\\item")
            continue
        if text.startswith("\\startycommalist", cursor):
            output.append("DIRECTCOMMASTART")
            cursor += len("\\startycommalist")
            continue
        if text.startswith("\\ycomma", cursor):
            output.append(", ")
            cursor += len("\\ycomma")
            continue
        output.append(text[cursor])
        cursor += 1
    result = "".join(output)
    result = re.sub(r"DIRECTCOMMASTART\s*,\s*", "", result)
    result = result.replace("DIRECTCOMMASTART", "")
    return result


@dataclass
class ReferenceState:
    part: str = "udf"
    chapter: str = "udf"
    section: str = "udf"

    def local(self, suffix: str) -> str:
        return f"{self.part}:{self.chapter}:{self.section}:{suffix}"


@dataclass
class ReferenceDiagnostics:
    malformed_conditional_tail_repairs: int = 0
    duplicate_label_definitions_renamed: int = 0


def _split_top_level_empty_group(value: str) -> tuple[str, str] | None:
    """Split the one upstream ``{yes{}tail}`` brace defect without guessing."""
    depth = 0
    cursor = 0
    while cursor < len(value):
        if value[cursor] == "\\":
            cursor += 2
            continue
        if value.startswith("{}", cursor) and depth == 0:
            return value[:cursor], value[cursor + 2 :]
        if value[cursor] == "{":
            depth += 1
        elif value[cursor] == "}":
            depth -= 1
            require(depth >= 0, "unbalanced conditional-label argument")
        cursor += 1
    require(depth == 0, "unbalanced conditional-label argument")
    return None


def _read_olfileid(text: str, position: int, state: ReferenceState) -> int:
    _locale, position = _optional(text, position)
    part, position = _group(text, position)
    chapter, position = _group(text, position)
    section, position = _group(text, position)
    state.part, state.chapter, state.section = part.strip(), chapter.strip(), section.strip()
    return position


def collect_reference_labels(texts: Sequence[str]) -> set[str]:
    labels: set[str] = set()
    state = ReferenceState()
    for text in texts:
        cursor = 0
        while cursor < len(text):
            if text.startswith("\\olfileid", cursor):
                cursor = _read_olfileid(text, cursor + len("\\olfileid"), state)
                continue
            if text.startswith("\\olpart", cursor):
                position = cursor + len("\\olpart")
                _short, position = _optional(text, position)
                part, position = _group(text, position)
                _title, position = _group(text, position)
                labels.add(f"{part.strip()}:::part")
                cursor = position
                continue
            if text.startswith("\\olchapter", cursor):
                position = cursor + len("\\olchapter")
                _short, position = _optional(text, position)
                part, position = _group(text, position)
                chapter, position = _group(text, position)
                _title, position = _group(text, position)
                labels.add(f"{part.strip()}:{chapter.strip()}::chap")
                cursor = position
                continue
            if text.startswith("\\olsection", cursor):
                position = cursor + len("\\olsection")
                _short, position = _optional(text, position)
                _title, position = _group(text, position)
                labels.add(state.local("sec"))
                cursor = position
                continue
            if text.startswith("\\ollabel", cursor):
                suffix, cursor = _group(text, cursor + len("\\ollabel"))
                labels.add(state.local(suffix.strip()))
                continue
            if text.startswith("\\label", cursor):
                label, cursor = _group(text, cursor + len("\\label"))
                labels.add(label.strip())
                continue
            cursor += 1
    return labels


def _olref_target(optionals: Sequence[str | None], suffix: str, state: ReferenceState) -> str:
    first, second, third = optionals
    if first is None:
        prefix = f"{state.part}:{state.chapter}:{state.section}"
    elif second is None:
        prefix = f"{state.part}:{state.chapter}:{first}"
    elif third is None:
        prefix = f"{state.part}:{first}:{second}"
    else:
        prefix = f"{first}:{second}:{third}"
    return f"{prefix}:{suffix}"


def transform_references(
    text: str,
    state: ReferenceState,
    known_labels: set[str],
    register: str,
    diagnostics: ReferenceDiagnostics | None = None,
    *,
    unit_id: str,
    emitted_labels: set[str],
) -> str:
    output: list[str] = []
    cursor = 0
    missing_text = (
        "verwijzing buiten deze gedeeltelijke editie"
        if register == "nl-standard"
        else "verwijzing naar een passage die nog niet in deze gedeeltelijke editie staat"
    )
    link_text = "de betreffende passage" if register == "nl-standard" else "die passage"

    def anchor(label: str) -> str:
        target = label
        if target in emitted_labels:
            target = f"{label}--unit-{unit_id.lower()}"
            suffix = 2
            while target in emitted_labels:
                target = f"{label}--unit-{unit_id.lower()}-{suffix}"
                suffix += 1
            if diagnostics is not None:
                diagnostics.duplicate_label_definitions_renamed += 1
        emitted_labels.add(target)
        return f"\\hypertarget{{{target}}}{{}}"

    def link(target: str, text_value: str | None = None) -> str:
        return f"\\hyperlink{{{target}}}{{{text_value or link_text}}}"

    while cursor < len(text):
        if text.startswith("\\olfileid", cursor):
            cursor = _read_olfileid(text, cursor + len("\\olfileid"), state)
            continue
        if text.startswith("\\olpart", cursor):
            position = cursor + len("\\olpart")
            _short, position = _optional(text, position)
            part, position = _group(text, position)
            title, position = _group(text, position)
            state.part = part.strip()
            label = f"{state.part}:::part"
            output.append(f"\\part{{{title}}}{anchor(label)}")
            cursor = position
            continue
        if text.startswith("\\olchapter", cursor):
            position = cursor + len("\\olchapter")
            _short, position = _optional(text, position)
            part, position = _group(text, position)
            chapter, position = _group(text, position)
            title, position = _group(text, position)
            state.part, state.chapter = part.strip(), chapter.strip()
            label = f"{state.part}:{state.chapter}::chap"
            output.append(f"\\chapter{{{title}}}{anchor(label)}")
            cursor = position
            continue
        if text.startswith("\\olsection", cursor):
            position = cursor + len("\\olsection")
            _short, position = _optional(text, position)
            title, position = _group(text, position)
            output.append(f"\\section{{{title}}}{anchor(state.local('sec'))}")
            cursor = position
            continue
        if text.startswith("\\ollabel", cursor):
            suffix, cursor = _group(text, cursor + len("\\ollabel"))
            output.append(anchor(state.local(suffix.strip())))
            continue
        if text.startswith("\\label", cursor):
            label, cursor = _group(text, cursor + len("\\label"))
            output.append(anchor(label.strip()))
            continue
        if text.startswith("\\olref", cursor) or text.startswith("\\Olref", cursor):
            name = "\\olref" if text.startswith("\\olref", cursor) else "\\Olref"
            position = cursor + len(name)
            options: list[str | None] = []
            for _ in range(3):
                value, next_position = _optional(text, position)
                if value is None:
                    options.extend([None] * (3 - len(options)))
                    break
                options.append(value.strip())
                position = next_position
            suffix, position = _group(text, position)
            target = _olref_target(options[:3], suffix.strip(), state)
            output.append(link(target) if target in known_labels else f"\\textit{{{missing_text}}}")
            cursor = position
            continue
        if text.startswith("\\oliflabeldef", cursor):
            label, position = _group(text, cursor + len("\\oliflabeldef"))
            yes, position = _group(text, position)
            unconditional_tail = ""
            if _skip_space(text, position) < len(text) and text[_skip_space(text, position)] == "{":
                no, position = _group(text, position)
            else:
                repaired = _split_top_level_empty_group(yes)
                require(
                    repaired is not None,
                    f"conditional label {label.strip()!r} has no false branch",
                )
                yes, unconditional_tail = repaired
                no = ""
                if diagnostics is not None:
                    diagnostics.malformed_conditional_tail_repairs += 1
            chosen = yes if label.strip() in known_labels else no
            output.append(
                transform_references(
                    chosen,
                    state,
                    known_labels,
                    register,
                    diagnostics,
                    unit_id=unit_id,
                    emitted_labels=emitted_labels,
                )
            )
            if unconditional_tail:
                output.append(
                    transform_references(
                        unconditional_tail,
                        state,
                        known_labels,
                        register,
                        diagnostics,
                        unit_id=unit_id,
                        emitted_labels=emitted_labels,
                    )
                )
            cursor = position
            continue
        if text.startswith("\\cref", cursor) or text.startswith("\\Cref", cursor):
            name = "\\cref" if text.startswith("\\cref", cursor) else "\\Cref"
            labels, position = _group(text, cursor + len(name))
            targets = [label.strip() for label in labels.split(",") if label.strip() in known_labels]
            rendered = [
                link(target, f"{link_text} {index}" if len(targets) > 1 else link_text)
                for index, target in enumerate(targets, start=1)
            ]
            output.append(", ".join(rendered) if rendered else f"\\textit{{{missing_text}}}")
            cursor = position
            continue
        if text.startswith("\\ref", cursor) and (
            cursor + len("\\ref") == len(text) or not text[cursor + len("\\ref")].isalpha()
        ):
            label, position = _group(text, cursor + len("\\ref"))
            target = label.strip()
            output.append(link(target) if target in known_labels else f"\\textit{{{missing_text}}}")
            cursor = position
            continue
        output.append(text[cursor])
        cursor += 1
    return "".join(output)


def _macro_math(value: str) -> str:
    stripped = value.strip()
    # Bussproofs arguments sometimes mix an outer textual bracket with inner
    # ``$...$`` delimiters (for example ``[$A$]^n``).  The whole argument is
    # mathematical in the linearized reader, so collapse those nested
    # delimiters before adding one well-formed pair.
    stripped = re.sub(r"(?<!\\)\$", "", stripped)
    return "$" + stripped + "$"


def transform_proof_commands(text: str, register: str) -> tuple[str, int]:
    """Linearize bussproofs commands while retaining every premise/conclusion."""
    labels = {
        "Axiom": "Premisse",
        "AxiomC": "Premisse",
        "UnaryInf": "Conclusie uit één voorafgaande regel",
        "UnaryInfC": "Conclusie uit één voorafgaande regel",
        "BinaryInf": "Conclusie uit twee voorafgaande regels",
        "BinaryInfC": "Conclusie uit twee voorafgaande regels",
        "TrinaryInf": "Conclusie uit drie voorafgaande regels",
        "TrinaryInfC": "Conclusie uit drie voorafgaande regels",
        "Deduce": "Tussenstap",
        "DeduceC": "Tussenstap",
    }
    output: list[str] = []
    cursor = 0
    converted = 0
    names = sorted(labels, key=len, reverse=True)
    while cursor < len(text):
        found = None
        for candidate in names:
            marker = "\\" + candidate
            if text.startswith(marker, cursor):
                found = candidate
                break
        if found:
            position = cursor + len(found) + 1
            if found.endswith("C"):
                value, position = _group(text, position)
            else:
                position = _skip_space(text, position)
                require(position < len(text) and text[position] == "$", f"{found} lacks delimited math")
                end = text.find("$", position + 1)
                require(end >= 0, f"{found} has unclosed delimited math")
                value, position = text[position + 1 : end], end + 1
            if value.strip():
                rendered_value = _macro_math(value)
            else:
                rendered_value = "\\textit{Premisseplaats zonder ingevulde formule.}"
            output.append(f"\n\\par\\textit{{{labels[found]}.}} {rendered_value}\n")
            converted += 1
            cursor = position
            continue
        if text.startswith("\\RightLabel", cursor) or text.startswith("\\LeftLabel", cursor):
            name = "\\RightLabel" if text.startswith("\\RightLabel", cursor) else "\\LeftLabel"
            value, cursor = _group(text, cursor + len(name))
            output.append(f"\\textit{{Regel:}} {value}. ")
            converted += 1
            continue
        if text.startswith("\\DischargeRule", cursor):
            values, cursor = _macro_arguments(text, cursor + len("\\DischargeRule"), 2)
            output.append(
                f"\\textit{{Regel:}} {values[0]} "
                f"(trekt aanname {values[1]} in). "
            )
            converted += 1
            continue
        replacements = {
            "\\DisplayProof": "\\par\n",
            "\\bottomAlignProof": "",
            "\\insertBetweenHyps": "",
            "\\noLine": "",
            "\\doubleLine": "\\textit{Dubbele afleidingslijn.}",
        }
        matched = next((key for key in replacements if text.startswith(key, cursor)), None)
        if matched:
            output.append(replacements[matched])
            cursor += len(matched)
            continue
        output.append(text[cursor])
        cursor += 1
    result = "".join(output)
    result = result.replace("\\begin{prooftree}", "\\begin{quote}\\textbf{Afleiding, regel voor regel.}\\par")
    result = result.replace("\\end{prooftree}", "\\end{quote}")
    return result, converted


def _argument(text: str, position: int) -> tuple[str, int]:
    """Read one ordinary TeX macro argument, braced or a single token."""
    position = _skip_space(text, position)
    require(position < len(text), "missing mandatory macro argument")
    if text[position] == "{":
        return _group(text, position)
    if text[position] == "\\":
        end = _command_end(text, position)
        return text[position:end], end
    return text[position], position + 1


def _macro_arguments(text: str, start: int, count: int) -> tuple[list[str], int]:
    values: list[str] = []
    position = start
    for _ in range(count):
        value, position = _argument(text, position)
        values.append(value)
    return values, position


def linearize_tableaux(text: str) -> tuple[str, int]:
    count = 0
    for environment in ("oltableau", "tableau"):
        begin = f"\\begin{{{environment}}}"
        while begin in text:
            start = text.index(begin)
            body, end = _environment_body(text, start, environment)
            formulas: list[str] = []
            cursor = 0
            while cursor < len(body):
                marker = body.find("\\sFmla", cursor)
                if marker < 0:
                    break
                values, position = _macro_arguments(body, marker + len("\\sFmla"), 2)
                prefix, position = _optional(body, position)
                formula = (f"{prefix}\\," if prefix is not None else "") + values[0] + "\\," + values[1]
                tail = body[position : body.find("\\sFmla", position) if body.find("\\sFmla", position) >= 0 else len(body)]
                rule_match = re.search(r"just\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|\\TAss|\\TRule\{[^{}]*\}\{[^{}]*\}(?:\[[^\]]*\])?)", tail, re.S)
                rule = rule_match.group(1).strip("{}") if rule_match else ""
                close = bool(re.search(r"(?:,|\s)close(?:,|\]|\s)", tail))
                line = f"\\item ${formula}$"
                if rule:
                    line += f" — regel {rule}"
                if close:
                    line += " — tak gesloten"
                formulas.append(line + ".")
                cursor = position
            require(bool(formulas), f"{environment} contains no signed formula nodes")
            replacement = (
                "\\begin{quote}\\textbf{Tableau, regel voor regel.}"
                "\\begin{enumerate}\n" + "\n".join(formulas) +
                "\n\\end{enumerate}\\end{quote}"
            )
            text = text[:start] + replacement + text[end:]
            count += 1
    return text, count


def linearize_derivations(text: str) -> tuple[str, int]:
    environment = "derivation"
    begin = f"\\begin{{{environment}}}"
    count = 0
    while begin in text:
        start = text.index(begin)
        body, end = _environment_body(text, start, environment)
        body = re.sub(r"\\\\(?:\[[^\]]*\])?", "\\\\par\n", body)
        body = body.replace("&", " ")
        replacement = "\\begin{quote}\\textbf{Afleiding.}\\par\n" + body + "\n\\end{quote}"
        text = text[:start] + replacement + text[end:]
        count += 1
    return text, count


def _split_align_rows(chunk: str) -> list[str]:
    rows: list[str] = []
    start = 0
    cursor = 0
    brace_depth = 0
    environment_depth = 0
    environment_marker = re.compile(r"\\(begin|end)\{([^{}]+)\}")
    while cursor < len(chunk):
        marker = environment_marker.match(chunk, cursor)
        if marker:
            environment_depth += 1 if marker.group(1) == "begin" else -1
            require(environment_depth >= 0, "unbalanced nested environment in align")
            cursor = marker.end()
            continue
        if chunk[cursor] == "\\":
            if (
                cursor + 1 < len(chunk)
                and chunk[cursor + 1] == "\\"
                and brace_depth == 0
                and environment_depth == 0
            ):
                rows.append(chunk[start:cursor])
                cursor += 2
                if cursor < len(chunk) and chunk[cursor] == "[":
                    _skip, cursor = _group(chunk, cursor, "[", "]")
                start = cursor
                continue
            cursor += 2
            continue
        if chunk[cursor] == "{":
            brace_depth += 1
        elif chunk[cursor] == "}":
            brace_depth -= 1
            require(brace_depth >= 0, "unbalanced braces in align")
        cursor += 1
    require(brace_depth == 0, "unbalanced braces in align")
    require(environment_depth == 0, "unbalanced nested environment in align")
    rows.append(chunk[start:])
    return rows


def _align_math_chunk(chunk: str) -> str:
    chunk = re.sub(r"\\emph\{([^{}]*)\}", r"\\text{\1}", chunk)
    rendered: list[str] = []
    for row in _split_align_rows(chunk):
        row = row.replace("&", "").strip()
        if row:
            rendered.append("\\[\n" + row + "\n\\]")
    return "\n".join(rendered)


def unwrap_display_proof_quotes(text: str) -> tuple[str, int]:
    pattern = re.compile(r"\\\[((?:(?!\\\[|\\\]).)*)\\\]", re.S)
    converted = 0

    def unwrap(match: re.Match[str]) -> str:
        nonlocal converted
        body = match.group(1)
        if "\\par\\textit{Premisse." not in body:
            return match.group(0)
        converted += 1
        return body.strip()

    return pattern.sub(unwrap, text), converted


def normalize_align_environments(text: str) -> tuple[str, int]:
    """Turn align rows and interposed prose into Pandoc-safe blocks."""
    count = 0
    for environment in ("align*", "align"):
        begin = f"\\begin{{{environment}}}"
        while begin in text:
            start = text.index(begin)
            body, end = _environment_body(text, start, environment)
            pieces: list[str] = []
            cursor = 0
            while cursor < len(body):
                marker = body.find("\\intertext", cursor)
                if marker < 0:
                    pieces.append(_align_math_chunk(body[cursor:]))
                    break
                pieces.append(_align_math_chunk(body[cursor:marker]))
                prose, position = _group(body, marker + len("\\intertext"))
                pieces.append("\n" + prose.strip() + "\n")
                cursor = position
            replacement = "\n".join(piece for piece in pieces if piece.strip())
            text = text[:start] + replacement + text[end:]
            count += 1
    return text, count


ANCHOR_RE = re.compile(r"\\hypertarget\{([^{}]+)\}\{\}")


def move_math_anchors_outside(text: str) -> tuple[str, int]:
    """Move structural anchors next to, rather than inside, math spans."""
    moved = 0

    def rewrite(pattern: re.Pattern[str], opener: str, closer: str, value: str) -> str:
        nonlocal moved

        def replace(match: re.Match[str]) -> str:
            nonlocal moved
            body = match.group(1)
            anchors = ANCHOR_RE.findall(body)
            if not anchors:
                return match.group(0)
            moved += len(anchors)
            clean = ANCHOR_RE.sub("", body)
            prefix = "".join(f"\\hypertarget{{{anchor}}}{{}}" for anchor in anchors)
            return prefix + opener + clean + closer

        return pattern.sub(replace, value)

    text = rewrite(re.compile(r"\$\$(.*?)\$\$", re.S), "$$", "$$", text)
    text = rewrite(re.compile(r"\\\[(.*?)\\\]", re.S), "\\[", "\\]", text)
    text = rewrite(re.compile(r"\\\((.*?)\\\)", re.S), "\\(", "\\)", text)
    inline = re.compile(r"(?<![\\$])\$(?!\$)(.*?)(?<![\\$])\$(?!\$)", re.S)
    text = rewrite(inline, "$", "$", text)
    return text, moved


def normalize_local_math_declarations(text: str) -> tuple[str, int]:
    """Remove the one source-local relation declaration we expand ourselves."""
    pattern = re.compile(
        r"\\DeclareRobustCommand\s*\{\\VDash\}\s*"
        r"\{\\mathrel\{\|\|\}\\joinrel\\Relbar\}"
    )
    return pattern.subn("", text)


def _flatten_hyperlinks(value: str) -> tuple[str, int]:
    output: list[str] = []
    cursor = 0
    converted = 0
    marker = "\\hyperlink"
    while cursor < len(value):
        if value.startswith(marker, cursor):
            _target, position = _group(value, cursor + len(marker))
            label, position = _group(value, position)
            output.append(label)
            cursor = position
            converted += 1
            continue
        output.append(value[cursor])
        cursor += 1
    return "".join(output), converted


def flatten_math_hyperlinks(text: str) -> tuple[str, int]:
    """Keep reference wording in math while avoiding unsupported link nodes."""
    pattern = re.compile(
        r"\$\$(.*?)\$\$|"
        r"\\\[(.*?)\\\]|"
        r"\\\((.*?)\\\)|"
        r"(?<![\\$])\$(?!\$)(.*?)(?<![\\$])\$(?!\$)",
        re.S,
    )
    converted = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal converted
        value, count = _flatten_hyperlinks(match.group(0))
        converted += count
        return value

    return pattern.sub(replace, text), converted


def transform_special_math(text: str) -> tuple[str, int]:
    """Expand xparse-only optional/slash macros into ordinary LaTeX."""
    output: list[str] = []
    cursor = 0
    converted = 0
    while cursor < len(text):
        name = None
        for candidate in (
            "elemequiv", "QuantRank", "substruct", "forallx", "mModel",
            "Theory", "concat", "lambd", "OCon", "OPrf", "Expan", "PIso",
            "mSat", "Part", "VDash", "Var", "gn", "iso", "Th",
            "lexists", "lforall", "eq", "Sat", "Proves", "Entails", "varAssign",
            "Value", "pValue", "pSat", "Log", "Trm", "Frm", "Intro", "Elim",
            "TRule", "sFmla", "indcase",
        ):
            marker = "\\" + candidate
            if text.startswith(marker, cursor) and (
                cursor + len(marker) == len(text) or not text[cursor + len(marker)].isalpha()
            ):
                name = candidate
                break
        if name is None:
            output.append(text[cursor])
            cursor += 1
            continue
        position = cursor + len(name) + 1
        if name in {"Var", "substruct", "concat", "forallx", "VDash"}:
            output.append(
                {
                    "Var": r"\mathrm{Var}",
                    "substruct": r"\subseteq",
                    "concat": r"\frown",
                    "forallx": r"\forall x",
                    "VDash": r"\Vdash",
                }[name]
            )
        elif name in {"Proves", "Entails"}:
            position = _skip_space(text, position)
            negated = position < len(text) and text[position] == "/"
            if negated:
                position += 1
            subscript, position = _optional(text, position)
            symbol = "\\nvdash" if name == "Proves" and negated else "\\vdash" if name == "Proves" else "\\nvDash" if negated else "\\vDash"
            output.append(symbol + (f"_{{{subscript}}}" if subscript is not None else "{}"))
        elif name in {"lexists", "lforall"}:
            position = _skip_space(text, position)
            unique = name == "lexists" and position < len(text) and text[position] == "!"
            if unique:
                position += 1
            variable, position = _optional(text, position)
            scope, position = _optional(text, position)
            symbol = "\\exists!" if unique else "\\exists" if name == "lexists" else "\\forall"
            output.append(symbol + (f" {variable}" if variable is not None else "") + (f"\\,{scope}" if scope is not None else ""))
        elif name == "eq":
            position = _skip_space(text, position)
            negated = position < len(text) and text[position] == "/"
            if negated:
                position += 1
            left, position = _optional(text, position)
            right, position = _optional(text, position)
            symbol = "\\neq" if negated else "="
            output.append(f"{left} {symbol} {right}" if right is not None else symbol)
        elif name in {"elemequiv", "iso"}:
            position = _skip_space(text, position)
            negated = position < len(text) and text[position] == "/"
            if negated:
                position += 1
            subscript, position = _optional(text, position)
            if name == "elemequiv":
                symbol = r"\not\equiv" if negated else r"\equiv"
            else:
                symbol = r"\not\simeq" if negated else r"\simeq"
            output.append(symbol + (f"_{{{subscript}}}" if subscript is not None else ""))
        elif name == "lambd":
            variable, position = _optional(text, position)
            body, position = _optional(text, position)
            output.append(
                r"\lambda"
                + (f" {variable}" if variable is not None else "")
                + (f".\\,{body}" if body is not None else "")
            )
        elif name == "mSat":
            position = _skip_space(text, position)
            negated = position < len(text) and text[position] == "/"
            if negated:
                position += 1
            values, position = _macro_arguments(text, position, 2)
            world, position = _optional(text, position)
            prefix = f"\\mathfrak{{{values[0]}}}" + (
                f", {world}" if world is not None else ""
            )
            output.append(prefix + (r" \nVdash " if negated else r" \Vdash ") + values[1])
        elif name == "mModel":
            value, position = _argument(text, position)
            output.append(f"\\mathfrak{{{value}}}")
        elif name in {"Part", "Expan"}:
            values, position = _macro_arguments(text, position, 2)
            if name == "Part":
                output.append(f"\\mathsf{{P}}({values[0]}, {values[1]})")
            else:
                output.append(f"(\\mathfrak{{{values[0]}}}, {values[1]})")
        elif name in {"Theory", "QuantRank", "PIso", "Th", "gn"}:
            value, position = _argument(text, position)
            output.append(
                {
                    "Theory": f"\\mathrm{{Th}}(\\mathfrak{{{value}}})",
                    "QuantRank": f"\\mathrm{{qr}}({value})",
                    "PIso": f"\\mathcal{{{value}}}",
                    "Th": f"\\mathbf{{{value}}}",
                    "gn": f"\\ulcorner {value} \\urcorner",
                }[name]
            )
        elif name in {"OCon", "OPrf"}:
            subscript, position = _optional(text, position)
            operator = "Con" if name == "OCon" else "Prf"
            output.append(
                f"\\mathsf{{{operator}}}"
                + (f"_{{{subscript}}}" if subscript is not None else "")
            )
        elif name == "Sat":
            position = _skip_space(text, position)
            negated = position < len(text) and text[position] == "/"
            if negated:
                position += 1
            values, position = _macro_arguments(text, position, 2)
            assignment, position = _optional(text, position)
            prefix = f"\\mathfrak{{{values[0]}}}" + (f", {assignment}" if assignment is not None else "")
            output.append(prefix + (" \\nvDash " if negated else " \\vDash ") + values[1])
        elif name == "pSat":
            position = _skip_space(text, position)
            negated = position < len(text) and text[position] == "/"
            if negated:
                position += 1
            values, position = _macro_arguments(text, position, 2)
            logic, position = _optional(text, position)
            symbol = "\\nvDash" if negated else "\\vDash"
            output.append(f"\\mathfrak{{{values[0]}}} {symbol}" + (f"_{{{logic}}}" if logic is not None else "") + f" {values[1]}")
        elif name == "varAssign":
            values, position = _macro_arguments(text, position, 3)
            value, position = _optional(text, position)
            output.append(f"{values[0]} = {values[1]}[^{value}/{values[2]}]" if value is not None else f"{values[0]} \\sim_{{{values[2]}}} {values[1]}")
        elif name == "Value":
            values, position = _macro_arguments(text, position, 2)
            assignment, position = _optional(text, position)
            output.append(f"\\mathrm{{Val}}^{{\\mathfrak{{{values[1]}}}}}" + (f"_{{{assignment}}}" if assignment is not None else "") + f"({values[0]})")
        elif name == "pValue":
            values, position = _macro_arguments(text, position, 1)
            argument = None
            position = _skip_space(text, position)
            if position < len(text) and text[position] == "(":
                argument, position = _group(text, position, "(", ")")
            logic, position = _optional(text, position)
            output.append(f"\\overline{{\\mathfrak{{{values[0]}}}}}" + (f"_{{{logic}}}" if logic is not None else "") + (f"({argument})" if argument is not None else ""))
        elif name in {"Log", "Intro", "Elim", "TRule", "sFmla"}:
            mandatory = 1 if name in {"Log", "Intro", "Elim"} else 2
            values, position = _macro_arguments(text, position, mandatory)
            optional, position = _optional(text, position)
            if name == "Log":
                rendered = f"\\mathbf{{{values[0]}}}" + (f"_{{{optional}}}" if optional is not None else "")
            elif name in {"Intro", "Elim"}:
                rendered = f"{values[0]}\\mathrm{{{'Intro' if name == 'Intro' else 'Elim'}}}" + (f"_{{{optional}}}" if optional is not None else "")
            elif name == "TRule":
                rendered = f"{values[1]}{values[0]}" + (f"\\,{optional}" if optional is not None else "")
            else:
                rendered = (f"{optional}\\," if optional is not None else "") + f"{values[0]}\\,{values[1]}"
            output.append(rendered)
        elif name in {"Trm", "Frm"}:
            language, position = _optional(text, position)
            operator = "Trm" if name == "Trm" else "Frm"
            output.append(f"\\mathrm{{{operator}}}" + (f"(\\mathcal{{{language}}})" if language is not None else ""))
        else:  # indcase
            position = _skip_space(text, position)
            atomic = position < len(text) and text[position] == "*"
            exercise = position < len(text) and text[position] == "!"
            if atomic or exercise:
                position += 1
            values, position = _macro_arguments(text, position, 3)
            body = values[2].replace("\\indfrm", values[0]).replace("\\indfrmp", values[0]).replace("\\indcomplex", values[1])
            if exercise:
                rendered = "Opgave."
            elif atomic:
                rendered = f"${values[0]}$ is atomair: {body}"
            else:
                rendered = f"Als ${values[0]} \\equiv {values[1]}$, dan {body}"
            output.append(rendered)
        cursor = position
        converted += 1
    return "".join(output), converted


def _svg(title: str, description: str, body: str, *, view_box: str = "0 0 480 260") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box}" role="img" aria-labelledby="title desc">'
        f'<title id="title">{html_module.escape(title)}</title>'
        f'<desc id="desc">{html_module.escape(description)}</desc>'
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#222"/></marker></defs>'
        '<rect width="100%" height="100%" fill="white"/>' + body + '</svg>\n'
    ).encode("utf-8")


def known_diagram_svg(kind: str, register: str) -> tuple[bytes, str]:
    ordinary = register == "nl-gewoon"
    if kind in {"union", "intersection", "difference"}:
        operation = {"union": "vereniging", "intersection": "doorsnede", "difference": "verschil"}[kind]
        title = f"Venn-diagram van de {operation} van A en B"
        if kind == "union":
            fills = '<circle cx="185" cy="130" r="92" fill="#79aee3" fill-opacity=".55"/><circle cx="295" cy="130" r="92" fill="#e8a25d" fill-opacity=".55"/>'
        elif kind == "intersection":
            fills = '<circle cx="185" cy="130" r="92" fill="none"/><circle cx="295" cy="130" r="92" fill="none"/><path d="M240 56 A92 92 0 0 0 240 204 A92 92 0 0 0 240 56" fill="#8e6bbd" fill-opacity=".62"/>'
        else:
            fills = '<circle cx="185" cy="130" r="92" fill="#79aee3" fill-opacity=".55"/><circle cx="295" cy="130" r="92" fill="white"/>'
        body = fills + '<circle cx="185" cy="130" r="92" fill="none" stroke="#245" stroke-width="4"/><circle cx="295" cy="130" r="92" fill="none" stroke="#742" stroke-width="4"/><text x="135" y="125" font-size="28">A</text><text x="330" y="125" font-size="28">B</text>'
        desc = f"Twee overlappende verzamelingen A en B; het gekleurde gebied is hun {operation}."
        return _svg(title, desc, body), desc
    if kind in {"function", "surjective", "injective", "bijective"}:
        maps = {
            "function": [(0, 0), (1, 1), (2, 1)],
            "surjective": [(0, 0), (1, 1), (2, 0)],
            "injective": [(0, 0), (1, 2)],
            "bijective": [(0, 0), (1, 1), (2, 2)],
        }[kind]
        dutch = {"function": "functie", "surjective": "surjectieve functie", "injective": "injectieve functie", "bijective": "bijectieve functie"}[kind]
        ys = [70, 130, 190]
        body = '<ellipse cx="120" cy="130" rx="82" ry="112" fill="#dcecff" stroke="#245" stroke-width="3"/><ellipse cx="360" cy="130" rx="82" ry="112" fill="#fff0dc" stroke="#742" stroke-width="3"/>'
        for x in (120, 360):
            for y in ys:
                body += f'<circle cx="{x}" cy="{y}" r="7" fill="#222"/>'
        for left, right in maps:
            body += f'<line x1="128" y1="{ys[left]}" x2="350" y2="{ys[right]}" stroke="#222" stroke-width="3" marker-end="url(#arrow)"/>'
        desc = f"Pijlendiagram van een {dutch}; elk getekend domeinelement heeft precies één pijl naar het codomein."
        return _svg(f"Pijlendiagram: {dutch}", desc, body), desc
    if kind == "composition":
        body = ''.join(f'<ellipse cx="{x}" cy="130" rx="60" ry="105" fill="{fill}" stroke="#333" stroke-width="3"/>' for x, fill in ((80, '#dcecff'), (240, '#fff0dc'), (400, '#e4f5de')))
        for x in (80, 240, 400):
            for y in (80, 130, 180):
                body += f'<circle cx="{x}" cy="{y}" r="6" fill="#222"/>'
        for y1, y2 in ((80, 130), (130, 180), (180, 80)):
            body += f'<line x1="88" y1="{y1}" x2="230" y2="{y2}" stroke="#222" stroke-width="3" marker-end="url(#arrow)"/>'
        for y1, y2 in ((130, 80), (180, 180), (80, 130)):
            body += f'<line x1="248" y1="{y1}" x2="390" y2="{y2}" stroke="#222" stroke-width="3" marker-end="url(#arrow)"/>'
        desc = "Drie verzamelingen met pijlen van de eerste naar de tweede en van de tweede naar de derde; de gestippelde gedachtegang is de samengestelde functie van de eerste naar de derde."
        return _svg("Samenstelling van twee functies", desc, body), desc
    if kind in {"directed-graph-four", "directed-graph-three"}:
        nodes = {"1": (90, 70), "2": (250, 70), "3": (250, 190)}
        if kind.endswith("four"):
            nodes["4"] = (410, 70)
        edges = [("1", "2"), ("1", "3"), ("2", "3")]
        body = '<path d="M75 52 C45 5, 130 5, 105 52" fill="none" stroke="#222" stroke-width="3" marker-end="url(#arrow)"/>'
        for a, b in edges:
            x1, y1 = nodes[a]; x2, y2 = nodes[b]
            body += f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#222" stroke-width="3" marker-end="url(#arrow)"/>'
        for label, (x, y) in nodes.items():
            body += f'<circle cx="{x}" cy="{y}" r="24" fill="white" stroke="#222" stroke-width="3"/><text x="{x}" y="{y+8}" text-anchor="middle" font-size="24">{label}</text>'
        desc = "Gerichte graaf met een lus bij 1 en de pijlen 1→2, 1→3 en 2→3" + ("; knoop 4 is geïsoleerd." if kind.endswith("four") else ".")
        return _svg("Gerichte graaf", desc, body), desc
    if kind == "rooted-tree":
        points = {"r": (240, 40), "a": (150, 115), "b": (330, 115), "c": (80, 210), "d": (150, 210), "e": (220, 210)}
        body = ""
        for a, b in (("r", "a"), ("r", "b"), ("a", "c"), ("a", "d"), ("a", "e")):
            x1, y1 = points[a]; x2, y2 = points[b]
            body += f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#222" stroke-width="3"/>'
        for label, (x, y) in points.items():
            body += f'<circle cx="{x}" cy="{y}" r="22" fill="white" stroke="#222" stroke-width="3"/><text x="{x}" y="{y+7}" text-anchor="middle" font-size="22">{label}</text>'
        desc = "Boom met wortel r; r heeft kinderen a en b, en a heeft kinderen c, d en e."
        return _svg("Gewortelde boom", desc, body), desc
    if kind == "real-rectangle":
        body = '<rect x="50" y="30" width="310" height="190" fill="none" stroke="#222" stroke-width="4"/><rect x="50" y="75" width="235" height="145" fill="#e66" fill-opacity=".45" stroke="#933" stroke-width="3"/><rect x="125" y="30" width="235" height="145" fill="#edc54f" fill-opacity=".45" stroke="#986" stroke-width="3"/><rect x="125" y="75" width="160" height="100" fill="#e98a34" fill-opacity=".55"/><line x1="395" y1="75" x2="395" y2="220" stroke="#222" stroke-width="3"/><text x="410" y="150" font-size="24">n</text><line x1="445" y1="30" x2="445" y2="220" stroke="#222" stroke-width="3"/><text x="458" y="130" font-size="24">m</text>'
        desc = "Twee verschoven vierkanten binnen een omhullende rechthoek; de verticale lengten n en m geven de twee schaalmaten aan."
        return _svg("Overlappende rechthoeken met maten n en m", desc, body), desc
    if kind == "hilbert-hotel":
        body = '<text x="30" y="75" font-size="20">oude kamer</text><text x="30" y="175" font-size="20">nieuwe kamer</text>'
        for n in range(1, 10):
            x = 125 + (n - 1) * 36
            body += f'<text x="{x}" y="72" text-anchor="middle" font-size="18">{n}</text><text x="{x}" y="178" text-anchor="middle" font-size="18">{n}</text>'
            if n < 9:
                body += f'<line x1="{x}" y1="82" x2="{x+36}" y2="158" stroke="#222" stroke-width="2" marker-end="url(#arrow)"/>'
        body += '<text x="455" y="72" font-size="20">…</text><text x="455" y="178" font-size="20">…</text><circle cx="125" cy="174" r="18" fill="none" stroke="#b22" stroke-width="3"/>'
        desc = "Elke gast uit kamer n verhuist naar kamer n+1; daardoor komt kamer 1 vrij voor een nieuwe gast."
        return _svg("Hilberts hotel: verschuiving n naar n+1", desc, body), desc
    raise DirectConversionError(f"unknown source diagram kind: {kind}")


def replace_diagrams(
    text: str,
    register: str,
    assets: MutableMapping[str, bytes],
    *,
    unit_id: str,
) -> tuple[str, int]:
    count = 0
    cursor = 0
    output: list[str] = []
    while cursor < len(text):
        if text.startswith("\\olasset", cursor):
            position = cursor + len("\\olasset")
            _width, position = _optional(text, position)
            path, position = _group(text, position)
            kind = Path(path.strip()).stem
            payload, description = known_diagram_svg(kind, register)
            name = f"diagram-{unit_id.lower()}-{count+1:03d}-{kind}.svg"
            require(name not in assets, f"duplicate generated diagram name: {name}")
            assets[name] = payload
            output.append(
                f"\\includegraphics[alt={{{description}}}]{{{name}}}"
                f"\\par\\textit{{Beschrijving: {description}}}"
            )
            count += 1
            cursor = position
            continue
        if text.startswith("\\begin{tikzpicture}", cursor):
            body, position = _environment_body(text, cursor, "tikzpicture")
            if "loop above" in body:
                kind = "directed-graph-four" if "(D)" in body else "directed-graph-three"
            elif "grow'=up" in body:
                kind = "rooted-tree"
            elif "rectangle (3,3)" in body or "rectangle (3, 3)" in body:
                kind = "real-rectangle"
            elif "\\foreach" in body and "(1b)--(2a)" in body:
                kind = "hilbert-hotel"
            else:
                raise DirectConversionError(
                    "unrecognized inline TikZ diagram sha256=" + sha256_bytes(body.encode("utf-8"))
                )
            payload, description = known_diagram_svg(kind, register)
            name = f"diagram-{unit_id.lower()}-{count+1:03d}-{kind}.svg"
            require(name not in assets, f"duplicate generated diagram name: {name}")
            assets[name] = payload
            output.append(
                f"\\includegraphics[alt={{{description}}}]{{{name}}}"
                f"\\par\\textit{{Beschrijving: {description}}}"
            )
            count += 1
            cursor = position
            continue
        output.append(text[cursor])
        cursor += 1
    result = "".join(output)
    # The two graph illustrations occur in an align environment.  Once the
    # diagrams are real image blocks, retain the interposed prose and remove
    # only the alignment scaffolding.
    pattern = re.compile(r"\\begin\{align\*?\}(.*?)\\end\{align\*?\}", re.S)
    def flatten(match: re.Match[str]) -> str:
        body = match.group(1)
        if "\\includegraphics" not in body:
            return match.group(0)
        body = re.sub(r"\\intertext\{((?:[^{}]|\{[^{}]*\})*)\}", r"\n\1\n", body)
        body = re.sub(r"\\\\(?:\[[^\]]*\])?", "\n", body)
        return body.replace("&", "")
    result = pattern.sub(flatten, result)
    return result, count


def normalize_auxiliary_environments(text: str) -> str:
    labels = {
        "explain": "Uitleg",
        "editorial": "Redactionele opmerking",
        "intro": "Inleiding",
        "digress": "Uitweiding",
        "history": "Historische achtergrond",
    }
    for environment, label in labels.items():
        text = text.replace(f"\\begin{{{environment}}}", f"\\begin{{quote}}\\textbf{{{label}.}} ")
        text = text.replace(f"\\end{{{environment}}}", "\\end{quote}")
    text = text.replace("\\begin{defish}", "\\begin{quote}\\textbf{Regel of definitie.} ")
    text = text.replace("\\end{defish}", "\\end{quote}")
    return text


PANDOC_PREAMBLE = r"""
\documentclass{book}
\usepackage{amsmath,amssymb,graphicx}
\newtheorem{defn}{Definitie}
\newtheorem{thm}{Stelling}
\newtheorem{lem}{Lemma}
\newtheorem{prop}{Propositie}
\newtheorem{cor}{Gevolg}
\newtheorem{ex}{Voorbeeld}
\newtheorem{prob}{Opgave}
\newcommand{\formula}[1]{#1}
\newcommand{\True}{\mathbb{T}}
\newcommand{\False}{\mathbb{F}}
\newcommand{\lfalse}{\bot}
\newcommand{\ltrue}{\top}
\newcommand{\lif}{\mathbin{\rightarrow}}
\newcommand{\liff}{\mathbin{\leftrightarrow}}
\newcommand{\Sequent}{\Rightarrow}
\newcommand{\fCenter}{\Rightarrow}
\newcommand{\LeftR}[1]{{#1}\mathrm{L}}
\newcommand{\RightR}[1]{{#1}\mathrm{R}}
\newcommand{\Weakening}{\mathrm{W}}
\newcommand{\Contraction}{\mathrm{C}}
\newcommand{\Exchange}{\mathrm{X}}
\newcommand{\Cut}{\mathrm{Cut}}
\newcommand{\FalseInt}{\bot_I}
\newcommand{\FalseCl}{\bot_C}
\newcommand{\Discharge}[2]{[#1]^{#2}}
\newcommand{\SSubst}[2]{#1[#2]}
\newcommand{\Subst}[3]{#1[#2/#3]}
\newcommand{\subst}[2]{#1/#2}
\newcommand{\PAx}{\mathrm{Ax}_0}
\newcommand{\Domain}[1]{\left|\mathfrak{#1}\right|}
\newcommand{\Assign}[2]{#1^{\mathfrak{#2}}}
\newcommand{\pAssign}[1]{\mathfrak{#1}}
\newcommand{\ident}{\equiv}
\newcommand{\Setabs}[2]{\{#1:#2\}}
\newcommand{\Pow}[1]{\wp(#1)}
\newcommand{\dom}[1]{\mathrm{dom}(#1)}
\newcommand{\ran}[1]{\mathrm{ran}(#1)}
\newcommand{\len}[1]{\mathrm{len}(#1)}
\newcommand{\emptyseq}{\Lambda}
\newcommand{\cardle}[2]{#1\preceq #2}
\newcommand{\cardless}[2]{#1\prec #2}
\newcommand{\cardeq}[2]{#1\approx #2}
\newcommand{\cardneq}[2]{#1\not\approx #2}
\newcommand{\tuple}[1]{\langle #1\rangle}
\newcommand{\Nat}{\mathbb{N}}
\newcommand{\Int}{\mathbb{Z}}
\newcommand{\PosInt}{\mathbb{Z}^{+}}
\newcommand{\Real}{\mathbb{R}}
\newcommand{\Rat}{\mathbb{Q}}
\newcommand{\Bin}{\mathbb{B}}
\newcommand{\Id}[1]{\mathrm{Id}_{#1}}
\newcommand{\Struct}[1]{\mathfrak{#1}}
\newcommand{\Lang}[1]{\mathcal{#1}}
\newcommand{\Obj}[1]{\mathsf{#1}}
\newcommand{\Atom}[2]{#1(#2)}
\newcommand{\PVar}{\mathrm{At}_0}
\newcommand{\funimage}[2]{#1[#2]}
\newcommand{\closureofunder}[2]{\mathrm{clo}_{#1}(#2)}
\newcommand{\Closureofunder}[2]{\mathrm{Clo}_{#1}(#2)}
\newcommand{\equivrep}[2]{[#1]_{#2}}
\newcommand{\equivclass}[2]{#1/_{#2}}
\newcommand{\Intequiv}{\sim}
\newcommand{\Ratequiv}{\backsim}
\newcommand{\Realequiv}{\bumpeq}
\newcommand{\funrestrictionto}[2]{#1\restriction_{#2}}
\newcommand{\nicefrac}[2]{\frac{#1}{#2}}
\newcommand{\num}[1]{\overline{#1}}
\newcommand{\pto}{\rightharpoonup}
\newcommand{\fdefined}{\downarrow}
\newcommand{\fundefined}{\uparrow}
\newcommand{\sqsubseteq}{\subseteq}
\newcommand{\defis}{=}
"""


@dataclass(frozen=True)
class DirectDocument:
    tex: bytes
    assets: Mapping[str, bytes]
    unit_input_sha256: Mapping[str, str]
    metrics: Mapping[str, int]


def make_direct_document(
    units: Sequence[tuple[str, str]],
    *,
    locale_path: Path,
    register: str,
    scope_title: str,
    modified: str,
) -> DirectDocument:
    """Create a standalone Pandoc-readable LaTeX document without running TeX."""
    require(register in {"nl-standard", "nl-gewoon"}, f"unsupported register: {register}")
    tokens = load_text_tokens(locale_path)
    tags = initial_tags()
    selector_diagnostics = SelectorDiagnostics()
    selected: list[tuple[str, str]] = []
    for unit_id, raw in units:
        body = _strip_comments(raw)
        body = select_tagged_content(body, tags, diagnostics=selector_diagnostics)
        body = expand_text_tokens(body, tokens)
        selected.append((unit_id, body))
    labels = collect_reference_labels([body for _unit_id, body in selected])
    state = ReferenceState()
    reference_diagnostics = ReferenceDiagnostics()
    emitted_labels: set[str] = set()
    assets: dict[str, bytes] = {}
    fragments: list[str] = []
    hashes: dict[str, str] = {}
    proof_commands = proof_math_wrappers = tableaux = derivations = diagrams = alignments = special_math = math_anchors = math_hyperlinks = 0
    for unit_id, body in selected:
        body = transform_references(
            body,
            state,
            labels,
            register,
            reference_diagnostics,
            unit_id=unit_id,
            emitted_labels=emitted_labels,
        )
        body, converted = linearize_tableaux(body)
        tableaux += converted
        body, converted = transform_proof_commands(body, register)
        proof_commands += converted
        body, converted = unwrap_display_proof_quotes(body)
        proof_math_wrappers += converted
        body, converted = linearize_derivations(body)
        derivations += converted
        body, converted = replace_diagrams(body, register, assets, unit_id=unit_id)
        diagrams += converted
        body, converted = normalize_align_environments(body)
        alignments += converted
        body = normalize_auxiliary_environments(body)
        body, converted = normalize_local_math_declarations(body)
        special_math += converted
        for _pass in range(16):
            body, converted = transform_special_math(body)
            special_math += converted
            if converted == 0:
                break
        else:
            raise DirectConversionError(f"special-math normalization did not converge in {unit_id}")
        body = re.sub(r"!([A-Z])", r"{\1}", body)
        body = body.replace("\\shoveright", "").replace("\\shoveleft", "")
        body = body.replace("\\small", "").replace("\\bottomAlignProof", "")
        body = body.replace("\\OLEndChapterHook", "").replace("\\OLEndPartHook", "")
        body = re.sub(r"\\(?:notag|nonumber)\b", "", body)
        body, converted = flatten_math_hyperlinks(body)
        math_hyperlinks += converted
        body, converted = move_math_anchors_outside(body)
        math_anchors += converted
        require("\\iftag" not in body and "\\tagitem" not in body, f"unresolved selector in {unit_id}")
        require("!!" not in body, f"unresolved token in {unit_id}")
        normalized = body.strip() + "\n"
        hashes[unit_id] = sha256_bytes(normalized.encode("utf-8"))
        fragments.append(
            f"\\hypertarget{{unit-{unit_id}}}{{}}\n"
            f"\\typeout{{DIRECT-EPUB-UNIT {unit_id}}}\n"
            + normalized +
            f"\\hypertarget{{unit-end-{unit_id}}}{{}}\n"
        )
    title = "The Open Logic Text — " + scope_title
    front = (
        f"\\title{{{title}}}\n"
        "\\author{The Open Logic Project en genoemde medewerkers}\n"
        f"\\date{{{modified}}}\n"
        "\\begin{document}\n\\maketitle\n"
        "\\chapter*{Reikwijdte van deze gedeeltelijke editie}\n"
        f"Deze reader bevat uitsluitend {len(units)} opeenvolgende geaccepteerde broneenheden. "
        "Vertaalde maar nog niet geaccepteerde tekst staat er niet in.\n"
        "\\chapter*{Status en verantwoording}\n"
        "De termen ‘geaccepteerd’ en ‘gecontroleerd’ verwijzen naar de vastgelegde "
        "AI-productiecontroles, niet naar onafhankelijk deskundigenonderzoek.\n\n"
        + AI_DISCLOSURE_NL
        + "\n"
        "\\tableofcontents\n"
    )
    tex = (PANDOC_PREAMBLE + "\n" + front + "\n".join(fragments) + "\n\\end{document}\n").encode("utf-8")
    return DirectDocument(
        tex=tex,
        assets=assets,
        unit_input_sha256=hashes,
        metrics={
            "units": len(units),
            "proof_commands_linearized": proof_commands,
            "proof_math_wrappers_removed": proof_math_wrappers,
            "tableaux_linearized": tableaux,
            "derivations_linearized": derivations,
            "diagrams_converted_to_svg": diagrams,
            "align_environments_linearized": alignments,
            "special_math_macros_expanded": special_math,
            "math_hyperlinks_flattened": math_hyperlinks,
            "math_anchors_moved_outside": math_anchors,
            "reference_labels_in_scope": len(labels),
            "malformed_conditional_tail_repairs": reference_diagnostics.malformed_conditional_tail_repairs,
            "duplicate_label_definitions_renamed": reference_diagnostics.duplicate_label_definitions_renamed,
            "text_tokens_defined": len(tokens),
            "selector_missing_else_defaulted": selector_diagnostics.missing_else_defaulted,
        },
    )
