"""
Rename images copied off the camera's memory card using the GUI's shot log.

During a dive the camera saves to its own card (nothing is sent up the
tether), but it tells the GUI each time it has saved a shot. The GUI writes
one row per shot to shot_log_<session>.csv with the PC time, seabed altitude,
strobe/lamp levels and camera settings. After the dive, copy the dive's
images off the card into a folder and run:

    python card_rename.py <image folder> <shot_log.csv> [--prefix DIVE01] [--no-alt] [--dry-run]

(or use "Rename Card Images..." in the GUI). Each image is matched to its
shot and renamed

    <prefix>_<YYYYmmdd_HHMMSS_mmm>_alt<x.xx>m_<camera name>.<ext>

using the shot's PC time and altitude. Rows are also written to
image_log.csv in the image folder.

Matching: the camera reports exactly one event per saved shot, so when the
folder holds the same number of shots as the log they are paired in order
(by capture time). Otherwise each image's EXIF capture time is aligned to the
log, allowing for the camera clock being offset from the PC clock, and only
images that line up are renamed.
"""
import argparse
import csv
import datetime
import os
import re
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # only needed to read EXIF times
    Image = None

IMAGE_EXTS = {".jpg", ".jpeg", ".arw", ".hif", ".heif", ".dng"}

SHOT_LOG_FIELDS = ["shot", "time", "seabed_alt_m", "strobe_intensity",
                   "lamp_intensity", "mode", "shutter", "aperture", "iso", "focus"]
IMAGE_LOG_FILENAME = "image_log.csv"

_EXIF_IFD = 0x8769
_DATETIME_ORIGINAL = 36867
_SUBSEC_ORIGINAL = 37521


def build_image_name(src, when, alt=None, prefix="", add_alt=True):
    """<prefix>_<YYYYmmdd_HHMMSS_mmm>[_alt<x.xx>m]_<camera name><ext> beside src."""
    src = Path(src)
    parts = []
    prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", (prefix or "").strip()).strip("-")
    if prefix:
        parts.append(prefix)
    parts.append(when.strftime("%Y%m%d_%H%M%S_") + f"{when.microsecond // 1000:03d}")
    if add_alt and alt is not None:
        parts.append(f"alt{alt:.2f}m")
    parts.append(src.stem)
    dest = src.with_name("_".join(parts) + src.suffix)
    n = 1
    while dest.exists() and dest != src:
        dest = src.with_name("_".join(parts) + f"_{n}" + src.suffix)
        n += 1
    return dest


def append_csv(path, fields, row):
    path = Path(path)
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(row)


# ---------- reading the inputs ----------
def read_shot_log(path):
    shots = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                row["_time"] = datetime.datetime.fromisoformat(row["time"])
            except (KeyError, ValueError):
                continue
            alt = row.get("seabed_alt_m", "")
            row["_alt"] = float(alt) if alt not in ("", None) else None
            shots.append(row)
    shots.sort(key=lambda r: r["_time"])
    return shots


def _exif_time(path):
    """(capture datetime, has sub-seconds) from EXIF, or (None, False)."""
    if Image is None:
        return None, False
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            ifd = exif.get_ifd(_EXIF_IFD)
            raw = ifd.get(_DATETIME_ORIGINAL) or exif.get(306)  # 306 = DateTime
            if not raw:
                return None, False
            t = datetime.datetime.strptime(str(raw).strip("\x00 "), "%Y:%m:%d %H:%M:%S")
            sub = str(ifd.get(_SUBSEC_ORIGINAL, "") or "").strip("\x00 ")
            if sub.isdigit():
                t += datetime.timedelta(seconds=float("0." + sub))
                return t, True
            return t, False
    except Exception:
        return None, False


def read_card_images(folder):
    """
    Group the folder's images by camera name (RAW + JPEG of one shot share a
    name) and give each group a capture time. Returns a list of dicts sorted
    by capture time: {"stem", "files", "time", "subsec"}.
    Files already renamed by this tool are skipped.
    """
    groups = {}
    for p in Path(folder).iterdir():
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            groups.setdefault(p.stem.upper(), []).append(p)

    shots = []
    for stem, files in groups.items():
        if re.match(r".*\d{8}_\d{6}_\d{3}", stem):
            continue  # already renamed
        t, subsec = None, False
        # JPEG/HEIF carry EXIF that Pillow can read; RAW falls back to file time
        for p in sorted(files, key=lambda p: p.suffix.lower() not in (".jpg", ".jpeg")):
            t, subsec = _exif_time(p)
            if t is not None:
                break
        if t is None:
            t = datetime.datetime.fromtimestamp(min(p.stat().st_mtime for p in files))
        shots.append({"stem": stem, "files": sorted(files), "time": t, "subsec": subsec})
    shots.sort(key=lambda g: (g["time"], g["stem"]))
    return shots


# ---------- matching ----------
def _match_with_offset(images, shots, offset, tol):
    """Monotonic greedy match of image times (minus offset) to shot times."""
    pairs, j = [], 0
    for i, img in enumerate(images):
        t = img["time"] - offset
        while j < len(shots) and shots[j]["_time"] < t - tol:
            j += 1
        if j < len(shots) and abs(shots[j]["_time"] - t) <= tol:
            pairs.append((i, j))
            j += 1
    return pairs


def match_images(images, shots):
    """
    Return (pairs, how) where pairs is a list of (image index, shot index).
    """
    if not images or not shots:
        return [], "nothing to match"
    if len(images) == len(shots):
        return [(i, i) for i in range(len(images))], "paired in order (counts match)"

    # Counts differ: find the camera-clock offset that lines most images up.
    subsec = all(img["subsec"] for img in images)
    tol = datetime.timedelta(seconds=0.5 if subsec else 1.0)
    candidates = set()
    for img in images[:15] + images[-15:]:
        for s in shots[:15] + shots[-15:]:
            candidates.add(img["time"] - s["_time"])
    best = []
    for offset in candidates:
        pairs = _match_with_offset(images, shots, offset, tol)
        if len(pairs) > len(best):
            best = pairs
    return best, f"matched by capture time ({len(best)} of {len(images)} images)"


# ---------- renaming ----------
def plan_renames(folder, shot_log, prefix="", add_alt=True):
    """
    Work out the renames without touching any file.
    Returns (renames, unmatched, how): renames is a list of
    (src Path, dest Path, shot row), unmatched a list of image groups.
    """
    images = read_card_images(folder)
    shots = read_shot_log(shot_log)
    pairs, how = match_images(images, shots)
    matched = {i for i, _ in pairs}
    renames, taken = [], set()
    for i, j in pairs:
        shot = shots[j]
        for src in images[i]["files"]:
            dest = build_image_name(src, shot["_time"], shot["_alt"], prefix, add_alt)
            while dest in taken:
                dest = dest.with_name(dest.stem + "_dup" + dest.suffix)
            taken.add(dest)
            renames.append((src, dest, shot))
    unmatched = [img for k, img in enumerate(images) if k not in matched]
    return renames, unmatched, how


def apply_renames(folder, renames):
    """Rename the files and log each one to image_log.csv. Returns a list of errors."""
    errors = []
    fields = ["file", "camera_file"] + SHOT_LOG_FIELDS
    for src, dest, shot in renames:
        try:
            os.replace(src, dest)
        except OSError as e:
            errors.append(f"{src.name}: {e}")
            continue
        row = {k: v for k, v in shot.items() if not k.startswith("_")}
        row.update(file=dest.name, camera_file=src.name)
        append_csv(Path(folder) / IMAGE_LOG_FILENAME, fields, row)
    return errors


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="folder of images copied off the camera card")
    ap.add_argument("shot_log", help="shot_log_<session>.csv written by the GUI")
    ap.add_argument("--prefix", default="", help="text at the start of each new name")
    ap.add_argument("--no-alt", action="store_true", help="leave seabed altitude out of names")
    ap.add_argument("--dry-run", action="store_true", help="only print what would be renamed")
    args = ap.parse_args(argv)

    renames, unmatched, how = plan_renames(args.folder, args.shot_log, args.prefix, not args.no_alt)
    print(f"{how}; {len(renames)} files to rename, {len(unmatched)} images unmatched")
    for src, dest, _ in renames:
        print(f"  {src.name} -> {dest.name}")
    for img in unmatched:
        print(f"  unmatched: {', '.join(p.name for p in img['files'])}")
    if args.dry_run or not renames:
        return 0
    errors = apply_renames(args.folder, renames)
    for e in errors:
        print("  error:", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
