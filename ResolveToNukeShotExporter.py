#!/usr/bin/env python3
"""Resolve-to-Nuke Shot Exporter.

Install this file in DaVinci Resolve's Fusion/Scripts/Utility directory.  It
uses only Resolve/Fusion's bundled Python APIs at runtime.  The pure planning
and Nuke-writing functions are deliberately kept independent of Resolve so
they can be exercised with the tests in this package.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import importlib
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCRIPT_VERSION = "0.4.7"
TOKENS = {
    "project", "timeline", "sequence", "shot", "shot_index", "track",
    "track_index", "clip", "clip_index", "version", "ext", "frame", "date",
}
INVALID_FILENAME_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')
TOKEN_PATTERN = re.compile(r"\{([a-z_]+)(?::(0?\d+))?\}")

DEFAULT_SCRIPT_TEMPLATE = "Projects/Nuke/{sequence}/{shot}/{shot}_comp_{version}.nk"
DEFAULT_WRITE_TEMPLATE = "Renders/Nuke/{sequence}/{shot}/{shot}_comp_{version}_{frame}.{ext}"
DEFAULT_PLATE_TEMPLATE = "Video/Footage/Transodes/{sequence}/{track}/{shot}_{track}_{clip}_{version}.{frame}.{ext}"

# Resolve exposes numerical enum values for these values.  These names are
# intentionally conservative and match the documented TimelineItem API.
SCALING_METHODS = {
    "Use Project": 0,
    "Crop": 1,
    "Fit": 2,
    "Fill": 3,
    "Stretch": 4,
}
RESIZE_FILTERS = [
    "Use Project", "Sharper", "Smoother", "Bicubic", "Bilinear", "Bessel",
    "Box", "Catmull-Rom", "Cubic", "Gaussian", "Lanczos", "Mitchell",
    "Nearest Neighbor", "Quadratic", "Sinc", "Linear",
]
RESIZE_FILTER_VALUES = {name: index for index, name in enumerate(RESIZE_FILTERS)}
NUKE_WRITE_TYPES = {
    "EXR": ("exr", "exr"),
    "TIFF": ("tiff", "tif"),
    "PNG": ("png", "png"),
    "DPX": ("dpx", "dpx"),
    "QuickTime MOV": ("mov", "mov"),
    "MXF": ("mxf", "mxf"),
}
NUKE_MOV_CODECS = ("Apple ProRes", "H.264", "DNxHR", "Uncompressed")
IMAGE_SEQUENCE_EXTENSIONS = frozenset({
    "ari", "bmp", "cin", "dpx", "exr", "iff", "jpeg", "jpg", "png", "sgi", "tif", "tiff", "tga",
})


class ExportError(RuntimeError):
    """A user-facing preflight or export error."""


@dataclass
class ItemRef:
    """A Resolve-independent description of a timeline item."""

    track_index: int
    track_name: str
    clip_index: int
    name: str
    start: int
    end: int
    media_path: str = ""
    media_id: str = ""
    source_timecode: str = ""
    left_offset: int = 0
    right_offset: int = 0
    source_width: Optional[int] = None
    source_height: Optional[int] = None
    clip_metadata: Dict[str, Any] = field(default_factory=dict)
    timeline_item: Any = field(default=None, repr=False, compare=False)

    @property
    def duration(self) -> int:
        return max(0, self.end - self.start)


@dataclass
class Shot:
    index: int
    name: str
    primary: ItemRef
    sources: List[ItemRef] = field(default_factory=list)

    @property
    def start(self) -> int:
        return self.primary.start

    @property
    def end(self) -> int:
        return self.primary.end


@dataclass
class ExportSettings:
    project_root: str
    sequence: str
    version: str = "v001"
    shot_template: str = "{sequence}_{shot_index:03}"
    script_template: str = DEFAULT_SCRIPT_TEMPLATE
    write_template: str = DEFAULT_WRITE_TEMPLATE
    plate_template: str = DEFAULT_PLATE_TEMPLATE
    write_extension: str = "exr"
    plate_extension: str = "exr"
    write_file_type: str = "exr"
    nuke_write_codec: str = "Apple ProRes"
    nuke_start_frame: int = 1001
    plate_format: str = "exr"
    plate_codec: str = ""
    selected_tracks: List[int] = field(default_factory=list)  # empty = every video track
    shot_track: int = 0  # 0 = lowest-numbered non-empty video track
    export_plates: bool = False
    plate_tracks: List[int] = field(default_factory=list)  # empty = every selected track
    handles: int = 0
    scale_mode: str = "source"  # source | timeline
    scaling_method: str = "Use Project"
    resize_filter: str = "Use Project"
    sidecar_manifest: bool = False
    timecode_track: int = 1
    color_space_tag: str = "Same as Project"
    gamma_tag: str = "Same as Project"
    include_audio: bool = False


@dataclass
class PlannedSource:
    shot: Shot
    item: ItemRef
    first: int
    last: int
    path: Path
    use_plate: bool


@dataclass
class PlannedShot:
    shot: Shot
    script_path: Path
    write_path: Path
    manifest_path: Optional[Path]
    sources: List[PlannedSource]
    timecode: str = ""


def _safe_component(value: Any) -> str:
    """Return a portable path component without silently losing meaning."""
    clean = INVALID_FILENAME_CHARS.sub("_", str(value or "").strip())
    clean = re.sub(r"\s+", "_", clean).strip(". ")
    return clean or "untitled"


def _normalise_token_value(key: str, value: Any) -> str:
    if key in {"project", "timeline", "sequence", "shot", "track", "clip", "version"}:
        return _safe_component(value)
    return str(value)


def expand_tokens(template: str, values: Mapping[str, Any]) -> str:
    """Expand the public token grammar and reject unknown/missing tokens."""
    def replace(match: re.Match[str]) -> str:
        key, width = match.group(1), match.group(2)
        if key not in TOKENS:
            raise ExportError("Unknown token: {%s}" % key)
        if key not in values or values[key] in (None, ""):
            raise ExportError("No value supplied for token: {%s}" % key)
        value = _normalise_token_value(key, values[key])
        if width:
            try:
                return str(int(value)).zfill(int(width))
            except ValueError as exc:
                raise ExportError("Token {%s:%s} needs an integer" % (key, width)) from exc
        return value

    result = TOKEN_PATTERN.sub(replace, template)
    dangling = re.search(r"\{[^}]*\}", result)
    if dangling:
        raise ExportError("Invalid token syntax: %s" % dangling.group(0))
    return result.replace("\\", "/")


def _token_values(settings: ExportSettings, shot: Shot, item: Optional[ItemRef], ext: str) -> Dict[str, Any]:
    item = item or shot.primary
    return {
        "project": Path(settings.project_root).name,
        "timeline": settings.sequence,
        "sequence": settings.sequence,
        "shot": shot.name,
        "shot_index": shot.index,
        "track": item.track_name,
        "track_index": item.track_index,
        "clip": item.name,
        "clip_index": item.clip_index,
        "version": settings.version,
        "ext": ext.lstrip("."),
        "frame": "####",
        "date": date.today().isoformat(),
    }


def _is_image_sequence(extension: str) -> bool:
    """Return whether an output extension conventionally represents frames."""
    return str(extension or "").lower().lstrip(".") in IMAGE_SEQUENCE_EXTENSIONS


def _template_without_frame_token(template: str) -> str:
    """Remove a frame token and its adjoining filename separator for movie outputs."""
    return re.sub(r"[_\-.]?\{frame(?::0?\d+)?\}", "", template)


def source_range(shot: Shot, item: ItemRef, handles: int) -> Tuple[int, int]:
    """Return inclusive timeline frames, clamped to the item's available handles."""
    overlap_start = max(shot.start, item.start)
    overlap_end = min(shot.end, item.end)
    if overlap_end <= overlap_start:
        raise ExportError("%s does not overlap shot %s" % (item.name, shot.name))
    available_start = item.start - max(0, item.left_offset)
    available_end = item.end + max(0, item.right_offset)
    first = max(available_start, overlap_start - max(0, handles))
    last_exclusive = min(available_end, overlap_end + max(0, handles))
    return first, last_exclusive - 1


def _find_timecode_source(shot: Shot, track_index: int) -> ItemRef:
    candidates = [s for s in shot.sources if s.track_index == track_index]
    if candidates:
        # Prefer an item covering the primary edit's first frame, then the earliest.
        return sorted(candidates, key=lambda x: (not (x.start <= shot.start < x.end), x.start))[0]
    return shot.primary


def _parse_tc(tc: str) -> Optional[Tuple[int, int, int, int, str]]:
    match = re.match(r"^(\d\d):(\d\d):(\d\d)([:;])(\d\d)$", str(tc or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(5)), match.group(4)


def add_frames_to_timecode(tc: str, frames: int, fps: float) -> str:
    """Add frame offsets to SMPTE timecode, including 29.97/59.94 drop-frame."""
    parsed = _parse_tc(tc)
    if not parsed:
        return ""
    h, m, s, f, sep = parsed
    nominal = int(round(fps))
    if nominal <= 0:
        return ""
    if sep == ";":
        if nominal not in (30, 60):
            return ""
        dropped = 2 if nominal == 30 else 4
        total_minutes = h * 60 + m
        actual = (((h * 3600 + m * 60 + s) * nominal) + f) - dropped * (total_minutes - total_minutes // 10)
        ten_minutes = nominal * 600 - dropped * 9
        frames_per_day = ten_minutes * 6 * 24
        actual = (actual + frames) % frames_per_day
        chunks, remaining = divmod(actual, ten_minutes)
        display_frames = actual + dropped * 9 * chunks
        if remaining >= dropped:
            display_frames += dropped * ((remaining - dropped) // (nominal * 60 - dropped))
        h, display_frames = divmod(display_frames, nominal * 3600)
        m, display_frames = divmod(display_frames, nominal * 60)
        s, f = divmod(display_frames, nominal)
        return "%02d:%02d:%02d;%02d" % (h, m, s, f)
    total = (((h * 60 + m) * 60 + s) * nominal + f + frames) % (24 * 60 * 60 * nominal)
    h, total = divmod(total, 3600 * nominal)
    m, total = divmod(total, 60 * nominal)
    s, f = divmod(total, nominal)
    return "%02d:%02d:%02d%s%02d" % (h, m, s, sep, f)


def timecode_for_shot(shot: Shot, settings: ExportSettings, fps: float) -> str:
    item = _find_timecode_source(shot, settings.timecode_track)
    first, _ = source_range(shot, item, settings.handles)
    # left_offset is Resolve's available source offset at item record in.
    # The record offset from the item start produces the source-frame offset.
    source_offset = item.left_offset + (first - item.start)
    return add_frames_to_timecode(item.source_timecode, source_offset, fps)


def _resolved_path(root: Path, template: str, values: Mapping[str, Any]) -> Path:
    path = Path(expand_tokens(template, values))
    if path.is_absolute():
        raise ExportError("Output templates must be relative to Project Root: %s" % template)
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ExportError("Template escapes Project Root: %s" % template) from exc
    return candidate


def _resolved_output_path(root: Path, template: str, values: Mapping[str, Any], image_sequence: bool) -> Path:
    """Resolve an output path, omitting frame padding for a single-file format."""
    return _resolved_path(root, template if image_sequence else _template_without_frame_token(template), values)


def build_plan(shots: Sequence[Shot], settings: ExportSettings, fps: float) -> List[PlannedShot]:
    root = Path(settings.project_root).expanduser().resolve()
    if not settings.project_root:
        raise ExportError("Choose a Project Root")
    if not settings.sequence:
        raise ExportError("Sequence is required")
    if not settings.version:
        raise ExportError("Version is required")
    if settings.nuke_start_frame < 0:
        raise ExportError("Nuke start frame must be zero or greater")
    plans: List[PlannedShot] = []
    for shot in shots:
        base = _token_values(settings, shot, shot.primary, settings.write_extension)
        script = _resolved_output_path(root, settings.script_template, base, False)
        write = _resolved_output_path(root, settings.write_template, base, _is_image_sequence(settings.write_extension))
        manifest = script.with_suffix(".json") if settings.sidecar_manifest else None
        sources: List[PlannedSource] = []
        for item in shot.sources:
            first, last = source_range(shot, item, settings.handles)
            use_plate = settings.export_plates and (not settings.plate_tracks or item.track_index in settings.plate_tracks)
            template = settings.plate_template if use_plate else "{clip}"
            if use_plate:
                path = _resolved_output_path(
                    root, template, _token_values(settings, shot, item, settings.plate_extension),
                    _is_image_sequence(settings.plate_extension),
                )
            else:
                if not item.media_path:
                    raise ExportError("No source path for %s in %s" % (item.name, shot.name))
                path = Path(item.media_path)
            sources.append(PlannedSource(shot, item, first, last, path, use_plate))
        if not sources:
            # The primary shot item must always appear, even if no other source overlaps it.
            first, last = source_range(shot, shot.primary, settings.handles)
            sources.append(PlannedSource(shot, shot.primary, first, last, Path(shot.primary.media_path), False))
        plans.append(PlannedShot(shot, script, write, manifest, sources, timecode_for_shot(shot, settings, fps)))
    return plans


def _path_with_frame_glob(path: Path) -> str:
    return re.sub(r"#+", "*", str(path))


def preflight(plans: Sequence[PlannedShot]) -> List[str]:
    """Return all output conflicts; no files or folders are created here."""
    errors: List[str] = []
    seen: Dict[Path, str] = {}
    for plan in plans:
        candidates = [(plan.script_path, "Nuke script"), (plan.write_path, "Nuke Write target")]
        if plan.manifest_path:
            candidates.append((plan.manifest_path, "metadata manifest"))
        candidates += [(s.path, "transcoded plate") for s in plan.sources if s.use_plate]
        for candidate, label in candidates:
            key = candidate.resolve()
            if key in seen:
                errors.append("%s collides with %s: %s" % (label, seen[key], key))
            else:
                seen[key] = label
            if "#" in str(candidate):
                if list(candidate.parent.glob(Path(_path_with_frame_glob(candidate)).name)):
                    errors.append("Existing %s sequence: %s" % (label, candidate))
            elif candidate.exists():
                errors.append("Existing %s: %s" % (label, candidate))
    return errors


def _nuke_quote(value: Any) -> str:
    return str(value).replace("\\", "/").replace("\"", "\\\"").replace("\n", "\\n")


def _nuke_name(value: str, fallback: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not name or name[0].isdigit():
        name = fallback + "_" + name
    return name


def _nuke_read_range(source: PlannedSource) -> Tuple[int, int]:
    """Return the file-native range for a Nuke Read node.

    Transcoded movie plates begin at frame 1 because each export is a new,
    trimmed file. Original movie files expose their whole available source
    range; TimeClip is responsible for selecting the Resolve timeline in/out.
    Image-sequence filenames retain their rendered numbering.
    """
    duration = max(1, source.last - source.first + 1)
    if source.use_plate and not _is_image_sequence(source.path.suffix):
        return 1, duration
    if not source.use_plate and not _is_image_sequence(source.path.suffix):
        full_duration = max(1, source.item.left_offset + source.item.duration + source.item.right_offset)
        return 1, full_duration
    return source.first, source.last


def _primary_source(plan: PlannedShot) -> PlannedSource:
    return next((source for source in plan.sources if source.item is plan.shot.primary), plan.sources[0])


def _nuke_timeclip_range(source: PlannedSource) -> Tuple[int, int]:
    """Return the source frames that TimeClip must retain from a Read node."""
    duration = max(1, source.last - source.first + 1)
    if source.use_plate and not _is_image_sequence(source.path.suffix):
        return 1, duration
    if not source.use_plate and not _is_image_sequence(source.path.suffix):
        source_first = max(1, source.item.left_offset + (source.first - source.item.start) + 1)
        return source_first, source_first + duration - 1
    return source.first, source.last


def _nuke_output_start(plan: PlannedShot, source: PlannedSource, start_frame: int) -> int:
    """Map each source's timeline placement into the Nuke shot range."""
    primary = _primary_source(plan)
    return start_frame + (source.first - primary.first)


def _sticky_text(source: PlannedSource, settings: ExportSettings, colour_context: Mapping[str, Any]) -> str:
    item = source.item
    lines = [
        "Resolve-to-Nuke Shot Exporter %s" % SCRIPT_VERSION,
        "Track: %s (%d)" % (item.track_name, item.track_index),
        "Source: %s" % (item.media_path or source.path),
        "Timeline range: %d-%d" % (source.first, source.last),
        "Source timecode: %s" % (item.source_timecode or "unavailable"),
        "Scale mode: %s" % settings.scale_mode,
        "Scaling: %s / %s" % (settings.scaling_method, settings.resize_filter),
        "Source dimensions: %sx%s" % (item.source_width or "?", item.source_height or "?"),
        "Resolve color pipeline: %s" % colour_context.get("colorScienceMode", "unknown"),
        "Color/Gamma tags: %s / %s" % (settings.color_space_tag, settings.gamma_tag),
    ]
    return "\\n".join(lines)


def _write_text_exclusive(path: Path, content: str) -> None:
    """Create output atomically; an export must never replace another artist's file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError as exc:
        raise ExportError("Refusing to overwrite existing file: %s" % path) from exc


def write_nuke_script(plan: PlannedShot, settings: ExportSettings, colour_context: Mapping[str, Any]) -> None:
    """Write a simple, editable Nuke graph. Caller must preflight first."""
    primary = _primary_source(plan)
    primary_first, primary_last = _nuke_timeclip_range(primary)
    script_first = settings.nuke_start_frame
    script_last = script_first + (primary_last - primary_first)
    lines = [
        "#! /usr/bin/env nuke",
        "# Generated by Resolve-to-Nuke Shot Exporter %s" % SCRIPT_VERSION,
        "Root {",
        " first_frame %d" % script_first,
        " last_frame %d" % script_last,
        "}",
        "",
    ]
    read_names: Dict[int, str] = {}
    output_names: Dict[int, str] = {}
    for ordinal, source in enumerate(plan.sources, 1):
        item = source.item
        read_first, read_last = _nuke_read_range(source)
        clip_first, clip_last = _nuke_timeclip_range(source)
        output_first = _nuke_output_start(plan, source, settings.nuke_start_frame)
        node_name = _nuke_name("Read_%s_%s_%02d" % (item.track_name, item.name, ordinal), "Read")
        timeclip_name = _nuke_name("TimeClip_%s_%s_%02d" % (item.track_name, item.name, ordinal), "TimeClip")
        read_names[id(source)] = node_name
        path = source.path.as_posix()
        lines.extend([
            "Read {",
            " file \"%s\"" % _nuke_quote(path),
            " first %d" % read_first,
            " last %d" % read_last,
            " origfirst %d" % read_first,
            " origlast %d" % read_last,
            " name %s" % node_name,
            " xpos %d" % ((ordinal - 1) * 260),
            " ypos 0",
            "}",
            "set %s [stack 0]" % node_name,
            "push $%s" % node_name,
            "TimeClip {",
            " inputs 1",
            " first %d" % clip_first,
            " last %d" % clip_last,
            " frame_mode \"start at\"",
            " frame %d" % output_first,
            " name %s" % timeclip_name,
            " xpos %d" % ((ordinal - 1) * 260),
            " ypos 160",
            "}",
            "set %s [stack 0]" % timeclip_name,
            "StickyNote {",
            " inputs 0",
            " label \"%s\"" % _nuke_quote(_sticky_text(source, settings, colour_context)),
            " note_font_size 14",
            " xpos %d" % ((ordinal - 1) * 260),
            " ypos -120",
            " name %s" % _nuke_name("Info_%s" % node_name, "Info"),
            "}",
            "",
        ])
        output_names[id(source)] = timeclip_name

    # The primary-shot Read is the default stream to the comp Write. Extra Reads are
    # deliberately left unconnected for the compositor to build the comp.
    primary_name = output_names[id(primary)]
    lines.extend(["# Default output chain begins at the primary-shot TimeClip.", "push $%s" % primary_name])
    if plan.timecode:
        tc_name = "AddTimeCode_%s" % _nuke_name(plan.shot.name, "shot")
        lines.extend([
            "AddTimeCode {",
            " inputs 1",
            " startcode \"%s\"" % _nuke_quote(plan.timecode),
            " useFrame true",
            " frame %d" % script_first,
            " name %s" % tc_name,
            " xpos 0",
            " ypos 220",
            "}",
        ])
    else:
        lines.append("# Source timecode unavailable: AddTimeCode intentionally omitted.")
    lines.extend([
        "Write {",
        " inputs 1",
        " file \"%s\"" % _nuke_quote(plan.write_path.as_posix()),
        " file_type %s" % _nuke_quote(settings.write_file_type),
        *( [" mov64_codec \"%s\"" % _nuke_quote(settings.nuke_write_codec)] if settings.write_file_type == "mov" and settings.nuke_write_codec else [] ),
        " channels rgb",
        " create_directories true",
        " name %s" % _nuke_name("Write_%s" % plan.shot.name, "Write"),
        " xpos 0",
        " ypos 320",
        "}",
        "# %s is the default Write input; additional reads are left for comp setup." % primary_name,
        "",
    ])
    _write_text_exclusive(plan.script_path, "\n".join(lines))


def write_manifest(plan: PlannedShot, settings: ExportSettings, colour_context: Mapping[str, Any]) -> None:
    if not plan.manifest_path:
        return
    payload = {
        "schema": "resolve-to-nuke-shot-exporter/v1",
        "shot": {"index": plan.shot.index, "name": plan.shot.name, "range": [plan.shot.start, plan.shot.end - 1]},
        "script": str(plan.script_path),
        "write_target": str(plan.write_path),
        "timecode": plan.timecode or None,
        "settings": asdict(settings),
        "resolve_color_context": dict(colour_context),
        "sources": [
            {
                "track": s.item.track_name, "track_index": s.item.track_index, "clip": s.item.name,
                "source_path": s.item.media_path, "resolved_path": str(s.path),
                "range": [s.first, s.last], "source_timecode": s.item.source_timecode or None,
                "dimensions": [s.item.source_width, s.item.source_height],
                "metadata": s.item.clip_metadata,
            } for s in plan.sources
        ],
    }
    _write_text_exclusive(plan.manifest_path, json.dumps(payload, indent=2, sort_keys=True, default=str))


def _values(collection: Any) -> List[Any]:
    if not collection:
        return []
    return list(collection.values()) if isinstance(collection, dict) else list(collection)


def _call(obj: Any, name: str, default: Any = None, *args: Any) -> Any:
    method = getattr(obj, name, None)
    if not callable(method):
        return default
    try:
        result = method(*args)
        return default if result is None else result
    except Exception:
        return default


def _clip_property(media: Any) -> Dict[str, Any]:
    props = _call(media, "GetClipProperty", {})
    return dict(props) if isinstance(props, dict) else {}


def _first(props: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = props.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _dimensions(props: Mapping[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    width = _int_or_none(_first(props, "Width", "Resolution Width"))
    height = _int_or_none(_first(props, "Height", "Resolution Height"))
    if width and height:
        return width, height
    match = re.search(r"(\d+)\s*[xX]\s*(\d+)", _first(props, "Resolution"))
    return (int(match.group(1)), int(match.group(2))) if match else (width, height)


def inspect_video_tracks(timeline: Any) -> Dict[int, List[ItemRef]]:
    """Read every valid timeline item without changing the Resolve timeline."""
    count = int(_call(timeline, "GetTrackCount", 0, "video") or 0)
    if count < 1:
        raise ExportError("Current timeline has no video tracks")
    by_track: Dict[int, List[ItemRef]] = {}
    for index in range(1, count + 1):
        track_name = _call(timeline, "GetTrackName", "V%d" % index, "video", index)
        refs: List[ItemRef] = []
        for clip_index, item in enumerate(_values(_call(timeline, "GetItemListInTrack", None, "video", index) or _call(timeline, "GetItemsInTrack", {}, "video", index)), 1):
            start = int(_call(item, "GetStart", 0) or 0)
            end = int(_call(item, "GetEnd", start) or start)
            if end <= start:
                continue
            media = _call(item, "GetMediaPoolItem")
            props = _clip_property(media)
            source_width, source_height = _dimensions(props)
            refs.append(ItemRef(
                track_index=index, track_name=str(track_name), clip_index=clip_index,
                name=str(_call(item, "GetName", "clip_%d" % clip_index)), start=start, end=end,
                media_path=_first(props, "File Path", "FilePath"),
                media_id=str(_call(media, "GetMediaId", "")),
                source_timecode=_first(props, "Start TC", "Start Timecode", "Timecode"),
                left_offset=int(_call(item, "GetLeftOffset", 0) or 0),
                right_offset=int(_call(item, "GetRightOffset", 0) or 0),
                source_width=source_width, source_height=source_height,
                clip_metadata=props, timeline_item=item,
            ))
        by_track[index] = refs
    return by_track


def inspect_timeline(timeline: Any, selected_tracks: Sequence[int], shot_track: int = 0) -> List[Shot]:
    """Build shots from a non-empty primary track and collect overlapping sources.

    ``shot_track`` is one-based when set; zero automatically selects the lowest
    numbered video track containing clips. An empty ``selected_tracks`` includes
    every video track as a source.
    """
    by_track = inspect_video_tracks(timeline)
    all_tracks = sorted(by_track)
    count = len(all_tracks)
    selected = {int(index) for index in selected_tracks if 1 <= int(index) <= count}
    source_tracks = sorted(selected or set(all_tracks))
    if shot_track:
        if shot_track < 1 or shot_track > count:
            raise ExportError("Shot track V%d is outside this timeline's video-track range" % shot_track)
        primary_track = shot_track
    else:
        primary_track = next((index for index in all_tracks if by_track[index]), 0)
    if not primary_track or not by_track[primary_track]:
        label = "Shot track V%d" % shot_track if shot_track else "Timeline"
        raise ExportError("%s has no valid video clips to define shots" % label)
    source_tracks = sorted(set(source_tracks + [primary_track]))
    shots: List[Shot] = []
    for number, primary in enumerate(by_track[primary_track], 1):
        sources = [item for track in source_tracks for item in by_track[track] if item.end > primary.start and item.start < primary.end]
        shots.append(Shot(number, primary.name, primary, sources))
    return shots


def rename_shots(shots: Sequence[Shot], settings: ExportSettings) -> List[str]:
    """Apply a name template to primary shot-track items without blocking duplicates."""
    names: List[str] = []
    for shot in shots:
        name = expand_tokens(settings.shot_template, _token_values(settings, shot, shot.primary, ""))
        names.append(name)
    for shot, name in zip(shots, names):
        item = shot.primary.timeline_item
        success = _call(item, "SetName", False, name)
        if not success:
            success = _call(item, "SetProperty", False, "Clip Name", name)
        if not success:
            raise ExportError("Resolve rejected rename for primary shot %s; Resolve 20.2+ is required" % shot.name)
        shot.name = name
    return names


def rename_timeline_items(
    tracks: Mapping[int, Sequence[ItemRef]], settings: ExportSettings, selected_tracks: Sequence[int],
) -> Tuple[List[str], List[str]]:
    """Rename clips on every requested video track, retaining duplicate-name warnings.

    Numbering intentionally restarts per track so a template can use ``{track}``
    when distinct cross-track names are required.
    """
    available = sorted(tracks)
    chosen = {int(index) for index in selected_tracks if int(index) in tracks}
    selected = sorted(chosen or set(available))
    pending: List[Tuple[ItemRef, str]] = []
    for track_index in selected:
        for item in tracks[track_index]:
            item_shot = Shot(item.clip_index, item.name, item, [item])
            pending.append((item, expand_tokens(settings.shot_template, _token_values(settings, item_shot, item, ""))))
    if not pending:
        raise ExportError("No clips found on the selected rename tracks")
    names = [name for _, name in pending]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    for item, name in pending:
        success = _call(item.timeline_item, "SetName", False, name)
        if not success:
            success = _call(item.timeline_item, "SetProperty", False, "Clip Name", name)
        if not success:
            raise ExportError("Resolve rejected rename for %s on %s; Resolve 20.2+ is required" % (item.name, item.track_name))
        item.name = name
    return ["%s: %s" % (item.track_name, name) for item, name in pending], duplicates


def rename_timeline_items_preview(
    tracks: Mapping[int, Sequence[ItemRef]], settings: ExportSettings, selected_tracks: Sequence[int],
) -> Tuple[List[str], List[str]]:
    """Return the all-track rename preview and duplicate-name warnings without mutation."""
    chosen = {int(index) for index in selected_tracks if int(index) in tracks}
    selected = sorted(chosen or set(tracks))
    pending: List[Tuple[ItemRef, str]] = []
    for track_index in selected:
        for item in tracks[track_index]:
            item_shot = Shot(item.clip_index, item.name, item, [item])
            pending.append((item, expand_tokens(settings.shot_template, _token_values(settings, item_shot, item, ""))))
    if not pending:
        raise ExportError("No clips found on the selected rename tracks")
    names = [name for _, name in pending]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    return ["%s  %03d  %s" % (item.track_name, item.clip_index, name) for item, name in pending], duplicates


def resolve_version_tuple(resolve: Any) -> Tuple[int, int, int]:
    """Normalise Resolve's documented GetVersion() list for feature checks."""
    version = _call(resolve, "GetVersion", [])
    values: List[int] = []
    for value in _values(version)[:3]:
        try:
            values.append(int(value))
        except (TypeError, ValueError):
            values.append(0)
    return tuple((values + [0, 0, 0])[:3])  # type: ignore[return-value]


def supports_timeline_item_rename(resolve: Any) -> bool:
    # Blackmagic added scripting support for setting Timeline clip names in
    # Resolve 20.2. Earlier releases expose only GetName().
    return resolve_version_tuple(resolve) >= (20, 2, 0)


class ResolveExporter:
    """Resolve adapter. All mutations happen only after preflight succeeds."""

    def __init__(self, resolve: Any, project: Any, timeline: Any):
        self.resolve, self.project, self.timeline = resolve, project, timeline

    def colour_context(self) -> Dict[str, Any]:
        settings = _call(self.project, "GetSetting", {})
        timeline_settings = _call(self.timeline, "GetSetting", {})
        wanted = ("colorScienceMode", "timelineFrameRate", "timelineResolutionWidth", "timelineResolutionHeight",
                  "colorSpaceTimeline", "colorSpaceOutput", "colorSpaceInput")
        return {key: settings.get(key, timeline_settings.get(key, "")) for key in wanted if settings.get(key, timeline_settings.get(key, "")) != ""}

    def fps(self) -> float:
        value = _call(self.timeline, "GetSetting", "", "timelineFrameRate") or _call(self.project, "GetSetting", "", "timelineFrameRate")
        try:
            return float(str(value).replace(" DF", ""))
        except ValueError:
            return 24.0

    def render_options(self) -> Dict[str, Dict[str, Any]]:
        formats = _call(self.project, "GetRenderFormats", {})
        result: Dict[str, Dict[str, Any]] = {}
        if isinstance(formats, dict):
            # GetRenderFormats returns display name -> Resolve format ID. The
            # format ID, not the display name, is required by GetRenderCodecs.
            for label, format_id in formats.items():
                codecs = _call(self.project, "GetRenderCodecs", {}, str(format_id))
                result[str(label)] = {
                    "format": str(format_id),
                    "extension": str(format_id),
                    "codecs": dict(codecs) if isinstance(codecs, dict) else {},
                }
        return result

    def render_isolated_plate(self, source: PlannedSource, settings: ExportSettings) -> None:
        """Queue and wait for one source-only render using a disposable timeline.

        Resolve's public API has no per-item render setting. Duplicating the
        timeline and disabling non-target items keeps the user timeline intact.
        """
        existing = list(source.path.parent.glob(Path(_path_with_frame_glob(source.path)).name)) if "#" in str(source.path) else [source.path] if source.path.exists() else []
        if existing:
            raise ExportError("Refusing to overwrite existing plate: %s" % source.path)
        duplicate = _call(self.timeline, "DuplicateTimeline", None, "__RTN_%s_%s" % (source.shot.name, source.item.clip_index))
        if not duplicate:
            raise ExportError("Could not create temporary timeline for %s" % source.item.name)
        media_pool = _call(self.project, "GetMediaPool")
        old_timeline = self.timeline
        try:
            if not _call(self.project, "SetCurrentTimeline", False, duplicate):
                raise ExportError("Could not activate temporary render timeline")
            self._isolate_duplicate(duplicate, source.item, settings)
            if settings.scale_mode == "timeline":
                target_settings = self._timeline_render_dimensions(old_timeline)
            else:
                target_settings = self._source_render_dimensions(source.item)
            render_settings = {
                "MarkIn": source.first, "MarkOut": source.last,
                "TargetDir": str(source.path.parent), "CustomName": source.path.name.split(".")[0],
                "ExportVideo": True, "ExportAudio": False,
                "ColorSpaceTag": settings.color_space_tag, "GammaTag": settings.gamma_tag,
                **target_settings,
            }
            source.path.parent.mkdir(parents=True, exist_ok=True)
            _call(self.project, "SetCurrentRenderFormatAndCodec", False, settings.plate_format, settings.plate_codec)
            if not _call(self.project, "SetRenderSettings", False, render_settings):
                raise ExportError("Resolve rejected render settings for %s" % source.item.name)
            job_id = _call(self.project, "AddRenderJob", "")
            if not job_id or not _call(self.project, "StartRendering", False, [job_id]):
                raise ExportError("Could not start render for %s" % source.item.name)
            while _call(self.project, "IsRenderingInProgress", False):
                time.sleep(0.25)
            status = _call(self.project, "GetRenderJobStatus", {}, job_id)
            if isinstance(status, dict) and str(status.get("JobStatus", "")).lower() not in {"complete", "completed", ""}:
                raise ExportError("Render failed for %s: %s" % (source.item.name, status))
        finally:
            _call(self.project, "SetCurrentTimeline", False, old_timeline)
            if media_pool and duplicate:
                _call(media_pool, "DeleteTimelines", False, [duplicate])

    @staticmethod
    def _isolate_duplicate(timeline: Any, target: ItemRef, settings: ExportSettings) -> None:
        for track in range(1, int(_call(timeline, "GetTrackCount", 0, "video") or 0) + 1):
            items = _values(_call(timeline, "GetItemListInTrack", None, "video", track) or _call(timeline, "GetItemsInTrack", {}, "video", track))
            for item in items:
                same = (track == target.track_index and int(_call(item, "GetStart", -1) or -1) == target.start and int(_call(item, "GetEnd", -1) or -1) == target.end)
                _call(item, "SetClipEnabled", False, same)
                if same:
                    _call(item, "SetProperty", False, "Scaling", SCALING_METHODS.get(settings.scaling_method, 0))
                    _call(item, "SetProperty", False, "ResizeFilter", RESIZE_FILTER_VALUES.get(settings.resize_filter, 0))

    @staticmethod
    def _timeline_render_dimensions(timeline: Any) -> Dict[str, Any]:
        values = _call(timeline, "GetSetting", {})
        return {"FormatWidth": _int_or_none(values.get("timelineResolutionWidth")) or 0,
                "FormatHeight": _int_or_none(values.get("timelineResolutionHeight")) or 0}

    @staticmethod
    def _source_render_dimensions(item: ItemRef) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        if item.source_width:
            result["FormatWidth"] = item.source_width
        if item.source_height:
            result["FormatHeight"] = item.source_height
        return result

    def export(self, settings: ExportSettings) -> List[PlannedShot]:
        shots = inspect_timeline(self.timeline, settings.selected_tracks, settings.shot_track)
        plans = build_plan(shots, settings, self.fps())
        collisions = preflight(plans)
        if collisions:
            raise ExportError("Export blocked; no files were written:\n- " + "\n- ".join(collisions))
        for plan in plans:
            for source in plan.sources:
                if source.use_plate:
                    self.render_isolated_plate(source, settings)
            write_nuke_script(plan, settings, self.colour_context())
            write_manifest(plan, settings, self.colour_context())
        return plans


def _preset_path() -> Path:
    """Use the per-user config location; the Resolve install can be read-only."""
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", str(Path.home())))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "ResolveToNukeShotExporter" / "presets.json"


def load_presets() -> Dict[str, Dict[str, Any]]:
    try:
        return json.loads(_preset_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_preset(name: str, settings: ExportSettings) -> None:
    presets = load_presets()
    presets[_safe_component(name)] = asdict(settings)
    path = _preset_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(presets, indent=2, sort_keys=True), encoding="utf-8")


def _resolve_scripting_module_paths() -> List[Path]:
    """Known module locations for external Python fallback on all platforms."""
    paths: List[Path] = []
    configured = os.environ.get("RESOLVE_SCRIPT_API")
    if configured:
        paths.extend([Path(configured) / "Modules", Path(configured)])
    if sys.platform == "darwin":
        paths.append(Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"))
    elif sys.platform.startswith("win"):
        program_data = os.environ.get("PROGRAMDATA", r"C:\\ProgramData")
        paths.append(Path(program_data) / "Blackmagic Design" / "DaVinci Resolve" / "Support" / "Developer" / "Scripting" / "Modules")
    else:
        paths.extend([Path("/opt/resolve/Developer/Scripting/Modules"), Path("/home/resolve/Developer/Scripting/Modules")])
    return paths


def _resolve_api() -> Tuple[Any, Any, Any, Any]:
    """Connect in Scripts-menu, console, and external-Python environments.

    Scripts launched through Workspace > Scripts do not always put
    DaVinciResolveScript on ``sys.path``.  Their supported bridge is Fusion's
    already-loaded ``bmd.scriptapp`` object, so that route is intentionally
    attempted first.
    """
    resolve = globals().get("resolve")
    project = globals().get("project")
    fusion = globals().get("fusion") or globals().get("fu")
    bmd_module = globals().get("bmd")
    if bmd_module is None:
        try:
            bmd_module = importlib.import_module("bmd")
        except ImportError:
            bmd_module = None
    if bmd_module is not None:
        resolve = resolve or _call(bmd_module, "scriptapp", None, "Resolve")
        fusion = fusion or _call(bmd_module, "scriptapp", None, "Fusion")
    if resolve is None:
        for module_path in _resolve_scripting_module_paths():
            if module_path.is_dir() and str(module_path) not in sys.path:
                sys.path.insert(0, str(module_path))
        try:
            dvr = importlib.import_module("DaVinciResolveScript")
            resolve = _call(dvr, "scriptapp", None, "Resolve")
        except ImportError:
            resolve = None
    if not resolve:
        raise ExportError("Could not connect to Resolve. Launch from Workspace > Scripts, or set RESOLVE_SCRIPT_API for external Python.")
    # These are Resolve's documented Script/Workflow Integration entrypoints:
    # Resolve provides resolve/project in workflow scripts; for regular scripts
    # the current project and Fusion object are reached from Resolve itself.
    project = project or _call(_call(resolve, "GetProjectManager"), "GetCurrentProject")
    fusion = fusion or _call(resolve, "Fusion")
    timeline = _call(project, "GetCurrentTimeline")
    if not resolve or not project or not timeline:
        raise ExportError("Open a project and timeline before launching the exporter")
    if not fusion or not getattr(fusion, "UIManager", None) or bmd_module is None:
        raise ExportError("Resolve connected, but its Fusion UI Manager is unavailable. Run from Resolve's Scripts menu, not an external shell.")
    try:
        return resolve, project, timeline, (fusion, fusion.UIManager, bmd_module.UIDispatcher(fusion.UIManager))
    except Exception as exc:
        raise ExportError("Resolve Fusion UI Manager is unavailable: %s" % exc) from exc


def _ui_settings(items: Mapping[str, Any], render_options: Optional[Mapping[str, Mapping[str, Any]]] = None) -> ExportSettings:
    def text(key: str, fallback: str = "") -> str:
        if key not in items:
            return fallback
        value = getattr(items[key], "Text", "") or getattr(items[key], "CurrentText", "")
        return str(value).strip() or fallback
    def checked(key: str, fallback: bool = False) -> bool:
        return bool(items[key].Checked) if key in items else fallback
    tracks = [int(part) for part in re.findall(r"\d+", text("tracks"))]
    plate_tracks = [int(part) for part in re.findall(r"\d+", text("plate_tracks", ""))]
    shot_track_values = re.findall(r"\d+", text("shot_track", ""))
    tc_values = re.findall(r"\d+", text("tc_track", "1"))
    plate_label = text("plate_format", "EXR")
    plate_spec = (render_options or {}).get(plate_label, {})
    plate_format = str(plate_spec.get("format", plate_label))
    plate_codec = str(plate_spec.get("codecs", {}).get(text("plate_codec"), text("plate_codec")))
    write_label = text("write_file_type", "EXR")
    write_file_type, write_extension = NUKE_WRITE_TYPES.get(write_label, (write_label.lower(), text("write_ext", "exr")))
    return ExportSettings(
        project_root=text("root"), sequence=text("sequence"), version=text("version", "v001"),
        shot_template=text("shot_template", "{sequence}_{shot_index:03}"),
        script_template=text("script_template", DEFAULT_SCRIPT_TEMPLATE),
        write_template=text("write_template", DEFAULT_WRITE_TEMPLATE),
        plate_template=text("plate_template", DEFAULT_PLATE_TEMPLATE),
        write_extension=write_extension, plate_extension=text("plate_ext", "exr"),
        write_file_type=write_file_type, nuke_write_codec=text("nuke_write_codec", "Apple ProRes"),
        nuke_start_frame=int(text("nuke_start_frame", "1001")), plate_format=plate_format,
        plate_codec=plate_codec, selected_tracks=tracks, shot_track=int(shot_track_values[-1]) if shot_track_values else 0,
        export_plates=checked("export_plates"), plate_tracks=plate_tracks,
        handles=int(text("handles", "0") or 0), scale_mode=text("scale_mode", "source"),
        scaling_method=text("scaling_method", "Use Project"), resize_filter=text("resize_filter", "Use Project"),
        sidecar_manifest=checked("sidecar"), timecode_track=int(tc_values[-1]) if tc_values else 1,
        color_space_tag=text("color_tag", "Same as Project"), gamma_tag=text("gamma_tag", "Same as Project"),
    )


def show_ui(resolve: Any, project: Any, timeline: Any, fusion: Any, ui: Any, dispatcher: Any) -> None:
    """Show a compact, consistently aligned Fusion UI Manager dialog."""
    exporter = ResolveExporter(resolve, project, timeline)
    label_width = 130
    field_width = 240
    token_help = """TOKENS

{project}  project name
{timeline}  timeline name
{sequence}  sequence field
{shot}  shot/clip name
{shot_index:03}  per-track clip number
{track} / {track_index}
{clip} / {clip_index}
{version}, {ext}, {date}
{frame}  #### for image sequences; omitted for MOV/MXF

Paths are relative to Project Root. Use {track} in rename templates when names must be unique across tracks."""

    def section(text: str) -> Any:
        return ui.Label({"Text": text, "Weight": 0, "StyleSheet": "font-weight: bold; font-size: 14px"})

    def row(label: str, controls: Sequence[Any]) -> Any:
        return ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.Label({"Text": label, "MinimumSize": [label_width, 0], "Weight": 0}), *controls,
        ])

    field = lambda options: ui.LineEdit({"MinimumSize": [field_width, 0], "Weight": 1, **options})
    combo = lambda options: ui.ComboBox({"MinimumSize": [field_width, 0], "Weight": 1, **options})
    window = dispatcher.AddWindow({"ID": "RTNShotExporter", "WindowTitle": "Resolve to Nuke Shot Exporter", "Geometry": [120, 80, 1320, 840]}, [
        ui.VGroup({"Spacing": 8}, [
            ui.Label({"Text": "Resolve to Nuke Shot Exporter", "Weight": 0, "StyleSheet": "font-size: 18px; font-weight: bold"}),
            row("Sequence", [field({"ID": "sequence", "Text": _call(timeline, "GetName", "sequence")})]),
            ui.TabBar({"ID": "tabs", "CurrentIndex": 0, "Weight": 0}),
            ui.HGroup({"Weight": 0, "Spacing": 14}, [
                # Do not use ui.Stack here.  In Resolve's embedded UIManager it
                # can initially paint every child page on top of the selected
                # one.  A regular layout plus explicit Hidden states is stable.
                ui.VGroup({"ID": "pages", "Spacing": 0, "Weight": 1}, [
                    ui.VGroup({"ID": "rename_page", "Spacing": 8, "Weight": 1}, [
                        section("Rename timeline clips"),
                        ui.Label({"Text": "Blank tracks means every video track. Duplicate names are allowed and reported as a warning.", "Weight": 0}),
                        row("Rename tracks", [field({"ID": "rename_tracks", "PlaceholderText": "blank = all video tracks; e.g. 1,2,4"})]),
                        row("Name template", [field({"ID": "shot_template", "Text": "{sequence}_{shot_index:03}"})]),
                        ui.HGroup({"Weight": 0}, [ui.Button({"ID": "preview_rename", "Text": "Preview Rename", "MinimumSize": [140, 0]}), ui.Button({"ID": "apply_rename", "Text": "Rename Clips", "MinimumSize": [140, 0]}), ui.HGap(0, 1)]),
                    ]),
                    ui.HGroup({"ID": "export_page", "Spacing": 14, "Weight": 1, "Hidden": True}, [
                        ui.VGroup({"Spacing": 7, "Weight": 1}, [
                            section("Shot and source selection"),
                            row("Project Root", [field({"ID": "root", "PlaceholderText": "/show/project"}), ui.Button({"ID": "choose_root", "Text": "Choose…", "MinimumSize": [95, 0], "Weight": 0})]),
                            row("Version", [field({"ID": "version", "Text": "v001"})]),
                            row("Shot track", [combo({"ID": "shot_track"})]),
                            row("Source tracks", [field({"ID": "tracks", "PlaceholderText": "blank = all video tracks; e.g. 1,2,4"})]),
                            row("Handles", [field({"ID": "handles", "Text": "0"})]),
                            row("Write timecode", [combo({"ID": "tc_track"})]),
                            section("Isolated source plates"),
                            row("Export plates", [ui.CheckBox({"ID": "export_plates", "Text": "Render isolated source plates", "Weight": 0}), ui.CheckBox({"ID": "sidecar", "Text": "Write metadata JSON", "Weight": 0}), ui.HGap(0, 1)]),
                            row("Plate tracks", [field({"ID": "plate_tracks", "PlaceholderText": "blank = every source track"})]),
                            row("Resolve format", [combo({"ID": "plate_format"})]),
                            row("Resolve codec", [combo({"ID": "plate_codec"})]),
                            row("Plate extension", [field({"ID": "plate_ext", "ReadOnly": True})]),
                            row("Scale mode", [combo({"ID": "scale_mode"})]),
                            row("Scaling method", [combo({"ID": "scaling_method"})]),
                            row("Resize filter", [combo({"ID": "resize_filter"})]),
                        ]),
                        ui.VGroup({"Spacing": 7, "Weight": 1}, [
                            section("Nuke Write node"),
                            row("Write file type", [combo({"ID": "write_file_type"})]),
                        row("Write extension", [field({"ID": "write_ext", "ReadOnly": True})]),
                        row("Nuke start frame", [field({"ID": "nuke_start_frame", "Text": "1001"})]),
                        row("Nuke MOV codec", [combo({"ID": "nuke_write_codec"})]),
                            row("Colour tag", [field({"ID": "color_tag", "Text": "Same as Project"})]),
                            row("Gamma tag", [field({"ID": "gamma_tag", "Text": "Same as Project"})]),
                            section("Output templates"),
                            row("Nuke script", [field({"ID": "script_template", "Text": DEFAULT_SCRIPT_TEMPLATE})]),
                            row("Nuke Write target", [field({"ID": "write_template", "Text": DEFAULT_WRITE_TEMPLATE})]),
                            row("Transcoded plate", [field({"ID": "plate_template", "Text": DEFAULT_PLATE_TEMPLATE})]),
                            section("Preset and actions"),
                            row("Preset", [field({"ID": "preset_name", "Text": "default"}), ui.Button({"ID": "load_preset", "Text": "Load", "MinimumSize": [70, 0], "Weight": 0}), ui.Button({"ID": "save_preset", "Text": "Save", "MinimumSize": [70, 0], "Weight": 0})]),
                            ui.HGroup({"Weight": 0}, [ui.HGap(0, 1), ui.Button({"ID": "preview_export", "Text": "Preflight", "MinimumSize": [130, 0]}), ui.Button({"ID": "run_export", "Text": "Export Shots", "MinimumSize": [130, 0]})]),
                        ]),
                    ]),
                ]),
                ui.VGroup({"MinimumSize": [290, 0], "MaximumSize": [340, 16777215], "Spacing": 5, "Weight": 0}, [
                    section("Token reference"),
                    ui.TextEdit({"ID": "token_reference", "ReadOnly": True, "PlainText": token_help, "MinimumSize": [290, 500], "Weight": 1}),
                ]),
            ]),
            section("Status"),
            ui.TextEdit({"ID": "status", "ReadOnly": True, "PlainText": "Ready. Preview Rename or Preflight before applying changes.", "MinimumSize": [0, 160], "Weight": 1}),
            ui.HGroup({"Weight": 0}, [ui.HGap(0, 1), ui.Button({"ID": "close", "Text": "Close", "MinimumSize": [110, 0], "Weight": 0})]),
        ])
    ])
    items = window.GetItems()
    for label in ("Rename", "Export"):
        items["tabs"].AddTab(label)
    # A TabBar has no valid selection until its tabs have been added.
    items["tabs"].CurrentIndex = 0
    if not supports_timeline_item_rename(resolve):
        items["apply_rename"].Enabled = False
        items["status"].PlainText = "Rename unavailable: Resolve 20.2 or later is required. Export remains available."
    for label in ("source", "timeline"):
        items["scale_mode"].AddItem(label)
    for label in SCALING_METHODS:
        items["scaling_method"].AddItem(label)
    for label in RESIZE_FILTERS:
        items["resize_filter"].AddItem(label)
    items["scale_mode"].CurrentText = "source"
    items["scaling_method"].CurrentText = "Use Project"
    items["resize_filter"].CurrentText = "Use Project"
    items["shot_track"].AddItem("Auto (lowest non-empty)")
    for index in range(1, int(_call(timeline, "GetTrackCount", 1, "video") or 1) + 1):
        track_label = "%s (%d)" % (_call(timeline, "GetTrackName", "V%d" % index, "video", index), index)
        items["shot_track"].AddItem(track_label)
        items["tc_track"].AddItem(track_label)
    items["shot_track"].CurrentIndex = 0
    items["tc_track"].CurrentIndex = 0
    render_options = exporter.render_options()
    for fmt in sorted(render_options):
        items["plate_format"].AddItem(fmt)
    if not render_options:
        items["plate_format"].AddItem("exr")
    for label in NUKE_WRITE_TYPES:
        items["write_file_type"].AddItem(label)

    def refresh_codecs(ev: Any = None) -> None:
        fmt = str(getattr(items["plate_format"], "CurrentText", ""))
        specification = render_options.get(fmt, {})
        items["plate_codec"].Clear()
        for codec_description in sorted(specification.get("codecs", {})):
            items["plate_codec"].AddItem(codec_description)
        if not specification.get("codecs"):
            items["plate_codec"].AddItem("")
        items["plate_ext"].Text = str(specification.get("extension", fmt)).lstrip(".")

    def refresh_write_options(ev: Any = None) -> None:
        label = str(getattr(items["write_file_type"], "CurrentText", "EXR"))
        file_type, extension = NUKE_WRITE_TYPES.get(label, ("exr", "exr"))
        items["write_ext"].Text = extension
        items["nuke_write_codec"].Clear()
        if file_type == "mov":
            items["nuke_write_codec"].Enabled = True
            for codec in NUKE_MOV_CODECS:
                items["nuke_write_codec"].AddItem(codec)
            items["nuke_write_codec"].CurrentText = "Apple ProRes"
        else:
            items["nuke_write_codec"].Enabled = False
            items["nuke_write_codec"].AddItem("Not applicable")

    items["write_file_type"].CurrentText = "EXR"
    if "EXR" in render_options:
        items["plate_format"].CurrentText = "EXR"
    refresh_codecs()
    refresh_write_options()

    def status(message: str) -> None:
        items["status"].PlainText = message

    def parse_track_list(control: str) -> List[int]:
        value = str(getattr(items[control], "Text", "") or "")
        return [int(part) for part in re.findall(r"\d+", value)]

    def shots_from_ui() -> Tuple[ExportSettings, List[Shot]]:
        settings = _ui_settings(items, render_options)
        return settings, inspect_timeline(timeline, settings.selected_tracks, settings.shot_track)

    def rename_preview(ev: Any = None) -> None:
        try:
            settings = _ui_settings(items, render_options)
            tracks = inspect_video_tracks(timeline)
            selected = parse_track_list("rename_tracks")
            preview, duplicates = rename_timeline_items_preview(tracks, settings, selected)
            warning = "\n\nWARNING — duplicate names: " + ", ".join(duplicates) if duplicates else "\n\nNo duplicate names."
            status("RENAME PREVIEW\n" + "\n".join(preview) + warning)
        except Exception as exc:
            status("Preview error: %s" % exc)

    def rename_apply(ev: Any = None) -> None:
        try:
            settings = _ui_settings(items, render_options)
            renamed, duplicates = rename_timeline_items(inspect_video_tracks(timeline), settings, parse_track_list("rename_tracks"))
            warning = "\nWARNING — duplicate names retained: " + ", ".join(duplicates) if duplicates else ""
            status("Renamed %d clips:\n%s%s" % (len(renamed), "\n".join(renamed), warning))
        except Exception as exc:
            status("Rename failed: %s" % exc)

    def export_preview(ev: Any = None) -> None:
        try:
            settings, shots = shots_from_ui()
            plans = build_plan(shots, settings, exporter.fps())
            errors = preflight(plans)
            body = []
            for plan in plans:
                body.append("%s\n  script: %s\n  write: %s" % (plan.shot.name, plan.script_path, plan.write_path))
                body.extend("  read: %s" % src.path for src in plan.sources)
            status(("PRE-FLIGHT BLOCKED:\n- " + "\n- ".join(errors) if errors else "PRE-FLIGHT OK") + "\n\n" + "\n".join(body))
        except Exception as exc:
            status("Preflight error: %s" % exc)

    def export_run(ev: Any = None) -> None:
        try:
            plans = exporter.export(_ui_settings(items, render_options))
            status("Completed %d shot exports.\n%s" % (len(plans), "\n".join(str(plan.script_path) for plan in plans)))
        except Exception as exc:
            status("Export failed: %s" % exc)

    def preset_save(ev: Any = None) -> None:
        try:
            name = str(getattr(items["preset_name"], "Text", "default") or "default")
            save_preset(name, _ui_settings(items, render_options))
            status("Saved preset '%s' to %s" % (name, _preset_path()))
        except Exception as exc:
            status("Could not save preset: %s" % exc)

    def preset_load(ev: Any = None) -> None:
        try:
            name = str(getattr(items["preset_name"], "Text", "default") or "default")
            data = load_presets().get(_safe_component(name))
            if not data:
                raise ExportError("No preset named '%s'" % name)
            fields = {
                "project_root": "root", "sequence": "sequence", "version": "version", "shot_template": "shot_template",
                "script_template": "script_template", "write_template": "write_template", "plate_template": "plate_template",
                "handles": "handles", "scaling_method": "scaling_method", "resize_filter": "resize_filter",
                "color_space_tag": "color_tag", "gamma_tag": "gamma_tag", "nuke_start_frame": "nuke_start_frame",
            }
            for source, control in fields.items():
                if source in data:
                    items[control].CurrentText = str(data[source]) if hasattr(items[control], "CurrentText") else str(data[source])
                    if not hasattr(items[control], "CurrentText"):
                        items[control].Text = str(data[source])
            items["tracks"].Text = ",".join(str(value) for value in data.get("selected_tracks", []))
            items["plate_tracks"].Text = ",".join(str(value) for value in data.get("plate_tracks", []))
            items["shot_track"].CurrentIndex = max(0, int(data.get("shot_track", 0)))
            items["export_plates"].Checked = bool(data.get("export_plates", False))
            items["sidecar"].Checked = bool(data.get("sidecar_manifest", False))
            items["scale_mode"].CurrentText = str(data.get("scale_mode", "source"))
            format_label = next((label for label, spec in render_options.items() if spec.get("format") == str(data.get("plate_format", ""))), "EXR")
            items["plate_format"].CurrentText = format_label
            refresh_codecs()
            saved_codec = str(data.get("plate_codec", ""))
            items["plate_codec"].CurrentText = next((label for label, codec_id in render_options.get(format_label, {}).get("codecs", {}).items() if codec_id == saved_codec), "")
            items["write_file_type"].CurrentText = next((label for label, spec in NUKE_WRITE_TYPES.items() if spec[0] == data.get("write_file_type")), "EXR")
            refresh_write_options()
            items["nuke_write_codec"].CurrentText = str(data.get("nuke_write_codec", "Apple ProRes"))
            items["tc_track"].CurrentIndex = max(0, int(data.get("timecode_track", 1)) - 1)
            status("Loaded preset '%s'." % name)
        except Exception as exc:
            status("Could not load preset: %s" % exc)

    def choose_root(ev: Any = None) -> None:
        path = _call(fusion, "RequestDir", "")
        if path:
            items["root"].Text = str(path)

    def tab_changed(ev: Any = None) -> None:
        show_rename = items["tabs"].CurrentIndex == 0
        items["rename_page"].Hidden = not show_rename
        items["export_page"].Hidden = show_rename
        window.RecalcLayout()

    window.On.preview_rename.Clicked = rename_preview
    window.On.apply_rename.Clicked = rename_apply
    window.On.preview_export.Clicked = export_preview
    window.On.run_export.Clicked = export_run
    window.On.save_preset.Clicked = preset_save
    window.On.load_preset.Clicked = preset_load
    window.On.choose_root.Clicked = choose_root
    window.On.plate_format.CurrentIndexChanged = refresh_codecs
    window.On.write_file_type.CurrentIndexChanged = refresh_write_options
    window.On.tabs.CurrentChanged = tab_changed
    window.On.close.Clicked = lambda ev=None: dispatcher.ExitLoop()
    window.On.RTNShotExporter.Close = lambda ev=None: dispatcher.ExitLoop()
    window.Show()
    # Apply explicit visibility after UIManager has realized the window.
    items["tabs"].CurrentIndex = 0
    tab_changed()
    dispatcher.RunLoop()
    window.Hide()


def main() -> None:
    try:
        resolve, project, timeline, ui_parts = _resolve_api()
        show_ui(resolve, project, timeline, *ui_parts)
    except ExportError as exc:
        print("Resolve-to-Nuke Shot Exporter: %s" % exc)


if __name__ == "__main__":
    main()
