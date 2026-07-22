"""
utils/label_copy_engine.py

Canonical Label Copy data builder for the Stay Independent Tool.

The module contains no direct HTTP implementation. Spotify release/track
payloads are supplied by the caller, while iTunes and MusicBrainz enrichment
is performed only through injected fetchers. This keeps
``build_label_copy_data`` deterministic, testable, and independent from the
Streamlit UI.

Approved provider policy:
- Spotify: release identity, track list, durations, explicit flags and IDs.
- iTunes Search/Lookup: default genre enrichment and release cross-checks.
- MusicBrainz: release fallback and artist/work relationship credits.
- TIDAL: deliberately not used.
- MusicKit / paid Apple Music API: deliberately not implemented.
"""

from __future__ import annotations

import difflib
import inspect
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


ATHENS_TIMEZONE = ZoneInfo("Europe/Athens")
DEFAULT_COMPANY = "Stay Independent"
DEFAULT_PUBLISHER = "Stay Independent"
DEFAULT_LANGUAGE_SUGGESTION = "Greek (GR)"
DEFAULT_AUDIO_CHANNEL = "Stereo"
DEFAULT_RESOURCE_TYPE = "Audio"
VALID_PRODUCT_TYPES = ("Album", "Single", "EP", "Compilation")
VALID_DATE_PRECISIONS = ("day", "month", "year", "unknown")

ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$", re.IGNORECASE)
FEATURED_TITLE_RE = re.compile(
    r"(?i)\s*(?:[\[(]\s*)?\b(?:feat(?:uring)?\.?|ft\.?|with)\s+"
    r"(?P<artists>.+?)(?:\s*[\])])?\s*$"
)
COPYRIGHT_PREFIX_RE = re.compile(
    r"^\s*(?:℗|©|\(\s*[PC]\s*\)|[PC]\s*[:\-])?\s*",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2}|21\d{2})\b")


# --------------------------------------------------------------------------
# Canonical role model
# --------------------------------------------------------------------------
# Canonical role IDs are stable machine keys. The central map uses the correct
# spelling "Guitarist(s)". The legacy template typo "Guirtarist(s)" exists only
# in utils/docx_engine.py, as explicitly approved.
ROLE_DEFINITIONS: "OrderedDict[str, dict[str, Any]]" = OrderedDict(
    [
        (
            "composer",
            {
                "aliases": (
                    "composer",
                    "composition",
                    "composed by",
                    "music",
                    "music by",
                ),
                "docx_label": "Composer(s)",
                "pdf_label": "Music by",
                "display_label": "Composer",
            },
        ),
        (
            "lyricist",
            {
                "aliases": (
                    "lyricist",
                    "lyrics",
                    "lyrics by",
                    "words",
                    "words by",
                ),
                "docx_label": "Author(s)",
                "pdf_label": "Lyrics by",
                "display_label": "Lyricist",
            },
        ),
        (
            "writer",
            {
                "aliases": (
                    "writer",
                    "author",
                    "songwriter",
                    "written by",
                    "songwriting",
                ),
                "docx_label": "Author(s)",
                "pdf_label": "Written by",
                "display_label": "Writer",
            },
        ),
        (
            "lyrics_adaptation",
            {
                "aliases": (
                    "lyrics adaptation",
                    "lyric adaptation",
                    "adapted lyrics",
                    "adaptation",
                ),
                "docx_label": "Author(s)",
                "pdf_label": "Lyrics Adaptation",
                "display_label": "Lyrics Adaptation",
            },
        ),
        (
            "translator",
            {
                "aliases": ("translator", "translated by", "translation"),
                "docx_label": "Author(s)",
                "pdf_label": "Translated by",
                "display_label": "Translator",
            },
        ),
        (
            "producer",
            {
                "aliases": (
                    "producer",
                    "produced by",
                    "recording producer",
                    "production",
                ),
                "docx_label": "Producer(s)",
                "pdf_label": "Produced by",
                "display_label": "Producer",
            },
        ),
        (
            "co_producer",
            {
                "aliases": ("co producer", "coproducer", "co-produced by"),
                "docx_label": "Producer(s)",
                "pdf_label": "Co-Produced by",
                "display_label": "Co-Producer",
            },
        ),
        (
            "executive_producer",
            {
                "aliases": (
                    "executive producer",
                    "exec producer",
                    "exec. producer",
                ),
                "docx_label": "Producer(s)",
                "pdf_label": "Executive Producer",
                "display_label": "Executive Producer",
            },
        ),
        (
            "recording_engineer",
            {
                "aliases": (
                    "recording engineer",
                    "recorded by",
                    "recording",
                    "sound engineer",
                    "engineer",
                ),
                "docx_label": "Recording Engineer(s)",
                "pdf_label": "Recording Engineer",
                "display_label": "Recording Engineer",
            },
        ),
        (
            "mixing_engineer",
            {
                "aliases": (
                    "mixer",
                    "mixing engineer",
                    "mix engineer",
                    "mixed by",
                    "mixing",
                    "mix",
                ),
                "docx_label": "Mixing Engineer(s)",
                "pdf_label": "Mixed by",
                "display_label": "Mixing Engineer",
            },
        ),
        (
            "mastering_engineer",
            {
                "aliases": (
                    "mastering engineer",
                    "masterer",
                    "mastered by",
                    "mastering",
                ),
                "docx_label": "Mastering Engineer(s)",
                "pdf_label": "Mastering Engineer",
                "display_label": "Mastering Engineer",
            },
        ),
        (
            "vocalist",
            {
                "aliases": (
                    "vocalist",
                    "vocals",
                    "vocal",
                    "lead vocals",
                    "lead vocal",
                    "background vocals",
                    "backing vocals",
                    "voice",
                ),
                "docx_label": "Vocalist(s)",
                "pdf_label": "Vocals",
                "display_label": "Vocals",
            },
        ),
        (
            "guitarist",
            {
                "aliases": (
                    "guitarist",
                    "guitar",
                    "guitars",
                    "electric guitar",
                    "acoustic guitar",
                    "bass guitar",
                    "classical guitar",
                ),
                "docx_label": "Guitarist(s)",
                "pdf_label": "Guitars",
                "display_label": "Guitars",
            },
        ),
        (
            "arranger",
            {
                "aliases": (
                    "arranger",
                    "arranged by",
                    "arrangement",
                    "orchestrator",
                    "orchestration",
                ),
                "docx_label": None,
                "pdf_label": "Arranged by",
                "display_label": "Arranged by",
            },
        ),
        (
            "drummer",
            {
                "aliases": ("drummer", "drums", "percussion", "percussionist"),
                "docx_label": None,
                "pdf_label": "Drums",
                "display_label": "Drums",
            },
        ),
        (
            "bassist",
            {
                "aliases": ("bassist", "bass", "bass player", "double bass"),
                "docx_label": None,
                "pdf_label": "Bass",
                "display_label": "Bass",
            },
        ),
        (
            "keyboardist",
            {
                "aliases": (
                    "keyboardist",
                    "keyboard",
                    "keyboards",
                    "piano",
                    "pianist",
                    "synth",
                    "synths",
                    "synthesizer",
                    "organ",
                ),
                "docx_label": None,
                "pdf_label": "Keyboards / Synths",
                "display_label": "Keyboards / Synths",
            },
        ),
        (
            "programmer",
            {
                "aliases": ("programmer", "programming", "music programming"),
                "docx_label": None,
                "pdf_label": "Programming",
                "display_label": "Programming",
            },
        ),
        (
            "performer",
            {
                "aliases": ("performer", "performance", "instrument", "musician"),
                "docx_label": None,
                "pdf_label": "Performed by",
                "display_label": "Performed by",
            },
        ),
        (
            "conductor",
            {
                "aliases": ("conductor", "conducted by"),
                "docx_label": None,
                "pdf_label": "Conducted by",
                "display_label": "Conducted by",
            },
        ),
        (
            "remixer",
            {
                "aliases": ("remixer", "remixed by", "remix"),
                "docx_label": None,
                "pdf_label": "Remixed by",
                "display_label": "Remixed by",
            },
        ),
        (
            "recitation",
            {
                "aliases": ("recitation", "spoken word", "spoken vocals"),
                "docx_label": None,
                "pdf_label": "Recitation",
                "display_label": "Recitation",
            },
        ),
        (
            "documentary_excerpt",
            {
                "aliases": ("documentary excerpt", "documentary sample"),
                "docx_label": None,
                "pdf_label": "Documentary Excerpt",
                "display_label": "Documentary Excerpt",
            },
        ),
        (
            "poetry_excerpt",
            {
                "aliases": ("poetry excerpt", "poem excerpt"),
                "docx_label": None,
                "pdf_label": "Poetry Excerpt",
                "display_label": "Poetry Excerpt",
            },
        ),
        (
            "publisher",
            {
                "aliases": ("publisher", "published by", "music publisher"),
                "docx_label": None,
                "pdf_label": "Published by",
                "display_label": "Published by",
            },
        ),
    ]
)

COMPOUND_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "produced and mixed by": ("producer", "mixing_engineer"),
    "producer and mixer": ("producer", "mixing_engineer"),
    "produced mixed by": ("producer", "mixing_engineer"),
    "arranged and produced": ("arranger", "producer"),
    "arranged and produced by": ("arranger", "producer"),
    "arranger and producer": ("arranger", "producer"),
    "drums bass synths": ("drummer", "bassist", "keyboardist"),
    "drums bass and synths": ("drummer", "bassist", "keyboardist"),
}


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _comparison_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _clean_text(value)).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _slug(value: Any) -> str:
    key = _comparison_key(value).replace(" ", "_")
    return key or "credit"


ROLE_ALIAS_INDEX: dict[str, str] = {}
for _role_id, _definition in ROLE_DEFINITIONS.items():
    ROLE_ALIAS_INDEX[_comparison_key(_role_id)] = _role_id
    ROLE_ALIAS_INDEX[_comparison_key(_definition["display_label"])] = _role_id
    ROLE_ALIAS_INDEX[_comparison_key(_definition["pdf_label"])] = _role_id
    docx_label = _definition.get("docx_label")
    if docx_label:
        ROLE_ALIAS_INDEX[_comparison_key(docx_label)] = _role_id
    for _alias in _definition.get("aliases", ()):
        ROLE_ALIAS_INDEX[_comparison_key(_alias)] = _role_id


# --------------------------------------------------------------------------
# Generic data helpers
# --------------------------------------------------------------------------
def _as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_nonnegative_int(value: Any, default: int = 0) -> int:
    parsed = _as_int(value)
    return max(parsed, 0) if parsed is not None else default


def _unique_texts(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        key = _comparison_key(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _merge_name_lists(*groups: Iterable[Any]) -> list[str]:
    combined: list[Any] = []
    for group in groups:
        combined.extend(group)
    return _unique_texts(combined)


def _append_warning(warnings: list[str], message: Any) -> None:
    text = _clean_text(message)
    if text and text not in warnings:
        warnings.append(text)


def _source_info(
    source: str,
    confidence: str,
    *,
    confirmed: bool = False,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "source": source,
        "confidence": confidence,
        "confirmed": bool(confirmed),
        "detail": _clean_text(detail),
    }


def _unwrap_fetcher_result(result: Any) -> tuple[Any, str | None]:
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], _clean_text(result[1]) or None
    return result, None


def _invoke_callable(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> tuple[Any, str | None]:
    """Invokes an injected fetcher without assuming it accepts every keyword."""
    try:
        try:
            signature = inspect.signature(func)
            accepts_var_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            try:
                bound_names = set(signature.bind_partial(*args).arguments)
            except TypeError:
                bound_names = set()
            if accepts_var_kwargs:
                filtered_kwargs = {
                    key: value for key, value in kwargs.items() if key not in bound_names
                }
            else:
                filtered_kwargs = {
                    key: value
                    for key, value in kwargs.items()
                    if key in signature.parameters and key not in bound_names
                }
        except (TypeError, ValueError):
            filtered_kwargs = kwargs

        result = func(*args, **filtered_kwargs)
        return _unwrap_fetcher_result(result)
    except Exception as exc:
        return None, str(exc)


def _similarity(left: Any, right: Any) -> float:
    left_key = _comparison_key(left)
    right_key = _comparison_key(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    return difflib.SequenceMatcher(None, left_key, right_key).ratio()


def normalize_isrc(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def validate_isrc(value: Any) -> bool:
    return bool(ISRC_RE.fullmatch(normalize_isrc(value)))


# --------------------------------------------------------------------------
# Role normalization and MusicBrainz relationship parsing
# --------------------------------------------------------------------------
def _role_heuristic(role_key: str) -> str | None:
    if not role_key:
        return None
    if "composer" in role_key or role_key == "music":
        return "composer"
    if "lyric" in role_key and "adapt" in role_key:
        return "lyrics_adaptation"
    if "lyric" in role_key or role_key.startswith("words"):
        return "lyricist"
    if "songwriter" in role_key or "writer" in role_key or "author" in role_key:
        return "writer"
    if "executive" in role_key and "producer" in role_key:
        return "executive_producer"
    if ("co " in f"{role_key} " or role_key.startswith("coproducer")) and "producer" in role_key:
        return "co_producer"
    if "producer" in role_key or "production" in role_key:
        return "producer"
    if "master" in role_key:
        return "mastering_engineer"
    if "mix" in role_key:
        return "mixing_engineer"
    if "record" in role_key and "engineer" in role_key:
        return "recording_engineer"
    if role_key == "engineer" or "sound engineer" in role_key:
        return "recording_engineer"
    if "vocal" in role_key or role_key == "voice":
        return "vocalist"
    if "guitar" in role_key:
        return "guitarist"
    if "arrang" in role_key or "orchestrat" in role_key:
        return "arranger"
    if "drum" in role_key or "percussion" in role_key:
        return "drummer"
    if role_key == "bass" or "double bass" in role_key or "bassist" in role_key:
        return "bassist"
    if any(word in role_key for word in ("keyboard", "piano", "synth", "organ")):
        return "keyboardist"
    if "program" in role_key:
        return "programmer"
    if "conduct" in role_key:
        return "conductor"
    if "remix" in role_key:
        return "remixer"
    if "recitation" in role_key or "spoken word" in role_key:
        return "recitation"
    if "documentary" in role_key and ("excerpt" in role_key or "sample" in role_key):
        return "documentary_excerpt"
    if ("poetry" in role_key or "poem" in role_key) and "excerpt" in role_key:
        return "poetry_excerpt"
    if "publisher" in role_key or "published by" in role_key:
        return "publisher"
    if "translat" in role_key:
        return "translator"
    if role_key in {"performer", "performance", "instrument", "musician"}:
        return "performer"
    return None


def resolve_canonical_roles(
    raw_role: Any,
    attributes: Iterable[Any] | None = None,
) -> list[tuple[str, str]]:
    """
    Maps one raw role (plus optional MusicBrainz attributes) to role IDs.

    Returns ``[(role_id, display_label), ...]``. Unknown roles are preserved as
    dynamic ``other:<slug>`` IDs and retain their raw label for rendering.
    """
    raw_label = _clean_text(raw_role) or "Other Credit"
    role_key = _comparison_key(raw_label)
    attribute_labels = _unique_texts(attributes or [])

    # MusicBrainz often uses a generic relation type and stores the actual
    # instrument/vocal role in its attributes.
    if role_key in {"instrument", "performer", "performance", "vocal"}:
        mapped_from_attributes: list[tuple[str, str]] = []
        for attribute in attribute_labels:
            attr_key = _comparison_key(attribute)
            role_id = ROLE_ALIAS_INDEX.get(attr_key) or _role_heuristic(attr_key)
            if role_id:
                mapped_from_attributes.append(
                    (role_id, ROLE_DEFINITIONS[role_id]["display_label"])
                )
        if mapped_from_attributes:
            deduped: list[tuple[str, str]] = []
            seen = set()
            for pair in mapped_from_attributes:
                if pair[0] not in seen:
                    seen.add(pair[0])
                    deduped.append(pair)
            return deduped

    if role_key in COMPOUND_ROLE_ALIASES:
        return [
            (role_id, ROLE_DEFINITIONS[role_id]["display_label"])
            for role_id in COMPOUND_ROLE_ALIASES[role_key]
        ]

    exact = ROLE_ALIAS_INDEX.get(role_key)
    if exact:
        return [(exact, ROLE_DEFINITIONS[exact]["display_label"])]

    heuristic = _role_heuristic(role_key)
    if heuristic:
        return [(heuristic, ROLE_DEFINITIONS[heuristic]["display_label"])]

    # Try combined role strings only after an exact/heuristic match of the full
    # phrase, so "bass guitar" is not incorrectly split into two roles.
    parts = [
        _clean_text(part)
        for part in re.split(r"\s*(?:/|&|\+|;|,|\band\b)\s*", raw_label, flags=re.I)
        if _clean_text(part)
    ]
    if len(parts) > 1:
        resolved: list[tuple[str, str]] = []
        seen = set()
        for part in parts:
            part_key = _comparison_key(part)
            role_id = ROLE_ALIAS_INDEX.get(part_key) or _role_heuristic(part_key)
            if role_id and role_id not in seen:
                seen.add(role_id)
                resolved.append((role_id, ROLE_DEFINITIONS[role_id]["display_label"]))
        if resolved:
            return resolved

    dynamic_id = f"other:{_slug(raw_label)}"
    return [(dynamic_id, raw_label)]


def _names_from_credit_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # Preserve parenthetical annotations and avoid splitting on ampersands;
        # API adapters should normally supply a list when multiple names exist.
        return [_clean_text(value)] if _clean_text(value) else []
    if isinstance(value, Mapping):
        if "names" in value:
            return _names_from_credit_value(value.get("names"))
        name = value.get("name") or value.get("artist_name") or value.get("artist")
        if isinstance(name, Mapping):
            name = name.get("name")
        return [_clean_text(name)] if _clean_text(name) else []
    if isinstance(value, Iterable):
        names: list[str] = []
        for item in value:
            names.extend(_names_from_credit_value(item))
        return _unique_texts(names)
    return [_clean_text(value)] if _clean_text(value) else []


def normalize_credit_map(
    raw_credits: Mapping[str, Any] | None,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Normalizes a raw ``role -> names`` mapping to canonical role IDs."""
    if not isinstance(raw_credits, Mapping):
        return {}, {}

    credits: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for raw_role, raw_names in raw_credits.items():
        names = _names_from_credit_value(raw_names)
        if not names:
            continue

        role_text = str(raw_role)
        if role_text in ROLE_DEFINITIONS:
            resolved_roles = [
                (role_text, ROLE_DEFINITIONS[role_text]["display_label"])
            ]
        elif role_text.startswith("other:"):
            resolved_roles = [
                (role_text, role_text.removeprefix("other:").replace("_", " ").title())
            ]
        else:
            resolved_roles = resolve_canonical_roles(raw_role)

        for role_id, display_label in resolved_roles:
            credits[role_id] = _merge_name_lists(credits.get(role_id, []), names)
            labels.setdefault(role_id, display_label)

    ordered: dict[str, list[str]] = {}
    ordered_labels: dict[str, str] = {}
    for role_id in ROLE_DEFINITIONS:
        if credits.get(role_id):
            ordered[role_id] = credits[role_id]
            ordered_labels[role_id] = labels.get(
                role_id, ROLE_DEFINITIONS[role_id]["display_label"]
            )
    for role_id, names in credits.items():
        if role_id not in ordered:
            ordered[role_id] = names
            ordered_labels[role_id] = labels.get(role_id, role_id.removeprefix("other:"))
    return ordered, ordered_labels


def merge_credit_maps(
    *credit_sets: tuple[Mapping[str, Iterable[Any]], Mapping[str, str]],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    credits: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for credit_map, label_map in credit_sets:
        for role_id, names in credit_map.items():
            credits[role_id] = _merge_name_lists(credits.get(role_id, []), names)
            if role_id in label_map:
                labels.setdefault(role_id, label_map[role_id])

    ordered: dict[str, list[str]] = {}
    ordered_labels: dict[str, str] = {}
    for role_id in ROLE_DEFINITIONS:
        if credits.get(role_id):
            ordered[role_id] = credits[role_id]
            ordered_labels[role_id] = labels.get(
                role_id, ROLE_DEFINITIONS[role_id]["display_label"]
            )
    for role_id, names in credits.items():
        if role_id not in ordered:
            ordered[role_id] = names
            ordered_labels[role_id] = labels.get(role_id, role_id.removeprefix("other:"))
    return ordered, ordered_labels


def _relation_attributes(relation: Mapping[str, Any]) -> list[str]:
    raw = relation.get("attributes") or relation.get("attribute-list") or []
    if isinstance(raw, str):
        return [_clean_text(raw)] if _clean_text(raw) else []
    if isinstance(raw, Mapping):
        raw = raw.values()
    if not isinstance(raw, Iterable):
        return []

    output: list[str] = []
    for item in raw:
        if isinstance(item, Mapping):
            text = item.get("name") or item.get("value") or item.get("type")
        else:
            text = item
        if _clean_text(text):
            output.append(_clean_text(text))
    return _unique_texts(output)


def _relation_target_name(relation: Mapping[str, Any]) -> str:
    for key in ("artist", "label", "target", "person"):
        target = relation.get(key)
        if isinstance(target, Mapping):
            name = (
                target.get("name")
                or target.get("artist-credit-phrase")
                or target.get("sort-name")
            )
            if _clean_text(name):
                return _clean_text(name)
        elif _clean_text(target):
            return _clean_text(target)

    for key in ("credited-as", "target-credit", "name"):
        if _clean_text(relation.get(key)):
            return _clean_text(relation.get(key))
    return ""


def _relation_lists(node: Mapping[str, Any]) -> Iterable[Sequence[Any]]:
    for key in (
        "artist-relation-list",
        "artist_relations",
        "artist-relations",
        "label-relation-list",
        "label_relations",
        "label-relations",
        "work-relation-list",
        "work_relations",
        "work-relations",
        "relations",
        "relation-list",
    ):
        value = node.get(key)
        if isinstance(value, list):
            yield value


def extract_musicbrainz_credits(
    payload: Any,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """
    Extracts artist/work relationship credits from musicbrainzngs-style data.

    The parser accepts either a pre-normalized ``role -> names`` mapping or raw
    recording/work payloads containing ``artist-relation-list`` and nested
    ``work`` objects.
    """
    if payload is None:
        return {}, {}

    if isinstance(payload, Mapping) and isinstance(payload.get("credits"), Mapping):
        normalized, labels = normalize_credit_map(payload.get("credits"))
        supplied_labels = payload.get("credit_labels")
        if isinstance(supplied_labels, Mapping):
            for role_id, label in supplied_labels.items():
                if role_id in normalized and _clean_text(label):
                    labels[role_id] = _clean_text(label)
        return normalized, labels

    entity_keys = {
        "recording",
        "work",
        "release",
        "recording-list",
        "work-list",
        "release-list",
        "artist-relation-list",
        "work-relation-list",
        "relations",
        "results",
    }
    if isinstance(payload, Mapping) and not (set(payload) & entity_keys):
        # A direct role map is the most useful interpretation when no known
        # MusicBrainz entity keys are present.
        direct, labels = normalize_credit_map(payload)
        if direct:
            return direct, labels

    raw_credits: dict[str, list[str]] = {}
    visited: set[int] = set()

    def add_raw(role: Any, name: Any, attributes: Iterable[Any] | None = None) -> None:
        clean_name = _clean_text(name)
        if not clean_name:
            return
        resolved = resolve_canonical_roles(role, attributes)
        for role_id, display_label in resolved:
            raw_credits.setdefault(display_label if role_id.startswith("other:") else role_id, [])
            key = display_label if role_id.startswith("other:") else role_id
            raw_credits[key] = _merge_name_lists(raw_credits[key], [clean_name])

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            marker = id(node)
            if marker in visited:
                return
            visited.add(marker)

            for relations in _relation_lists(node):
                for relation in relations:
                    if not isinstance(relation, Mapping):
                        continue
                    relation_type = relation.get("type") or relation.get("relation-type")
                    name = _relation_target_name(relation)
                    if name:
                        add_raw(relation_type, name, _relation_attributes(relation))

                    # Expanded relation targets can contain the Work's own
                    # composer/lyricist relationships.
                    for nested_key in ("work", "recording", "target"):
                        nested = relation.get(nested_key)
                        if isinstance(nested, (Mapping, list)):
                            visit(nested)

            for key, value in node.items():
                if key in {
                    "recording",
                    "work",
                    "work-list",
                    "works",
                    "recording-list",
                    "recordings",
                    "result",
                    "results",
                } and isinstance(value, (Mapping, list)):
                    visit(value)

        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)

    # raw_credits already uses canonical IDs for known roles and raw labels for
    # unknown roles. Feed it through normalize_credit_map once more to create a
    # uniformly ordered result and dynamic label map.
    return normalize_credit_map(raw_credits)


def extract_musicbrainz_work_ids(payload: Any) -> list[str]:
    work_ids: list[str] = []
    visited: set[int] = set()

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            marker = id(node)
            if marker in visited:
                return
            visited.add(marker)

            work = node.get("work")
            if isinstance(work, Mapping) and _clean_text(work.get("id")):
                work_ids.append(_clean_text(work.get("id")))
                visit(work)

            for key in ("work-relation-list", "work_relations", "work-relations"):
                relations = node.get(key)
                if isinstance(relations, list):
                    for relation in relations:
                        if isinstance(relation, Mapping):
                            visit(relation)

            for key in ("recording", "work-list", "works", "result", "results"):
                value = node.get(key)
                if isinstance(value, (Mapping, list)):
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    return _unique_texts(work_ids)


def _extract_artist_credit_names(entity: Mapping[str, Any]) -> list[str]:
    phrase = _clean_text(entity.get("artist-credit-phrase"))
    raw_credit = entity.get("artist-credit") or entity.get("artist_credit")
    names: list[str] = []
    if isinstance(raw_credit, list):
        for item in raw_credit:
            if isinstance(item, Mapping):
                artist = item.get("artist")
                if isinstance(artist, Mapping):
                    names.append(artist.get("name"))
                else:
                    names.append(item.get("name"))
    if names:
        return _unique_texts(names)
    return [phrase] if phrase else []


def _recording_candidates(payload: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    visited: set[int] = set()

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            marker = id(node)
            if marker in visited:
                return
            visited.add(marker)

            if _clean_text(node.get("id")) and (
                _clean_text(node.get("title")) or node.get("artist-credit")
            ):
                candidates.append(dict(node))

            for key in (
                "recording",
                "recording-list",
                "recordings",
                "isrc",
                "result",
                "results",
            ):
                value = node.get(key)
                if isinstance(value, (Mapping, list)):
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)

    unique: list[dict[str, Any]] = []
    seen = set()
    for candidate in candidates:
        mbid = _clean_text(candidate.get("id"))
        if mbid and mbid not in seen:
            seen.add(mbid)
            unique.append(candidate)
    return unique


def _select_recording_candidate(
    candidates: Sequence[Mapping[str, Any]],
    track: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if not candidates:
        return None
    if not track:
        return candidates[0]

    title = _clean_text(track.get("name") or track.get("title"))
    artists = _extract_artist_names(track)
    artist_phrase = ", ".join(artists)
    duration_ms = _as_int(track.get("duration_ms"))

    scored: list[tuple[float, Mapping[str, Any]]] = []
    for candidate in candidates:
        title_score = _similarity(candidate.get("title"), title)
        mb_artists = _extract_artist_credit_names(candidate)
        artist_score = _similarity(", ".join(mb_artists), artist_phrase) if artist_phrase else 0.5
        mb_length = _as_int(candidate.get("length"))
        if duration_ms is not None and mb_length is not None:
            duration_score = max(0.0, 1.0 - abs(duration_ms - mb_length) / 15_000.0)
        else:
            duration_score = 0.5
        score = title_score * 0.58 + artist_score * 0.32 + duration_score * 0.10
        scored.append((score, candidate))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def make_musicbrainz_credit_fetcher(
    *,
    find_recordings_by_isrc: Callable[..., Any],
    get_recording: Callable[..., Any],
    get_work: Callable[..., Any] | None = None,
) -> Callable[..., tuple[dict[str, Any] | None, str | None]]:
    """
    Builds an adapter around the repository's existing MusicBrainz wrappers.

    The adapter expects the wrappers to return musicbrainzngs-style dictionaries
    (optionally wrapped in ``(data, note)``). It requests ``artist-rels`` and
    ``work-rels`` on recordings, then ``artist-rels`` on referenced Works.
    No second MusicBrainz client is created.
    """

    def fetch(
        isrc: str,
        *,
        track: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        clean_isrc = normalize_isrc(isrc)
        if not validate_isrc(clean_isrc):
            return None, "Το ISRC δεν είναι έγκυρο για αναζήτηση MusicBrainz credits."

        search_payload, search_note = _invoke_callable(
            find_recordings_by_isrc,
            clean_isrc,
            isrc=clean_isrc,
        )
        if search_payload is None:
            return None, search_note or "Δεν βρέθηκε recording στο MusicBrainz για το ISRC."

        candidate = _select_recording_candidate(_recording_candidates(search_payload), track)
        if candidate is None:
            return None, "Δεν βρέθηκε recording στο MusicBrainz για το ISRC."

        recording_mbid = _clean_text(candidate.get("id"))
        recording_payload, recording_note = _invoke_callable(
            get_recording,
            recording_mbid,
            recording_id=recording_mbid,
            mbid=recording_mbid,
            includes=["artist-rels", "work-rels"],
        )
        if recording_payload is None:
            # Search results may already contain expanded relationships.
            recording_payload = candidate

        credit_sets = [extract_musicbrainz_credits(recording_payload)]
        work_mbids = extract_musicbrainz_work_ids(recording_payload)
        notes = [note for note in (search_note, recording_note) if note]

        if get_work is not None:
            for work_mbid in work_mbids[:10]:
                work_payload, work_note = _invoke_callable(
                    get_work,
                    work_mbid,
                    work_id=work_mbid,
                    mbid=work_mbid,
                    includes=["artist-rels"],
                )
                if work_payload is not None:
                    credit_sets.append(extract_musicbrainz_credits(work_payload))
                if work_note:
                    notes.append(work_note)

        credits, labels = merge_credit_maps(*credit_sets)
        if not credits:
            return None, notes[-1] if notes else "Δεν βρέθηκαν MusicBrainz credits."

        return {
            "credits": credits,
            "credit_labels": labels,
            "recording_mbid": recording_mbid,
            "work_mbids": work_mbids,
        }, notes[-1] if notes else None

    return fetch


# --------------------------------------------------------------------------
# Release and track normalization
# --------------------------------------------------------------------------
def _extract_artist_names(entity: Mapping[str, Any]) -> list[str]:
    raw_artists = entity.get("artists") or []
    if isinstance(raw_artists, Mapping):
        raw_artists = [raw_artists]
    names: list[str] = []
    if isinstance(raw_artists, Iterable) and not isinstance(raw_artists, (str, bytes)):
        for artist in raw_artists:
            if isinstance(artist, Mapping):
                names.append(artist.get("name"))
            else:
                names.append(artist)
    return _unique_texts(names)


def _unwrap_spotify_release(payload: Mapping[str, Any]) -> dict[str, Any]:
    album = payload.get("album")
    if isinstance(album, Mapping) and not payload.get("album_type"):
        return dict(album)
    return dict(payload)


def _unwrap_spotify_track(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("item", "track"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            return dict(nested)
    return dict(payload)


def _embedded_spotify_tracks(release: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_tracks = release.get("tracks")
    if isinstance(raw_tracks, Mapping):
        raw_tracks = raw_tracks.get("items")
    if not isinstance(raw_tracks, list):
        return []
    return [_unwrap_spotify_track(track) for track in raw_tracks if isinstance(track, Mapping)]


def _spotify_external_id(entity: Mapping[str, Any], *keys: str) -> str:
    external_ids = entity.get("external_ids") or entity.get("externalIds") or {}
    if not isinstance(external_ids, Mapping):
        return ""
    for key in keys:
        value = external_ids.get(key)
        if _clean_text(value):
            return _clean_text(value)
    return ""


def _spotify_album_ids(tracks: Sequence[Mapping[str, Any]]) -> list[str]:
    ids: list[str] = []
    for raw_track in tracks:
        track = _unwrap_spotify_track(raw_track)
        album = track.get("album")
        if isinstance(album, Mapping) and _clean_text(album.get("id")):
            ids.append(_clean_text(album.get("id")))
    return _unique_texts(ids)


def _parse_featured_title(title: Any) -> tuple[str, list[str]]:
    raw_title = _clean_text(title)
    if not raw_title:
        return "", []
    match = FEATURED_TITLE_RE.search(raw_title)
    if not match:
        return raw_title, []

    featured_text = _clean_text(match.group("artists"))
    display_title = _clean_text(raw_title[: match.start()])
    featured = [
        _clean_text(part)
        for part in re.split(r"\s*(?:,|;|\band\b|&|\bx\b)\s*", featured_text, flags=re.I)
        if _clean_text(part)
    ]
    return display_title or raw_title, _unique_texts(featured)


def _derive_product_type(
    album_type: Any,
    title: str,
    track_count: int,
) -> str:
    normalized_type = _comparison_key(album_type)
    title_key = _comparison_key(title)

    if normalized_type == "compilation":
        return "Compilation"
    if re.search(r"(?:^|\s)ep(?:$|\s)", title_key) or title_key.endswith(" ep"):
        return "EP"
    if track_count <= 1:
        return "Single"
    if 2 <= track_count <= 6:
        return "EP"
    return "Album"


def _infer_release_date_precision(raw_date: str, supplied_precision: Any) -> str:
    precision = _comparison_key(supplied_precision)
    if precision in VALID_DATE_PRECISIONS:
        return precision
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", raw_date):
        return "day"
    if re.fullmatch(r"\d{4}-\d{2}", raw_date):
        return "month"
    if re.fullmatch(r"\d{4}", raw_date):
        return "year"
    return "unknown"


def format_partial_date(raw_date: Any, precision: Any = None) -> tuple[str, str, str]:
    """Returns ``(display_date, normalized_raw_date, normalized_precision)``."""
    raw = _clean_text(raw_date)
    if not raw:
        return "", "", "unknown"

    # iTunes often supplies an ISO timestamp; only the calendar part matters.
    calendar = raw[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", raw) else raw
    normalized_precision = _infer_release_date_precision(calendar, precision)

    try:
        if normalized_precision == "day":
            parsed = datetime.strptime(calendar[:10], "%Y-%m-%d")
            return parsed.strftime("%d/%m/%Y"), parsed.strftime("%Y-%m-%d"), "day"
        if normalized_precision == "month":
            parsed = datetime.strptime(calendar[:7], "%Y-%m")
            return parsed.strftime("%m/%Y"), parsed.strftime("%Y-%m"), "month"
        if normalized_precision == "year" and re.fullmatch(r"\d{4}", calendar[:4]):
            return calendar[:4], calendar[:4], "year"
    except ValueError:
        pass
    return raw, raw, "unknown"


def _release_year(raw_date: str, display_date: str) -> int | None:
    match = YEAR_RE.search(raw_date) or YEAR_RE.search(display_date)
    return int(match.group(1)) if match else None


def parse_copyright_statement(text: Any) -> dict[str, Any]:
    raw = _clean_text(text)
    if not raw:
        return {"year": None, "owner": "", "raw": ""}

    stripped = COPYRIGHT_PREFIX_RE.sub("", raw)
    year_match = YEAR_RE.search(stripped)
    year = int(year_match.group(1)) if year_match else None
    if year_match:
        owner = _clean_text(stripped[year_match.end() :].lstrip(" -–—:,."))
    else:
        owner = _clean_text(stripped)
    return {"year": year, "owner": owner, "raw": raw}


def _spotify_copyright_line(
    release: Mapping[str, Any],
    line_type: str,
) -> tuple[dict[str, Any], bool]:
    copyrights = release.get("copyrights") or []
    if not isinstance(copyrights, list):
        return {"year": None, "owner": "", "raw": "", "confirmed": False}, False

    for item in copyrights:
        if not isinstance(item, Mapping):
            continue
        if _comparison_key(item.get("type")) != _comparison_key(line_type):
            continue
        parsed = parse_copyright_statement(item.get("text"))
        parsed["confirmed"] = bool(parsed.get("year") or parsed.get("owner"))
        return parsed, parsed["confirmed"]
    return {"year": None, "owner": "", "raw": "", "confirmed": False}, False


def _release_candidates(payload: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    visited: set[int] = set()

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            marker = id(node)
            if marker in visited:
                return
            visited.add(marker)

            if _clean_text(node.get("id")) and _clean_text(node.get("title")):
                candidates.append(dict(node))
            for key in ("release", "release-list", "releases", "result", "results"):
                value = node.get(key)
                if isinstance(value, (Mapping, list)):
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    unique: list[dict[str, Any]] = []
    seen = set()
    for candidate in candidates:
        mbid = _clean_text(candidate.get("id"))
        if mbid and mbid not in seen:
            seen.add(mbid)
            unique.append(candidate)
    return unique


def _musicbrainz_label_name(release: Mapping[str, Any]) -> str:
    label_info = release.get("label-info-list") or release.get("label_info_list") or []
    if isinstance(label_info, list):
        for item in label_info:
            if not isinstance(item, Mapping):
                continue
            label = item.get("label")
            if isinstance(label, Mapping) and _clean_text(label.get("name")):
                return _clean_text(label.get("name"))
    label = release.get("label")
    if isinstance(label, Mapping):
        return _clean_text(label.get("name"))
    return _clean_text(label)


def _musicbrainz_track_count(release: Mapping[str, Any]) -> int | None:
    medium_list = release.get("medium-list") or release.get("medium_list") or []
    if not isinstance(medium_list, list):
        return _as_int(release.get("track-count") or release.get("track_count"))
    total = 0
    found = False
    for medium in medium_list:
        if not isinstance(medium, Mapping):
            continue
        count = _as_int(medium.get("track-count") or medium.get("track_count"))
        if count is not None:
            total += count
            found = True
    return total if found else None


def normalize_musicbrainz_release(
    payload: Any,
    *,
    upc: str,
    title: str,
    artist: str,
    expected_track_count: int,
) -> dict[str, Any] | None:
    if payload is None:
        return None

    candidates = _release_candidates(payload)
    if not candidates and isinstance(payload, Mapping):
        candidates = [dict(payload)]
    if not candidates:
        return None

    clean_upc = re.sub(r"\D+", "", upc)
    scored: list[tuple[float, Mapping[str, Any]]] = []
    for candidate in candidates:
        barcode = re.sub(r"\D+", "", _clean_text(candidate.get("barcode")))
        barcode_score = 1.0 if clean_upc and barcode == clean_upc else 0.0
        title_score = _similarity(candidate.get("title"), title)
        artist_score = _similarity(", ".join(_extract_artist_credit_names(candidate)), artist)
        count = _musicbrainz_track_count(candidate)
        if count and expected_track_count:
            count_score = max(0.0, 1.0 - abs(count - expected_track_count) / expected_track_count)
        else:
            count_score = 0.5
        if barcode_score:
            score = 0.75 + title_score * 0.15 + artist_score * 0.08 + count_score * 0.02
        else:
            score = title_score * 0.54 + artist_score * 0.34 + count_score * 0.12
        scored.append((score, candidate))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    score, selected = scored[0]
    if score < 0.68:
        return None

    return {
        "release_mbid": _clean_text(selected.get("id")),
        "title": _clean_text(selected.get("title")),
        "artist": ", ".join(_extract_artist_credit_names(selected)),
        "barcode": _clean_text(selected.get("barcode")),
        "label": _musicbrainz_label_name(selected),
        "date": _clean_text(selected.get("date")),
        "country": _clean_text(selected.get("country")),
        "track_count": _musicbrainz_track_count(selected),
        "match_score": round(score, 4),
        "raw": dict(selected),
    }


def _normalize_itunes_release(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    # utils/apple_music_api.py already returns this normalized structure.
    if "collection_name" in payload or "tracks" in payload:
        return dict(payload)
    return None


def _itunes_track_match(
    spotify_track: Mapping[str, Any],
    itunes_tracks: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, float]:
    if not itunes_tracks:
        return None, 0.0
    try:
        from utils.apple_music_api import match_itunes_track

        return match_itunes_track(spotify_track, itunes_tracks)
    except Exception:
        # Lightweight local fallback for isolated tests/imports.
        best: tuple[float, Mapping[str, Any]] | None = None
        for candidate in itunes_tracks:
            title_score = _similarity(
                candidate.get("track_name"),
                spotify_track.get("name") or spotify_track.get("title"),
            )
            coordinate = 0.0
            if _as_int(candidate.get("track_number")) == _as_int(spotify_track.get("track_number")):
                coordinate += 0.7
            if _as_int(candidate.get("disc_number")) == _as_int(spotify_track.get("disc_number")):
                coordinate += 0.3
            score = title_score * 0.45 + coordinate * 0.55
            if best is None or score > best[0]:
                best = (score, candidate)
        if best and best[0] >= 0.62:
            return dict(best[1]), round(best[0], 4)
        return None, best[0] if best else 0.0


def _parental_advisory(
    spotify_explicit: Any,
    itunes_explicitness: Any,
) -> tuple[str | None, bool, str, str | None]:
    spotify_value = spotify_explicit if isinstance(spotify_explicit, bool) else None
    apple_value = _comparison_key(itunes_explicitness)

    if spotify_value is True or apple_value == "explicit":
        conflict = apple_value in {"notexplicit", "cleaned", "not explicit"}
        return (
            "Explicit",
            not conflict,
            "spotify" if spotify_value is True else "itunes",
            "Spotify και iTunes διαφωνούν για το explicit status." if conflict else None,
        )

    if apple_value in {"notexplicit", "not explicit", "cleaned"}:
        return "Non-Applicable", True, "itunes", None

    if spotify_value is False:
        return (
            "Non-Applicable",
            False,
            "spotify",
            "Το Spotify explicit=false χρησιμοποιήθηκε ως μη επιβεβαιωμένη πρόταση.",
        )

    return (
        None,
        False,
        "missing",
        "Δεν ήταν δυνατό να επιβεβαιωθεί το Parental Advisory.",
    )


def _coerce_credits_payload(payload: Any) -> tuple[dict[str, list[str]], dict[str, str]]:
    if isinstance(payload, Mapping) and isinstance(payload.get("credits"), Mapping):
        credits = payload.get("credits")
        labels = payload.get("credit_labels")
        normalized, normalized_labels = normalize_credit_map(credits)
        if isinstance(labels, Mapping):
            for role_id, label in labels.items():
                if role_id in normalized and _clean_text(label):
                    normalized_labels[role_id] = _clean_text(label)
        return normalized, normalized_labels
    return extract_musicbrainz_credits(payload)


# --------------------------------------------------------------------------
# Canonical LabelCopyData builder
# --------------------------------------------------------------------------
def build_label_copy_data(
    spotify_release: Mapping[str, Any],
    spotify_tracks: Sequence[Mapping[str, Any]] | None = None,
    *,
    itunes_release: Mapping[str, Any] | None = None,
    itunes_fetcher: Callable[..., Any] | None = None,
    musicbrainz_release: Mapping[str, Any] | None = None,
    musicbrainz_release_fetcher: Callable[..., Any] | None = None,
    musicbrainz_credits_fetcher: Callable[..., Any] | None = None,
    tidal_credits_fetcher: Callable[..., Any] | None = None,
    now: datetime | None = None,
    company: str = DEFAULT_COMPANY,
    publisher: str = DEFAULT_PUBLISHER,
    metadata_language_suggestion: str | None = DEFAULT_LANGUAGE_SUGGESTION,
    lyrics_language_suggestion: str | None = DEFAULT_LANGUAGE_SUGGESTION,
    audio_channel_default: str | None = DEFAULT_AUDIO_CHANNEL,
    itunes_country: str = "GR",
    progress_callback: Callable[[int, int, str], Any] | None = None,
    ensure_single_release: bool = True,
) -> dict[str, Any]:
    """
    Builds the canonical ``LabelCopyData`` dictionary.

    No API is called directly. Optional enrichment fetchers are injected and
    may return either ``data`` or ``(data, note)``. Any fetcher failure degrades
    to warnings instead of aborting the build.
    """
    if not isinstance(spotify_release, Mapping):
        raise TypeError("spotify_release must be a mapping")

    release = _unwrap_spotify_release(spotify_release)
    tracks = [
        _unwrap_spotify_track(track)
        for track in (spotify_tracks or _embedded_spotify_tracks(release))
        if isinstance(track, Mapping)
    ]

    if ensure_single_release:
        album_ids = _spotify_album_ids(tracks)
        release_id = _clean_text(release.get("id"))
        if release_id:
            album_ids = _unique_texts([*album_ids, release_id])
        if len(album_ids) > 1:
            raise ValueError(
                "Η playlist περιέχει tracks από περισσότερες από μία κυκλοφορίες."
            )

    warnings: list[str] = []
    generated_at = now or datetime.now(ATHENS_TIMEZONE)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=ATHENS_TIMEZONE)
    else:
        generated_at = generated_at.astimezone(ATHENS_TIMEZONE)

    project_name = _clean_text(release.get("name"))
    release_artists = _extract_artist_names(release)
    track_count = len(tracks) or _as_nonnegative_int(release.get("total_tracks"))
    spotify_upc = _spotify_external_id(release, "upc", "ean", "eanupc")

    # MusicBrainz release fallback/verification.
    mb_release_payload: Any = musicbrainz_release
    if mb_release_payload is None and musicbrainz_release_fetcher is not None:
        mb_release_payload, mb_note = _invoke_callable(
            musicbrainz_release_fetcher,
            upc=spotify_upc,
            barcode=spotify_upc,
            title=project_name,
            artist=release_artists[0] if release_artists else "",
            track_count=track_count,
        )
        if mb_note:
            _append_warning(warnings, f"MusicBrainz release lookup: {mb_note}")

    normalized_mb_release = normalize_musicbrainz_release(
        mb_release_payload,
        upc=spotify_upc,
        title=project_name,
        artist=release_artists[0] if release_artists else "",
        expected_track_count=track_count,
    )

    upc = spotify_upc or (
        _clean_text(normalized_mb_release.get("barcode")) if normalized_mb_release else ""
    )
    upc_source = "spotify" if spotify_upc else (
        "musicbrainz" if upc else "missing"
    )

    # iTunes genre/cross-check enrichment. The public resolver accepts these
    # keyword arguments; injected test doubles may accept any subset.
    itunes_payload: Any = itunes_release
    if itunes_payload is None and itunes_fetcher is not None:
        itunes_payload, itunes_note = _invoke_callable(
            itunes_fetcher,
            collection_id=None,
            upc=upc,
            artist=release_artists[0] if release_artists else "",
            album=project_name,
            expected_track_count=track_count,
            country=itunes_country,
        )
        if itunes_note:
            _append_warning(warnings, f"iTunes: {itunes_note}")
    normalized_itunes = _normalize_itunes_release(itunes_payload)

    if normalized_itunes:
        itunes_count = _as_int(normalized_itunes.get("track_count"))
        if itunes_count and track_count and itunes_count != track_count:
            _append_warning(
                warnings,
                f"Το iTunes αναφέρει {itunes_count} tracks, ενώ το Spotify {track_count}.",
            )
        itunes_title = _clean_text(normalized_itunes.get("collection_name"))
        if itunes_title and _similarity(itunes_title, project_name) < 0.80:
            _append_warning(
                warnings,
                "Ο τίτλος της αντιστοίχισης iTunes διαφέρει από τον τίτλο Spotify.",
            )

    release_date_raw = _clean_text(release.get("release_date"))
    release_date_precision = _clean_text(release.get("release_date_precision"))
    release_date_source = "spotify" if release_date_raw else "missing"
    if not release_date_raw and normalized_itunes:
        release_date_raw = _clean_text(normalized_itunes.get("release_date"))
        release_date_precision = "day" if release_date_raw else "unknown"
        release_date_source = "itunes" if release_date_raw else "missing"
    if not release_date_raw and normalized_mb_release:
        release_date_raw = _clean_text(normalized_mb_release.get("date"))
        release_date_precision = ""
        release_date_source = "musicbrainz" if release_date_raw else "missing"

    release_date, release_date_raw, release_date_precision = format_partial_date(
        release_date_raw,
        release_date_precision,
    )
    release_year = _release_year(release_date_raw, release_date)

    p_line, p_direct = _spotify_copyright_line(release, "P")
    c_line, c_direct = _spotify_copyright_line(release, "C")

    mb_label = _clean_text(normalized_mb_release.get("label")) if normalized_mb_release else ""
    spotify_label = _clean_text(release.get("label"))
    parsed_p_owner = _clean_text(p_line.get("owner"))
    if mb_label:
        label_imprint = mb_label
        label_source = "musicbrainz"
    elif spotify_label:
        label_imprint = spotify_label
        label_source = "spotify"
    elif parsed_p_owner:
        label_imprint = parsed_p_owner
        label_source = "derived"
    else:
        label_imprint = ""
        label_source = "missing"

    if not p_direct and (release_year or label_imprint):
        p_line.update(
            {
                "year": p_line.get("year") or release_year,
                "owner": p_line.get("owner") or label_imprint,
                "confirmed": False,
            }
        )
        _append_warning(
            warnings,
            "Η γραμμή (P) συμπληρώθηκε ως μη επιβεβαιωμένη πρόταση και χρειάζεται έλεγχο.",
        )
    if not c_direct and (release_year or label_imprint):
        c_line.update(
            {
                "year": c_line.get("year") or release_year,
                "owner": c_line.get("owner") or label_imprint,
                "confirmed": False,
            }
        )
        _append_warning(
            warnings,
            "Η γραμμή (C) συμπληρώθηκε ως μη επιβεβαιωμένη πρόταση και χρειάζεται έλεγχο.",
        )

    genre = ""
    genre_source = "missing"
    if normalized_itunes and _clean_text(normalized_itunes.get("primary_genre_name")):
        genre = _clean_text(normalized_itunes.get("primary_genre_name"))
        genre_source = "itunes"

    product_type = _derive_product_type(
        release.get("album_type"),
        project_name,
        track_count,
    )

    metadata_language = _clean_text(metadata_language_suggestion) or None
    if metadata_language:
        _append_warning(
            warnings,
            "Η γλώσσα μεταδεδομένων είναι προτεινόμενη και απαιτεί επιβεβαίωση χρήστη.",
        )

    track_results: list[dict[str, Any]] = []
    itunes_tracks = (
        normalized_itunes.get("tracks", [])
        if normalized_itunes and isinstance(normalized_itunes.get("tracks"), list)
        else []
    )

    for index, track in enumerate(tracks, start=1):
        raw_title = _clean_text(track.get("name"))
        display_title, title_featured = _parse_featured_title(raw_title)
        track_artists = _extract_artist_names(track)
        release_artist_keys = {_comparison_key(name) for name in release_artists}

        primary_artists = [
            name for name in track_artists if _comparison_key(name) in release_artist_keys
        ]
        if not primary_artists and track_artists:
            primary_artists = [track_artists[0]]
        featured_artists = _merge_name_lists(
            [
                name
                for name in track_artists
                if _comparison_key(name) not in {_comparison_key(a) for a in primary_artists}
            ],
            title_featured,
        )

        duration_ms = _as_nonnegative_int(track.get("duration_ms"))
        disc_number = _as_nonnegative_int(track.get("disc_number"), 1) or 1
        track_number = _as_nonnegative_int(track.get("track_number"), index) or index
        isrc = normalize_isrc(_spotify_external_id(track, "isrc") or track.get("isrc"))

        if not isrc:
            _append_warning(warnings, f"Το track «{raw_title or index}» δεν έχει ISRC.")
        elif not validate_isrc(isrc):
            _append_warning(
                warnings,
                f"Το ISRC «{isrc}» του track «{raw_title or index}» δεν έχει έγκυρη μορφή.",
            )

        itunes_track, itunes_track_score = _itunes_track_match(track, itunes_tracks)
        track_genre = genre
        track_genre_source = genre_source
        if itunes_track and _clean_text(itunes_track.get("primary_genre_name")):
            track_genre = _clean_text(itunes_track.get("primary_genre_name"))
            track_genre_source = "itunes"

        parental_advisory, advisory_confirmed, advisory_source, advisory_note = _parental_advisory(
            track.get("explicit"),
            itunes_track.get("track_explicitness") if itunes_track else "",
        )
        if advisory_note:
            _append_warning(warnings, f"{raw_title or f'Track {index}'}: {advisory_note}")

# --- NEW: Tidal & MusicBrainz Merge ---
        tidal_credits: dict[str, list[str]] = {}
        tidal_labels: dict[str, str] = {}
        tidal_note: str | None = None

        if tidal_credits_fetcher is not None and validate_isrc(isrc):
            raw_tidal, tidal_note = _invoke_callable(tidal_credits_fetcher, isrc, isrc=isrc, track=track)
            if raw_tidal:
                tidal_credits, tidal_labels = normalize_credit_map(raw_tidal)

        mb_credits: dict[str, list[str]] = {}
        mb_labels: dict[str, str] = {}
        credits_note: str | None = None

        if musicbrainz_credits_fetcher is not None and validate_isrc(isrc):
            credit_payload, credits_note = _invoke_callable(
                musicbrainz_credits_fetcher,
                isrc,
                isrc=isrc,
                track=track,
            )
            if credit_payload is not None:
                mb_credits, mb_labels = _coerce_credits_payload(credit_payload)

        # Merge them (Tidal goes first, so it has priority)
        credits, credit_labels = merge_credit_maps(
            (tidal_credits, tidal_labels),
            (mb_credits, mb_labels)
        )

        if tidal_note:
            _append_warning(warnings, f"Tidal credits για «{raw_title or index}»: {tidal_note}")
        if credits_note:
            _append_warning(warnings, f"MusicBrainz credits για «{raw_title or index}»: {credits_note}")
        if (musicbrainz_credits_fetcher is not None or tidal_credits_fetcher is not None) and validate_isrc(isrc) and not credits:
            _append_warning(warnings, f"Δεν βρέθηκαν αυτόματα credits για το track «{raw_title or index}».")
        # --- END NEW ---

        lyrics_language = _clean_text(lyrics_language_suggestion) or None
        audio_channel = _clean_text(audio_channel_default) or None

        per_track_p_line = {
            "year": p_line.get("year"),
            "owner": p_line.get("owner") or label_imprint,
            "raw": p_line.get("raw", ""),
            "confirmed": bool(p_line.get("confirmed")) and False,
        }

        sources = {
            "number": "derived",
            "disc_number": "spotify",
            "track_number": "spotify",
            "title": "spotify",
            "raw_title": "spotify",
            "duration_ms": "spotify",
            "primary_artists": "spotify",
            "featured_artists": "derived" if featured_artists else "missing",
            "isrc": "spotify" if isrc else "missing",
            "genre": track_genre_source,
            "subgenre": "missing",
            "lyrics_language": "suggestion" if lyrics_language else "missing",
            "parental_advisory": advisory_source,
            "credits": "tidal" if tidal_credits else ("musicbrainz" if mb_credits else "missing"),
            "publisher": "default" if publisher else "missing",
            "resource_type": "static",
            "audio_channel": "default" if audio_channel else "missing",
            "p_line": "spotify" if p_direct else "derived",
        }
        provenance = {
            field: _source_info(
                source,
                "high"
                if source in {"spotify", "static"}
                else "medium"
                if source in {"itunes", "musicbrainz", "derived"}
                else "low",
                confirmed=(
                    source in {"spotify", "static"}
                    and field not in {"parental_advisory"}
                ),
            )
            for field, source in sources.items()
        }
        provenance["genre"]["detail"] = (
            f"iTunes track match score {itunes_track_score:.0%}"
            if itunes_track
            else "Release-level fallback"
        )
        provenance["lyrics_language"]["confirmed"] = False
        provenance["audio_channel"]["confirmed"] = False
        provenance["parental_advisory"]["confirmed"] = advisory_confirmed
        provenance["p_line"]["confirmed"] = False

        track_results.append(
            {
                "number": index,
                "disc_number": disc_number,
                "track_number": track_number,
                "title": display_title,
                "raw_title": raw_title,
                "duration_ms": duration_ms,
                "primary_artists": primary_artists,
                "featured_artists": featured_artists,
                "isrc": isrc,
                "genre": track_genre,
                "subgenre": "",
                "lyrics_language": lyrics_language,
                "lyrics_language_suggestion": lyrics_language,
                "lyrics_language_confirmed": False,
                "parental_advisory": parental_advisory,
                "parental_advisory_confirmed": advisory_confirmed,
                "publisher": _clean_text(publisher),
                "resource_type": DEFAULT_RESOURCE_TYPE,
                "audio_channel": audio_channel,
                "audio_channel_confirmed": False,
                "p_line": per_track_p_line,
                "credits": credits,
                "credit_labels": credit_labels,
                "sources": sources,
                "provenance": provenance,
            }
        )

        if progress_callback is not None:
            try:
                progress_callback(index, len(tracks), display_title or raw_title or f"Track {index}")
            except Exception:
                pass

    total_duration_ms = sum(track.get("duration_ms", 0) for track in track_results)

    release_sources = {
        "project_name": "spotify" if project_name else "missing",
        "issue_date": "system",
        "artists": "spotify" if release_artists else "missing",
        "product_type": "derived",
        "upc": upc_source,
        "release_date": release_date_source,
        "release_date_precision": release_date_source,
        "label_imprint": label_source,
        "company": "static",
        "publisher": "default" if publisher else "missing",
        "metadata_language": "suggestion" if metadata_language else "missing",
        "genre": genre_source,
        "subgenre": "missing",
        "total_duration_ms": "derived",
        "p_line": "spotify" if p_direct else "derived",
        "c_line": "spotify" if c_direct else "derived",
    }
    release_provenance = {
        field: _source_info(
            source,
            "high"
            if source in {"spotify", "system", "static"}
            else "medium"
            if source in {"itunes", "musicbrainz", "derived"}
            else "low",
            confirmed=source in {"spotify", "system", "static"},
        )
        for field, source in release_sources.items()
    }
    release_provenance["metadata_language"]["confirmed"] = False
    release_provenance["publisher"]["confirmed"] = False
    release_provenance["p_line"]["confirmed"] = bool(p_direct)
    release_provenance["c_line"]["confirmed"] = bool(c_direct)

    data: dict[str, Any] = {
        "schema_version": 1,
        "project_name": project_name,
        "issue_date": generated_at.strftime("%d/%m/%Y"),
        "artists": release_artists,
        "product_type": product_type,
        "upc": upc,
        "release_date": release_date,
        "release_date_raw": release_date_raw,
        "release_date_precision": release_date_precision,
        "label_imprint": label_imprint,
        "company": _clean_text(company) or DEFAULT_COMPANY,
        "publisher": _clean_text(publisher),
        "publisher_confirmed": False,
        "metadata_language": metadata_language,
        "metadata_language_suggestion": metadata_language,
        "metadata_language_confirmed": False,
        "genre": genre,
        "subgenre": "",
        "total_duration_ms": total_duration_ms,
        "p_line": p_line,
        "c_line": c_line,
        "tracks": track_results,
        "sources": release_sources,
        "provenance": release_provenance,
        "warnings": warnings,
        "provider_matches": {
            "spotify_release_id": _clean_text(release.get("id")),
            "itunes_collection_id": (
                normalized_itunes.get("collection_id") if normalized_itunes else None
            ),
            "itunes_match_score": (
                normalized_itunes.get("match_score") if normalized_itunes else None
            ),
            "musicbrainz_release_mbid": (
                normalized_mb_release.get("release_mbid") if normalized_mb_release else None
            ),
            "musicbrainz_match_score": (
                normalized_mb_release.get("match_score") if normalized_mb_release else None
            ),
        },
    }

    for validation_warning in validate_label_copy_data(data):
        _append_warning(data["warnings"], validation_warning)
    return data


def validate_label_copy_data(data: Mapping[str, Any]) -> list[str]:
    """Returns Greek warnings for unresolved or explicitly unconfirmed fields."""
    warnings: list[str] = []

    required_release_fields = {
        "project_name": "τίτλος κυκλοφορίας",
        "artists": "καλλιτέχνης κυκλοφορίας",
        "product_type": "τύπος προϊόντος",
        "release_date": "ημερομηνία κυκλοφορίας",
        "label_imprint": "label imprint",
        "upc": "UPC/EAN",
        "genre": "genre",
    }
    for field, label in required_release_fields.items():
        value = data.get(field)
        if not value:
            warnings.append(f"Δεν επιλύθηκε αυτόματα το πεδίο «{label}».")

    if not data.get("subgenre"):
        warnings.append("Το Subgenre απαιτεί χειροκίνητη συμπλήρωση.")
    if not data.get("metadata_language_confirmed"):
        warnings.append("Η Metadata Language απαιτεί επιβεβαίωση χρήστη.")
    if not data.get("publisher"):
        warnings.append("Ο Publisher απαιτεί χειροκίνητη συμπλήρωση.")

    tracks = data.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        warnings.append("Η κυκλοφορία δεν περιέχει tracks.")
        return warnings

    for index, track in enumerate(tracks, start=1):
        if not isinstance(track, Mapping):
            warnings.append(f"Το Track {index} έχει μη έγκυρη δομή δεδομένων.")
            continue
        title = _clean_text(track.get("title")) or f"Track {index}"
        if not track.get("isrc"):
            warnings.append(f"Το «{title}» απαιτεί χειροκίνητη συμπλήρωση ISRC.")
        if not track.get("subgenre"):
            warnings.append(f"Το Subgenre του «{title}» απαιτεί χειροκίνητη συμπλήρωση.")
        if not track.get("lyrics_language_confirmed"):
            warnings.append(f"Η Lyrics Language του «{title}» απαιτεί επιβεβαίωση.")
        if not track.get("audio_channel_confirmed"):
            warnings.append(f"Το Audio Channel του «{title}» είναι editable default και χρειάζεται έλεγχο.")
        if not track.get("credits"):
            warnings.append(f"Τα credits του «{title}» απαιτούν χειροκίνητη συμπλήρωση ή επιβεβαίωση.")

    return _unique_texts(warnings)


__all__ = [
    "ATHENS_TIMEZONE",
    "DEFAULT_AUDIO_CHANNEL",
    "DEFAULT_COMPANY",
    "DEFAULT_LANGUAGE_SUGGESTION",
    "DEFAULT_PUBLISHER",
    "DEFAULT_RESOURCE_TYPE",
    "ROLE_DEFINITIONS",
    "VALID_PRODUCT_TYPES",
    "build_label_copy_data",
    "extract_musicbrainz_credits",
    "extract_musicbrainz_work_ids",
    "format_partial_date",
    "make_musicbrainz_credit_fetcher",
    "merge_credit_maps",
    "normalize_credit_map",
    "normalize_isrc",
    "normalize_musicbrainz_release",
    "parse_copyright_statement",
    "resolve_canonical_roles",
    "validate_isrc",
    "validate_label_copy_data",
]
