#!/usr/bin/env python3
"""
=============================================================================
  Google Takeout Metadata Merger  —  v2
  Merges JSON sidecar metadata into image and video files.

  Produces:
    takeout_merge.log         — full per-file detailed log
    takeout_merge_report.txt  — summary report with counts and errors

  Supported formats:
    JPEG/JPG  → piexif  (pure Python, zero external tools needed)
    PNG       → ExifTool
    HEIC/HEIF → ExifTool
    WebP      → ExifTool
    MP4/MOV   → ExifTool  (QuickTime tags)
    All others→ ExifTool

  Metadata written:
    Date/Time taken, GPS coordinates, Description/Caption,
    Title, People/keywords, Starred/Favourite (Rating)

  Requirements:
    pip install piexif pillow
    ExifTool  →  https://exiftool.org
                 Linux:  sudo apt install libimage-exiftool-perl
                 Mac:    brew install exiftool
                 Windows: download exe, add to PATH

  Usage:
    python takeout_merger.py /path/to/Takeout --output /path/to/merged
    python takeout_merger.py /path/to/Takeout --output /path/to/merged --dry-run
    python takeout_merger.py /path/to/Takeout  (in-place — modifies originals!)
    python takeout_merger.py /path/to/Takeout --output /path/merged --no-gps --workers 4
    python takeout_merger.py /path/to/Takeout --output /path/merged --force-exiftool
=============================================================================
"""

import os
import sys
import json
import re
import shutil
import argparse
import subprocess
import traceback
import threading
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Optional deps
# ──────────────────────────────────────────────────────────────────────────────
try:
    import piexif
    HAS_PIEXIF = True
except ImportError:
    HAS_PIEXIF = False

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


# ──────────────────────────────────────────────────────────────────────────────
# Extension sets
# ──────────────────────────────────────────────────────────────────────────────
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif", ".cr2", ".cr3", ".nef", ".arw",
    ".dng", ".orf", ".rw2", ".pef", ".srw",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".mpeg", ".mpg",
    ".m4v", ".wmv", ".3gp", ".3g2", ".flv", ".ts", ".mts", ".m2ts",
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
JPEG_EXTENSIONS  = {".jpg", ".jpeg"}

JSON_SIDECAR_SUFFIXES = [
    ".json",
    ".supplemental-metadata.json",
    ".sup-meta.json",
    ".metadata.json",
]
GOOGLE_TRUNCATION_LENGTHS = [46, 47, 48, 49, 50, 51]


# ──────────────────────────────────────────────────────────────────────────────
# Thread-safe logger — writes to file only, nothing to terminal
# ──────────────────────────────────────────────────────────────────────────────
class FileLogger:
    def __init__(self, log_path: Path):
        self._f    = open(log_path, "w", encoding="utf-8")
        self._lock = threading.Lock()
        self.log_path = log_path

    def _ts(self): return datetime.now().strftime("%H:%M:%S")

    def _w(self, line):
        with self._lock:
            self._f.write(line + "\n")
            self._f.flush()

    def write(self, line=""): self._w(line)
    def sep(self, c="─", w=72): self._w(c * w)

    def header(self, title):
        self._w(""); self.sep("═")
        self._w(f"  {title}"); self.sep("═")

    def section(self, title):
        self._w(""); self.sep()
        self._w(f"  {title}"); self.sep()

    def ok(self,   msg): self._w(f"  [OK   {self._ts()}]  {msg}")
    def warn(self, msg): self._w(f"  [WARN {self._ts()}]  {msg}")
    def err(self,  msg): self._w(f"  [ERR  {self._ts()}]  {msg}")
    def skip(self, msg): self._w(f"  [SKIP {self._ts()}]  {msg}")
    def dry(self,  msg): self._w(f"  [DRY  {self._ts()}]  {msg}")
    def info(self, msg): self._w(f"  [INFO {self._ts()}]  {msg}")
    def raw(self,  msg): self._w(f"  {msg}")

    def close(self): self._f.close()


LOG: FileLogger = None


# ──────────────────────────────────────────────────────────────────────────────
# Thread-safe counters
# ──────────────────────────────────────────────────────────────────────────────
class Counters:
    def __init__(self):
        self._lock = threading.Lock()
        self.ok = self.warn = self.err = self.skip = self.dry = self.no_json = 0
        self.piexif_ok = self.exiftool_ok = 0
        self.by_ext: dict = defaultdict(lambda: {"ok": 0, "err": 0, "skip": 0})
        self.failed: list = []
        self.warned: list = []

    def inc(self, attr, n=1):
        with self._lock:
            setattr(self, attr, getattr(self, attr) + n)

    def add_ext(self, ext, result):
        with self._lock:
            self.by_ext[ext][result] += 1

    def add_failed(self, entry):
        with self._lock: self.failed.append(entry)

    def add_warned(self, entry):
        with self._lock: self.warned.append(entry)


CTR: Counters = None


# ──────────────────────────────────────────────────────────────────────────────
# Filename matching — handles all known Google Takeout naming quirks
# ──────────────────────────────────────────────────────────────────────────────
def stem_variants(media_path: Path) -> list:
    name   = media_path.name
    stem   = media_path.stem
    suffix = media_path.suffix
    variants = [name]

    # Strip (1) counter from media: photo(1).jpg → photo.jpg
    stripped = re.sub(r'\(\d+\)$', '', stem) + suffix
    if stripped != name:
        variants.append(stripped)

    # JSON has counter: photo.jpg → photo(1).jpg.json ... photo(5).jpg.json
    for n in range(1, 6):
        variants.append(f"{stem}({n}){suffix}")
        bare = re.sub(r'\(\d+\)$', '', stem)
        variants.append(f"{bare}({n}){suffix}")

    # Google truncates filenames at 46-51 characters
    for trunc in GOOGLE_TRUNCATION_LENGTHS:
        base = stem[:trunc]
        if base != stem:
            variants.append(base + suffix)
            for n in range(1, 4):
                variants.append(f"{base}({n}){suffix}")

    # Handle Google's -edited suffix: IMG_001-edited.jpg → look for IMG_001.jpg.json
    edited_stripped = re.sub(r'-edited$', '', stem, flags=re.IGNORECASE) + suffix
    if edited_stripped != name:
        variants.append(edited_stripped)

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
                return json_lookup[key], f"variant='{base}' suf='{suf}'"
    # Bare stem fallback: photo.jpg → photo.json
    key = (folder, media_path.stem + ".json")
    if key in json_lookup:
        return json_lookup[key], "bare-stem"
    # Parent folder fallback (rare: JSON in parent dir)
    for base in stem_variants(media_path)[:3]:
        for suf in JSON_SIDECAR_SUFFIXES:
            key = (folder.parent, base + suf)
            if key in json_lookup:
                return json_lookup[key], f"parent-folder '{base}{suf}'"
    return None, None


# ──────────────────────────────────────────────────────────────────────────────
# JSON parsing & tag extraction
# ──────────────────────────────────────────────────────────────────────────────
def parse_json(json_path: Path) -> Optional[dict]:
    try:
        with open(json_path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception as e:
        LOG.err(f"Cannot parse JSON: {json_path.name}  →  {e}")
        return None


def ts_to_exif_dt(timestamp: str) -> str:
    """Unix timestamp → 'YYYY:MM:DD HH:MM:SS' local time"""
    dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).astimezone()
    return dt.strftime("%Y:%m:%d %H:%M:%S")


def ts_to_iso(timestamp: str) -> str:
    """Unix timestamp → ISO 8601 UTC for XMP"""
    dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def decimal_to_dms(decimal: float):
    """Decimal degrees → ((deg,1),(min,1),(sec*100,100)) for piexif GPS."""
    abs_val = abs(decimal)
    deg  = int(abs_val)
    mins = int((abs_val - deg) * 60)
    sec  = round(((abs_val - deg) * 60 - mins) * 60 * 100)
    return ((deg, 1), (mins, 1), (sec, 100))


def extract_tags(data: dict, opts: dict) -> dict:
    tags = {}

    # Date/Time
    if opts["date"]:
        ts = (data.get("photoTakenTime") or data.get("creationTime") or {}).get("timestamp")
        if ts:
            tags["datetime"]     = ts_to_exif_dt(ts)
            tags["datetime_iso"] = ts_to_iso(ts)

    # GPS — prefer geoDataExif (more accurate camera GPS vs display GPS)
    if opts["gps"]:
        for geo_key in ("geoDataExif", "geoData"):
            geo = data.get(geo_key)
            if not geo:
                continue
            lat = float(geo.get("latitude",  0))
            lon = float(geo.get("longitude", 0))
            alt = float(geo.get("altitude",  0))
            if abs(lat) < 0.0001 and abs(lon) < 0.0001:
                continue  # (0,0) = Google placeholder for "no GPS"
            tags["gps_lat"] = lat
            tags["gps_lon"] = lon
            tags["gps_alt"] = alt
            break

    # Description
    if opts["desc"] and data.get("description", "").strip():
        tags["description"] = data["description"].strip()

    # Title
    if opts["title"] and data.get("title", "").strip():
        tags["title"] = data["title"].strip()

    # People
    if opts["people"]:
        people = [p["name"].strip() for p in data.get("people", []) if p.get("name", "").strip()]
        if people:
            tags["people"] = people

    # Starred
    if opts["starred"] and data.get("starred"):
        tags["rating"] = 5

    return tags


# ──────────────────────────────────────────────────────────────────────────────
# JPEG merger — pure Python via piexif (fast, no ExifTool needed)
# ──────────────────────────────────────────────────────────────────────────────
def merge_jpeg_piexif(src: Path, dst: Path, tags: dict, dry_run: bool) -> bool:
    if dry_run:
        LOG.dry(f"[piexif] {src.name}  tags={list(tags.keys())}"); return True
    if not HAS_PIEXIF:
        return False
    try:
        try:
            exif_dict = piexif.load(str(src))
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        for ifd in ("0th", "Exif", "GPS", "1st"):
            if not isinstance(exif_dict.get(ifd), dict):
                exif_dict[ifd] = {}

        if "datetime" in tags:
            dt_b = tags["datetime"].encode("ascii")
            exif_dict["0th"][piexif.ImageIFD.DateTime]          = dt_b
            exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal]  = dt_b
            exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = dt_b

        if "gps_lat" in tags:
            lat, lon, alt = tags["gps_lat"], tags["gps_lon"], tags["gps_alt"]
            g = exif_dict["GPS"]
            g[piexif.GPSIFD.GPSVersionID]    = (2, 3, 0, 0)
            g[piexif.GPSIFD.GPSLatitudeRef]  = b"N" if lat >= 0 else b"S"
            g[piexif.GPSIFD.GPSLatitude]     = decimal_to_dms(lat)
            g[piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"
            g[piexif.GPSIFD.GPSLongitude]    = decimal_to_dms(lon)
            g[piexif.GPSIFD.GPSAltitudeRef]  = b"\x00" if alt >= 0 else b"\x01"
            g[piexif.GPSIFD.GPSAltitude]     = (abs(round(alt * 100)), 100)

        if "description" in tags:
            exif_dict["0th"][piexif.ImageIFD.ImageDescription] = (
                tags["description"][:1000].encode("utf-8", errors="replace"))

        if "title" in tags:
            exif_dict["0th"][piexif.ImageIFD.XPTitle] = (
                (tags["title"] + "\x00").encode("utf-16-le"))

        if "people" in tags:
            kw = "; ".join(tags["people"])
            exif_dict["0th"][piexif.ImageIFD.XPKeywords] = (
                (kw + "\x00").encode("utf-16-le"))

        if "rating" in tags:
            exif_dict["0th"][piexif.ImageIFD.Rating] = tags["rating"]

        exif_bytes = piexif.dump(exif_dict)
        if src != dst:
            shutil.copy2(src, dst)
        piexif.insert(exif_bytes, str(dst))
        return True

    except piexif.InvalidImageDataError as e:
        LOG.err(f"[piexif] Invalid JPEG: {src.name}  →  {e}")
    except Exception as e:
        LOG.err(f"[piexif] {src.name}  →  {type(e).__name__}: {e}")
        if src != dst and dst.exists():
            try: dst.unlink()
            except: pass
    return False


# ──────────────────────────────────────────────────────────────────────────────
# ExifTool merger — handles PNG, HEIC, WebP, MP4, MOV, etc.
# ──────────────────────────────────────────────────────────────────────────────
def check_exiftool() -> Optional[str]:
    for cmd in ("exiftool", "exiftool.exe"):
        try:
            r = subprocess.run([cmd, "-ver"], capture_output=True, timeout=5)
            if r.returncode == 0:
                LOG.info(f"ExifTool version: {r.stdout.decode().strip()}")
                return cmd
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


EXIFTOOL_CMD: Optional[str] = None


def build_exiftool_args(tags: dict, ext: str) -> list:
    args = []
    is_video = ext in VIDEO_EXTENSIONS

    if "datetime" in tags:
        dt  = tags["datetime"]
        iso = tags.get("datetime_iso", dt)
        if is_video:
            for tag in ("QuickTime:CreateDate", "QuickTime:ModifyDate",
                        "QuickTime:TrackCreateDate", "QuickTime:TrackModifyDate",
                        "QuickTime:MediaCreateDate", "QuickTime:MediaModifyDate"):
                args.append(f"-{tag}={dt}")
        else:
            args += [
                f"-EXIF:DateTimeOriginal={dt}",
                f"-EXIF:CreateDate={dt}",
                f"-EXIF:ModifyDate={dt}",
                f"-XMP:DateTimeOriginal={iso}",
                f"-XMP:CreateDate={iso}",
            ]

    if "gps_lat" in tags:
        lat, lon, alt = tags["gps_lat"], tags["gps_lon"], tags["gps_alt"]
        args += [
            f"-EXIF:GPSLatitude={abs(lat)}",
            f"-EXIF:GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
            f"-EXIF:GPSLongitude={abs(lon)}",
            f"-EXIF:GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
            f"-EXIF:GPSAltitude={abs(alt)}",
            f"-EXIF:GPSAltitudeRef={'0' if alt >= 0 else '1'}",
            f"-XMP:GPSLatitude={lat}",
            f"-XMP:GPSLongitude={lon}",
        ]

    if "description" in tags:
        d = tags["description"].replace('"', '\\"')
        args += [f"-EXIF:ImageDescription={d}",
                 f"-IPTC:Caption-Abstract={d}",
                 f"-XMP:Description={d}"]
        if is_video:
            args.append(f"-QuickTime:Description={d}")

    if "title" in tags:
        t = tags["title"].replace('"', '\\"')
        args += [f"-XMP:Title={t}", f"-IPTC:ObjectName={t}"]
        if is_video:
            args.append(f"-QuickTime:Title={t}")

    if "people" in tags:
        for p in tags["people"]:
            args += [f"-XMP:Subject+={p}", f"-IPTC:Keywords+={p}"]

    if "rating" in tags:
        args += [f"-EXIF:Rating={tags['rating']}", f"-XMP:Rating={tags['rating']}"]

    return args


def merge_exiftool(src: Path, dst: Path, tags: dict, dry_run: bool) -> bool:
    if not EXIFTOOL_CMD:
        LOG.warn(f"[exiftool] Not available — copying as-is: {src.name}")
        if not dry_run and src != dst:
            shutil.copy2(src, dst)
        return False

    et_args = build_exiftool_args(tags, src.suffix.lower())
    if not et_args:
        if not dry_run and src != dst:
            shutil.copy2(src, dst)
        LOG.skip(f"[exiftool] No tag args: {src.name}")
        return True

    if dry_run:
        LOG.dry(f"[exiftool] {src.name}  {len(et_args)} args"); return True

    if src != dst:
        shutil.copy2(src, dst)

    try:
        cmd = ([EXIFTOOL_CMD, "-overwrite_original", "-ignoreMinorErrors", "-m"]
               + et_args + [str(dst)])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            LOG.err(f"[exiftool] {src.name}  →  {result.stderr.strip()[:200]}")
            return False
        stdout = result.stdout.strip()
        if stdout and "1 image files updated" not in stdout:
            LOG.warn(f"[exiftool] {src.name}  stdout: {stdout[:120]}")
        return True
    except subprocess.TimeoutExpired:
        LOG.err(f"[exiftool] Timeout (120s): {src.name}"); return False
    except Exception as e:
        LOG.err(f"[exiftool] {src.name}: {type(e).__name__}: {e}"); return False


# ──────────────────────────────────────────────────────────────────────────────
# Per-file processing
# ──────────────────────────────────────────────────────────────────────────────
def process_file(src: Path, json_path: Optional[Path], dst: Path,
                 opts: dict, dry_run: bool) -> str:
    ext = src.suffix.lower()

    if json_path is None:
        if not dry_run and src != dst:
            shutil.copy2(src, dst)
        LOG.warn(f"No JSON sidecar — copied as-is: {src.name}")
        CTR.inc("warn"); CTR.inc("no_json")
        CTR.add_ext(ext, "skip")
        CTR.add_warned({"file": str(src), "reason": "no JSON sidecar"})
        return "warn"

    data = parse_json(json_path)
    if data is None:
        if not dry_run and src != dst:
            shutil.copy2(src, dst)
        CTR.inc("err"); CTR.add_ext(ext, "err")
        CTR.add_failed({"file": str(src), "reason": "JSON parse failed"})
        return "err"

    tags = extract_tags(data, opts)

    if not tags:
        if not dry_run and src != dst:
            shutil.copy2(src, dst)
        LOG.skip(f"No applicable tags — copied: {src.name}")
        CTR.inc("skip"); CTR.add_ext(ext, "skip")
        return "skip"

    tag_parts = []
    if "datetime"    in tags: tag_parts.append(f"date={tags['datetime']}")
    if "gps_lat"     in tags: tag_parts.append(f"GPS={tags['gps_lat']:.4f},{tags['gps_lon']:.4f}")
    if "description" in tags: tag_parts.append(f"desc=\"{tags['description'][:40]}\"")
    if "title"       in tags: tag_parts.append(f"title=\"{tags['title'][:40]}\"")
    if "people"      in tags: tag_parts.append(f"people={tags['people']}")
    if "rating"      in tags: tag_parts.append("starred=5")
    tag_str = "  |  ".join(tag_parts)

    if ext in JPEG_EXTENSIONS and HAS_PIEXIF and not opts["force_exiftool"]:
        success = merge_jpeg_piexif(src, dst, tags, dry_run)
        tool    = "piexif"
        if success: CTR.inc("piexif_ok")
    else:
        success = merge_exiftool(src, dst, tags, dry_run)
        tool    = "exiftool"
        if success: CTR.inc("exiftool_ok")

    if success:
        LOG.ok(f"[{tool}] {src.name}")
        LOG.raw(f"         JSON   : {json_path.name}")
        LOG.raw(f"         Tags   : {tag_str}")
        if not dry_run:
            LOG.raw(f"         Output : {dst}")
        CTR.inc("ok"); CTR.add_ext(ext, "ok")
        return "ok"
    else:
        CTR.inc("err"); CTR.add_ext(ext, "err")
        CTR.add_failed({"file": str(src), "reason": f"{tool} failed"})
        return "err"


# ──────────────────────────────────────────────────────────────────────────────
# Scanner
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class Job:
    src:       Path
    json_path: Optional[Path]
    dst:       Path
    rel:       str


def scan_and_plan(root: Path, output: Optional[Path]) -> list:
    media_files = []
    json_lookup = {}
    skip_names  = {"user-generated-memory-titles.json", "metadata.json", "album-metadata.json"}

    LOG.section("SCANNING FOLDER TREE")
    LOG.info(f"Root: {root}")

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        folder = Path(dirpath)
        for fname in sorted(filenames):
            fpath = folder / fname
            ext   = fpath.suffix.lower()
            if ext in MEDIA_EXTENSIONS:
                media_files.append(fpath)
            elif fname.lower().endswith(".json"):
                if fname.lower() in skip_names:
                    LOG.skip(f"Non-sidecar JSON skipped: {fname}")
                    continue
                json_lookup[(folder, fname)]         = fpath
                json_lookup[(folder, fname.lower())] = fpath

    LOG.info(f"Media files : {len(media_files)}")
    LOG.info(f"JSON files  : {len(json_lookup) // 2}")

    jobs = []
    for media in media_files:
        json_path, method = find_json_for_media(media, json_lookup)
        if output:
            try:   rel = media.relative_to(root)
            except ValueError: rel = Path(media.name)
            dst = output / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
        else:
            dst = media

        rel_str = str(media.relative_to(root)) if (
            root in media.parents or media.parent == root) else media.name

        if json_path:
            LOG.info(f"PLAN  {rel_str}  →  {json_path.name}  ({method})")
        else:
            LOG.warn(f"PLAN  {rel_str}  →  NO JSON")

        jobs.append(Job(src=media, json_path=json_path, dst=dst, rel=rel_str))

    return jobs


# ──────────────────────────────────────────────────────────────────────────────
# Report writer
# ──────────────────────────────────────────────────────────────────────────────
def write_report(report_path: Path, opts: dict, root: Path, output: Optional[Path],
                 total: int, start_time: datetime, dry_run: bool):
    elapsed = (datetime.now() - start_time).total_seconds()
    W = 72
    def pad(label, val):
        dots = W - len(label) - len(str(val)) - 4
        return f"  {label} {'.' * max(dots, 1)} {val}"

    with open(report_path, "w", encoding="utf-8") as f:
        def w(line=""): f.write(line + "\n")
        def sep(c="─"): w(c * W)

        w("=" * W)
        w("  GOOGLE TAKEOUT MERGER — MERGE REPORT")
        w("=" * W)
        w(f"  Run at   : {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        w(f"  Elapsed  : {elapsed:.1f}s")
        w(f"  Source   : {root}")
        w(f"  Output   : {output if output else '(in-place)'}")
        w(f"  Dry run  : {'YES — no files were modified' if dry_run else 'No'}")
        w()

        sep("═"); w("  RESULTS SUMMARY"); sep("═")
        w(pad("Total media files",            total))
        w(pad("Successfully merged",          CTR.ok))
        w(pad("  ├─ via piexif (JPEG)",       CTR.piexif_ok))
        w(pad("  └─ via ExifTool",            CTR.exiftool_ok))
        w(pad("No JSON (copied as-is)",       CTR.no_json))
        w(pad("Skipped (no applicable tags)", CTR.skip))
        w(pad("Errors",                       CTR.err))
        w()
        ok_rate = (CTR.ok / total * 100) if total else 0
        bar = "█" * int(ok_rate / 2) + "░" * (50 - int(ok_rate / 2))
        w(f"  Merge rate : {ok_rate:.1f}%  [{bar}]")
        w()

        sep(); w("  OPTIONS USED"); sep()
        for k, v in opts.items():
            w(f"  [{'ON ' if v else 'OFF'}]  {k}")
        w()

        sep(); w("  RESULTS BY EXTENSION"); sep()
        w(f"  {'Extension':<14}  {'OK':>6}  {'Skip':>6}  {'Error':>6}")
        w("  " + "─" * (W - 2))
        for ext in sorted(CTR.by_ext.keys()):
            s = CTR.by_ext[ext]
            w(f"  {ext:<14}  {s['ok']:>6}  {s['skip']:>6}  {s['err']:>6}")
        w()

        sep(); w(f"  ERRORS  ({CTR.err} files)"); sep()
        if CTR.failed:
            for e in CTR.failed:
                w(f"  x  {e['file']}")
                w(f"       reason: {e['reason']}")
                w()
        else:
            w("  None — all files processed without errors.")
        w()

        sep(); w(f"  FILES WITH NO JSON SIDECAR  ({CTR.no_json} — copied without metadata)"); sep()
        if CTR.warned:
            for e in CTR.warned:
                w(f"  -  {e['file']}")
        else:
            w("  None.")
        w()

        w("=" * W)
        w(f"  Log file  : {LOG.log_path}")
        w(f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        w("=" * W)
        w()


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
def main():
    global LOG, EXIFTOOL_CMD, CTR

    parser = argparse.ArgumentParser(
        description="Merge Google Takeout JSON metadata into image/video files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Output files (in --log-dir, default: same folder as script):
  takeout_merge.log         Full per-file processing log
  takeout_merge_report.txt  Summary report

Examples:
  python takeout_merger.py /path/to/Takeout --output /path/to/merged
  python takeout_merger.py /path/to/Takeout --output /path/to/merged --dry-run
  python takeout_merger.py /path/to/Takeout  (in-place!)
  python takeout_merger.py /path/to/Takeout --output /path/merged --no-gps --workers 4
  python takeout_merger.py /path/to/Takeout --output /path/merged --force-exiftool
        """,
    )
    parser.add_argument("folder", help="Root Google Takeout folder")
    parser.add_argument("--output", "-o", metavar="DIR",
                        help="Write merged files here. Omit to modify IN-PLACE.")
    parser.add_argument("--log-dir", metavar="DIR", default=None,
                        help="Directory for log/report files (default: script folder)")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Show what would be done — no files modified")
    parser.add_argument("--workers", "-w", type=int, default=1,
                        help="Parallel workers (default 1; use 4-8 for large libraries)")

    meta = parser.add_argument_group("Metadata fields — all ON by default")
    meta.add_argument("--no-date",    action="store_true", help="Skip date/time")
    meta.add_argument("--no-gps",     action="store_true", help="Skip GPS coordinates")
    meta.add_argument("--no-desc",    action="store_true", help="Skip description/caption")
    meta.add_argument("--no-title",   action="store_true", help="Skip title")
    meta.add_argument("--no-people",  action="store_true", help="Skip people/keywords")
    meta.add_argument("--no-starred", action="store_true", help="Skip starred/rating")

    tools = parser.add_argument_group("Tool options")
    tools.add_argument("--force-exiftool", action="store_true",
                       help="Use ExifTool for ALL files including JPEG")
    tools.add_argument("--no-exiftool",    action="store_true",
                       help="Disable ExifTool — only JPEG via piexif; others copied as-is")
    args = parser.parse_args()

    root = Path(args.folder).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        print(f"ERROR: Not a valid directory: {root}", file=sys.stderr); sys.exit(1)

    output  = Path(args.output).expanduser().resolve() if args.output else None
    if output: output.mkdir(parents=True, exist_ok=True)

    log_dir = Path(args.log_dir).expanduser().resolve() if args.log_dir else Path(__file__).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path    = log_dir / "takeout_merge.log"
    report_path = log_dir / "takeout_merge_report.txt"

    LOG = FileLogger(log_path)
    CTR = Counters()
    start_time = datetime.now()

    LOG.header("GOOGLE TAKEOUT METADATA MERGER — DETAILED LOG")
    LOG.write(f"  Source   : {root}")
    LOG.write(f"  Output   : {output if output else '(in-place)'}")
    LOG.write(f"  Log      : {log_path}")
    LOG.write(f"  Report   : {report_path}")
    LOG.write(f"  Started  : {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    LOG.write(f"  Dry run  : {args.dry_run}")
    LOG.write(f"  Workers  : {args.workers}")

    print(f"\n  Google Takeout Merger")
    print(f"  Source   : {root}")
    print(f"  Output   : {output if output else '(in-place — originals will be modified!)'}")
    print(f"  Log    → {log_path}")
    print(f"  Report → {report_path}")
    if args.dry_run:
        print(f"\n  *** DRY RUN — no files will be modified ***")
    print()

    LOG.section("DEPENDENCY CHECK")
    if HAS_PIEXIF:
        LOG.ok("piexif installed"); print("  [OK] piexif")
    else:
        LOG.warn("piexif not installed — pip install piexif"); print("  [!!] piexif missing  →  pip install piexif")

    if not args.no_exiftool:
        EXIFTOOL_CMD = check_exiftool()
        if EXIFTOOL_CMD:
            print(f"  [OK] ExifTool: {EXIFTOOL_CMD}")
        else:
            LOG.warn("ExifTool not found — HEIC/PNG/WebP/MP4/MOV copied without metadata")
            print("  [!!] ExifTool not found → https://exiftool.org")
            print("       HEIC / PNG / WebP / MP4 / MOV will be copied without metadata.")
    else:
        print("  [--] ExifTool disabled")

    opts = {
        "date":           not args.no_date,
        "gps":            not args.no_gps,
        "desc":           not args.no_desc,
        "title":          not args.no_title,
        "people":         not args.no_people,
        "starred":        not args.no_starred,
        "force_exiftool": args.force_exiftool,
    }
    LOG.section("OPTIONS")
    for k, v in opts.items():
        LOG.raw(f"  [{'ON ' if v else 'OFF'}]  {k}")
    print(f"\n  Metadata : {', '.join(k for k, v in opts.items() if v and k != 'force_exiftool')}")

    print(f"\n  Scanning ...", end="", flush=True)
    jobs  = scan_and_plan(root, output)
    total = len(jobs)
    matched = sum(1 for j in jobs if j.json_path)
    print(f"  {total} files  ({matched} matched, {total - matched} no JSON)\n")

    if args.dry_run:
        LOG.section("DRY RUN — plan only, no files touched")

    LOG.section(f"PROCESSING  ({total} files, workers={args.workers})")
    done_count = 0
    done_lock  = threading.Lock()

    def run_job(job: Job) -> str:
        result = process_file(job.src, job.json_path, job.dst, opts, args.dry_run)
        nonlocal done_count
        with done_lock:
            done_count += 1
            pct = done_count / total * 100
            print(f"\r  Progress: {done_count}/{total} ({pct:.1f}%)  "
                  f"OK={CTR.ok}  Warn={CTR.warn}  Err={CTR.err}  Skip={CTR.skip}   ",
                  end="", flush=True)
        return result

    if args.workers > 1 and total > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(run_job, j): j for j in jobs}
            for fut in as_completed(futures):
                try: fut.result()
                except Exception as e: LOG.err(f"Thread error: {e}")
    else:
        for job in jobs:
            try: run_job(job)
            except Exception as e:
                LOG.err(f"Error on {job.src.name}: {e}"); traceback.print_exc()

    print()
    elapsed = (datetime.now() - start_time).total_seconds()
    LOG.section("DONE")
    LOG.info(f"Elapsed:{elapsed:.1f}s  OK:{CTR.ok}  Warn:{CTR.warn}  Err:{CTR.err}  Skip:{CTR.skip}")
    LOG.close()
    write_report(report_path, opts, root, output, total, start_time, args.dry_run)

    ok_rate = (CTR.ok / total * 100) if total else 0
    print(f"\n  {'─'*52}")
    print(f"  Total           : {total}")
    print(f"  Merged          : {CTR.ok}  ({ok_rate:.1f}%)")
    print(f"    ├─ piexif     : {CTR.piexif_ok}  (JPEG)")
    print(f"    └─ ExifTool   : {CTR.exiftool_ok}  (HEIC/PNG/MP4/MOV/etc)")
    print(f"  No JSON (copied): {CTR.no_json}")
    print(f"  Skipped         : {CTR.skip}")
    print(f"  Errors          : {CTR.err}")
    print(f"  Elapsed         : {elapsed:.1f}s")
    print(f"  {'─'*52}")
    print(f"\n  Log    → {log_path}")
    print(f"  Report → {report_path}\n")
    if CTR.err > 0:
        print(f"  ⚠  {CTR.err} error(s) — check report for details.\n")


if __name__ == "__main__":
    main()
