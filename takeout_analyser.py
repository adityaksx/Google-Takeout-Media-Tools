#!/usr/bin/env python3
"""
Google Takeout Folder Analyser
Scans folder + subfolders and produces:
  1. takeout_analysis.log   — full detailed log (every file, every match/mismatch)
  2. takeout_overview.txt   — human-readable overview / summary report

Usage:
  python takeout_analyser.py /path/to/Takeout
  python takeout_analyser.py /path/to/Takeout --out-dir /path/to/reports
  python takeout_analyser.py /path/to/Takeout --quiet
"""

import os
import sys
import json
import re
import argparse
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Extension sets
# ──────────────────────────────────────────────────────────────────────────────
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif", ".cr2", ".cr3", ".nef", ".arw",
    ".dng", ".orf", ".rw2", ".pef", ".srw", ".svg", ".ico",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".mpeg", ".mpg",
    ".m4v", ".wmv", ".3gp", ".3g2", ".flv", ".ts", ".mts", ".m2ts",
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

JSON_SIDECAR_SUFFIXES = [
    ".json",
    ".supplemental-metadata.json",
    ".sup-meta.json",
    ".metadata.json",
]
GOOGLE_TRUNCATION_LENGTHS = [46, 47, 48, 49, 50, 51]


# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class MediaFile:
    path:         Path
    extension:    str
    file_type:    str           # "image" | "video"
    size_bytes:   int
    json_path:    Optional[Path] = None
    match_method: Optional[str]  = None

@dataclass
class JsonFile:
    path:              Path
    size_bytes:        int
    title_in_json:     Optional[str]  = None
    media_path:        Optional[Path] = None
    is_album_metadata: bool           = False

@dataclass
class AnalysisResult:
    root:          Path
    media_files:   list = field(default_factory=list)
    json_files:    list = field(default_factory=list)
    orphan_media:  list = field(default_factory=list)
    orphan_json:   list = field(default_factory=list)
    matched_pairs: list = field(default_factory=list)
    ext_stats:     dict = field(default_factory=lambda: defaultdict(int))
    folder_stats:  dict = field(default_factory=lambda: defaultdict(
        lambda: {"media": 0, "json": 0, "matched": 0, "orphan_media": 0, "orphan_json": 0}
    ))
    scanned_at:    str  = ""
    total_files:   int  = 0


# ──────────────────────────────────────────────────────────────────────────────
# Matching helpers
# ──────────────────────────────────────────────────────────────────────────────
def stem_variants(media_path: Path) -> list:
    name   = media_path.name
    stem   = media_path.stem
    suffix = media_path.suffix
    variants = [name]

    stripped = re.sub(r'\(\d+\)$', '', stem) + suffix
    if stripped != name:
        variants.append(stripped)

    for n in range(1, 6):
        variants.append(f"{stem}({n}){suffix}")
        bare = re.sub(r'\(\d+\)$', '', stem)
        variants.append(f"{bare}({n}){suffix}")

    for trunc_len in GOOGLE_TRUNCATION_LENGTHS:
        base = stem[:trunc_len]
        if base != stem:
            variants.append(base + suffix)
            for n in range(1, 4):
                variants.append(f"{base}({n}){suffix}")

    seen, unique = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v); unique.append(v)
    return unique


def find_json_for_media(media_path: Path, json_lookup: dict) -> tuple:
    folder = media_path.parent
    for base in stem_variants(media_path):
        for suf in JSON_SIDECAR_SUFFIXES:
            key = (folder, base + suf)
            if key in json_lookup:
                return json_lookup[key], f"variant='{base}' suffix='{suf}'"
    key = (folder, media_path.stem + ".json")
    if key in json_lookup:
        return json_lookup[key], "bare-stem"
    for base in stem_variants(media_path)[:3]:
        for suf in JSON_SIDECAR_SUFFIXES:
            key = (folder.parent, base + suf)
            if key in json_lookup:
                return json_lookup[key], f"parent-folder '{base}{suf}'"
    return None, None


def read_json_meta(json_path: Path) -> dict:
    result = {"title": None, "description": None, "date": None, "has_gps": False}
    try:
        with open(json_path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        result["title"]       = data.get("title")
        result["description"] = data.get("description")
        ts = data.get("photoTakenTime", {}).get("timestamp")
        if ts:
            result["date"] = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
        geo = data.get("geoData") or data.get("geoDataExif")
        if geo:
            lat = float(geo.get("latitude", 0))
            lon = float(geo.get("longitude", 0))
            result["has_gps"] = not (abs(lat) < 0.0001 and abs(lon) < 0.0001)
    except Exception:
        pass
    return result


def is_album_metadata_json(json_path: Path) -> bool:
    return json_path.name.lower() in ("metadata.json", "album-metadata.json")


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def relative(base: Path, target: Path) -> str:
    try:
        return str(target.relative_to(base))
    except ValueError:
        return str(target)


# ──────────────────────────────────────────────────────────────────────────────
# Logger — writes ONLY to file, nothing to terminal except progress
# ──────────────────────────────────────────────────────────────────────────────
class FileLogger:
    def __init__(self, log_path: Path):
        self._f = open(log_path, "w", encoding="utf-8")
        self.log_path = log_path

    def _ts(self):
        return datetime.now().strftime("%H:%M:%S")

    def write(self, line: str = ""):
        self._f.write(line + "\n")
        self._f.flush()

    def sep(self, char="─", width=72):
        self.write(char * width)

    def header(self, title: str):
        self.write()
        self.sep("═")
        self.write(f"  {title}")
        self.sep("═")

    def section(self, title: str):
        self.write()
        self.sep()
        self.write(f"  {title}")
        self.sep()

    def ok(self,   msg): self.write(f"  [OK   {self._ts()}]  {msg}")
    def warn(self, msg): self.write(f"  [WARN {self._ts()}]  {msg}")
    def err(self,  msg): self.write(f"  [ERR  {self._ts()}]  {msg}")
    def info(self, msg): self.write(f"  [INFO {self._ts()}]  {msg}")
    def raw(self,  msg): self.write(f"  {msg}")

    def close(self):
        self._f.close()


# ──────────────────────────────────────────────────────────────────────────────
# Scanner
# ──────────────────────────────────────────────────────────────────────────────
def scan_folder(root: Path, log: FileLogger, verbose: bool) -> AnalysisResult:
    result = AnalysisResult(root=root, scanned_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    raw_media: list   = []
    raw_json:  list   = []
    json_lookup: dict = {}

    log.header("GOOGLE TAKEOUT FOLDER ANALYSER — DETAILED LOG")
    log.write(f"  Scan root  : {root}")
    log.write(f"  Scanned at : {result.scanned_at}")
    log.write()

    # ── Walk and collect ─────────────────────────────────────────────────────
    log.section("FILE DISCOVERY")
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        folder     = Path(dirpath)
        folder_rel = relative(root, folder) or "."
        files_in   = sorted(filenames)
        if not files_in:
            continue

        log.write()
        log.raw(f"  📁 {folder_rel}/  ({len(files_in)} files)")

        for fname in files_in:
            total += 1
            fpath = folder / fname
            try:
                size = fpath.stat().st_size
            except OSError:
                size = 0
            ext = fpath.suffix.lower()

            if fname.lower().endswith(".json"):
                jf = JsonFile(
                    path=fpath,
                    size_bytes=size,
                    is_album_metadata=is_album_metadata_json(fpath),
                )
                if not jf.is_album_metadata:
                    meta = read_json_meta(fpath)
                    jf.title_in_json = meta["title"] or meta["description"]
                raw_json.append(jf)
                json_lookup[(folder, fname)]         = jf
                json_lookup[(folder, fname.lower())] = jf
                result.ext_stats["[json]"] += 1
                result.folder_stats[folder_rel]["json"] += 1
                if verbose:
                    tag = "[ALBUM-META]" if jf.is_album_metadata else "[JSON     ]"
                    log.raw(f"       {tag}  {fname}  ({human_size(size)})")

            elif ext in MEDIA_EXTENSIONS:
                ftype = "image" if ext in IMAGE_EXTENSIONS else "video"
                mf = MediaFile(path=fpath, extension=ext, file_type=ftype, size_bytes=size)
                raw_media.append(mf)
                result.ext_stats[ext] += 1
                result.folder_stats[folder_rel]["media"] += 1
                if verbose:
                    tag = "[IMAGE    ]" if ftype == "image" else "[VIDEO    ]"
                    log.raw(f"       {tag}  {fname}  ({human_size(size)})")
            else:
                result.ext_stats["[other:" + ext + "]"] += 1
                if verbose:
                    log.raw(f"       [OTHER    ]  {fname}  ({human_size(size)})")

    result.total_files = total
    log.write()
    log.info(f"Total files scanned : {total}")
    log.info(f"Media files found   : {len(raw_media)}")
    log.info(f"JSON files found    : {len(raw_json)}")

    # ── Match media -> JSON ───────────────────────────────────────────────────
    log.section("MATCHING  —  Media <-> JSON")
    matched_json_ids = set()

    for mf in raw_media:
        jf, method    = find_json_for_media(mf.path, json_lookup)
        folder_rel    = relative(root, mf.path.parent) or "."
        if jf:
            mf.json_path    = jf.path
            mf.match_method = method
            jf.media_path   = mf.path
            matched_json_ids.add(id(jf))
            result.matched_pairs.append((mf, jf))
            result.folder_stats[folder_rel]["matched"] += 1
            meta     = read_json_meta(jf.path)
            gps_tag  = "📍 GPS" if meta["has_gps"] else "no GPS"
            date_tag = meta["date"] or "no date"
            log.ok(f"{relative(root, mf.path)}")
            log.raw(f"           ↳ JSON   : {relative(root, jf.path)}")
            log.raw(f"           ↳ method : {method}")
            log.raw(f"           ↳ date   : {date_tag}  |  {gps_tag}")
            if meta["title"]:
                t = (meta["title"][:60] + "…") if len(meta["title"]) > 60 else meta["title"]
                log.raw(f"           ↳ title  : \"{t}\"")
        else:
            result.orphan_media.append(mf)
            result.folder_stats[folder_rel]["orphan_media"] += 1
            log.warn(f"NO JSON FOUND  ->  {relative(root, mf.path)}  ({human_size(mf.size_bytes)})")

    for jf in raw_json:
        if id(jf) not in matched_json_ids and not jf.is_album_metadata:
            result.orphan_json.append(jf)
            folder_rel = relative(root, jf.path.parent) or "."
            result.folder_stats[folder_rel]["orphan_json"] += 1
            t = f'  title="{jf.title_in_json[:50]}"' if jf.title_in_json else ""
            log.warn(f"NO MEDIA FOUND ->  {relative(root, jf.path)}{t}")

    result.media_files = raw_media
    result.json_files  = raw_json
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Overview file
# ──────────────────────────────────────────────────────────────────────────────
def write_overview(result: AnalysisResult, overview_path: Path):
    root    = result.root
    matched = len(result.matched_pairs)
    total_m = len(result.media_files)
    total_j = len(result.json_files)
    album_j = sum(1 for j in result.json_files if j.is_album_metadata)
    o_media = len(result.orphan_media)
    o_json  = len(result.orphan_json)
    rate    = (matched / total_m * 100) if total_m else 0

    total_size_media = sum(m.size_bytes for m in result.media_files)
    total_size_json  = sum(j.size_bytes for j in result.json_files)
    images = [m for m in result.media_files if m.file_type == "image"]
    videos = [m for m in result.media_files if m.file_type == "video"]

    W = 72
    def pad(label, value):
        dots = W - len(label) - len(str(value)) - 4
        return f"  {label} {'.' * max(dots, 1)} {value}"

    with open(overview_path, "w", encoding="utf-8") as f:
        def w(line=""): f.write(line + "\n")
        def sep(c="─"): w(c * W)

        w("=" * W)
        w("  GOOGLE TAKEOUT FOLDER — OVERVIEW REPORT")
        w("=" * W)
        w(f"  Root       : {root}")
        w(f"  Scanned at : {result.scanned_at}")
        w(f"  Report     : {overview_path}")
        w()

        # Summary
        sep("═")
        w("  SUMMARY")
        sep("═")
        w(pad("Total files on disk",           result.total_files))
        w(pad("Total media files",             total_m))
        w(pad("  ├─ Images",                   len(images)))
        w(pad("  └─ Videos",                   len(videos)))
        w(pad("Total JSON files",              total_j))
        w(pad("  ├─ Sidecar JSONs",            total_j - album_j))
        w(pad("  └─ Album-level metadata",     album_j))
        w(pad("Total media size",              human_size(total_size_media)))
        w(pad("Total JSON size",               human_size(total_size_json)))
        w()
        w(pad("Matched pairs  (media + JSON)", matched))
        w(pad("Orphan media   (NO JSON found)", o_media))
        w(pad("Orphan JSON    (NO media found)", o_json))
        w()
        bar_filled = int(rate / 2)
        bar = "█" * bar_filled + "░" * (50 - bar_filled)
        w(f"  Match rate : {rate:.1f}%  [{bar}]")
        w()

        # Extension breakdown
        sep()
        w("  EXTENSION BREAKDOWN")
        sep()
        img_exts = {k: v for k, v in result.ext_stats.items() if k in IMAGE_EXTENSIONS}
        vid_exts = {k: v for k, v in result.ext_stats.items() if k in VIDEO_EXTENSIONS}
        oth_exts = {k: v for k, v in result.ext_stats.items()
                    if k not in IMAGE_EXTENSIONS and k not in VIDEO_EXTENSIONS}
        if img_exts:
            w("  Images:")
            for ext in sorted(img_exts): w(pad(f"    {ext}", img_exts[ext]))
        if vid_exts:
            w("  Videos:")
            for ext in sorted(vid_exts): w(pad(f"    {ext}", vid_exts[ext]))
        if oth_exts:
            w("  Other:")
            for ext in sorted(oth_exts): w(pad(f"    {ext}", oth_exts[ext]))
        w()

        # Per-folder table
        sep()
        w("  PER-FOLDER OVERVIEW")
        sep()
        w(f"  {'Folder':<40}  {'Media':>5}  {'JSON':>5}  {'Matched':>7}  {'OMed':>5}  {'OJSN':>5}")
        w("  " + "─" * (W - 2))
        for folder_rel in sorted(result.folder_stats.keys()):
            s    = result.folder_stats[folder_rel]
            name = folder_rel if len(folder_rel) <= 40 else "…" + folder_rel[-38:]
            flag = " ⚠" if (s["orphan_media"] > 0 or s["orphan_json"] > 0) else ""
            w(f"  {name:<40}  {s['media']:>5}  {s['json']:>5}  {s['matched']:>7}"
              f"  {s['orphan_media']:>5}  {s['orphan_json']:>5}{flag}")
        w()
        w("  OMed = Orphan Media (no JSON)  |  OJSN = Orphan JSON (no media)")
        w()

        # Orphan media
        sep()
        w(f"  ORPHAN MEDIA — no JSON sidecar found  ({o_media} files)")
        sep()
        if result.orphan_media:
            for mf in result.orphan_media:
                w(f"  x  [{mf.file_type.upper():<5}]  {relative(root, mf.path)}")
                w(f"          size : {human_size(mf.size_bytes)}")
        else:
            w("  None — every media file has a matching JSON. OK")
        w()

        # Orphan JSON
        sep()
        w(f"  ORPHAN JSON — no matching media found  ({o_json} files)")
        sep()
        if result.orphan_json:
            for jf in result.orphan_json:
                w(f"  x  {relative(root, jf.path)}")
                w(f"          size  : {human_size(jf.size_bytes)}")
                if jf.title_in_json:
                    t = (jf.title_in_json[:60] + "…") if len(jf.title_in_json) > 60 else jf.title_in_json
                    w(f"          title : \"{t}\"")
        else:
            w("  None — every JSON has a matching media file. OK")
        w()

        # Matched pairs
        sep()
        w(f"  MATCHED PAIRS  ({matched} total)")
        sep()
        if result.matched_pairs:
            for mf, jf in result.matched_pairs:
                meta = read_json_meta(jf.path)
                w(f"  v  {relative(root, mf.path)}")
                w(f"       JSON  : {relative(root, jf.path)}")
                parts = []
                if meta["date"]:    parts.append(f"date={meta['date']}")
                if meta["has_gps"]: parts.append("GPS=yes")
                if meta["title"]:   parts.append(f"title=\"{meta['title'][:30]}\"")
                if parts:
                    sep_str = "  |  "
                    w(f"       meta  : {sep_str.join(parts)}")
                w()
        else:
            w("  No matched pairs found.")

        # Footer
        w("=" * W)
        w(f"  Generated by takeout_analyser.py  |  {result.scanned_at}")
        w("=" * W)
        w()


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Analyse Google Takeout folder. Produces a log file and overview file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Output files (written next to the script, or in --out-dir):
  takeout_analysis.log    Full per-file log with match details
  takeout_overview.txt    Human-readable summary report

Examples:
  python takeout_analyser.py /path/to/Takeout
  python takeout_analyser.py /path/to/Takeout --out-dir /path/to/reports
  python takeout_analyser.py /path/to/Takeout --quiet
        """,
    )
    parser.add_argument("folder",
                        help="Root Google Takeout folder to scan")
    parser.add_argument("--out-dir", "-o", metavar="DIR", default=None,
                        help="Directory to write output files (default: same folder as script)")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Skip per-file discovery listing in the log")
    args = parser.parse_args()

    root = Path(args.folder).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        print(f"ERROR: Not a valid directory: {root}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else Path(__file__).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    log_path      = out_dir / "takeout_analysis.log"
    overview_path = out_dir / "takeout_overview.txt"

    # Terminal: only startup info + final summary
    print(f"\n  Google Takeout Analyser")
    print(f"  Root     : {root}")
    print(f"  Log    → {log_path}")
    print(f"  Overview → {overview_path}")
    print(f"\n  Scanning ...", end="", flush=True)

    log    = FileLogger(log_path)
    result = scan_folder(root, log, verbose=not args.quiet)

    print("  done.")
    print(f"  Writing overview ...", end="", flush=True)
    write_overview(result, overview_path)
    log.close()
    print("  done.\n")

    matched = len(result.matched_pairs)
    total_m = len(result.media_files)
    o_media = len(result.orphan_media)
    o_json  = len(result.orphan_json)
    rate    = (matched / total_m * 100) if total_m else 0

    print(f"  {'─'*46}")
    print(f"  Media files     : {total_m}")
    print(f"  Matched pairs   : {matched}  ({rate:.1f}%)")
    print(f"  Orphan media    : {o_media}  <- no JSON")
    print(f"  Orphan JSON     : {o_json}  <- no media")
    print(f"  {'─'*46}")
    print(f"\n  Log      -> {log_path}")
    print(f"  Overview -> {overview_path}\n")


if __name__ == "__main__":
    main()
