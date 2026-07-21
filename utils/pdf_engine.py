"""
utils/pdf_engine.py

ReportLab-only PDF renderer for canonical LabelCopyData dictionaries.

The renderer intentionally does not convert the DOCX. It builds the denser,
print-ready Label Copy layout independently with ReportLab Platypus:

- dark release header with a two-column key/value grid;
- bordered, non-splitting track cards;
- static PDF fields (Publisher, Resource Type and Audio Channel);
- natural-language credit labels derived from ROLE_DEFINITIONS.pdf_label;
- a dynamic footer on every page with "Page N of M";
- a final phonographic/copyright and rights-reservation block.

Greek / Unicode font deployment
--------------------------------
ReportLab's built-in Type-1 fonts do not contain Greek glyphs. The preferred
production setup is to commit one Unicode family to the application repo:

    assets/fonts/DejaVuSans.ttf
    assets/fonts/DejaVuSans-Bold.ttf
    assets/fonts/DejaVuSans-Oblique.ttf       # optional

Noto Sans may be used instead with the equivalent file names. You can also set
these environment variables to absolute paths:

    LABELCOPY_FONT_REGULAR
    LABELCOPY_FONT_BOLD
    LABELCOPY_FONT_ITALIC                     # optional

If no local family is found and ``allow_font_download=True`` (the default), the
module downloads Noto Sans Regular/Bold/Italic from the official googlefonts
repository into a temporary cache, validates the TTF/OTF signature, and then
registers the family with ``pdfmetrics.registerFont`` and
``pdfmetrics.registerFontFamily``. No font is fetched at import time.
"""

from __future__ import annotations

import hashlib
import html
import io
import os
import re
import tempfile
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import requests
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from utils.label_copy_engine import ROLE_DEFINITIONS, resolve_canonical_roles


# ---------------------------------------------------------------------------
# Visual constants, tuned against the supplied four-page PDF reference.
# ---------------------------------------------------------------------------
PAGE_SIZE = A4
PAGE_LEFT_MARGIN = 14 * mm
PAGE_RIGHT_MARGIN = 14 * mm
PAGE_TOP_MARGIN = 17 * mm
PAGE_BOTTOM_MARGIN = 20 * mm
FOOTER_Y = 8.5 * mm

COLOR_HEADER = colors.HexColor("#192630")
COLOR_HEADER_LABEL = colors.HexColor("#9EABB0")
COLOR_ACCENT = colors.HexColor("#1497D4")
COLOR_TEXT = colors.HexColor("#243548")
COLOR_MUTED = colors.HexColor("#7D8B8D")
COLOR_CARD_HEADER = colors.HexColor("#F4F6F8")
COLOR_BORDER = colors.HexColor("#DDE4E9")
COLOR_RULE = colors.HexColor("#E3E9ED")
COLOR_WHITE = colors.white

FONT_DOWNLOAD_TIMEOUT_SECONDS = 20
FONT_DOWNLOAD_RETRIES = 3
FONT_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024
FONT_CACHE_FOLDER = "stay-independent-label-copy-fonts"

NOTO_FONT_URLS = {
    "regular": (
        "https://raw.githubusercontent.com/googlefonts/noto-fonts/main/"
        "hinted/ttf/NotoSans/NotoSans-Regular.ttf"
    ),
    "bold": (
        "https://raw.githubusercontent.com/googlefonts/noto-fonts/main/"
        "hinted/ttf/NotoSans/NotoSans-Bold.ttf"
    ),
    "italic": (
        "https://raw.githubusercontent.com/googlefonts/noto-fonts/main/"
        "hinted/ttf/NotoSans/NotoSans-Italic.ttf"
    ),
}

PDF_ROLE_ORDER = (
    "writer",
    "lyricist",
    "composer",
    "lyrics_adaptation",
    "translator",
    "vocalist",
    "guitarist",
    "drummer",
    "bassist",
    "keyboardist",
    "programmer",
    "performer",
    "recitation",
    "documentary_excerpt",
    "poetry_excerpt",
    "arranger",
    "producer",
    "co_producer",
    "executive_producer",
    "recording_engineer",
    "mixing_engineer",
    "mastering_engineer",
    "conductor",
    "remixer",
)


class PdfFontError(RuntimeError):
    """Raised when no Greek-compatible Unicode font family can be registered."""


class PdfRenderError(RuntimeError):
    """Raised when ReportLab cannot render a valid Label Copy PDF."""


@dataclass(frozen=True)
class RegisteredFontFamily:
    regular: str
    bold: str
    italic: str
    regular_path: str
    bold_path: str
    italic_path: str


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if value != value:  # NaN-like scalar
            return ""
    except (TypeError, ValueError):
        pass
    return re.sub(r"\s+", " ", str(value).strip())


def _escape(value: Any) -> str:
    return html.escape(_clean_text(value), quote=False)


def _as_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _unique_texts(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        key = unicodedata.normalize("NFKC", text).casefold()
        if not text or not key or key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _credit_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_clean_text(value)] if _clean_text(value) else []
    if isinstance(value, Mapping):
        if "names" in value:
            return _credit_names(value.get("names"))
        name = value.get("name") or value.get("artist_name") or value.get("artist")
        if isinstance(name, Mapping):
            name = name.get("name")
        return [_clean_text(name)] if _clean_text(name) else []
    if isinstance(value, Iterable):
        names: list[str] = []
        for item in value:
            names.extend(_credit_names(item))
        return _unique_texts(names)
    return [_clean_text(value)] if _clean_text(value) else []


def _duration_ms(entity: Mapping[str, Any]) -> int:
    value = entity.get("duration_ms")
    if value is None:
        value = entity.get("total_duration_ms")
    if value is not None:
        return max(_as_int(value), 0)

    legacy = _clean_text(entity.get("duration") or entity.get("total_duration"))
    if not legacy:
        return 0
    parts = legacy.split(":")
    if not all(part.isdigit() for part in parts):
        return 0
    numbers = [int(part) for part in parts]
    if len(numbers) == 2:
        minutes, seconds = numbers
        return ((minutes * 60) + seconds) * 1000
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        return ((hours * 3600) + (minutes * 60) + seconds) * 1000
    return 0


def format_duration_pdf(duration_ms: Any) -> str:
    """Formats canonical milliseconds as unbounded ``M:SS`` for the PDF."""
    total_seconds = max(_as_int(duration_ms), 0) // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def _date_year(value: Any) -> int | None:
    match = re.search(r"\b(19\d{2}|20\d{2}|21\d{2})\b", _clean_text(value))
    return int(match.group(1)) if match else None


def _rights_parts(
    line: Any,
    *,
    fallback_year: int | None = None,
    fallback_owner: str = "",
) -> tuple[int | None, str]:
    if isinstance(line, Mapping):
        year = _as_int(line.get("year"), 0) or fallback_year
        owner = _clean_text(line.get("owner")) or _clean_text(fallback_owner)
        return year, owner

    text = _clean_text(line)
    year = _date_year(text) or fallback_year
    owner = re.sub(r"^.*?\b(?:19\d{2}|20\d{2}|21\d{2})\b\s*", "", text).strip()
    return year, owner or _clean_text(fallback_owner)


def _rights_text(
    prefix: str,
    line: Any,
    *,
    fallback_year: int | None = None,
    fallback_owner: str = "",
) -> str:
    year, owner = _rights_parts(
        line,
        fallback_year=fallback_year,
        fallback_owner=fallback_owner,
    )
    components = [prefix]
    if year:
        components.append(str(year))
    if owner:
        components.append(owner)
    return " ".join(components)


def _records_name(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return "Stay Independent Records"
    if re.search(r"\brecords?\b", text, re.IGNORECASE):
        return text
    return f"{text} Records"


def _artist_list(track: Mapping[str, Any]) -> list[str]:
    # The PDF reference does not have a separate Featured Artist line. The
    # chosen behavior is to merge featured artists into Primary Artist(s).
    return _unique_texts(
        [
            *(_credit_names(track.get("primary_artists"))),
            *(_credit_names(track.get("featured_artists"))),
        ]
    )


def _genre_text(track: Mapping[str, Any], release: Mapping[str, Any]) -> str:
    genre = _clean_text(track.get("genre")) or _clean_text(release.get("genre"))
    subgenre = _clean_text(track.get("subgenre")) or _clean_text(release.get("subgenre"))
    return " / ".join(part for part in (genre, subgenre) if part)


# ---------------------------------------------------------------------------
# Unicode font discovery, optional download and ReportLab registration
# ---------------------------------------------------------------------------
def _font_magic_is_valid(data: bytes) -> bool:
    return any(
        data.startswith(prefix)
        for prefix in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1")
    )


def _font_file_is_valid(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 1024:
            return False
        with path.open("rb") as handle:
            return _font_magic_is_valid(handle.read(4))
    except OSError:
        return False


def _candidate_font_directories() -> list[Path]:
    repo_root = Path(__file__).resolve().parents[1]
    cwd = Path.cwd()
    directories = [
        repo_root / "assets" / "fonts",
        repo_root / "fonts",
        cwd / "assets" / "fonts",
        cwd / "fonts",
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/truetype/noto"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".fonts",
    ]
    output: list[Path] = []
    seen: set[str] = set()
    for directory in directories:
        key = str(directory.resolve()) if directory.exists() else str(directory)
        if key not in seen:
            seen.add(key)
            output.append(directory)
    return output


def _find_font_file(
    explicit_path: str | os.PathLike[str] | None,
    environment_key: str,
    filenames: tuple[str, ...],
) -> Path | None:
    explicit = Path(explicit_path).expanduser() if explicit_path else None
    if explicit and _font_file_is_valid(explicit):
        return explicit.resolve()

    environment_value = _clean_text(os.environ.get(environment_key))
    if environment_value:
        environment_path = Path(environment_value).expanduser()
        if _font_file_is_valid(environment_path):
            return environment_path.resolve()

    for directory in _candidate_font_directories():
        for filename in filenames:
            candidate = directory / filename
            if _font_file_is_valid(candidate):
                return candidate.resolve()
    return None


def _retry_after_seconds(value: Any, default: float = 1.0) -> float:
    try:
        return max(0.0, min(float(value), 30.0))
    except (TypeError, ValueError):
        return default


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _download_font(url: str, destination: Path) -> tuple[Path | None, str | None]:
    """Downloads one font with a bounded, never-raises network contract."""
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"Αδυναμία δημιουργίας font cache: {exc}"

    last_error = "Άγνωστο σφάλμα λήψης font."
    for attempt in range(FONT_DOWNLOAD_RETRIES):
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{time.time_ns()}.part"
        )
        try:
            with requests.get(
                url,
                timeout=FONT_DOWNLOAD_TIMEOUT_SECONDS,
                stream=True,
                headers={"User-Agent": "stay-independent-label-copy/1.0"},
            ) as response:
                if response.status_code == 429:
                    last_error = "Ο font server επέβαλε προσωρινό rate limit."
                    if attempt + 1 < FONT_DOWNLOAD_RETRIES:
                        time.sleep(_retry_after_seconds(response.headers.get("Retry-After")))
                        continue
                    return None, last_error
                if 500 <= response.status_code < 600:
                    last_error = f"Ο font server επέστρεψε HTTP {response.status_code}."
                    if attempt + 1 < FONT_DOWNLOAD_RETRIES:
                        time.sleep(0.5 * (2**attempt))
                        continue
                    return None, last_error
                if not response.ok:
                    return (
                        None,
                        f"Η λήψη Unicode font απέτυχε με HTTP {response.status_code}.",
                    )

                buffer = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    buffer.extend(chunk)
                    if len(buffer) > FONT_DOWNLOAD_MAX_BYTES:
                        return None, "Το Unicode font υπερβαίνει το επιτρεπόμενο μέγεθος."

                data = bytes(buffer)
                if len(data) < 1024 or not _font_magic_is_valid(data[:4]):
                    return None, "Το αρχείο που λήφθηκε δεν είναι έγκυρο TTF/OTF font."

                temporary.write_bytes(data)
                temporary.replace(destination)
                return destination.resolve(), None

        except requests.RequestException as exc:
            last_error = f"Αποτυχία δικτύου κατά τη λήψη Unicode font: {exc}"
            if attempt + 1 < FONT_DOWNLOAD_RETRIES:
                time.sleep(0.5 * (2**attempt))
                continue
        except OSError as exc:
            return None, f"Αδυναμία αποθήκευσης Unicode font: {exc}"
        except Exception as exc:  # Defensive: the helper must never raise to its caller.
            return None, f"Μη αναμενόμενο σφάλμα λήψης Unicode font: {exc}"
        finally:
            _safe_unlink(temporary)

    return None, last_error


def _download_noto_family(cache_dir: Path | None = None) -> tuple[Path, Path, Path]:
    base = cache_dir or (Path(tempfile.gettempdir()) / FONT_CACHE_FOLDER)
    regular = base / "NotoSans-Regular.ttf"
    bold = base / "NotoSans-Bold.ttf"
    italic = base / "NotoSans-Italic.ttf"

    for role, path in (("regular", regular), ("bold", bold), ("italic", italic)):
        if _font_file_is_valid(path):
            continue
        downloaded, note = _download_font(NOTO_FONT_URLS[role], path)
        if downloaded is None:
            raise PdfFontError(note or f"Αποτυχία λήψης του {role} Unicode font.")

    return regular.resolve(), bold.resolve(), italic.resolve()


def register_greek_font_family(
    *,
    regular_path: str | os.PathLike[str] | None = None,
    bold_path: str | os.PathLike[str] | None = None,
    italic_path: str | os.PathLike[str] | None = None,
    allow_download: bool = True,
    cache_dir: str | os.PathLike[str] | None = None,
) -> RegisteredFontFamily:
    """Finds/downloads and registers a Unicode family suitable for Greek text."""
    regular = _find_font_file(
        regular_path,
        "LABELCOPY_FONT_REGULAR",
        ("DejaVuSans.ttf", "NotoSans-Regular.ttf", "NotoSans.ttf"),
    )
    bold = _find_font_file(
        bold_path,
        "LABELCOPY_FONT_BOLD",
        ("DejaVuSans-Bold.ttf", "NotoSans-Bold.ttf"),
    )
    italic = _find_font_file(
        italic_path,
        "LABELCOPY_FONT_ITALIC",
        ("DejaVuSans-Oblique.ttf", "NotoSans-Italic.ttf"),
    )

    if (regular is None or bold is None) and allow_download:
        downloaded_regular, downloaded_bold, downloaded_italic = _download_noto_family(
            Path(cache_dir).expanduser() if cache_dir else None
        )
        regular = regular or downloaded_regular
        bold = bold or downloaded_bold
        italic = italic or downloaded_italic

    if regular is None or bold is None:
        raise PdfFontError(
            "Δεν βρέθηκε Greek-compatible Unicode font. Προσθέστε "
            "assets/fonts/DejaVuSans.ttf και DejaVuSans-Bold.ttf ή ορίστε "
            "LABELCOPY_FONT_REGULAR / LABELCOPY_FONT_BOLD."
        )
    italic = italic or regular

    identity = "|".join(str(path) for path in (regular, bold, italic))
    suffix = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
    regular_name = f"LabelCopySans-{suffix}"
    bold_name = f"LabelCopySansBold-{suffix}"
    italic_name = f"LabelCopySansItalic-{suffix}"

    registered = set(pdfmetrics.getRegisteredFontNames())
    try:
        if regular_name not in registered:
            pdfmetrics.registerFont(TTFont(regular_name, str(regular), validate=1))
        if bold_name not in registered:
            pdfmetrics.registerFont(TTFont(bold_name, str(bold), validate=1))
        if italic_name not in registered:
            pdfmetrics.registerFont(TTFont(italic_name, str(italic), validate=1))
        pdfmetrics.registerFontFamily(
            f"LabelCopyFamily-{suffix}",
            normal=regular_name,
            bold=bold_name,
            italic=italic_name,
            boldItalic=bold_name,
        )
    except Exception as exc:
        raise PdfFontError(f"Αποτυχία καταχώρισης Unicode font στο ReportLab: {exc}") from exc

    return RegisteredFontFamily(
        regular=regular_name,
        bold=bold_name,
        italic=italic_name,
        regular_path=str(regular),
        bold_path=str(bold),
        italic_path=str(italic),
    )


# ---------------------------------------------------------------------------
# Credit normalization and natural-language PDF rows
# ---------------------------------------------------------------------------
def _canonical_credits(
    track: Mapping[str, Any],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    raw_credits = track.get("credits")
    supplied_labels = track.get("credit_labels")
    labels = dict(supplied_labels) if isinstance(supplied_labels, Mapping) else {}
    if not isinstance(raw_credits, Mapping):
        return {}, labels

    credits: dict[str, list[str]] = OrderedDict()
    for raw_role, raw_value in raw_credits.items():
        names = _credit_names(raw_value)
        if not names:
            continue

        role_text = _clean_text(raw_role)
        if role_text in ROLE_DEFINITIONS:
            resolved = [(role_text, ROLE_DEFINITIONS[role_text]["pdf_label"])]
        elif role_text.startswith("other:"):
            fallback = role_text.removeprefix("other:").replace("_", " ").title()
            resolved = [(role_text, labels.get(role_text, fallback))]
        else:
            resolved = resolve_canonical_roles(role_text)

        for role_id, display_label in resolved:
            credits[role_id] = _unique_texts([*credits.get(role_id, []), *names])
            labels.setdefault(role_id, _clean_text(display_label) or role_text)

    ordered: dict[str, list[str]] = OrderedDict()
    for role_id in PDF_ROLE_ORDER:
        if credits.get(role_id):
            ordered[role_id] = credits[role_id]
    for role_id, names in credits.items():
        if role_id not in ordered:
            ordered[role_id] = names
    return dict(ordered), labels


def _same_names(left: Iterable[Any], right: Iterable[Any]) -> bool:
    left_keys = {unicodedata.normalize("NFKC", name).casefold() for name in _unique_texts(left)}
    right_keys = {unicodedata.normalize("NFKC", name).casefold() for name in _unique_texts(right)}
    return bool(left_keys) and left_keys == right_keys


def _pdf_role_label(role_id: str, labels: Mapping[str, str]) -> str:
    if role_id == "producer":
        # This is the wording used for a standalone producer row in the supplied
        # reference. Producer + mixer is combined separately below.
        return "Recording Producer"
    definition = ROLE_DEFINITIONS.get(role_id)
    if definition:
        return _clean_text(definition.get("pdf_label")) or _clean_text(
            definition.get("display_label")
        )
    return _clean_text(labels.get(role_id)) or role_id.removeprefix("other:").replace(
        "_", " "
    ).title()


def _pdf_credit_rows(
    track: Mapping[str, Any],
    release_publisher: str,
) -> list[tuple[str, list[str] | str]]:
    credits, labels = _canonical_credits(track)
    rows: list[tuple[str, list[str] | str]] = []
    consumed: set[str] = set()

    def add(role_id: str, label: str | None = None) -> None:
        names = credits.get(role_id, [])
        if not names or role_id in consumed:
            return
        rows.append((label or _pdf_role_label(role_id, labels), names))
        consumed.add(role_id)

    # Writing roles first, as in the reference PDF.
    for role_id in ("writer", "lyricist", "composer", "lyrics_adaptation", "translator"):
        add(role_id)

    # PDF-only publishing line: track override -> release publisher. It remains
    # present even when blank, matching the fixed field set of the reference.
    publisher = _clean_text(track.get("publisher")) or _clean_text(release_publisher)
    rows.append(("Published by", publisher))

    add("vocalist")
    add("guitarist")

    # Exact compound wording visible in the source PDF.
    if all(credits.get(role_id) for role_id in ("drummer", "bassist", "keyboardist")):
        if _same_names(credits["drummer"], credits["bassist"]) and _same_names(
            credits["drummer"], credits["keyboardist"]
        ):
            rows.append(("Drums, Bass, Synths", credits["drummer"]))
            consumed.update({"drummer", "bassist", "keyboardist"})

    for role_id in (
        "drummer",
        "bassist",
        "keyboardist",
        "programmer",
        "performer",
        "recitation",
        "documentary_excerpt",
        "poetry_excerpt",
    ):
        add(role_id)

    producer_names = credits.get("producer", [])
    mixing_names = credits.get("mixing_engineer", [])
    arranger_names = credits.get("arranger", [])

    if producer_names and mixing_names and _same_names(producer_names, mixing_names):
        rows.append(("Produced & Mixed by", producer_names))
        consumed.update({"producer", "mixing_engineer"})
    elif producer_names and arranger_names and not mixing_names and _same_names(
        producer_names, arranger_names
    ):
        rows.append(("Arranged & Produced", producer_names))
        consumed.update({"producer", "arranger"})

    add("arranger")
    add("producer")
    add("co_producer")
    add("executive_producer")
    add("recording_engineer")
    add("mixing_engineer")
    add("mastering_engineer")
    add("conductor")
    add("remixer")

    # Preserve every unknown/dynamic role verbatim, including Recitation,
    # Documentary Excerpt, Poetry Excerpt or future relationship types.
    for role_id, names in credits.items():
        if role_id in consumed:
            continue
        rows.append((_pdf_role_label(role_id, labels), names))
        consumed.add(role_id)

    return rows


# ---------------------------------------------------------------------------
# ReportLab styles and flowables
# ---------------------------------------------------------------------------
def _styles(fonts: RegisteredFontFamily) -> dict[str, ParagraphStyle]:
    return {
        "header_title": ParagraphStyle(
            "LabelCopyHeaderTitle",
            fontName=fonts.bold,
            fontSize=16.5,
            leading=20,
            textColor=COLOR_WHITE,
            alignment=TA_LEFT,
            spaceAfter=0,
        ),
        "header_artist": ParagraphStyle(
            "LabelCopyHeaderArtist",
            fontName=fonts.regular,
            fontSize=10.5,
            leading=13,
            textColor=COLOR_ACCENT,
            alignment=TA_LEFT,
        ),
        "header_label": ParagraphStyle(
            "LabelCopyHeaderLabel",
            fontName=fonts.bold,
            fontSize=8.1,
            leading=10.3,
            textColor=COLOR_HEADER_LABEL,
            alignment=TA_LEFT,
        ),
        "header_value": ParagraphStyle(
            "LabelCopyHeaderValue",
            fontName=fonts.regular,
            fontSize=8.3,
            leading=10.3,
            textColor=COLOR_WHITE,
            alignment=TA_LEFT,
        ),
        "track_number": ParagraphStyle(
            "LabelCopyTrackNumber",
            fontName=fonts.bold,
            fontSize=10,
            leading=12,
            textColor=COLOR_HEADER,
            alignment=TA_LEFT,
        ),
        "track_title": ParagraphStyle(
            "LabelCopyTrackTitle",
            fontName=fonts.bold,
            fontSize=10,
            leading=12,
            textColor=COLOR_HEADER,
            alignment=TA_LEFT,
        ),
        "duration": ParagraphStyle(
            "LabelCopyTrackDuration",
            fontName=fonts.regular,
            fontSize=8.7,
            leading=11,
            textColor=COLOR_MUTED,
            alignment=TA_RIGHT,
        ),
        "field_label": ParagraphStyle(
            "LabelCopyFieldLabel",
            fontName=fonts.bold,
            fontSize=7.5,
            leading=9.3,
            textColor=COLOR_MUTED,
            alignment=TA_LEFT,
        ),
        "field_value": ParagraphStyle(
            "LabelCopyFieldValue",
            fontName=fonts.regular,
            fontSize=7.5,
            leading=9.3,
            textColor=COLOR_TEXT,
            alignment=TA_LEFT,
        ),
        "credit_label": ParagraphStyle(
            "LabelCopyCreditLabel",
            fontName=fonts.bold,
            fontSize=7.55,
            leading=9.5,
            textColor=COLOR_TEXT,
            alignment=TA_LEFT,
        ),
        "credit_value": ParagraphStyle(
            "LabelCopyCreditValue",
            fontName=fonts.regular,
            fontSize=7.55,
            leading=9.5,
            textColor=COLOR_TEXT,
            alignment=TA_LEFT,
        ),
        "p_line": ParagraphStyle(
            "LabelCopyPLine",
            fontName=fonts.italic,
            fontSize=7.2,
            leading=9,
            textColor=COLOR_MUTED,
            alignment=TA_LEFT,
        ),
        "rights": ParagraphStyle(
            "LabelCopyRights",
            fontName=fonts.regular,
            fontSize=6.7,
            leading=9,
            textColor=COLOR_MUTED,
            alignment=TA_CENTER,
        ),
    }


def _paragraph(value: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_escape(value) or "&#160;", style)


def _markup_paragraph(markup: str, style: ParagraphStyle) -> Paragraph:
    """Creates a Paragraph from trusted, module-owned ReportLab markup."""
    return Paragraph(markup or "&#160;", style)


def _header_flowable(
    data: Mapping[str, Any],
    styles: Mapping[str, ParagraphStyle],
    available_width: float,
) -> Table:
    artists = ", ".join(_unique_texts(data.get("artists", [])))
    language = _clean_text(data.get("metadata_language")) or _clean_text(
        data.get("metadata_language_suggestion")
    )

    title = _paragraph(data.get("project_name"), styles["header_title"])
    artist = _paragraph(artists, styles["header_artist"])

    label_width = 27 * mm
    value_width = 41 * mm
    right_label_width = 26 * mm
    right_value_width = max(
        available_width - label_width - value_width - right_label_width,
        35 * mm,
    )

    rows = [
        [title, "", "", ""],
        [artist, "", "", ""],
        ["", "", "", ""],
        [
            _markup_paragraph("Product<br/>Type:", styles["header_label"]),
            _paragraph(data.get("product_type"), styles["header_value"]),
            _paragraph("UPC:", styles["header_label"]),
            _paragraph(data.get("upc"), styles["header_value"]),
        ],
        [
            _markup_paragraph("Release<br/>Date:", styles["header_label"]),
            _paragraph(data.get("release_date"), styles["header_value"]),
            _markup_paragraph("Label<br/>Imprint:", styles["header_label"]),
            _paragraph(data.get("label_imprint"), styles["header_value"]),
        ],
        [
            _paragraph("Publisher:", styles["header_label"]),
            _paragraph(data.get("publisher") or data.get("company"), styles["header_value"]),
            _paragraph("Language:", styles["header_label"]),
            _paragraph(language, styles["header_value"]),
        ],
    ]

    table = Table(
        rows,
        colWidths=[label_width, value_width, right_label_width, right_value_width],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_HEADER),
                ("SPAN", (0, 0), (-1, 0)),
                ("SPAN", (0, 1), (-1, 1)),
                ("SPAN", (0, 2), (-1, 2)),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("LEFTPADDING", (0, 0), (0, -1), 8 * mm),
                ("RIGHTPADDING", (0, 3), (0, -1), 1.5 * mm),
                ("RIGHTPADDING", (0, 0), (0, 2), 8 * mm),
                ("LEFTPADDING", (1, 3), (1, -1), 0.5 * mm),
                ("RIGHTPADDING", (1, 3), (1, -1), 2.5 * mm),
                ("LEFTPADDING", (2, 3), (2, -1), 4 * mm),
                ("RIGHTPADDING", (2, 3), (2, -1), 1.5 * mm),
                ("LEFTPADDING", (3, 3), (3, -1), 0.5 * mm),
                ("RIGHTPADDING", (3, 0), (3, -1), 5 * mm),
                ("TOPPADDING", (0, 0), (-1, 0), 7 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 1.2 * mm),
                ("TOPPADDING", (0, 1), (-1, 1), 0),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 4 * mm),
                ("TOPPADDING", (0, 2), (-1, 2), 0),
                ("BOTTOMPADDING", (0, 2), (-1, 2), 0),
                ("TOPPADDING", (0, 3), (-1, -1), 1.1 * mm),
                ("BOTTOMPADDING", (0, 3), (-1, -1), 1.1 * mm),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 7 * mm),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOX", (0, 0), (-1, -1), 0.35, COLOR_HEADER),
            ]
        )
    )
    return table


def _track_flowable(
    data: Mapping[str, Any],
    track: Mapping[str, Any],
    display_number: int,
    styles: Mapping[str, ParagraphStyle],
    available_width: float,
) -> KeepTogether:
    title = _clean_text(track.get("title")) or f"Track {display_number}"
    duration = format_duration_pdf(_duration_ms(track))

    header_table = Table(
        [[
            _paragraph(f"{display_number}.", styles["track_number"]),
            _paragraph(title, styles["track_title"]),
            _paragraph(duration, styles["duration"]),
        ]],
        colWidths=[13 * mm, available_width - 34 * mm, 21 * mm],
        hAlign="LEFT",
    )
    header_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_HEADER),
                ("LEFTPADDING", (0, 0), (0, 0), 4.8 * mm),
                ("LEFTPADDING", (1, 0), (1, 0), 0),
                ("RIGHTPADDING", (-1, 0), (-1, 0), 4.5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3.3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.1 * mm),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.45, COLOR_BORDER),
            ]
        )
    )

    primary_artists = _artist_list(track)
    primary_label = "Primary Artist" if len(primary_artists) == 1 else "Primary Artists"
    genre = _genre_text(track, data)
    resource_type = _clean_text(track.get("resource_type")) or "Audio"
    audio_channel = _clean_text(track.get("audio_channel")) or "Stereo"

    metadata_rows = [
        [
            _paragraph("ISRC:", styles["field_label"]),
            _paragraph(track.get("isrc"), styles["field_value"]),
            _paragraph("Resource Type:", styles["field_label"]),
            _paragraph(resource_type, styles["field_value"]),
        ],
        [
            _paragraph("Audio Channel:", styles["field_label"]),
            _paragraph(audio_channel, styles["field_value"]),
            _paragraph("Genre:", styles["field_label"]),
            _paragraph(genre, styles["field_value"]),
        ],
        [
            _paragraph("Parental Advisory:", styles["field_label"]),
            _paragraph(track.get("parental_advisory"), styles["field_value"]),
            _paragraph(f"{primary_label}:", styles["field_label"]),
            _paragraph(", ".join(primary_artists), styles["field_value"]),
        ],
    ]

    left_label_width = 31 * mm
    left_value_width = 48 * mm
    right_label_width = 31 * mm
    right_value_width = max(
        available_width - left_label_width - left_value_width - right_label_width - 10 * mm,
        42 * mm,
    )
    metadata_table = Table(
        metadata_rows,
        colWidths=[
            left_label_width,
            left_value_width,
            right_label_width,
            right_value_width,
        ],
        hAlign="LEFT",
    )
    metadata_table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1.2 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 0.65 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0.65 * mm),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    role_rows = _pdf_credit_rows(track, _clean_text(data.get("publisher")))
    role_table_data: list[list[Any]] = []
    for label, value in role_rows:
        if isinstance(value, str):
            rendered_value = value
        else:
            rendered_value = ", ".join(_unique_texts(value))
        role_table_data.append(
            [
                _paragraph(f"{_clean_text(label).rstrip(':')}:", styles["credit_label"]),
                _paragraph(rendered_value, styles["credit_value"]),
            ]
        )

    if role_table_data:
        role_table = Table(
            role_table_data,
            colWidths=[34 * mm, available_width - 34 * mm - 10 * mm],
            hAlign="LEFT",
        )
        role_table.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0.35 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0.35 * mm),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
    else:
        role_table = Spacer(1, 0)

    release_year = _date_year(data.get("release_date_raw") or data.get("release_date"))
    p_text = _rights_text(
        "(P)",
        track.get("p_line") or data.get("p_line"),
        fallback_year=release_year,
        fallback_owner=_clean_text(data.get("label_imprint")),
    )

    body_contents: list[Any] = [
        metadata_table,
        Spacer(1, 2.0 * mm),
        Table(
            [[""]],
            colWidths=[available_width - 10 * mm],
            rowHeights=[0.1 * mm],
            style=TableStyle(
                [
                    ("LINEABOVE", (0, 0), (-1, -1), 0.4, COLOR_RULE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            ),
        ),
        Spacer(1, 1.7 * mm),
        role_table,
        Spacer(1, 1.3 * mm),
        _paragraph(p_text, styles["p_line"]),
    ]

    body_table = Table(
        [[body_contents]],
        colWidths=[available_width],
        hAlign="LEFT",
    )
    body_table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3.3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.4 * mm),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    card = Table(
        [[header_table], [body_table]],
        colWidths=[available_width],
        hAlign="LEFT",
    )
    card.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("BOX", (0, 0), (-1, -1), 0.55, COLOR_BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return KeepTogether([card, Spacer(1, 4.2 * mm)])


def _final_rights_flowable(
    data: Mapping[str, Any],
    styles: Mapping[str, ParagraphStyle],
    available_width: float,
) -> KeepTogether:
    release_year = _date_year(data.get("release_date_raw") or data.get("release_date"))
    imprint = _clean_text(data.get("label_imprint"))
    p_year, p_owner = _rights_parts(
        data.get("p_line"), fallback_year=release_year, fallback_owner=imprint
    )
    c_year, c_owner = _rights_parts(
        data.get("c_line"), fallback_year=release_year, fallback_owner=imprint
    )

    p_records = _records_name(p_owner or imprint)
    c_records = _records_name(c_owner or imprint)
    p_segment = " ".join(part for part in ("(P)", str(p_year or ""), p_records) if part)
    c_segment = " ".join(part for part in ("©", str(c_year or ""), c_records) if part)
    first_line = f"{p_segment}   {c_segment}. All Rights Reserved."
    second_line = (
        "Unauthorized copying, reproduction, hiring, lending, public performance "
        "and broadcasting prohibited."
    )

    rule = Table(
        [[""]],
        colWidths=[available_width],
        rowHeights=[0.2 * mm],
        style=TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, -1), 1.0, COLOR_HEADER),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        ),
    )
    return KeepTogether(
        [
            Spacer(1, 0.7 * mm),
            rule,
            Spacer(1, 3.2 * mm),
            _paragraph(first_line, styles["rights"]),
            _paragraph(second_line, styles["rights"]),
        ]
    )


# ---------------------------------------------------------------------------
# Footer and deferred total-page-count canvas
# ---------------------------------------------------------------------------
class _NumberedFooterCanvas(pdf_canvas.Canvas):
    def __init__(
        self,
        *args: Any,
        footer_label: str,
        footer_font: str,
        footer_color: colors.Color,
        right_margin: float,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict[str, Any]] = []
        self._footer_label = footer_label
        self._footer_font = footer_font
        self._footer_color = footer_color
        self._footer_right_margin = right_margin

    def showPage(self) -> None:  # noqa: N802 - ReportLab API name
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        page_count = len(self._saved_page_states)
        for page_number, page_state in enumerate(self._saved_page_states, start=1):
            self.__dict__.update(page_state)
            self._draw_total_page_number(page_number, page_count)
            super().showPage()
        super().save()

    def _draw_total_page_number(self, page_number: int, page_count: int) -> None:
        self.saveState()
        self.setFont(self._footer_font, 6.8)
        self.setFillColor(self._footer_color)
        page_width, _ = self._pagesize
        self.drawRightString(
            page_width - self._footer_right_margin,
            FOOTER_Y,
            f"Page {page_number} of {page_count}",
        )
        self.restoreState()


def _footer_on_page(canvas: pdf_canvas.Canvas, document: SimpleDocTemplate) -> None:
    """onPage callback: draws the left footer; total pages are added at save."""
    page_number = canvas.getPageNumber()
    canvas.saveState()
    footer_font = getattr(canvas, "_footer_font", "Helvetica")
    footer_color = getattr(canvas, "_footer_color", COLOR_MUTED)
    footer_label = getattr(canvas, "_footer_label", "Label Copy")
    canvas.setFont(footer_font, 6.8)
    canvas.setFillColor(footer_color)
    canvas.drawString(document.leftMargin, FOOTER_Y, footer_label)
    if page_number == 1:
        title = getattr(canvas, "_document_title", "Label Copy")
        canvas.setTitle(title)
        canvas.setAuthor("Stay Independent")
        canvas.setSubject("Label Copy")
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Public renderer
# ---------------------------------------------------------------------------
def generate_label_copy_pdf(
    data: Mapping[str, Any],
    *,
    font_regular_path: str | os.PathLike[str] | None = None,
    font_bold_path: str | os.PathLike[str] | None = None,
    font_italic_path: str | os.PathLike[str] | None = None,
    allow_font_download: bool = True,
    font_cache_dir: str | os.PathLike[str] | None = None,
) -> io.BytesIO:
    """Renders ``LabelCopyData`` into a rewound in-memory PDF buffer."""
    if not isinstance(data, Mapping):
        raise TypeError("data must be a LabelCopyData mapping")
    tracks = data.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("Το LabelCopyData δεν περιέχει tracks για PDF εξαγωγή.")

    fonts = register_greek_font_family(
        regular_path=font_regular_path,
        bold_path=font_bold_path,
        italic_path=font_italic_path,
        allow_download=allow_font_download,
        cache_dir=font_cache_dir,
    )
    styles = _styles(fonts)

    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=PAGE_SIZE,
        leftMargin=PAGE_LEFT_MARGIN,
        rightMargin=PAGE_RIGHT_MARGIN,
        topMargin=PAGE_TOP_MARGIN,
        bottomMargin=PAGE_BOTTOM_MARGIN,
        title=_clean_text(data.get("project_name")) or "Label Copy",
        author="Stay Independent",
        subject="Label Copy",
        pageCompression=1,
    )

    story: list[Any] = [
        _header_flowable(data, styles, document.width),
        Spacer(1, 6 * mm),
    ]
    for display_number, raw_track in enumerate(tracks, start=1):
        if not isinstance(raw_track, Mapping):
            raise TypeError(f"Το track {display_number} δεν είναι έγκυρο mapping.")
        story.append(
            _track_flowable(
                data,
                raw_track,
                display_number,
                styles,
                document.width,
            )
        )
    story.append(_final_rights_flowable(data, styles, document.width))

    imprint = _clean_text(data.get("label_imprint")) or _clean_text(data.get("company"))
    footer_label = f"{_records_name(imprint)} — Label Copy"
    title = f"Label Copy - {_clean_text(data.get('project_name')) or 'Release'}"

    canvas_factory = partial(
        _NumberedFooterCanvas,
        footer_label=footer_label,
        footer_font=fonts.regular,
        footer_color=COLOR_MUTED,
        right_margin=PAGE_RIGHT_MARGIN,
    )

    try:
        # The onPage callback draws the left footer and records page-local
        # metadata. _NumberedFooterCanvas defers the right-hand Page N of M text
        # until the complete page count is known.
        def on_page(canvas: pdf_canvas.Canvas, doc: SimpleDocTemplate) -> None:
            setattr(canvas, "_document_title", title)
            _footer_on_page(canvas, doc)

        document.build(
            story,
            onFirstPage=on_page,
            onLaterPages=on_page,
            canvasmaker=canvas_factory,
        )
    except (PdfFontError, PdfRenderError):
        raise
    except Exception as exc:
        raise PdfRenderError(f"Αποτυχία δημιουργίας του Label Copy PDF: {exc}") from exc

    output.seek(0)
    if not output.getvalue().startswith(b"%PDF"):
        raise PdfRenderError("Το παραγόμενο αρχείο δεν έχει έγκυρη PDF υπογραφή.")
    return output


def make_label_copy_pdf_filename(
    project_name: Any,
    *,
    issue_date: Any = None,
) -> str:
    """Builds ``LabelCopy_<ASCII title>_<YYYYMMDD>.pdf``."""
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
    return f"LabelCopy_{ascii_title}_{date_token}.pdf"


render_label_copy_pdf = generate_label_copy_pdf


__all__ = [
    "PdfFontError",
    "PdfRenderError",
    "RegisteredFontFamily",
    "format_duration_pdf",
    "generate_label_copy_pdf",
    "make_label_copy_pdf_filename",
    "register_greek_font_family",
    "render_label_copy_pdf",
]
