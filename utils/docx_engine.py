"""
utils/docx_engine.py

Template-faithful DOCX renderer for canonical LabelCopyData dictionaries.

The renderer opens the supplied DOCX bytes, deep-copies paragraphs 13..32 for
all tracks, and mutates text at run level. It never rebuilds the document from
scratch, so embedded fonts, the floating logo, margins, numbering definitions,
and existing run/paragraph formatting remain in the package.
"""

from __future__ import annotations

import io
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from typing import Any

from docx import Document
from docx.document import Document as _DocumentType
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from lxml import etree

from utils.label_copy_engine import ROLE_DEFINITIONS, resolve_canonical_roles


TRACK_BLOCK_START = 13
TRACK_BLOCK_END_EXCLUSIVE = 33
TRACK_BLOCK_SIZE = TRACK_BLOCK_END_EXCLUSIVE - TRACK_BLOCK_START
P_LINE_TEMPLATE_INDEX = 33
C_LINE_TEMPLATE_INDEX = 34
MINIMUM_TEMPLATE_PARAGRAPHS = 35
TRACK_DURATION_COLUMN = 97

PLACEHOLDER_TOKENS = ("Project Name", "DD/MM/YYYY", "00:00:00")

DOCX_FIXED_LABEL_ORDER = (
    "Composer(s)",
    "Author(s)",
    "Producer(s)",
    "Recording Engineer(s)",
    "Mixing Engineer(s)",
    "Mastering Engineer(s)",
    "Vocalist(s)",
    "Guitarist(s)",
)

# The typo is intentionally isolated here and nowhere in the canonical model.
DOCX_TEMPLATE_LABELS = {
    "Composer(s)": "Composer(s)",
    "Author(s)": "Author(s)",
    "Producer(s)": "Producer(s)",
    "Recording Engineer(s)": "Recording Engineer(s)",
    "Mixing Engineer(s)": "Mixing Engineer(s)",
    "Mastering Engineer(s)": "Mastering Engineer(s)",
    "Vocalist(s)": "Vocalist(s)",
    "Guitarist(s)": "Guirtarist(s)",
}


class DocxTemplateError(ValueError):
    """Raised when the private template no longer matches the expected layout."""


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _unique_texts(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _as_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _duration_from_legacy_text(value: Any) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    parts = text.split(":")
    if not all(part.isdigit() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    if len(numbers) == 2:
        minutes, seconds = numbers
        return ((minutes * 60) + seconds) * 1000
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        return ((hours * 3600) + (minutes * 60) + seconds) * 1000
    return None


def _duration_ms(entity: Mapping[str, Any]) -> int | None:
    for canonical_key in ("duration_ms", "total_duration_ms"):
        if entity.get(canonical_key) is not None:
            return max(_as_int(entity.get(canonical_key)), 0)
    for legacy_key in ("duration", "total_duration"):
        parsed = _duration_from_legacy_text(entity.get(legacy_key))
        if parsed is not None:
            return parsed
    return None


def format_duration_docx(duration_ms: Any) -> str:
    """Formats canonical milliseconds as ``HH:MM:SS`` for the DOCX."""
    if duration_ms is None:
        return ""
    total_seconds = max(_as_int(duration_ms), 0) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _run_format_signature(run) -> bytes:
    r_pr = run._r.rPr
    return etree.tostring(r_pr, with_tail=False) if r_pr is not None else b""


def _is_simple_text_run(run) -> bool:
    allowed_tags = {qn("w:rPr"), qn("w:t")}
    return all(child.tag in allowed_tags for child in run._r)


def _merge_adjacent_text_runs_with_same_format(paragraph: Paragraph) -> None:
    """Merges adjacent text-only runs that have identical ``w:rPr``."""
    changed = True
    while changed:
        changed = False
        runs = list(paragraph.runs)
        for left, right in zip(runs, runs[1:]):
            if left._r.getnext() is not right._r:
                continue
            if not (_is_simple_text_run(left) and _is_simple_text_run(right)):
                continue
            if _run_format_signature(left) != _run_format_signature(right):
                continue
            left.text = f"{left.text}{right.text}"
            right._r.getparent().remove(right._r)
            changed = True
            break


def _text_runs_with_offsets(paragraph: Paragraph):
    runs = list(paragraph.runs)
    offsets = []
    cursor = 0
    for run in runs:
        text = run.text or ""
        start = cursor
        cursor += len(text)
        offsets.append((run, start, cursor))
    return runs, offsets, cursor


def _replace_range_across_runs(
    paragraph: Paragraph,
    start: int,
    end: int,
    replacement: str,
) -> None:
    """Replaces a character range without assigning to ``Paragraph.text``."""
    runs, offsets, total_length = _text_runs_with_offsets(paragraph)
    if start < 0 or end < start or end > total_length:
        raise DocxTemplateError("Invalid run-level replacement range.")

    if not runs:
        paragraph.add_run(replacement)
        return

    if start == end == total_length:
        target = next((run for run in reversed(runs) if _is_simple_text_run(run)), runs[-1])
        target.text = f"{target.text}{replacement}"
        return

    first_index = None
    last_index = None
    for index, (_, run_start, run_end) in enumerate(offsets):
        if first_index is None and run_start <= start < run_end:
            first_index = index
        if run_start < end <= run_end:
            last_index = index
            break

    # Empty text runs can make an insertion point fall exactly on a boundary.
    if first_index is None:
        for index, (_, run_start, _) in enumerate(offsets):
            if run_start == start:
                first_index = index
                break
    if last_index is None and end == total_length:
        last_index = len(offsets) - 1

    if first_index is None or last_index is None:
        raise DocxTemplateError("Could not map template text to its runs.")

    first_run, first_start, _ = offsets[first_index]
    last_run, last_start, _ = offsets[last_index]
    first_text = first_run.text or ""
    last_text = last_run.text or ""
    prefix = first_text[: max(start - first_start, 0)]
    suffix = last_text[max(end - last_start, 0) :]

    if first_index == last_index:
        first_run.text = f"{prefix}{replacement}{suffix}"
        return

    first_run.text = f"{prefix}{replacement}"
    for index in range(first_index + 1, last_index):
        offsets[index][0].text = ""
    last_run.text = suffix


def _replace_first_across_runs(
    paragraph: Paragraph,
    old: str,
    new: str,
    *,
    required: bool = True,
) -> bool:
    full_text = "".join(run.text or "" for run in paragraph.runs)
    start = full_text.find(old)
    if start < 0:
        if required:
            raise DocxTemplateError(
                f"The template paragraph does not contain the expected token: {old!r}"
            )
        return False
    _replace_range_across_runs(paragraph, start, start + len(old), new)
    return True


def _set_label_value(paragraph: Paragraph, label: str, value: Any) -> None:
    """Keeps the label runs and replaces only the text after the label."""
    _merge_adjacent_text_runs_with_same_format(paragraph)
    full_text = "".join(run.text or "" for run in paragraph.runs)
    label_start = full_text.find(label)
    if label_start < 0:
        raise DocxTemplateError(
            f"The template paragraph does not contain the expected label: {label!r}"
        )

    label_end = label_start + len(label)
    clean_value = str(value or "").strip()
    replacement = f" {clean_value}" if clean_value else " "
    _replace_range_across_runs(paragraph, label_end, len(full_text), replacement)


def _format_rights_value(line: Any, fallback_owner: str = "") -> str:
    if not isinstance(line, Mapping):
        return ""
    year = _as_int(line.get("year"), 0)
    owner = _clean_text(line.get("owner")) or _clean_text(fallback_owner)
    parts = [str(year)] if year else []
    if owner:
        parts.append(owner)
    return " ".join(parts)


def _validate_template(document: _DocumentType) -> None:
    paragraphs = document.paragraphs
    if len(paragraphs) < MINIMUM_TEMPLATE_PARAGRAPHS:
        raise DocxTemplateError(
            "Το Label Copy template έχει λιγότερες παραγράφους από την αναμενόμενη δομή."
        )

    expected_tokens = {
        0: ("Project Name", "DD/MM/YYYY"),
        2: ("Artist(s):",),
        3: ("Product Type:",),
        4: ("UPC:",),
        5: ("Release Date:",),
        6: ("Label Imprint:",),
        11: ("Total Duration:",),
        13: ("Track 1", "Duration:"),
        14: ("Primary Artist(s):",),
        15: ("Featured Artist(s):",),
        16: ("ISRC:",),
        21: ("Written By",),
        22: ("Composer(s):",),
        23: ("Author(s):",),
        25: ("Producer(s):",),
        31: ("Guirtarist(s):",),
        32: ("Other Credits:",),
        33: ("(P)",),
        34: ("(C)",),
    }
    for index, tokens in expected_tokens.items():
        text = paragraphs[index].text
        for token in tokens:
            if token not in text:
                raise DocxTemplateError(
                    f"Το Label Copy template άλλαξε: λείπει το {token!r} από την παράγραφο {index}."
                )


def _remove_clone_identity_attributes(element) -> None:
    # Deep-copying keeps Word's collaboration IDs. Removing them avoids duplicate
    # paraId/textId values while preserving all formatting and numbering data.
    for attribute in (qn("w14:paraId"), qn("w14:textId")):
        element.attrib.pop(attribute, None)


def _clone_track_blocks(
    document: _DocumentType,
    track_count: int,
) -> list[list[Paragraph]]:
    if track_count < 1:
        raise ValueError("Απαιτείται τουλάχιστον ένα track για δημιουργία Label Copy.")

    original_paragraphs = document.paragraphs
    original_nodes = [
        paragraph._p
        for paragraph in original_paragraphs[TRACK_BLOCK_START:TRACK_BLOCK_END_EXCLUSIVE]
    ]
    if len(original_nodes) != TRACK_BLOCK_SIZE:
        raise DocxTemplateError("Δεν εντοπίστηκε ο πλήρης επαναλαμβανόμενος track block.")

    body = document.element.body
    insertion_index = body.index(original_nodes[0])
    blocks: list[list[Paragraph]] = []

    for _ in range(track_count):
        block: list[Paragraph] = []
        for original_node in original_nodes:
            clone = deepcopy(original_node)
            _remove_clone_identity_attributes(clone)
            body.insert(insertion_index, clone)
            insertion_index += 1
            block.append(Paragraph(clone, document._body))
        blocks.append(block)

    for original_node in original_nodes:
        body.remove(original_node)

    # The body-level sectPr must remain the final body child.
    section_properties = body.sectPr
    if section_properties is not None and body[-1] is not section_properties:
        body.remove(section_properties)
        body.append(section_properties)

    return blocks


def _set_track_header(
    paragraph: Paragraph,
    *,
    display_number: int,
    title: str,
    duration_ms: int | None,
) -> None:
    original_text = paragraph.text
    duration_column = original_text.find("Duration:")
    if duration_column < 0:
        duration_column = TRACK_DURATION_COLUMN

    _merge_adjacent_text_runs_with_same_format(paragraph)
    text_runs = [run for run in paragraph.runs if _is_simple_text_run(run)]
    if not text_runs:
        raise DocxTemplateError("Το track header δεν περιέχει επεξεργάσιμα text runs.")

    left_text = f"Track {display_number}"
    if _clean_text(title):
        left_text += f": {_clean_text(title)}"

    duration_text = format_duration_docx(duration_ms)
    # A non-breaking space prevents Word/LibreOffice from separating the label
    # from the duration value when the title is close to the right column.
    duration_label = f"Duration:\u00a0{duration_text}" if duration_text else "Duration:"

    # The template uses ordinary spaces rather than a tab stop. Proportional-font
    # letters are wider than spaces, so a one-character-for-one-space replacement
    # is not sufficient when the real title replaces "Track 1". Compensate for
    # the added non-space glyphs while preserving the template's padding strategy.
    list_number_adjustment = max(len(str(display_number)) - 1, 0)
    placeholder_left = "Track 1"
    added_segment = left_text[len(placeholder_left):] if left_text.startswith("Track ") else left_text
    proportional_font_compensation = sum(
        1 for character in added_segment if not character.isspace()
    )
    padding = max(
        2,
        duration_column
        - len(left_text)
        - list_number_adjustment
        - proportional_font_compensation,
    )
    right_text = f"{' ' * padding}{duration_label}"

    if len(text_runs) == 1:
        text_runs[0].text = f"{left_text}{right_text}"
        return

    text_runs[0].text = left_text
    for run in text_runs[1:-1]:
        run.text = ""
    text_runs[-1].text = right_text


def _credit_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_clean_text(value)] if _clean_text(value) else []
    if isinstance(value, Mapping):
        if "names" in value:
            return _credit_names(value.get("names"))
        name = value.get("name")
        return [_clean_text(name)] if _clean_text(name) else []
    if isinstance(value, Iterable):
        names: list[str] = []
        for item in value:
            names.extend(_credit_names(item))
        return _unique_texts(names)
    return [_clean_text(value)] if _clean_text(value) else []


def _canonical_credits_for_render(
    track: Mapping[str, Any],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    raw_credits = track.get("credits")
    raw_labels = track.get("credit_labels")
    if not isinstance(raw_credits, Mapping):
        return {}, {}

    credits: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for raw_role, raw_names in raw_credits.items():
        names = _credit_names(raw_names)
        if not names:
            continue

        role_text = str(raw_role)
        if role_text in ROLE_DEFINITIONS or role_text.startswith("other:"):
            resolved = [
                (
                    role_text,
                    _clean_text(raw_labels.get(role_text))
                    if isinstance(raw_labels, Mapping)
                    else "",
                )
            ]
        else:
            resolved = resolve_canonical_roles(role_text)

        for role_id, display_label in resolved:
            if role_id in ROLE_DEFINITIONS and not display_label:
                display_label = ROLE_DEFINITIONS[role_id]["display_label"]
            if role_id.startswith("other:") and not display_label:
                display_label = role_text
            credits[role_id] = _unique_texts([*credits.get(role_id, []), *names])
            labels.setdefault(role_id, display_label or role_text)

    ordered: dict[str, list[str]] = OrderedDict()
    for role_id in ROLE_DEFINITIONS:
        if credits.get(role_id):
            ordered[role_id] = credits[role_id]
    for role_id, names in credits.items():
        if role_id not in ordered:
            ordered[role_id] = names
    return dict(ordered), labels


def _docx_credit_values(
    track: Mapping[str, Any],
) -> tuple[dict[str, list[str]], list[str]]:
    credits, labels = _canonical_credits_for_render(track)
    fixed: dict[str, list[str]] = {label: [] for label in DOCX_FIXED_LABEL_ORDER}
    other_lines: list[str] = []

    for role_id, names in credits.items():
        definition = ROLE_DEFINITIONS.get(role_id)
        docx_label = definition.get("docx_label") if definition else None
        if docx_label in fixed:
            fixed[docx_label] = _unique_texts([*fixed[docx_label], *names])
            continue

        display_label = labels.get(role_id)
        if not display_label and definition:
            display_label = definition.get("display_label") or definition.get("pdf_label")
        display_label = _clean_text(display_label) or role_id.removeprefix("other:").replace("_", " ").title()
        display_label = display_label.rstrip(":")
        other_lines.append(f"{display_label}: {', '.join(names)}")

    return fixed, other_lines


def _fill_release_header(
    release_paragraphs: Sequence[Paragraph],
    data: Mapping[str, Any],
) -> None:
    header = release_paragraphs[0]
    _merge_adjacent_text_runs_with_same_format(header)
    _replace_first_across_runs(header, "Project Name", _clean_text(data.get("project_name")))
    _replace_first_across_runs(header, "DD/MM/YYYY", _clean_text(data.get("issue_date")))

    _set_label_value(release_paragraphs[2], "Artist(s):", ", ".join(_unique_texts(data.get("artists", []))))
    _set_label_value(release_paragraphs[3], "Product Type:", data.get("product_type"))
    _set_label_value(release_paragraphs[4], "UPC:", data.get("upc"))
    _set_label_value(release_paragraphs[5], "Release Date:", data.get("release_date"))
    _set_label_value(release_paragraphs[6], "Label Imprint:", data.get("label_imprint"))
    _set_label_value(release_paragraphs[7], "Company:", data.get("company"))

    language = data.get("metadata_language")
    if language is None:
        language = data.get("metadata_language_suggestion")
    _set_label_value(release_paragraphs[8], "Metadata Language:", language)
    _set_label_value(release_paragraphs[9], "Genre:", data.get("genre"))
    _set_label_value(release_paragraphs[10], "Subgenre:", data.get("subgenre"))
    _set_label_value(
        release_paragraphs[11],
        "Total Duration:",
        format_duration_docx(_duration_ms(data)),
    )


def _fill_track_block(
    block: Sequence[Paragraph],
    track: Mapping[str, Any],
    display_number: int,
) -> None:
    if len(block) != TRACK_BLOCK_SIZE:
        raise DocxTemplateError("Το cloned track block έχει μη αναμενόμενο μέγεθος.")

    _set_track_header(
        block[0],
        display_number=display_number,
        title=_clean_text(track.get("title")),
        duration_ms=_duration_ms(track),
    )
    _set_label_value(
        block[1],
        "Primary Artist(s):",
        ", ".join(_unique_texts(track.get("primary_artists", []))),
    )
    _set_label_value(
        block[2],
        "Featured Artist(s):",
        ", ".join(_unique_texts(track.get("featured_artists", []))),
    )
    _set_label_value(block[3], "ISRC:", track.get("isrc"))
    _set_label_value(block[4], "Genre:", track.get("genre"))
    _set_label_value(block[5], "Subgenre:", track.get("subgenre"))

    lyrics_language = track.get("lyrics_language")
    if lyrics_language is None:
        lyrics_language = track.get("lyrics_language_suggestion")
    _set_label_value(block[6], "Lyrics Language:", lyrics_language)
    _set_label_value(block[7], "Parental Advisory:", track.get("parental_advisory"))

    fixed_credits, other_lines = _docx_credit_values(track)
    block_index_by_docx_label = {
        "Composer(s)": 9,
        "Author(s)": 10,
        "Producer(s)": 12,
        "Recording Engineer(s)": 13,
        "Mixing Engineer(s)": 14,
        "Mastering Engineer(s)": 15,
        "Vocalist(s)": 17,
        "Guitarist(s)": 18,
    }
    for canonical_label, block_index in block_index_by_docx_label.items():
        template_label = DOCX_TEMPLATE_LABELS[canonical_label]
        _set_label_value(
            block[block_index],
            f"{template_label}:",
            ", ".join(fixed_credits.get(canonical_label, [])),
        )

    _set_label_value(block[19], "Other Credits:", "\n".join(other_lines))


def generate_label_copy_docx(
    template_bytes: bytes,
    data: Mapping[str, Any],
) -> io.BytesIO:
    """
    Renders a Label Copy DOCX and returns a rewound ``BytesIO`` buffer.

    ``template_bytes`` must contain the original private ``Label_Copy.docx``.
    The function performs no network calls.
    """
    if not isinstance(template_bytes, (bytes, bytearray)) or not template_bytes:
        raise ValueError("Το Label Copy template είναι κενό ή μη έγκυρο.")
    if not isinstance(data, Mapping):
        raise TypeError("data must be a LabelCopyData mapping")

    try:
        document = Document(io.BytesIO(bytes(template_bytes)))
    except Exception as exc:
        raise DocxTemplateError("Αδυναμία ανοίγματος του Label Copy DOCX template.") from exc

    _validate_template(document)
    original_paragraphs = list(document.paragraphs)
    release_paragraphs = original_paragraphs[:TRACK_BLOCK_START]
    p_line_paragraph = original_paragraphs[P_LINE_TEMPLATE_INDEX]
    c_line_paragraph = original_paragraphs[C_LINE_TEMPLATE_INDEX]

    tracks = data.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("Το LabelCopyData δεν περιέχει tracks.")

    track_blocks = _clone_track_blocks(document, len(tracks))
    _fill_release_header(release_paragraphs, data)

    for display_number, (block, track) in enumerate(zip(track_blocks, tracks), start=1):
        if not isinstance(track, Mapping):
            raise TypeError(f"Το track {display_number} δεν είναι έγκυρο mapping.")
        _fill_track_block(block, track, display_number)

    _set_label_value(
        p_line_paragraph,
        "(P)",
        _format_rights_value(data.get("p_line"), data.get("label_imprint", "")),
    )
    _set_label_value(
        c_line_paragraph,
        "(C)",
        _format_rights_value(data.get("c_line"), data.get("label_imprint", "")),
    )

    output = io.BytesIO()
    document.save(output)
    output.seek(0)

    # Structural round-trip: catches malformed OOXML before it reaches the UI.
    try:
        Document(io.BytesIO(output.getvalue()))
    except Exception as exc:
        raise DocxTemplateError("Το παραγόμενο DOCX απέτυχε στον έλεγχο round-trip.") from exc

    return output


def make_label_copy_filename(
    project_name: Any,
    *,
    extension: str = "docx",
    issue_date: Any = None,
) -> str:
    """Builds ``LabelCopy_<ASCII title>_<YYYYMMDD>.<extension>``."""
    normalized = unicodedata.normalize("NFKD", _clean_text(project_name))
    ascii_title = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_title = re.sub(r"[^A-Za-z0-9]+", "_", ascii_title).strip("_") or "Release"

    date_token = ""
    issue_text = _clean_text(issue_date)
    for date_format in ("%d/%m/%Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            date_token = datetime.strptime(issue_text, date_format).strftime("%Y%m%d")
            break
        except ValueError:
            continue
    if not date_token:
        date_token = datetime.now().strftime("%Y%m%d")

    clean_extension = re.sub(r"[^A-Za-z0-9]", "", str(extension or "docx")).lower() or "docx"
    return f"LabelCopy_{ascii_title}_{date_token}.{clean_extension}"


# Readable alias for callers that prefer the "render" verb.
render_label_copy_docx = generate_label_copy_docx


__all__ = [
    "DocxTemplateError",
    "format_duration_docx",
    "generate_label_copy_docx",
    "make_label_copy_filename",
    "render_label_copy_docx",
]
