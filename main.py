from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SUPPORTED_EXTENSIONS = {".mp4", ".mov"}
AUDIO_SR = 16000


@dataclass
class VideoInfo:
    path: Path
    width: int
    height: int
    duration: float
    fps: float
    video_bitrate: int | None


@dataclass
class QualityProfile:
    crf: int
    preset: str


@dataclass
class PairOffsetResult:
    shift_samples: int
    used_strict: bool
    score: float


@dataclass
class AlignmentPlan:
    offsets: list[int]
    start_times: list[float]
    trimmed_duration: float
    strict_all: bool
    reference_index: int
    method: str
    avg_pair_score: float
    min_pair_score: float


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def ffprobe_json(path: Path) -> dict:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )
    return json.loads(result.stdout)


def get_video_info(path: Path) -> VideoInfo:
    data = ffprobe_json(path)
    video_stream = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise ValueError(f"video stream not found: {path}")
    duration = video_stream.get("duration") or data.get("format", {}).get("duration")
    if duration is None:
        raise ValueError(f"duration not found: {path}")
    bitrate = video_stream.get("bit_rate")
    fps_text = video_stream.get("avg_frame_rate", "0/1")
    try:
        num, den = fps_text.split("/")
        fps = float(num) / float(den) if float(den) != 0 else 0.0
    except Exception:
        fps = 0.0
    return VideoInfo(
        path=path,
        width=int(video_stream["width"]),
        height=int(video_stream["height"]),
        duration=float(duration),
        fps=fps,
        video_bitrate=int(bitrate) if bitrate else None,
    )


def discover_videos(input_dir: Path) -> list[Path]:
    files = [p for p in sorted(input_dir.iterdir()) if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS]
    if not files:
        raise ValueError(f"no video files found in {input_dir}")
    return files


def load_layout_tsv(
    layout_file: Path | None,
    available_paths: list[Path],
) -> tuple[list[Path], int, int, list[int]]:
    sorted_paths = sorted(available_paths, key=lambda p: p.name)
    if layout_file is None:
        # Default: file name order in a single row.
        return sorted_paths, len(sorted_paths), 1, list(range(len(sorted_paths)))

    if not layout_file.exists():
        raise ValueError(f"layout file not found: {layout_file}")
    name_to_path = {p.name: p for p in available_paths}
    text = layout_file.read_text(encoding="utf-8")
    lower_name = layout_file.name.lower()
    if lower_name.endswith(".tsv"):
        delimiter = "\t"
    elif lower_name.endswith(".csv"):
        delimiter = ","
    else:
        first_line = text.splitlines()[0] if text.splitlines() else ""
        delimiter = "\t" if first_line.count("\t") >= first_line.count(",") else ","

    reader = csv.reader(text.splitlines(), delimiter=delimiter)
    rows: list[list[str]] = []
    for row in reader:
        cols = [c.strip() for c in row]
        if any(c != "" for c in cols):
            rows.append(cols)
    if not rows:
        raise ValueError(f"layout file is empty: {layout_file}")

    max_cols = max(len(r) for r in rows)
    ordered_paths: list[Path] = []
    cell_indices: list[int] = []
    seen: set[str] = set()
    for r, row in enumerate(rows):
        for c in range(max_cols):
            token = row[c] if c < len(row) else ""
            if token == "":
                continue
            if "/" in token or "\\" in token:
                raise ValueError(f"layout file accepts file names only (no path): {token}")
            if token not in name_to_path:
                raise ValueError(f"file in layout not found in input-dir: {token}")
            if token in seen:
                raise ValueError(f"duplicate file name in layout: {token}")
            seen.add(token)
            ordered_paths.append(name_to_path[token])
            cell_indices.append(r * max_cols + c)

    # Fill unspecified files in file name order after the configured grid.
    next_cell = len(rows) * max_cols
    for p in sorted_paths:
        if p.name in seen:
            continue
        ordered_paths.append(p)
        cell_indices.append(next_cell)
        next_cell += 1

    if not ordered_paths:
        raise ValueError("layout file has no video names")
    total_cells = max(cell_indices) + 1
    rows_out = (total_cells + max_cols - 1) // max_cols
    return ordered_paths, max_cols, rows_out, cell_indices


def extract_audio_mono_f32(path: Path) -> np.ndarray:
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(AUDIO_SR),
        "-f",
        "f32le",
        "-",
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True)
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size == 0:
        raise ValueError(f"audio extraction failed or empty audio: {path}")
    return audio


def best_anchor_start(samples: np.ndarray, anchor_len: int) -> int:
    if samples.size <= anchor_len:
        return 0
    candidates = np.linspace(0, samples.size - anchor_len, num=25, dtype=int)
    powers = [float(np.sum(np.abs(samples[s : s + anchor_len]))) for s in candidates]
    return int(candidates[int(np.argmax(powers))])


def strict_offset_samples(reference: np.ndarray, target: np.ndarray) -> int | None:
    anchor_len = min(AUDIO_SR // 2, reference.size // 4, target.size // 4)
    if anchor_len < AUDIO_SR // 10:
        return None

    ref_start = best_anchor_start(reference, anchor_len)
    ref_anchor = reference[ref_start : ref_start + anchor_len]
    anchor_bytes = ref_anchor.tobytes()
    target_bytes = target.tobytes()
    idx_bytes = target_bytes.find(anchor_bytes)
    if idx_bytes < 0:
        return None

    sample_shift = (idx_bytes // 4) - ref_start
    ref_overlap_start = max(0, sample_shift)
    tgt_overlap_start = max(0, -sample_shift)
    overlap = min(reference.size - ref_overlap_start, target.size - tgt_overlap_start)
    if overlap <= 0:
        return None

    if np.array_equal(
        reference[ref_overlap_start : ref_overlap_start + overlap],
        target[tgt_overlap_start : tgt_overlap_start + overlap],
    ):
        return sample_shift
    return None


def energy_envelope(samples: np.ndarray, frame_size: int = 320, hop_size: int = 160) -> np.ndarray:
    if samples.size < frame_size:
        padded = np.pad(samples.astype(np.float64), (0, frame_size - samples.size))
        return np.array([float(np.sqrt(np.mean(padded * padded) + 1e-12))], dtype=np.float64)

    win = np.lib.stride_tricks.sliding_window_view(samples.astype(np.float64), frame_size)[::hop_size]
    rms = np.sqrt(np.mean(win * win, axis=1) + 1e-12)
    return rms


def envelope_offset_samples(
    reference: np.ndarray,
    target: np.ndarray,
    frame_size: int = 320,
    hop_size: int = 160,
) -> tuple[int, float]:
    ref_env = energy_envelope(reference, frame_size=frame_size, hop_size=hop_size)
    tgt_env = energy_envelope(target, frame_size=frame_size, hop_size=hop_size)

    ref_env -= np.mean(ref_env)
    tgt_env -= np.mean(tgt_env)
    ref_norm = float(np.linalg.norm(ref_env))
    tgt_norm = float(np.linalg.norm(tgt_env))
    if ref_norm <= 1e-9 or tgt_norm <= 1e-9:
        return 0, 0.0

    corr = np.correlate(ref_env, tgt_env, mode="full")
    lags = np.arange(-len(tgt_env) + 1, len(ref_env))
    ref_len = len(ref_env)
    tgt_len = len(tgt_env)
    overlap_starts_ref = np.maximum(0, lags)
    overlap_starts_tgt = np.maximum(0, -lags)
    overlaps = np.minimum(ref_len - overlap_starts_ref, tgt_len - overlap_starts_tgt)

    min_overlap = int(min(ref_len, tgt_len) * 0.6)
    if min_overlap < 10:
        min_overlap = min(ref_len, tgt_len)
    valid = overlaps >= min_overlap
    if not np.any(valid):
        valid = overlaps >= max(10, int(min(ref_len, tgt_len) * 0.3))
    if not np.any(valid):
        return 0, 0.0

    scored = np.full(corr.shape, -1e18, dtype=np.float64)
    scored[valid] = corr[valid] / (overlaps[valid] + 1e-12)
    best_idx = int(np.argmax(scored))
    lag_frames = int(lags[best_idx])
    score = float(corr[best_idx] / (ref_norm * tgt_norm + 1e-12))
    return lag_frames * hop_size, score


def active_bounds_samples(samples: np.ndarray, frame_size: int = 320, hop_size: int = 160) -> tuple[int, int]:
    env = energy_envelope(samples, frame_size=frame_size, hop_size=hop_size)
    q10 = float(np.percentile(env, 10))
    q90 = float(np.percentile(env, 90))
    thr = max(q10 + (q90 - q10) * 0.15, 1e-4)
    active = np.where(env > thr)[0]
    if active.size == 0:
        return 0, max(0, samples.size - 1)
    first = int(active[0] * hop_size)
    last = int(min(samples.size - 1, active[-1] * hop_size + frame_size - 1))
    return first, last


def estimate_pair_offset(reference: np.ndarray, target: np.ndarray, strict_first: bool) -> PairOffsetResult:
    if strict_first:
        strict_shift = strict_offset_samples(reference, target)
        if strict_shift is not None:
            return PairOffsetResult(shift_samples=strict_shift, used_strict=True, score=1.0)

    shift, score = envelope_offset_samples(reference, target)
    return PairOffsetResult(shift_samples=shift, used_strict=False, score=score)


def overlap_audio_segment(audio: np.ndarray, local_start_sec: float, duration_sec: float) -> np.ndarray:
    start = max(0, int(round(local_start_sec * AUDIO_SR)))
    length = int(round(duration_sec * AUDIO_SR))
    end = min(audio.size, start + length)
    if end <= start:
        return np.array([], dtype=np.float32)
    return audio[start:end]


def pair_similarity_score(a: np.ndarray, b: np.ndarray) -> float:
    if a.size == 0 or b.size == 0:
        return -1.0
    n = min(a.size, b.size)
    if n < AUDIO_SR // 4:
        return -1.0
    env_a = energy_envelope(a[:n], frame_size=640, hop_size=320)
    env_b = energy_envelope(b[:n], frame_size=640, hop_size=320)
    m = min(env_a.size, env_b.size)
    if m < 10:
        return -1.0
    x = env_a[:m] - np.mean(env_a[:m])
    y = env_b[:m] - np.mean(env_b[:m])
    nx = float(np.linalg.norm(x))
    ny = float(np.linalg.norm(y))
    if nx <= 1e-9 or ny <= 1e-9:
        return -1.0
    return float(np.dot(x, y) / (nx * ny))


def plan_pattern_scores(
    audios: list[np.ndarray],
    start_times: list[float],
    overlap_start: float,
    overlap_duration: float,
) -> tuple[float, float]:
    segs = [
        overlap_audio_segment(
            audios[i],
            local_start_sec=(overlap_start - start_times[i]),
            duration_sec=overlap_duration,
        )
        for i in range(len(audios))
    ]

    scores: list[float] = []
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            scores.append(pair_similarity_score(segs[i], segs[j]))

    if not scores:
        return -1.0, -1.0
    return float(np.mean(scores)), float(np.min(scores))


def compute_alignment_plan(
    infos: list[VideoInfo],
    audios: list[np.ndarray],
    strict_first: bool,
) -> AlignmentPlan:
    best_plan: AlignmentPlan | None = None
    best_score = -1e18
    bounds = [active_bounds_samples(a) for a in audios]
    methods = ["corr", "onset"]

    for method in methods:
        for ref_idx in range(len(audios)):
            offsets = [0] * len(audios)
            strict_all = True
            aggregate_score = 0.0
            for i in range(len(audios)):
                if i == ref_idx:
                    continue
                if method == "corr":
                    result = estimate_pair_offset(audios[ref_idx], audios[i], strict_first=strict_first)
                    offsets[i] = result.shift_samples
                    strict_all = strict_all and result.used_strict
                    aggregate_score += result.score
                elif method == "onset":
                    ref_first = bounds[ref_idx][0]
                    tgt_first = bounds[i][0]
                    offsets[i] = ref_first - tgt_first
                    strict_all = False
                    aggregate_score += 0.2

            start_times = [s / AUDIO_SR for s in offsets]
            latest_start = max(start_times)
            earliest_end = min(start_times[i] + infos[i].duration for i in range(len(infos)))
            overlap = earliest_end - latest_start
            if overlap <= 0:
                continue

            avg_pair, min_pair = plan_pattern_scores(audios, start_times, latest_start, overlap)
            # Prioritize pattern agreement across all videos, then overlap length.
            plan_score = avg_pair * 10000.0 + min_pair * 5000.0 + overlap * 100.0 + aggregate_score
            if plan_score > best_score:
                best_score = plan_score
                best_plan = AlignmentPlan(
                    offsets=offsets,
                    start_times=start_times,
                    trimmed_duration=overlap,
                    strict_all=strict_all,
                    reference_index=ref_idx,
                    method=method,
                    avg_pair_score=avg_pair,
                    min_pair_score=min_pair,
                )

    if best_plan is None:
        raise ValueError("no common overlap after alignment")
    return best_plan


def fmt_sec(value: float) -> str:
    return f"{value:.6f}"


def ffmpeg_trim(
    src: Path,
    dst: Path,
    trim_start: float,
    trim_duration: float,
    profile: QualityProfile,
    bitrate: int | None,
) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        fmt_sec(trim_start),
        "-i",
        str(src),
        "-t",
        fmt_sec(trim_duration),
        "-c:v",
        "libx264",
        "-preset",
        profile.preset,
        "-crf",
        str(profile.crf),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
    ]
    if bitrate:
        cmd.extend(["-b:v", str(bitrate)])
    cmd.append(str(dst))
    run(cmd)


def ffmpeg_align_to_reference(
    src: Path,
    dst: Path,
    start_time: float,
    target_duration: float,
    profile: QualityProfile,
    bitrate: int | None,
) -> None:
    cut_start = max(0.0, -start_time)
    lead = max(0.0, start_time)
    lead_ms = int(round(lead * 1000.0))
    # Add enough tail padding, then trim to exact target duration.
    tail = max(0.0, target_duration)

    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        fmt_sec(cut_start),
        "-i",
        str(src),
        "-filter:v",
        (
            f"setpts=PTS-STARTPTS,"
            f"tpad=start_duration={fmt_sec(lead)}:stop_mode=add:stop_duration={fmt_sec(tail)}:color=black,"
            f"trim=duration={fmt_sec(target_duration)}"
        ),
        "-filter:a",
        (
            f"asetpts=PTS-STARTPTS,"
            f"adelay={lead_ms}:all=1,"
            "apad,"
            f"atrim=duration={fmt_sec(target_duration)}"
        ),
        "-c:v",
        "libx264",
        "-preset",
        profile.preset,
        "-crf",
        str(profile.crf),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
    ]
    if bitrate:
        cmd.extend(["-b:v", str(bitrate)])
    cmd.append(str(dst))
    run(cmd)


def quality_profile(quality: str, infos: list[VideoInfo]) -> tuple[QualityProfile, int | None]:
    if quality == "source":
        max_bitrate = max((i.video_bitrate or 0) for i in infos) or None
        return QualityProfile(crf=17, preset="slow"), max_bitrate
    if quality == "youtube":
        max_h = max(i.height for i in infos)
        if max_h >= 2160:
            return QualityProfile(crf=16, preset="slow"), None
        if max_h >= 1440:
            return QualityProfile(crf=17, preset="slow"), None
        if max_h >= 1080:
            return QualityProfile(crf=18, preset="medium"), None
        return QualityProfile(crf=19, preset="medium"), None

    manual = {
        "high": QualityProfile(crf=18, preset="slow"),
        "medium": QualityProfile(crf=23, preset="medium"),
        "low": QualityProfile(crf=28, preset="veryfast"),
        "testfast": QualityProfile(crf=34, preset="ultrafast"),
    }
    return manual[quality], None


def align_videos(
    input_dir: Path,
    output_dir: Path,
    quality: str,
    strict_first: bool = False,
    reference_video: Path | None = None,
    pad_to_reference: bool = False,
) -> tuple[list[Path], bool]:
    paths = discover_videos(input_dir)
    infos = [get_video_info(p) for p in paths]
    profile, bitrate = quality_profile(quality, infos)

    audios = [extract_audio_mono_f32(p) for p in paths]
    if reference_video is not None:
        ref_resolved = reference_video.resolve()
        ref_map = {p.resolve(): i for i, p in enumerate(paths)}
        if ref_resolved not in ref_map:
            raise ValueError(f"reference video is not in input-dir: {reference_video}")
        ref_idx = ref_map[ref_resolved]

        offsets = [0] * len(paths)
        strict_all = True
        for i in range(len(paths)):
            if i == ref_idx:
                continue
            res = estimate_pair_offset(audios[ref_idx], audios[i], strict_first=strict_first)
            offsets[i] = res.shift_samples
            strict_all = strict_all and res.used_strict
        start_times = [s / AUDIO_SR for s in offsets]
        duration = infos[ref_idx].duration if pad_to_reference else (
            min(start_times[i] + infos[i].duration for i in range(len(infos))) - max(start_times)
        )
        if duration <= 0:
            raise ValueError("no common overlap after alignment")
        plan = AlignmentPlan(
            offsets=offsets,
            start_times=start_times,
            trimmed_duration=duration,
            strict_all=strict_all,
            reference_index=ref_idx,
            method="reference",
            avg_pair_score=0.0,
            min_pair_score=0.0,
        )
    else:
        plan = compute_alignment_plan(infos, audios, strict_first=strict_first)
        if plan.min_pair_score < 0.45:
            raise ValueError(
                "audio pattern mismatch: best alignment is not reliable "
                f"(min_pair_score={plan.min_pair_score:.3f}, avg_pair_score={plan.avg_pair_score:.3f}, "
                f"method={plan.method}, ref={paths[plan.reference_index].name}, overlap={plan.trimmed_duration:.3f}s)"
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    aligned_paths: list[Path] = []
    for i, src in enumerate(paths):
        dst = output_dir / src.name
        if reference_video is not None and pad_to_reference:
            ffmpeg_align_to_reference(
                src=src,
                dst=dst,
                start_time=plan.start_times[i],
                target_duration=plan.trimmed_duration,
                profile=profile,
                bitrate=bitrate,
            )
        else:
            local_start = max(0.0, max(plan.start_times) - plan.start_times[i])
            ffmpeg_trim(src, dst, local_start, plan.trimmed_duration, profile, bitrate)
        aligned_paths.append(dst)

    msg = (
        f"[align] files={len(paths)} strict_all={plan.strict_all} "
        f"method={plan.method} ref={paths[plan.reference_index].name} "
        f"duration={plan.trimmed_duration:.3f}s"
    )
    if reference_video is None:
        msg += f" avg_pair={plan.avg_pair_score:.3f} min_pair={plan.min_pair_score:.3f}"
    if reference_video is not None and pad_to_reference:
        msg += " padded_missing=black"
    print(msg)
    for i, src in enumerate(paths):
        print(f"[align] {src.name} offset={plan.start_times[i]:+.6f}s")

    return aligned_paths, plan.strict_all


def parse_grid_size(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*[*xX]\s*(\d+)\s*", text)
    if not m:
        raise ValueError(f"invalid grid size format: {text} (expected like 3x2 or 3*2)")
    cols = int(m.group(1))
    rows = int(m.group(2))
    if cols <= 0 or rows <= 0:
        raise ValueError("grid size values must be positive integers")
    return cols, rows


def normalize_color(color: str) -> str:
    if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        return "0x" + color[1:]
    if re.fullmatch(r"0x[0-9a-fA-F]{6}", color):
        return color
    if color.lower() == "black":
        return "black"
    raise ValueError("background color must be black, #RRGGBB, or 0xRRGGBB")


def plan_layout(
    layout: str,
    infos: list[VideoInfo],
    grid_size: str | None = None,
) -> tuple[list[tuple[int, int]], int, int]:
    sizes = [(i.width, i.height) for i in infos]
    n = len(sizes)

    if layout == "row":
        cols, rows = n, 1
    elif layout == "grid2x2":
        if n != 4:
            raise ValueError("grid2x2 layout requires 4 videos")
        cols, rows = 2, 2
    elif layout == "pyramid5":
        if n != 5:
            raise ValueError("pyramid5 layout requires 5 videos")
        rows_spec = [sizes[:2], sizes[2:]]
        row_heights = [max(h for _, h in row) for row in rows_spec]
        row_widths = [sum(w for w, _ in row) for row in rows_spec]
        canvas_w = max(row_widths)
        canvas_h = sum(row_heights)
        positions: list[tuple[int, int]] = []
        y = 0
        for r, row in enumerate(rows_spec):
            row_w = row_widths[r]
            x = (canvas_w - row_w) // 2
            for w, h in row:
                positions.append((x, y + (row_heights[r] - h) // 2))
                x += w
            y += row_heights[r]
        return positions, canvas_w, canvas_h
    elif layout == "top1bottom2":
        if n != 3:
            raise ValueError("top1bottom2 layout requires 3 videos")
        rows_spec = [sizes[:1], sizes[1:]]
        row_heights = [max(h for _, h in row) for row in rows_spec]
        row_widths = [sum(w for w, _ in row) for row in rows_spec]
        canvas_w = max(row_widths)
        canvas_h = sum(row_heights)
        positions: list[tuple[int, int]] = []
        y = 0
        for r, row in enumerate(rows_spec):
            row_w = row_widths[r]
            x = (canvas_w - row_w) // 2
            for w, h in row:
                positions.append((x, y + (row_heights[r] - h) // 2))
                x += w
            y += row_heights[r]
        return positions, canvas_w, canvas_h
    elif layout == "grid":
        if not grid_size:
            raise ValueError("grid layout requires --grid-size (example: 3x2)")
        cols, rows = parse_grid_size(grid_size)
        if n > cols * rows:
            raise ValueError(
                f"grid {cols}x{rows} has only {cols*rows} cells, but {n} videos were provided"
            )
    else:
        raise ValueError(f"unsupported layout: {layout}")

    # Generic grid placement (row is equivalent to X=video_count, Y=1).
    cell_w = max(w for w, _ in sizes)
    cell_h = max(h for _, h in sizes)
    canvas_w = cols * cell_w
    canvas_h = rows * cell_h
    positions: list[tuple[int, int]] = []
    for idx, (w, h) in enumerate(sizes):
        r = idx // cols
        c = idx % cols
        x = c * cell_w + (cell_w - w) // 2
        y = r * cell_h + (cell_h - h) // 2
        positions.append((x, y))
    return positions, canvas_w, canvas_h


def combine_videos(
    input_paths: list[Path],
    output_path: Path,
    layout: str,
    quality: str,
    grid_size: str | None = None,
    background_color: str = "black",
    layout_file: Path | None = None,
) -> None:
    if layout == "file" and grid_size:
        raise ValueError("--grid-size cannot be used with --layout file")
    if layout == "grid" and layout_file is not None:
        raise ValueError("--layout-file cannot be used with --layout grid")

    ordered_input_paths = input_paths
    if layout == "file":
        ordered_input_paths, cols, rows, cell_indices = load_layout_tsv(layout_file, input_paths)
        infos = [get_video_info(p) for p in ordered_input_paths]
        cell_w = max(i.width for i in infos)
        cell_h = max(i.height for i in infos)
        canvas_w = cols * cell_w
        canvas_h = rows * cell_h
        positions: list[tuple[int, int]] = []
        for idx, info in enumerate(infos):
            cell = cell_indices[idx]
            r = cell // cols
            c = cell % cols
            x = c * cell_w + (cell_w - info.width) // 2
            y = r * cell_h + (cell_h - info.height) // 2
            positions.append((x, y))
    else:
        infos = [get_video_info(p) for p in ordered_input_paths]
        positions, canvas_w, canvas_h = plan_layout(layout, infos, grid_size=grid_size)
    bg = normalize_color(background_color)
    profile, bitrate = quality_profile(quality, infos)
    min_duration = min(i.duration for i in infos)
    output_fps = max((i.fps for i in infos), default=30.0)
    if output_fps <= 0:
        output_fps = 30.0

    chunks: list[str] = []
    for i in range(len(ordered_input_paths)):
        chunks.append(f"[{i}:v]setpts=PTS-STARTPTS[v{i}]")
    chunks.append(f"color=c={bg}:size={canvas_w}x{canvas_h}:d={min_duration}:r={output_fps:.6f}[base]")

    current = "base"
    for i, (x, y) in enumerate(positions):
        out = f"tmp{i}" if i < len(positions) - 1 else "vout"
        chunks.append(f"[{current}][v{i}]overlay=x={x}:y={y}:shortest=1[{out}]")
        current = out

    filter_complex = ";".join(chunks)

    cmd = ["ffmpeg", "-y", "-v", "error"]
    for p in ordered_input_paths:
        cmd.extend(["-i", str(p)])
    cmd.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            profile.preset,
            "-crf",
            str(profile.crf),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
    )
    if bitrate:
        cmd.extend(["-b:v", str(bitrate)])
    cmd.append(str(output_path))
    run(cmd)
    print(f"[combine] layout={layout} size={canvas_w}x{canvas_h} bg={bg} out={output_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Align and combine videos by audio.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_quality_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "-q",
            "--q",
            "--quality",
            dest="quality",
            choices=["high", "medium", "low", "testfast", "youtube", "source"],
            default="high",
            help="quality profile",
        )

    pa = sub.add_parser("align", help="align and trim videos")
    pa.add_argument("--input-dir", "--in", dest="input_dir", type=Path, required=True)
    pa.add_argument("--output-dir", "--out", dest="output_dir", type=Path, required=True)
    pa.add_argument("--strict-first", action="store_true", help="try strict exact-match search first")
    pa.add_argument("--reference-video", "--ref", dest="reference_video", type=Path, help="align all videos against this file in input-dir")
    pa.add_argument("--pad-to-reference", "--pad", dest="pad_to_reference", action="store_true", help="pad missing ranges to black/silence")
    add_quality_args(pa)

    pc = sub.add_parser("combine", help="combine aligned videos")
    pc.add_argument("--input-dir", "--in", dest="input_dir", type=Path, required=True)
    pc.add_argument("--output", "--out", dest="output", type=Path, required=True)
    pc.add_argument("--layout", choices=["row", "grid2x2", "pyramid5", "top1bottom2", "grid", "file"], required=True)
    pc.add_argument("--grid-size", type=str, help="required for --layout grid. Example: 3x2 or 3*2")
    pc.add_argument("--layout-file", type=Path, help="TSV/CSV file for --layout file (file names only)")
    pc.add_argument("--background-color", "--bg", dest="background_color", type=str, default="black", help="black, #RRGGBB, or 0xRRGGBB")
    add_quality_args(pc)

    pp = sub.add_parser("process", help="align and combine in one command")
    pp.add_argument("--input-dir", "--in", dest="input_dir", type=Path, required=True)
    pp.add_argument("--output", "--out", dest="output", type=Path, required=True)
    pp.add_argument("--layout", choices=["row", "grid2x2", "pyramid5", "top1bottom2", "grid", "file"], required=True)
    pp.add_argument("--grid-size", type=str, help="required for --layout grid. Example: 3x2 or 3*2")
    pp.add_argument("--layout-file", type=Path, help="TSV/CSV file for --layout file (file names only)")
    pp.add_argument("--background-color", "--bg", dest="background_color", type=str, default="black", help="black, #RRGGBB, or 0xRRGGBB")
    pp.add_argument("--strict-first", action="store_true", help="try strict exact-match search first")
    pp.add_argument("--reference-video", "--ref", dest="reference_video", type=Path, help="align all videos against this file in input-dir")
    pp.add_argument("--pad-to-reference", "--pad", dest="pad_to_reference", action="store_true", help="pad missing ranges to black/silence")
    add_quality_args(pp)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        if args.command == "align":
            align_videos(
                input_dir=args.input_dir,
                output_dir=args.output_dir,
                quality=args.quality,
                strict_first=args.strict_first,
                reference_video=args.reference_video,
                pad_to_reference=args.pad_to_reference,
            )
            return

        if args.command == "combine":
            input_paths = discover_videos(args.input_dir)
            combine_videos(
                input_paths=input_paths,
                output_path=args.output,
                layout=args.layout,
                quality=args.quality,
                grid_size=args.grid_size,
                background_color=args.background_color,
                layout_file=args.layout_file,
            )
            return

        if args.command == "process":
            aligned_dir = args.output.parent / "aligned"
            aligned_paths, _ = align_videos(
                input_dir=args.input_dir,
                output_dir=aligned_dir,
                quality=args.quality,
                strict_first=args.strict_first,
                reference_video=args.reference_video,
                pad_to_reference=args.pad_to_reference,
            )
            combine_videos(
                input_paths=aligned_paths,
                output_path=args.output,
                layout=args.layout,
                quality=args.quality,
                grid_size=args.grid_size,
                background_color=args.background_color,
                layout_file=args.layout_file,
            )
            return
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() if e.stderr else str(e)
        print(f"error: command failed: {err}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
