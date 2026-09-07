# Google Takeout Media Tools

Python command-line tools for analyzing Google Takeout folders and restoring JSON metadata into image and video files.

## Tools

### 1. Takeout Analyser

Scans a Google Takeout folder and generates detailed information about media files, JSON sidecars, and their relationships.

It reports:

- Total files
- Images and videos
- JSON sidecars
- Matched media/JSON pairs
- Orphan media without JSON
- Orphan JSON without media
- File sizes
- Extension breakdown
- Per-folder statistics
- Metadata such as dates, titles, descriptions, and GPS availability

The analyser produces:

- `takeout_analysis.log` — detailed scan log
- `takeout_overview.txt` — human-readable summary report



### 2. Takeout Merger

Merges metadata from Google Takeout JSON sidecar files into the corresponding image and video files.

Supported metadata includes:

- Date/time taken
- GPS coordinates
- Description/caption
- Title
- People/keywords
- Starred/favourite rating



## Supported Media

### Images

- JPG / JPEG
- PNG
- GIF
- BMP
- TIFF
- WebP
- HEIC / HEIF
- CR2
- CR3
- NEF
- ARW
- DNG
- ORF
- RW2
- PEF
- SRW
- SVG
- ICO

### Videos

- MP4
- MOV
- AVI
- MKV
- WebM
- MPEG / MPG
- M4V
- WMV
- 3GP / 3G2
- FLV
- TS
- MTS
- M2TS

## Requirements

- Python 3
- `piexif`
- `Pillow`
- ExifTool

Install the Python dependencies:

```bash
pip install -r requirements.txt
```

ExifTool is required by the merger for formats such as PNG, HEIC, WebP, MP4, MOV, and other non-JPEG media. JPEG files can use `piexif` directly.

## Installation

Clone the repository:

```bash
git clone https://github.com/adityaksx/Google-Takeout-Media-Tools.git
cd google-takeout-media-tools
```

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Install ExifTool separately if required.

### Linux

```bash
sudo apt install libimage-exiftool-perl
```

### macOS

```bash
brew install exiftool
```

### Windows

Install ExifTool and make sure it is available through your system `PATH`.

## Usage

### Analyze a Takeout folder

```bash
python takeout_analyser.py /path/to/Takeout
```

Save reports to a specific directory:

```bash
python takeout_analyser.py /path/to/Takeout --out-dir /path/to/reports
```

Reduce detailed file listings:

```bash
python takeout_analyser.py /path/to/Takeout --quiet
```



### Merge metadata

Write merged files to a separate directory:

```bash
python takeout_merger.py /path/to/Takeout --output /path/to/merged
```

Perform a dry run without modifying files:

```bash
python takeout_merger.py /path/to/Takeout --output /path/to/merged --dry-run
```

Merge metadata directly into the original files:

```bash
python takeout_merger.py /path/to/Takeout
```

> **Warning:** Omitting `--output` modifies the original media files.

Use multiple workers:

```bash
python takeout_merger.py /path/to/Takeout --output /path/to/merged --workers 4
```

Disable GPS metadata:

```bash
python takeout_merger.py /path/to/Takeout --output /path/to/merged --no-gps
```

Force ExifTool for all supported files:

```bash
python takeout_merger.py /path/to/Takeout --output /path/to/merged --force-exiftool
```



## Metadata Options

The merger enables these metadata fields by default:

```text
Date/Time
GPS
Description
Title
People/Keywords
Starred/Rating
```

Individual fields can be disabled:

```bash
--no-date
--no-gps
--no-desc
--no-title
--no-people
--no-starred
```



## How It Works

### Analysis

1. Recursively scans the Takeout folder.
2. Identifies image, video, and JSON files.
3. Detects Google Takeout filename variations.
4. Matches media files with JSON sidecars.
5. Identifies orphan media and orphan JSON files.
6. Generates detailed logs and an overview report.

The matching logic handles filename counters, truncated Google filenames, and parent-folder metadata.

### Metadata Merging

1. Scans the Takeout folder.
2. Matches media files with their JSON sidecars.
3. Reads metadata from the JSON.
4. Extracts the selected metadata fields.
5. Writes metadata into the media file.
6. Generates a detailed log and summary report.

JPEG files can be processed using `piexif`; other supported formats are handled through ExifTool.

## Output

The merger generates:

```text
takeout_merge.log
takeout_merge_report.txt
```

The analyser generates:

```text
takeout_analysis.log
takeout_overview.txt
```

Reports include processing statistics, extension breakdowns, errors, unmatched files, and other useful information.

## Safety

For large photo libraries, it is recommended to:

1. Run the analyser first.
2. Review the matching results.
3. Run the merger with `--dry-run`.
4. Write output to a separate directory using `--output`.
5. Verify the results before modifying originals.

## License

Add your preferred open-source license to the repository.