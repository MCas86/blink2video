"""Merge Blink clips into one video per camera and local day.

Timestamps are now burned directly into the image (hardsub, via the drawtext
filter) rather than added as a "soft" subtitle track (mov_text). This fixes
WhatsApp send failures: WhatsApp never shows embedded subtitle tracks, and the
old track (one entry per second) inflated the moov atom enough to fail mobile
media validation on some long files.

As a result, each group is now re-encoded (no more -c:v copy), and timezone
conversion remains entirely handled in Python (zoneinfo) for reliability on all
platforms. ffmpeg only receives epochs already converted to "local wall clock
disguised as UTC" and runs with TZ=UTC0, avoiding any dependency on an IANA
timezone database in ffmpeg/OS (unreliable on Windows).
"""

from __future__ import annotations  # Python 3.8 (Windows 7 build): "X | None" annotations are only evaluated when building strings, never at runtime.

import argparse
import datetime as dt
import functools
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict, namedtuple
from pathlib import Path
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8 (Windows 7 build, see build-win7.yml): no zoneinfo in stdlib.
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError


from nico579_commons import atomique

import runtime

LIBELLES = {
    "en": {
        "aide_desc":
            "Incremental merge of Blink clips per camera: one video per day, "
            "then ISO-week and monthly aggregates.",
        "aide_normalized":
            "store of normalized, timestamped clips all assembled videos are "
            "built from",
        "aide_no_weekly": "do not (re)build the weekly aggregates",
        "aide_no_monthly": "do not (re)build the monthly aggregates",
        "aide_no_timestamp": "do not burn the date and time into the image",
        "aide_excluded_output": "folder where discarded clips are set aside",
        "aide_exclude":
            "discard clips (path under Blink_Clips, Blink_Normalized or "
            "Blink_Excluded): the raw file moves to Blink_Excluded, the segment "
            "is deleted, the clip is no longer re-downloaded or assembled",
        "aide_include":
            "undo a discard: the raw file comes back from Blink_Excluded and "
            "the clip is re-normalized",
        "aide_date": "limit to one local date YYYY-MM-DD",
        "aide_camera": "limit to one camera",
        "aide_force": "rebuild even if nothing changed",
        "aide_font": "path to a .ttf font for the burned-in timestamp",
        "aide_font_size":
            "burned-in timestamp size in pixels, 8 to 500 "
            "(default: proportional to the video's height)",
        "aide_font_color":
            "burned-in timestamp color: a name (white, yellow...) or hex "
            "(#RRGGBB, 0xRRGGBB), optionally @opacity (e.g. white@0.8), "
            "default white",
        "aide_box_opacity":
            "opacity of the box behind the timestamp, 0.0 (invisible) to "
            "1.0 (opaque), default 0.55",
        "aide_preset": "libx264 preset (ultrafast..veryslow), default veryfast",
        "aide_crf": "libx264 quality (0-51, lower = better quality), default 21",
        "registre_inconnu": "  Unknown to the registry, ignored: {identity}",
        "clip_exclu": "  Excluded: {identity}",
        "clip_reintegre": "  Reinstated: {identity}",
        "clip_reintegre_brut_absent":
            "  Reinstated: {identity} (raw file missing, restart blink2video.py)",
        "ffmpeg_introuvable":
            "FFmpeg not found. Install imageio-ffmpeg or add ffmpeg to PATH.",
        "drawtext_absent":
            "The ffmpeg binary in use does not include the drawtext filter "
            "(built without libfreetype). Use a full ffmpeg build "
            "(e.g. gyan.dev on Windows, or 'apt install ffmpeg' on Linux).",
        "horodatage_echec_police":
            "Burning in the timestamp fails with font {police}:\n{detail}",
        "horodatage_echec_sans_message": "failed with no message",
        "horodatage_vide":
            "Burning in the timestamp draws nothing with font "
            "{police} (test image entirely black). Font unreadable "
            "by drawtext, or date format unsupported by libc.",
        "police_introuvable": "Font not found: {chemin}",
        "police_absente":
            "No TrueType font found to burn in the timestamp. "
            "Place a .ttf file (e.g. DejaVuSans-Bold.ttf) next to "
            "{fichier}, or pass --font /path/to/font.ttf.",
        "registre_illisible": "Download registry unreadable: {chemin}",
        "duree_introuvable": "duration not found for {nom}",
        "duree_invalide": "invalid duration for {nom}",
        "flux_video_introuvable": "video stream not found for {nom}",
        "resolution_introuvable": "resolution not found for {nom}",
        "chemin_hors_racines": "Path outside of {racines}: {valeur}",
        "ffmpeg_silencieux":
            "FFmpeg silent for more than {secondes}s, process killed "
            "(will resume next round)",
        "mp4_invalide": "FFmpeg did not produce a valid MP4",
        "copie_refusee": "  Stream copy refused, re-encoding the period",
        "periode_deja_a_jour": "Already up to date: {nom} ({jours} day(s))",
        "periode_assemblage": "Assembling: {label} / {camera} / {jours} day(s)",
        "echec_generique": "  Failed: {erreur}",
        "cree": "  Created: {destination}",
        "periode_supprimee": "Deleted (empty period): {periode}",
        "periode_protegee":
            "Kept as is: {periode} (a day lost both its source and its daily video)",
        "assemblage_deja_en_cours": "Assembly already in progress ({erreur}). Nothing to do.",
        "normalisation_titre": "Normalizing: {total} clip(s) to encode",
        "echec_normalisation": "    Failed: {erreur}",
        "normalisation_resume": "Normalizing: {encoded} clip(s) encoded, {reused} reused.",
        "erreur_generique": "Error: {erreur}",
        "aucun_groupe": "No clip group to merge.",
        "cache_supprime": "Old cache deleted: {chemin}",
        "jour_reportee_source":
            "  Postponed (source missing or invalid): {nom}",
        "jour_supprimee": "Deleted (no clip left): {nom}",
        "jour_reportee_partielle": "  Postponed (partial encoding): {nom}",
        "jour_deja_a_jour": "Already up to date: {nom} ({clips} clip(s))",
        "jour_assemblage": "  [{index}/{total}] Assembling: {jour} / {camera} / {clips} clip(s)",
        "stock_normalise": "Normalized storage: {n} obsolete segment(s) deleted",
        "journalieres_resume": "Daily videos: {built} created, {skipped} already up to date, {failed} failed.",
        "agregats_reportes":
            "Aggregates postponed: at least one selected day has an unavailable source.",
        "label_hebdomadaires": "Weekly",
        "label_mensuelles": "Monthly",
        "periode_titre": "\n{label}:",
        "periode_resume": "{label}: {built} created, {skipped} already up to date, {failed} failed.",
        "couleur_invalide":
            "Invalid color: {valeur!r}. A name (white, yellow...) or a "
            "hex code (#RRGGBB, 0xRRGGBB), optionally followed by "
            "@opacity (e.g. white@0.8).",
        "taille_police_invalide": "Invalid font size: {valeur!r} (8 to 500).",
        "opacite_invalide": "Invalid opacity: {valeur!r} (0.0 to 1.0).",
    },
}
LIBELLES["fr"] = dict(LIBELLES["en"])



def msg(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


# Visible output folders; state (merge.log) stays in runtime.app_dir().
BASE_DIR = runtime.dossier_sorties()
DEFAULT_INPUT = BASE_DIR / "Blink_Clips"
DEFAULT_OUTPUT = BASE_DIR / "Blink_Daily"
DEFAULT_WEEKLY = BASE_DIR / "Blink_Weekly"
DEFAULT_MONTHLY = BASE_DIR / "Blink_Monthly"
DEFAULT_NORMALIZED = BASE_DIR / "Blink_Normalized"
DEFAULT_EXCLUDED = BASE_DIR / "Blink_Excluded"
DOWNLOAD_STATE = ".blink_download_state.json"
MERGE_STATE = ".blink_merge_state.json"
NORMALIZED_STATE = ".blink_normalized.json"
NORMALIZE_VERSION = "normalized-v1-hardsub"
RENDER_VERSION = "daily-v7-normalized"
PERIOD_VERSION = "period-v2-copy-first"

# Shared by run_ffmpeg_batch and concat_copy: a stuck ffmpeg (seen in production,
# AUDIT-2026-08-13 28.82) must never leave assembly frozen in silence forever.
SILENCE_MAX = 300.0  # 5 min: beyond this, ffmpeg is considered stuck.

# Daily video name: 2026-08-10_jardin.mp4. Weekly and monthly aggregates are
# built from these files (already timestamped and normalized), not from raw clips.
DAILY_NAME = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})_(?P<camera>.+)\.mp4$")

# Legacy segment cache, replaced by Blink_Normalized. Removed on first run: it
# was a hidden folder of disposable files; normalized storage is now a named
# pipeline layer.
LEGACY_SEGMENT_DIR = ".blink_segments"

ClipInfo = namedtuple(
    "ClipInfo", ["created", "source", "duration", "width", "height", "fps", "has_audio"]
)

# Burned-in timestamp appearance (GitHub #7: font file alone via --font was not
# enough for users who also wanted size, color, or banner opacity). size=None
# keeps the old automatic sizing (proportional to video height) rather than a
# fixed value that would look wrong at another resolution.
TimestampStyle = namedtuple("TimestampStyle", ["size", "color", "box_opacity"])
STYLE_PAR_DEFAUT = TimestampStyle(size=None, color="white", box_opacity=0.55)


def _tronquer_utf8(value: str, maximum: int) -> str:
    brut = value.encode("utf-8")[:maximum]
    return brut.decode("utf-8", errors="ignore")


def safe_name(value: str) -> str:
    """Build a safe path component from a free-form name (camera).

    Only version kept (code review 0eab463, bug #5): three nearly identical
    copies of this sanitization had drifted, each with part of the logic but
    not all - this one, the most complete (already in blink_models.py), stays
    here because blink_models already imports this module (the reverse would
    create a circular import), and serve.py imported it too. blink_models.safe_name
    and serve.safe_file now exist only as aliases to this function.

    Does NOT guarantee two distinct names map to different results: "A/B" and
    "A_B" yield the same output, an accepted trade-off (see I-16,
    test_T_I16_A_slash_B_et_A_underscore_B_restent_distincts) - a clip's real
    identity never relies on this string (state_key(), target_path() each add
    their own distinction), so only merge_daily.py's daily/weekly/monthly
    grouping can still confuse two cameras whose raw names differ but sanitize
    the same - rare enough to document rather than fix immediately (see
    BACKLOG.md)."""
    cleaned = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    cleaned = _tronquer_utf8(cleaned, 32).rstrip(". ") or "camera"
    racine = cleaned.split(".", 1)[0].casefold()
    reserves = {"con", "prn", "aux", "nul", *{f"com{i}" for i in range(1, 10)},
                *{f"lpt{i}" for i in range(1, 10)}}
    if racine in reserves:
        cleaned = f"_{cleaned}"
    return cleaned


def valid_mp4(path: Path) -> bool:
    """Quickly validate the outer structure of an MP4/fragmented MP4.

    Only ISO-BMFF box headers are walked, skipping their payloads. Cheap enough
    for repeated inventory scans, but stricter than merely finding ``ftyp``:
    each box must fit entirely in the file and a standalone media file must
    contain ``ftyp``, ``moov``, and ``mdat``. Deep validation of a new download
    is handled by :func:`valid_mp4_complet`.
    """
    try:
        taille_fichier = path.stat().st_size
        # At least three box headers; ftyp also needs major_brand and
        # minor_version fields (16 bytes including the box).
        if taille_fichier < 32:
            return False

        trouvees = set()
        position = 0
        with path.open("rb") as fichier:
            while position < taille_fichier:
                restant = taille_fichier - position
                if restant < 8:
                    return False
                fichier.seek(position)
                entete = fichier.read(8)
                if len(entete) != 8:
                    return False

                taille_boite = int.from_bytes(entete[:4], "big")
                type_boite = entete[4:8]
                taille_entete = 8
                if taille_boite == 1:
                    extension = fichier.read(8)
                    if len(extension) != 8:
                        return False
                    taille_boite = int.from_bytes(extension, "big")
                    taille_entete = 16
                elif taille_boite == 0:
                    # A zero-size box extends, by definition, to EOF and must
                    # therefore be the last one.
                    taille_boite = restant

                if taille_boite < taille_entete or taille_boite > restant:
                    return False
                if type_boite == b"ftyp" and taille_boite < taille_entete + 8:
                    return False
                if type_boite in {b"ftyp", b"moov", b"mdat"}:
                    trouvees.add(type_boite)
                position += taille_boite

        return position == taille_fichier and trouvees == {b"ftyp", b"moov", b"mdat"}
    except (OSError, OverflowError):
        return False


@functools.lru_cache(maxsize=1)
def _outil_validation_media() -> tuple[str, str] | None:
    """Find a media probe tool once, off the hot path of simple listings."""
    candidats_ffprobe = []
    for motif in ("ffprobe*.exe", "ffprobe-*", "ffprobe"):
        candidats_ffprobe.extend(
            str(candidat)
            for candidat in sorted(runtime.resource_dir().glob(motif))
            if candidat.is_file()
        )
    systeme = shutil.which("ffprobe")
    if systeme:
        candidats_ffprobe.append(systeme)
    if candidats_ffprobe:
        return "ffprobe", candidats_ffprobe[0]

    try:
        return "ffmpeg", find_ffmpeg()
    except RuntimeError:
        return None


# Lines ffprobe prints on clips that are still intact. Issue #49: each clip
# recorded on a Sync Module USB key contains an H.264 access unit with no
# picture, which the H.264 parser reports ("missing picture in access unit
# with size 9"), while the video is complete; cloud clips do not. Treated as
# an error, this line silently rejected all USB clips wherever ffprobe is
# present (Linux package since 0.12.29, Docker image, system ffprobe). Measured
# 2026-09-30 on 104 real clips and 15 truncated copies, with ffprobe from
# package 0.15.4 and FFmpeg stable 8.1.3: no truncation was signaled by this
# line alone, always accompanied by "Invalid NAL unit size" or a container
# error, which remain rejection reasons.
_BRUITS_FFPROBE = ("missing picture in access unit",)


def _erreurs_ffprobe(stderr: str) -> list:
    """ffprobe error lines that count, excluding the known noise above."""
    erreurs = []
    for ligne in (stderr or "").splitlines():
        ligne = ligne.strip()
        # "Last message repeated N times" repeats the previous line, already
        # judged on its own: error if it was one, noise otherwise.
        if not ligne or ligne.startswith("Last message repeated"):
            continue
        if any(bruit in ligne for bruit in _BRUITS_FFPROBE):
            continue
        erreurs.append(ligne)
    return erreurs


def _ligne_sans_donnee_privee(ligne: str, path: Path) -> str:
    """A probe error line with no path, file name, or address.

    ffprobe sometimes quotes the file ("/path/clip.mp4: Invalid data ...");
    rejection reasons are printed to the log and users paste them in public
    issues: never include a path or clip name."""
    for morceau in sorted({str(path), path.name, str(path.parent)}, key=len, reverse=True):
        if morceau:
            ligne = ligne.replace(morceau, "<fichier>")
    ligne = re.sub(r"@ 0x[0-9a-fA-F]+", "@ ADR", ligne)
    ligne = re.sub(r"[A-Za-z]:[\\/][^\s:]*|/[^\s:]+/[^\s:]*", "<chemin>", ligne)
    return " ".join(ligne.split())[:140]


def _motif_refus_mp4_complet(path: Path) -> str:
    """Why this download is rejected; empty string if valid.

    Single source of truth: ``valid_mp4_complet`` keeps only the boolean,
    ``raison_refus_mp4`` returns the text. Reasons are stable keywords plus,
    when the probe spoke, its first useful line."""
    if not valid_mp4(path):
        return "structure=MP4 box missing, truncated or not a video"
    outil = _outil_validation_media()
    if outil is None:
        return "tool=neither ffprobe nor ffmpeg available"
    genre, executable = outil
    if genre == "ffprobe":
        commande = [
            executable, "-v", "error", "-select_streams", "v:0",
            "-count_packets", "-show_entries", "stream=nb_read_packets",
            "-of", "csv=p=0", str(path),
        ]
        sortie = subprocess.PIPE
    else:
        commande = [
            executable, "-hide_banner", "-loglevel", "error", "-xerror",
            "-i", str(path), "-map", "0:v:0", "-c", "copy",
            "-f", "null", "-",
        ]
        sortie = subprocess.DEVNULL
    try:
        resultat = runtime.lancer(
            commande, stdin=subprocess.DEVNULL, stdout=sortie,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as erreur:
        return f"outil={genre} lancement={type(erreur).__name__}"
    lignes = _erreurs_ffprobe(resultat.stderr) if genre == "ffprobe" else [
        ligne.strip() for ligne in (resultat.stderr or "").splitlines() if ligne.strip()]
    premiere = _ligne_sans_donnee_privee(lignes[0], path) if lignes else ""
    if resultat.returncode != 0:
        return (f"outil={genre} code={resultat.returncode}"
                + (f" ligne={premiere}" if premiere else ""))
    if genre == "ffprobe":
        if lignes:
            return f"outil={genre} ligne={premiere}"
        nombres = [ligne.strip() for ligne in (resultat.stdout or "").splitlines()]
        try:
            if not any(int(nombre) > 0 for nombre in nombres if nombre):
                return f"outil={genre} video-packets=0"
        except ValueError:
            return f"outil={genre} output=unreadable"
    return ""


def valid_mp4_complet(path: Path) -> bool:
    """Verify a full download before publishing or deleting it.

    Structural checks eliminate HTML responses and box-level truncated files. A
    probe then walks all video packets: it catches a shortened ``mdat`` despite
    readable ``ftyp`` and ``moov``. Without a probe, fail closed so no remote
    source is deleted based on an incomplete check. Any ffprobe error line
    rejects the file except known noise from intact clips (_BRUITS_FFPROBE).
    """
    return not _motif_refus_mp4_complet(path)


def raison_refus_mp4(path: Path) -> str:
    """``valid_mp4_complet`` rejection reason for this file, with no path or
    clip name, ready to print. Never raises: diagnostics must not turn a
    failure into a crash. Re-runs the check, so only call after a rejection."""
    try:
        return _motif_refus_mp4_complet(path) or "none (the file is valid)"
    except Exception as erreur:
        return f"diagnosis impossible ({type(erreur).__name__})"


def load_json(path: Path, default: dict) -> dict:
    """Read a JSON object, or ``default`` if missing, corrupt, or not a dict.

    The read itself (transient Windows denial on concurrent replace, antivirus,
    indexer) is nico579_commons.atomique.lire_json: persistent denial raises
    instead of returning ``default``, which would rewrite empty content on every
    read-modify-write."""
    value = atomique.lire_json(path, default)
    return value if isinstance(value, dict) else default


def save_json(path: Path, value: dict) -> None:
    """Replace ``path`` atomically (nico579_commons.atomique.ecrire_json).

    A unique temp file per call: a fixed name was shared by all writers, and
    serve.py runs on a ThreadingHTTPServer where two routes rewrite
    ASSEMBLED_DURATIONS in parallel (FileNotFoundError, reproduced 197 times
    over 600 concurrent writes, audit 2026-09-24). Transient Windows denials (162
    of 600 in CI) are retried."""
    atomique.ecrire_json(path, value)


def find_ffmpeg() -> str:
    """Pick an ffmpeg that can burn in text.

    The first found may not work: imageio-ffmpeg Linux binaries are built without
    libfreetype, hence no drawtext filter, while Windows and macOS builds have
    it. A tool whose purpose is writing time into the image cannot settle for
    the first candidate: try them in order and keep the first that can.

    If none qualify, still return the first candidate: check_drawtext_available
    will then emit a clear message rather than a misleading "ffmpeg not found"
    when one exists."""
    candidats = []
    # Bundled ffmpeg ships with the executable: prefer it first so we do not
    # depend on whatever is on the target machine.
    for motif in ("ffmpeg*.exe", "ffmpeg-*", "ffmpeg"):
        candidats += [str(c) for c in sorted(runtime.resource_dir().glob(motif))
                      if c.is_file()]
    systeme = shutil.which("ffmpeg")
    if systeme:
        candidats.append(systeme)

    vendor = runtime.resource_dir() / "_vendor"
    if vendor.is_dir():
        sys.path.insert(0, str(vendor))
    try:
        import imageio_ffmpeg

        candidats.append(imageio_ffmpeg.get_ffmpeg_exe())
    except (ImportError, RuntimeError):
        pass

    if not candidats:
        raise RuntimeError(msg("ffmpeg_introuvable"))
    for candidat in candidats:
        if has_drawtext(candidat):
            return candidat
    return candidats[0]


def extraire_vignette(ffmpeg: str, source: Path, pending: Path, timeout=None) -> bool:
    """Extract a clip thumbnail into `pending`; true if the file exists.

    Grab slightly after the start, never the first frame: a camera that just
    triggered often delivers one or two black or overexposed frames while
    exposure settles. A clip shorter than the requested position falls back to
    its very first frame. Shared by serve.py (on demand) and the downloader
    (when the clip arrives): one way to build thumbnails, one look."""
    for entree in (["-ss", "1.5", "-i", str(source)], ["-i", str(source)]):
        # -ss before -i: ffmpeg seeks directly to the requested position
        # instead of decoding everything before it.
        runtime.lancer(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *entree,
             "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5", str(pending)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=timeout,
        )
        if pending.is_file() and pending.stat().st_size > 0:
            return True
    return False


def has_drawtext(ffmpeg: str) -> bool:
    try:
        result = runtime.lancer(
            [ffmpeg, "-hide_banner", "-filters"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "drawtext" in (result.stdout or "")


def check_drawtext_available(ffmpeg: str) -> None:
    """Some minimal ffmpeg builds (including some portable binaries) omit
    libfreetype and thus the drawtext filter. Detect early with a clear message
    rather than failing every merge."""
    result = runtime.lancer(
        [ffmpeg, "-hide_banner", "-filters"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if "drawtext" not in result.stdout:
        raise RuntimeError(msg("drawtext_absent"))


def check_timestamp_rendering(ffmpeg: str, font_path: Path,
                              style: TimestampStyle = STYLE_PAR_DEFAUT) -> None:
    """Burn a test timestamp on a black frame and verify something remains.

    End-to-end guard: drawtext does not report a badly encoded font path in the
    filtergraph (fontconfig takes over silently) or a strftime format the locale
    libc ignores (rendered text is then simply empty). Without this test, both
    failures only show up on the final video.

    Style color is applied here, not just the font: black text on this black
    test background would otherwise wrongly fail the guard (all-black image by
    construction, confused with an unreadable font)."""
    graph = (
        "color=c=black:s=640x120:d=0.1,"
        + drawtext_chain(quote_filter_path(font_path), 40, 0, style.color)
        + ",format=gray"
    )
    result = runtime.lancer(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-filter_complex", graph,
         "-frames:v", "1", "-f", "rawvideo", "-"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ, TZ="UTC0"),
        check=False,
    )
    stderr = result.stderr.decode("utf-8", "replace").strip()
    if result.returncode != 0 or stderr:
        raise RuntimeError(msg(
            "horodatage_echec_police", police=font_path,
            detail=stderr or msg("horodatage_echec_sans_message"),
        ))
    if not any(result.stdout):
        raise RuntimeError(msg("horodatage_vide", police=font_path))


def find_font(explicit: Path | None) -> Path:
    if explicit is not None:
        if explicit.is_file():
            return explicit
        raise RuntimeError(msg("police_introuvable", chemin=explicit))

    # Next to the program, as police_absente says (before 0.14, also the default
    # data folder).
    programme = runtime._dossier_ancre()
    candidates = [
        programme / "DejaVuSans-Bold.ttf",
        programme / "fonts" / "DejaVuSans-Bold.ttf",
    ]
    system = platform.system()
    if system == "Windows":
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        candidates += [
            windir / "Fonts" / "arialbd.ttf",
            windir / "Fonts" / "arial.ttf",
            windir / "Fonts" / "consolab.ttf",
        ]
    elif system == "Darwin":
        candidates += [
            Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path("/Library/Fonts/Arial Bold.ttf"),
        ]
    else:
        candidates += [
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
            Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
        ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise RuntimeError(msg("police_absente", fichier=Path(__file__).name))


def quote_filter_path(path: Path) -> str:
    """Encode a path for an ffmpeg filter option (e.g. drawtext:fontfile).

    Filtergraph text undergoes TWO successive unescape passes: first splitting
    the filter chain, then splitting a filter's arguments. A Windows path
    ('C:\\Windows\\Fonts\\arialbd.ttf') must therefore:
      - be in single quotes, protecting it from the 1st pass;
      - have ':' escaped as '\\:', consumed by the 2nd pass;
      - use only '/' as separators, otherwise '\\' is consumed inconsistently
        across the two passes (Windows accepts '/').
    """
    text = path.as_posix().replace("'", "'\\''").replace(":", "\\:")
    return f"'{text}'"


def parse_created_at(value: str) -> dt.datetime:
    created = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    return created.astimezone(dt.timezone.utc)


def read_registry(state_path: Path) -> dict:
    """Clips known to the downloader; empty until something was fetched.

    A missing registry is normal for a fresh install, not a failure: the UI
    should open and merge should report nothing to do before the first download.
    A present but unreadable registry is corruption and must be reported instead
    of conflating it with "nothing to show"."""
    if not state_path.exists():
        return {}
    state = load_json(state_path, None)
    entries = state.get("clips") if isinstance(state, dict) else None
    if not isinstance(entries, dict):
        raise RuntimeError(msg("registre_illisible", chemin=state_path))
    return entries


def _cles_camera_par_collision(entries: dict) -> dict:
    """(name, network_id) -> grouping key, only for names with a real collision.

    The registry distinguishes devices by network_id/device_id, but daily
    grouping used only the name: two cameras with the same name on two hubs
    ended up in one assembly (reproduced with two "jardin" on distinct networks,
    mixed into one daily video).

    The literal fix (group by stable identity everywhere) would wrongly split
    real data: one physical camera can have several inconsistent network_id/
    device_id pairs in the registry for historical reasons (old USB entries
    without network_id, matching bug fixed elsewhere but not retroactive on
    existing data) - verified on a real registry, "jardin" and "Terrasse1" each
    have several (network_id, device_id) pairs but only one KNOWN network_id
    (always the same when set, never two different values). That is not a true
    homonym, just inconsistency, and must change nothing.

    So only split names where at least two NON-EMPTY, DIFFERENT network_ids
    coexist (positive signal of two real hubs, same idea as _hub_compatible() in
    blink_registre.py): this never happened in a real registry, only in bug
    reproduction. The first sorted network_id keeps the bare name (does not
    invalidate history already assembled under that name); others get a readable
    suffix, keeping the name for display rather than an opaque id.

    Same rule for two DIFFERENT names that sanitize the same ("Garage" and
    "Garage!" map to the same folder via safe_name): without it, their dailies
    wrote to the same path, one overwriting the other. The name that already
    fits keeps its folder (assembled history is not moved); others get "(2)",
    "(3)"...; case does not distinguish folders because Windows and macOS
    treat them as the same."""
    reseaux_par_nom = defaultdict(set)
    paires = set()
    for entry in entries.values():
        camera = str(entry.get("camera") or "camera").strip() or "camera"
        network_id = str(entry.get("network_id") or "")
        paires.add((camera, network_id))
        if network_id:
            reseaux_par_nom[camera].add(network_id)

    cles = {}
    for camera, reseaux in reseaux_par_nom.items():
        if len(reseaux) <= 1:
            continue
        _principal, *autres = sorted(reseaux)
        for rang, reseau in enumerate(autres, start=2):
            cles[(camera, reseau)] = f"{camera} ({rang})"

    def dossier(cle: str) -> str:
        return safe_name(cle).casefold()

    cles_par_paire = {paire: cles.get(paire, paire[0]) for paire in paires}
    noms = sorted(set(cles_par_paire.values()),
                  key=lambda nom: (safe_name(nom) != nom, nom.casefold(), nom))
    pris, renommes = set(), {}
    for nom in noms:
        if dossier(nom) not in pris:
            pris.add(dossier(nom))
            continue
        rang = 2
        while dossier(f"{nom} ({rang})") in pris or f"{nom} ({rang})" in noms:
            rang += 1
        renommes[nom] = f"{nom} ({rang})"
        pris.add(dossier(renommes[nom]))
    for paire, nom in cles_par_paire.items():
        if nom in renommes:
            cles[paire] = renommes[nom]
    return cles


def _camera_key(cles_collision: dict, entry: dict, camera: str) -> str:
    return cles_collision.get((camera, str(entry.get("network_id") or "")), camera)


def load_groups(input_dir: Path, timezone: ZoneInfo) -> dict:
    """Group files recorded by the incremental downloader.

    A day with no surviving clips (all excluded, or none left valid) stays in
    the result with an empty list rather than disappearing: without that entry,
    once the last clip was excluded the day was never revisited, neither rebuilt
    (it should empty) nor deleted - its already assembled video stayed on disk
    forever and kept being aggregated into weekly and monthly (bug #2, code
    review 0eab463, see AUDIT-2026-08-13.md 28.61)."""
    entries = read_registry(input_dir / DOWNLOAD_STATE)
    cles_collision = _cles_camera_par_collision(entries)

    root = input_dir.resolve()
    groups = defaultdict(list)
    for entry in entries.values():
        try:
            created = parse_created_at(entry["created_at"])
            camera = str(entry.get("camera") or "camera").strip() or "camera"
        except (KeyError, TypeError, ValueError):
            continue
        camera = _camera_key(cles_collision, entry, camera)

        local_day = created.astimezone(timezone).date().isoformat()
        clips = groups[(camera, local_day)]
        try:
            if entry.get("excluded"):
                continue
            source = (root / entry["path"]).resolve()
            if not runtime.est_relatif_a(source, root) or not valid_mp4(source):
                continue
        except (KeyError, TypeError, OSError):
            continue
        clips.append((created, source))

    for clips in groups.values():
        clips.sort(key=lambda item: (item[0], str(item[1]).casefold()))
    return dict(groups)


def journees_a_source_indisponible(input_dir: Path, timezone: ZoneInfo) -> tuple[set, set]:
    """(days, identities) for NON-excluded clips whose raw file is missing or invalid.

    load_groups() treats a missing/invalid clip exactly like an explicitly
    excluded one: both vanish from its list with no trace of why. Needed so
    downstream does NOT confuse "all clips for this day were excluded by choice"
    (assembled daily and associated normalized segments must go away) with "at
    least one clip still exists in the registry but its file is gone" (both must
    be preserved instead, seen in production: USB source unplugged during a run,
    folder moved).

    days: (camera, local day) with at least one such clip, exempt from daily
    deletion in _executer(). identities: clip_identity() for each (computable
    without the file existing - a relative path; see clip_identity()), to add to
    used_segments before prune_normalized() so an already encoded still-valid
    segment is not erased just because its raw source disappeared."""
    entries = read_registry(input_dir / DOWNLOAD_STATE)
    cles_collision = _cles_camera_par_collision(entries)
    root = input_dir.resolve()
    jours, identites = set(), set()
    for entry in entries.values():
        try:
            created = parse_created_at(entry["created_at"])
            camera = str(entry.get("camera") or "camera").strip() or "camera"
        except (KeyError, TypeError, ValueError):
            continue
        camera = _camera_key(cles_collision, entry, camera)
        if entry.get("excluded"):
            continue
        local_day = created.astimezone(timezone).date().isoformat()
        try:
            source = (root / entry["path"]).resolve()
            if runtime.est_relatif_a(source, root) and valid_mp4(source):
                continue  # loaded normally by load_groups, nothing to report
        except (KeyError, TypeError, OSError):
            jours.add((camera, local_day))
            continue
        jours.add((camera, local_day))
        try:
            identites.add(clip_identity(input_dir, source))
        except ValueError:
            pass
    return jours, identites


def group_fingerprint(keys: list) -> str:
    """Fingerprint of a day: ordered list of render keys of its normalized segments.
    The daily video is only a concatenation of those segments, so it is up to
    date exactly when this list has not changed."""
    digest = hashlib.sha256()
    digest.update((RENDER_VERSION + "\n").encode("utf-8"))
    for key in keys:
        digest.update((key + "\n").encode("utf-8"))
    return digest.hexdigest()


def probe_clip_info(ffmpeg: str, source: Path) -> tuple[float, int, int, float, bool]:
    """Read duration, resolution, fps, and audio presence from the MP4 header."""
    result = runtime.lancer(
        [ffmpeg, "-hide_banner", "-i", str(source)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    stderr = result.stderr

    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if not duration_match:
        raise RuntimeError(msg("duree_introuvable", nom=source.name))
    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if duration <= 0:
        raise RuntimeError(msg("duree_invalide", nom=source.name))

    video_line_match = re.search(r"Stream #\d+:\d+[^\n]*Video:[^\n]*", stderr)
    if not video_line_match:
        raise RuntimeError(msg("flux_video_introuvable", nom=source.name))
    video_line = video_line_match.group(0)

    size_match = re.search(r"(\d{2,5})x(\d{2,5})", video_line)
    if not size_match:
        raise RuntimeError(msg("resolution_introuvable", nom=source.name))
    width, height = int(size_match.group(1)), int(size_match.group(2))

    fps_match = re.search(r"([\d.]+)\s*fps", video_line)
    fps = float(fps_match.group(1)) if fps_match else 15.0

    has_audio = bool(re.search(r"Stream #\d+:\d+[^\n]*Audio:", stderr))

    return duration, width, height, fps, has_audio


def resolve_identity(
    input_dir: Path, normalized_dir: Path, excluded_dir: Path, value: str
) -> str:
    """Resolve a clip identity from a hand-supplied path.

    Raw, normalized, or excluded paths are accepted equally: the three trees are
    mirrors with the same relative part. In practice this names the clip you
    just viewed."""
    # Strip spaces and line endings: paths often come from copy-paste or a list,
    # and a trailing "\\r" is invisible yet breaks matching.
    path = Path(value.strip()).resolve()
    roots = (input_dir.resolve(), normalized_dir.resolve(), excluded_dir.resolve())
    for root in roots:
        if runtime.est_relatif_a(path, root):
            return path.relative_to(root).as_posix()
    raise RuntimeError(msg(
        "chemin_hors_racines",
        racines=", ".join(root.name for root in roots), valeur=value,
    ))


def move_aside(source: Path, destination: Path) -> None:
    """Move a file, creating directories, never overwriting."""
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        source.unlink()
        return
    shutil.move(str(source), str(destination))


def set_excluded(
    input_dir: Path,
    normalized_dir: Path,
    excluded_dir: Path,
    values: list,
    excluded: bool,
) -> int:
    """Exclude clips, or reinstate them.

    Excluding destroys nothing: the raw file moves to Blink_Excluded, its
    normalized version is deleted (rebuilt from raw on demand), and a tombstone
    is set in the download registry so blink2video.py will not fetch it again.
    The affected daily then changes segment list and fingerprint, so it is
    reassembled on the next pass, aggregates included.

    Reinstating reverses the move and puts the raw file back: the clip is
    re-normalized in the same pass without depending on hub retention. Clearing
    Blink_Excluded remains a separate explicit action."""
    state_path = input_dir / DOWNLOAD_STATE

    changed = 0
    # Same lock as download (runtime.verrou distinguishes resources only by this
    # first name; "exclusion" is just a label). Read, decide, and write must
    # run under one lock held continuously: read then write separately left a
    # window where a concurrent download could add a clip between them, which
    # our stale copy then wiped by overwriting the whole clips table.
    with runtime.verrou("registre", "exclusion", stale_after=60, attente=10):
        clips = read_registry(state_path)
        for value in values:
            identity = resolve_identity(input_dir, normalized_dir, excluded_dir, value)
            keys = [
                key
                for key, entry in clips.items()
                if isinstance(entry, dict) and entry.get("path") == identity
            ]
            if not keys:
                print(msg("registre_inconnu", identity=identity))
                continue
            for key in keys:
                if excluded:
                    clips[key]["excluded"] = True
                    clips[key]["excluded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                    move_aside(input_dir / identity, excluded_dir / identity)
                    (normalized_dir / identity).unlink(missing_ok=True)
                    print(msg("clip_exclu", identity=identity))
                else:
                    # A timestamp rather than simply removing the field:
                    # _ecrire_registre() (blink_registre.py) uses it to know
                    # THIS reinstate decision is newer than an excluded=True a
                    # stale downloader would rewrite afterward (review 27/08,
                    # bug 2).
                    clips[key]["excluded"] = False
                    clips[key]["excluded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                    move_aside(excluded_dir / identity, input_dir / identity)
                    if (input_dir / identity).exists():
                        print(msg("clip_reintegre", identity=identity))
                    else:
                        print(msg("clip_reintegre_brut_absent", identity=identity))
                changed += 1

        if changed:
            # The registry also carries version and someday other fields that
            # read_registry() does not return: start from full state and replace
            # only the clips table. That state is unchanged since our read above,
            # the lock having stayed held without a gap.
            state = load_json(state_path, {})
            state["clips"] = clips
            save_json(state_path, state)
    return changed


def clip_identity(input_dir: Path, source: Path) -> str:
    """Stable clip identity: path relative to the download folder. Same path for
    its normalized version; the two trees are mirrors so raw-to-segment mapping
    is obvious without consulting a registry."""
    return source.resolve().relative_to(input_dir.resolve()).as_posix()


def stat_tag(source: Path) -> str:
    stat = source.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def clip_info(ffmpeg: str, registry: dict, identity: str, created, source: Path) -> ClipInfo:
    """Clip characteristics cached in the registry.

    Probing a clip costs an ffmpeg run; results are kept while the file is
    unchanged. This will also support reasoning about a clip whose raw file was
    deleted later."""
    entry = registry["clips"].get(identity) or {}
    tag = stat_tag(source)
    probe = entry.get("probe")
    if entry.get("stat") != tag or not isinstance(probe, dict):
        duration, width, height, fps, has_audio = probe_clip_info(ffmpeg, source)
        probe = {
            "duration": duration, "width": width, "height": height,
            "fps": fps, "has_audio": has_audio,
        }
        entry.update({"stat": tag, "probe": probe})
        registry["clips"][identity] = entry
    return ClipInfo(
        created=created,
        source=source,
        duration=probe["duration"],
        width=probe["width"],
        height=probe["height"],
        fps=probe["fps"],
        has_audio=probe["has_audio"],
    )


def camera_target(registry: dict, camera: str, infos: list) -> tuple:
    """Target resolution and frame rate for a camera, cached and monotonic.

    Computing per day would oscillate: a day without HD clips would lower the
    target, invalidating all encoded segments then re-invalidating on the next
    clip. Fix them per camera and only allow increases. A better clip raises
    the target and triggers a full re-encode for that camera, which is intended:
    letterbox old footage rather than shrink new."""
    stored = registry["cameras"].get(camera) or {}
    width = max([info.width for info in infos] + [int(stored.get("width", 0))])
    height = max([info.height for info in infos] + [int(stored.get("height", 0))])
    # Frame rate rounded to whole frames. Measured to thousandths it varies clip
    # to clip with no visible change: a cloud clip measured at 30.11 instead of
    # 30.0 was enough to raise the target and re-encode all thirty-two segments
    # for the camera. Increases must reflect real differences (15 vs 30), not
    # measurement noise.
    fps = float(round(max([info.fps for info in infos]
                          + [float(stored.get("fps", 0))])))
    registry["cameras"][camera] = {"width": width, "height": height, "fps": fps}
    return width, height, fps


def wall_clock_epoch(moment_utc: dt.datetime, timezone: ZoneInfo) -> int:
    """Convert a UTC instant to a "disguised" epoch: local time digits (computed
    correctly via zoneinfo in Python) are reinterpreted as if they were UTC. With
    ffmpeg run under TZ=UTC0, drawtext shows exactly that local time without ever
    consulting a timezone database in ffmpeg/OS."""
    local = moment_utc.astimezone(timezone)
    naive_as_utc = local.replace(tzinfo=dt.timezone.utc)
    return int(naive_as_utc.timestamp())


def drawtext_chain(font_value: str, fontsize: int, epoch: int,
                   color: str = "white", box_opacity: float = 0.55) -> str:
    """drawtext filter showing wall clock starting at `epoch`.

    %X (full time) rather than %T or %H:%M:%S: MSVC strftime does not know %T
    and then renders an empty string with no error, and a literal ':' does not
    survive successive filtergraph unescapes. ffmpeg does not call setlocale, so
    %X stays in "C" locale, i.e. HH:MM:SS everywhere.

    `color` was already validated (_couleur_ffmpeg_valide, called from argparse):
    only alphanumerics, "#", and optional "@opacity" can reach it, nothing that
    would break this filtergraph."""
    text_expr = f"'%{{pts\\:localtime\\:{epoch}\\:%d/%m/%Y %X}}'"
    return (
        f"drawtext=fontfile={font_value}:fontsize={fontsize}:fontcolor={color}:"
        f"box=1:boxcolor=black@{box_opacity}:boxborderw=8:x=20:y=h-th-20:text={text_expr}"
    )


def build_batch_filter(
    batch: list,
    target_w: int,
    target_h: int,
    target_fps: float,
    timezone: ZoneInfo,
    font_value: str | None,
    style: TimestampStyle = STYLE_PAR_DEFAUT,
) -> str:
    """Build a batch filtergraph: normalize + burned timestamp on each clip, then concat.

    With font_value=None the timestamp is omitted: used when joining already
    timestamped videos (weekly/monthly aggregates) whose parameters differ and
    must be re-encoded.

    Clips without audio get silence generated *inside* the graph via a dedicated
    anullsrc. A shared lavfi input would not work: a filter output can connect
    only once, so two silent clips in one batch would make the graph invalid.
    """
    fontsize = style.size if style.size is not None else max(18, target_h // 18)
    chains = []
    for i, clip in enumerate(batch):
        overlay = ""
        if font_value is not None:
            epoch = wall_clock_epoch(clip.created, timezone)
            overlay = "," + drawtext_chain(font_value, fontsize, epoch,
                                           style.color, style.box_opacity)
        chains.append(
            f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
            f"fps={target_fps},setpts=PTS-STARTPTS"
            + overlay
            + f"[v{i}]"
        )
        if clip.has_audio:
            chains.append(
                f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS[a{i}]"
            )
        else:
            chains.append(
                f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=0:{clip.duration:.3f},asetpts=PTS-STARTPTS[a{i}]"
            )

    concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(len(batch)))
    chains.append(f"{concat_inputs}concat=n={len(batch)}:v=1:a=1[outv][outa]")
    return ";".join(chains)


def run_ffmpeg_batch(
    ffmpeg: str,
    batch: list,
    target_w: int,
    target_h: int,
    target_fps: float,
    timezone: ZoneInfo,
    font_value: str | None,
    preset: str,
    crf: int,
    output_path: Path,
    on_progress=None,
    style: TimestampStyle = STYLE_PAR_DEFAUT,
) -> tuple[bool, str]:
    filter_graph = build_batch_filter(
        batch, target_w, target_h, target_fps, timezone, font_value, style
    )

    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for clip in batch:
        command += ["-i", str(clip.source)]
    command += [
        "-filter_complex", filter_graph,
        "-map", "[outv]",
        "-map", "[outa]",
        "-c:v", "libx264",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-preset", preset,
        "-crf", str(crf),
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "9999",
        # -progress writes a key=value stream for programs, without changing
        # anything else. The option meant for this since 2012; parsing the usual
        # status line would be fragile (shape varies by version, not meant to be
        # parsed).
        "-progress", "pipe:1", "-nostats",
        str(output_path),
    ]
    env = dict(os.environ, TZ="UTC0")
    process = runtime.demarrer(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        # bufsize=1: line-by-line reads. Without it Python buffers before yielding
        # and progress arrives in bursts.
        bufsize=1,
        env=env,
        # Own session off Windows, like side-by-side launched verbs (see
        # blink_cli.executer): avec_descendance=True on this PID makes
        # os.killpg(getpgid(pid)) (runtime.arreter_processus), which would
        # otherwise hit the whole terminal group that launched the parent.
        start_new_session=(sys.platform != "win32"),
    )
    # Register so a "stop" elsewhere kills it directly: the main PID that launched
    # us never gets taskkill /T (browser protection, see arreter_processus), so
    # without this ffmpeg would orphan and run until it finishes.
    runtime.inscrire_travailleur(process.pid)

    # stderr is drained on a dedicated thread: if it fills while reading stdout,
    # ffmpeg blocks on write and everything stalls.
    captured = []
    drain = threading.Thread(target=lambda: captured.append(process.stderr.read()))
    drain.start()

    total = sum(clip.duration for clip in batch) or 1.0

    # Watchdog: a stuck ffmpeg (seen in production, AUDIT-2026-08-13 28.82)
    # would otherwise leave `process.wait()` - and reading `process.stdout`
    # before it - waiting forever. The whole merge loop then froze silently,
    # with no error or log, until manual stop.
    #
    # Threshold is on LACK of progress, not a fixed total duration: `-progress
    # pipe:1` writes several lines per second while encoding advances, regardless
    # of machine speed or load. A fixed delay proportional to batch duration
    # would require guessing a speed factor (slow PC, busy machine...) that is
    # necessarily arbitrary; here it does not matter if the batch takes 10 seconds
    # or 2 hours as long as it keeps moving; only prolonged silence is suspect.
    # SILENCE_MAX (module): same threshold as concat_copy, which has no -progress
    # to watch.
    dernier_signe = time.monotonic()
    signe_verrou = threading.Lock()
    termine = threading.Event()
    tue_par_silence = False

    def surveiller() -> None:
        nonlocal tue_par_silence
        while not termine.wait(10):
            with signe_verrou:
                silence = time.monotonic() - dernier_signe
            if silence > SILENCE_MAX:
                tue_par_silence = True
                process.kill()
                return

    chien_de_garde = threading.Thread(target=surveiller, daemon=True)
    chien_de_garde.start()
    try:
        for line in process.stdout:
            with signe_verrou:
                dernier_signe = time.monotonic()
            if on_progress is None:
                continue
            name, _, value = line.strip().partition("=")
            # out_time_us and out_time_ms are both in microseconds; the latter
            # is misnamed and kept for compatibility.
            if name in ("out_time_us", "out_time_ms") and value.isdigit():
                on_progress(min(int(value) / 1_000_000 / total, 1.0))
        process.wait()
    finally:
        termine.set()
        chien_de_garde.join(timeout=5)
        runtime.retirer_travailleur(process.pid)
    drain.join(timeout=5)
    stderr = "".join(part for part in captured if part).strip()
    if tue_par_silence:
        return False, msg("ffmpeg_silencieux", secondes=int(SILENCE_MAX))
    if process.returncode != 0 or not valid_mp4(output_path):
        return False, stderr or msg("mp4_invalide")
    return True, ""


def concat_copy(ffmpeg: str, parts: list, destination: Path) -> tuple[bool, str]:
    """Join already re-encoded batches (same codec/resolution/fps) by stream copy:
    fast, lossless."""
    list_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".ffconcat",
            prefix="blink_",
            dir=destination.parent,
            encoding="utf-8",
            newline="\n",
            delete=False,
        ) as concat_file:
            list_path = Path(concat_file.name)
            for part in parts:
                escaped = part.resolve().as_posix().replace("'", "'\\''")
                concat_file.write(f"file '{escaped}'\n")

        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", "-movflags", "+faststart",
            str(destination),
        ]
        process = runtime.demarrer(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            # Same reason as run_ffmpeg_batch: without a separate session off
            # Windows, arreter_processus killpg would hit the terminal group
            # that launched the parent rather than this process alone.
            start_new_session=(sys.platform != "win32"),
        )
        # Same registration as run_ffmpeg_batch, same reason: this final concat
        # is a real child of the current process, but taskkill /T never applies
        # to the main PID (browser protection).
        runtime.inscrire_travailleur(process.pid)
        try:
            try:
                _, stderr = process.communicate(timeout=SILENCE_MAX)
            except subprocess.TimeoutExpired:
                # Same documented risk as run_ffmpeg_batch (AUDIT-2026-08-13
                # 28.82): a stuck ffmpeg would leave communicate() waiting
                # forever, freezing weekly or monthly assembly silently. No
                # -progress here (stream copy, nothing to watch): a fixed timeout
                # acts as watchdog instead. kill() then a second communicate()
                # without timeout: subprocess docs warn TimeoutExpired does not
                # kill the child, or pipes stay open and a later wait() may block
                # too.
                process.kill()
                process.communicate()
                return False, msg("ffmpeg_silencieux", secondes=int(SILENCE_MAX))
        finally:
            runtime.retirer_travailleur(process.pid)
        if process.returncode != 0 or not valid_mp4(destination):
            message = (stderr or "").strip() or msg("mp4_invalide")
            return False, message
        return True, ""
    finally:
        if list_path is not None:
            list_path.unlink(missing_ok=True)


def progress_printer(label: str):
    """Build a progress reporter for an encode.

    On a terminal the line is rewritten in place like any CLI tool. In a pipe
    (serve.py reading output), a bare carriage return emits no line and nothing
    arrives until the end: write full lines then, which the reader recognizes
    by shape and does not add to the log.

    Under pythonw.exe (autostart shortcuts, see autostart.py), sys.stdout is
    None: every merge failed on the first batch before encoding anything,
    `sys.stdout.isatty()` raising AttributeError, caught by the --loop loop and
    retried forever (seen in production 27 Aug 2026: "run interrupted by error"
    every 5 min for hours). No stream to write to: the reporter no-ops rather
    than retrying a doomed print()."""
    if sys.stdout is None:
        return lambda fraction: None
    interactive = sys.stdout.isatty()
    state = {"percent": -1, "when": 0.0}

    def report(fraction: float) -> None:
        percent = int(fraction * 100)
        now = time.monotonic()
        if percent < state["percent"] + 2 and percent < 100:
            return
        if now - state["when"] < 0.5 and percent < 100:
            return
        if percent == state["percent"]:
            return
        state["percent"], state["when"] = percent, now
        if interactive:
            print(f"\r  {label} {percent:3d}%", end="", flush=True)
            if percent >= 100:
                print()
        else:
            print(f"  {label} {percent}%", flush=True)

    return report


def render_key(
    identity: str,
    clip: ClipInfo,
    target: tuple,
    epoch: int,
    font_path: Path | None,
    preset: str,
    crf: int,
    style: TimestampStyle = STYLE_PAR_DEFAUT,
) -> str:
    """Render fingerprint of a normalized segment: anything that changes content
    must appear here, or a stale segment would be reused silently. Toggle with/
    without timestamp included: without it, an already timestamped segment would
    stay as-is after disabling the setting, timestamp baked in rather than
    truly removed.

    Style (size/color/opacity, GitHub #7) counts only when a timestamp is
    actually drawn: otherwise changing color with --no-timestamp active would
    re-encode everything for nothing, the value never reaching the image."""
    target_w, target_h, target_fps = target
    style_tag = (
        f"{style.size}/{style.color}/{style.box_opacity}"
        if font_path is not None else "sans-horodatage"
    )
    payload = "|".join(
        [
            NORMALIZE_VERSION,
            identity,
            stat_tag(clip.source),
            f"{target_w}x{target_h}@{target_fps}",
            str(epoch),
            font_path.as_posix() if font_path is not None else "sans-horodatage",
            preset,
            str(crf),
            style_tag,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_clip(
    ffmpeg: str,
    timezone: ZoneInfo,
    registry: dict,
    normalized_dir: Path,
    identity: str,
    clip: ClipInfo,
    target: tuple,
    key: str,
    font_path: Path | None,
    preset: str,
    crf: int,
    force: bool,
    on_progress=None,
    style: TimestampStyle = STYLE_PAR_DEFAUT,
) -> tuple[bool, str, bool]:
    """Produce the normalized (and timestamped if `font_path` is set) version of
    a clip when needed.

    Returns (ok, error, re-encoded). An existing segment whose render key
    matches is left alone: that makes merge truly incremental."""
    destination = normalized_dir / identity
    entry = registry["clips"].get(identity) or {}
    if not force and entry.get("key") == key and valid_mp4(destination):
        return True, "", False

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep .mp4 extension: ffmpeg picks output format from it; a bare .tmp suffix
    # would leave it without muxing.
    pending = destination.with_name(destination.stem + ".tmp.mp4")
    pending.unlink(missing_ok=True)
    target_w, target_h, target_fps = target
    font_value = quote_filter_path(font_path) if font_path is not None else None
    ok, error = run_ffmpeg_batch(
        ffmpeg, [clip], target_w, target_h, target_fps, timezone,
        font_value, preset, crf, pending, on_progress, style=style,
    )
    if not ok:
        pending.unlink(missing_ok=True)
        return False, error, False

    pending.replace(destination)
    entry["key"] = key
    entry["normalized_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    registry["clips"][identity] = entry
    return True, "", True


def merge_group(
    ffmpeg: str,
    segments: list,
    destination: Path,
) -> tuple[bool, str]:
    """Assemble a day's video from its normalized segments.

    Nothing left to encode: segments share codec, resolution, and frame rate, so
    stream copy is enough. The daily is a view of normalized storage, like weekly
    and monthly."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_video = destination.with_name(destination.stem + ".tmp.mp4")
    temporary_video.unlink(missing_ok=True)
    try:
        ok, error = concat_copy(ffmpeg, segments, temporary_video)
        if not ok:
            return False, error
        if not valid_mp4(temporary_video):
            return False, msg("mp4_invalide")
        temporary_video.replace(destination)
        return True, ""
    finally:
        temporary_video.unlink(missing_ok=True)


def prune_normalized(normalized_dir: Path, registry: dict, keep: set) -> int:
    """Delete segments no known clip claims anymore, and matching registry entries.

    Only after a full pass: with --camera or --date filtering, untreated groups
    would not have declared their segments and would be wrongly erased."""
    if not normalized_dir.is_dir():
        return 0
    removed = 0
    for candidate in normalized_dir.rglob("*.mp4*"):
        if candidate.is_file() and candidate not in keep:
            candidate.unlink(missing_ok=True)
            removed += 1
    for directory in sorted(
        (p for p in normalized_dir.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        if not any(directory.iterdir()):
            directory.rmdir()
    known = {
        identity
        for identity in registry["clips"]
        if (normalized_dir / identity) in keep
    }
    for identity in list(registry["clips"]):
        if identity not in known:
            del registry["clips"][identity]
    return removed


def collect_dailies(output_dir: Path) -> dict:
    """Inventory already produced daily videos, per camera.

    Read disk rather than merge registry so aggregates stay correct even if
    dailies were built across several filtered runs (--date, --camera)."""
    dailies = defaultdict(list)
    if not output_dir.is_dir():
        return {}
    for camera_dir in sorted(
        p for p in output_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    ):
        for video in sorted(camera_dir.glob("*.mp4")):
            match = DAILY_NAME.match(video.name)
            if not match or not valid_mp4(video):
                continue
            try:
                day = dt.date.fromisoformat(match.group("day"))
            except ValueError:
                continue
            dailies[camera_dir.name].append((day, video))
    return dict(dailies)


def period_label(day: dt.date, period: str) -> str:
    """Label of the period containing `day`.

    ISO 8601 week (Monday-Sunday, YYYY-Www): same ISO date convention used
    elsewhere in the project, avoiding "start of week" ambiguity by country."""
    if period == "weekly":
        year, week, _ = day.isocalendar()
        return f"{year}-W{week:02d}"
    return f"{day.year}-{day.month:02d}"


def period_fingerprint(parts: list) -> str:
    digest = hashlib.sha256()
    digest.update((PERIOD_VERSION + "\n").encode("utf-8"))
    for part in parts:
        stat = part.stat()
        digest.update(f"{part.name}|{stat.st_size}|{stat.st_mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


def concat_videos(
    ffmpeg: str,
    timezone: ZoneInfo,
    parts: list,
    destination: Path,
    preset: str,
    crf: int,
) -> tuple[bool, str]:
    """Join already timestamped videos.

    Fast path: if all share resolution and frame rate, stream copy is enough (no
    loss, seconds). Otherwise re-encode via the concat filter, the reliable way
    to assemble heterogeneous sources."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp.mp4")
    temporary.unlink(missing_ok=True)

    try:
        # Try stream copy first without measuring: one camera's dailies all come
        # from the same normalized store, hence same codec, resolution, and fps.
        #
        # Do not decide from measured fps: average fps of a concatenated file is
        # frames/duration and varies by hundredths day to day (30.00, 30.03,
        # 30.04 here) because Blink cameras are not perfectly regular. That test
        # labeled a homogeneous store "heterogeneous" and re-encoded whole weeks
        # and months every pass.
        ok, error = concat_copy(ffmpeg, parts, temporary)
        if not ok:
            print(msg("copie_refusee"))
            try:
                infos = []
                for part in parts:
                    duration, width, height, fps, has_audio = probe_clip_info(ffmpeg, part)
                    infos.append(
                        ClipInfo(None, part, duration, width, height, fps, has_audio)
                    )
            except RuntimeError as error:
                return False, str(error)
            ok, error = run_ffmpeg_batch(
                ffmpeg, infos,
                max(i.width for i in infos),
                max(i.height for i in infos),
                float(round(max(i.fps for i in infos))),
                timezone, None, preset, crf, temporary,
            )
        if not ok:
            return False, error
        if not valid_mp4(temporary):
            return False, msg("mp4_invalide")
        temporary.replace(destination)
        return True, ""
    finally:
        temporary.unlink(missing_ok=True)


def build_periods(
    ffmpeg: str,
    timezone: ZoneInfo,
    output_dir: Path,
    period_dir: Path,
    period: str,
    force: bool,
    preset: str,
    crf: int,
    proteges=frozenset(),
) -> tuple[int, int, int]:
    """Assemble daily videos into weekly or monthly aggregates.

    ``proteges``: "camera|period" keys where a day lost both raw clips and its
    daily (see periodes_a_proteger()). Their assembled aggregate may still
    contain that day: do not rebuild from other days or delete as empty period."""
    dailies = collect_dailies(output_dir)
    # No early return if dailies is empty: a camera with no dailies left (all
    # excluded) must still reach obsolete period cleanup at the bottom, or old
    # weekly/monthly files would never be revisited or deleted.

    state_path = period_dir / MERGE_STATE
    state = load_json(state_path, {"version": 1, "groups": {}})
    state.setdefault("groups", {})

    built = skipped = failed = 0
    # (camera|label) keys still covered by at least one daily: the rest of
    # state["groups"] are periods that became empty (all days excluded or gone),
    # never revisited by the loop below since it only walks collect_dailies() -
    # without this tracking, their assembled aggregate stayed on disk forever.
    labels_attendus = set()
    for camera, entries in sorted(dailies.items()):
        buckets = defaultdict(list)
        for day, video in entries:
            buckets[period_label(day, period)].append(video)

        for label, parts in sorted(buckets.items()):
            destination = period_dir / camera / f"{label}_{camera}.mp4"
            key = f"{camera}|{label}"
            labels_attendus.add(key)
            if key in proteges:
                print(msg("periode_protegee", periode=key))
                skipped += 1
                continue
            fingerprint = period_fingerprint(parts)

            if (
                not force
                and state["groups"].get(key, {}).get("fingerprint") == fingerprint
                and valid_mp4(destination)
            ):
                print(msg("periode_deja_a_jour", nom=destination.name, jours=len(parts)))
                skipped += 1
                continue

            print(msg("periode_assemblage", label=label, camera=camera, jours=len(parts)))
            ok, error = concat_videos(
                ffmpeg, timezone, parts, destination, preset, crf
            )
            if not ok:
                print(msg("echec_generique", erreur=error))
                failed += 1
                continue

            state["groups"][key] = {
                "fingerprint": fingerprint,
                "path": destination.relative_to(period_dir).as_posix(),
                "days": len(parts),
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            save_json(state_path, state)
            print(msg("cree", destination=destination))
            built += 1

    obsoletes = [key for key in state["groups"]
                 if key not in labels_attendus and key not in proteges]
    if obsoletes:
        for key in obsoletes:
            entree = state["groups"].pop(key)
            chemin = entree.get("path")
            if chemin:
                (period_dir / chemin).unlink(missing_ok=True)
            print(msg("periode_supprimee", periode=key))
        save_json(state_path, state)

    return built, skipped, failed


def periodes_a_proteger(indisponibles: set, output_dir: Path, period: str) -> set:
    """camera|period keys build_periods() must not rebuild or delete: days
    where a raw clip is unavailable AND the daily is missing (audit 26/09/2026,
    B02). Rebuilding from other days would shorten an aggregate that may still
    contain that day, sometimes its only copy.

    Computed over all days, not only --date or --camera filter: build_periods()
    rebuilds all periods regardless of filter. An unavailable day whose daily
    still exists protects nothing; the aggregate keeps it as-is."""
    presentes = {
        (dossier, jour)
        for dossier, entrees in collect_dailies(output_dir).items()
        for jour, _ in entrees
    }
    proteges = set()
    for camera, jour_texte in indisponibles:
        try:
            jour = dt.date.fromisoformat(jour_texte)
        except (TypeError, ValueError):
            continue
        dossier = safe_name(camera)
        if (dossier, jour) not in presentes:
            proteges.add(f"{dossier}|{period_label(jour, period)}")
    return proteges


COULEUR_FFMPEG_RE = re.compile(r"^#?[A-Za-z0-9]+(@(0(\.[0-9]+)?|1(\.0+)?))?$")


def _couleur_ffmpeg_valide(valeur: str) -> str:
    """Accept only a color name or hex code, with optional @opacity (0.0-1.0):
    full syntax fontcolor= in drawtext_chain() understands. Reject everything
    else, especially ":" and "," which would break this filtergraph (new option
    or new filter)."""
    if not COULEUR_FFMPEG_RE.match(valeur):
        raise argparse.ArgumentTypeError(msg("couleur_invalide", valeur=valeur))
    return valeur


def _taille_police_valide(valeur: str) -> int:
    try:
        nombre = int(valeur)
    except ValueError:
        nombre = -1
    if not 8 <= nombre <= 500:
        raise argparse.ArgumentTypeError(msg("taille_police_invalide", valeur=valeur))
    return nombre


def _opacite_valide(valeur: str) -> float:
    try:
        nombre = float(valeur)
    except ValueError:
        nombre = -1.0
    if not 0.0 <= nombre <= 1.0:
        raise argparse.ArgumentTypeError(msg("opacite_invalide", valeur=valeur))
    return nombre


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blink2video merge",
        description=msg("aide_desc"),
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--weekly-output", type=Path, default=DEFAULT_WEEKLY)
    parser.add_argument("--monthly-output", type=Path, default=DEFAULT_MONTHLY)
    parser.add_argument(
        "--normalized-output", type=Path, default=DEFAULT_NORMALIZED,
        help=msg("aide_normalized"),
    )
    parser.add_argument("--no-weekly", action="store_true", help=msg("aide_no_weekly"))
    parser.add_argument("--no-monthly", action="store_true", help=msg("aide_no_monthly"))
    parser.add_argument("--no-timestamp", action="store_true", help=msg("aide_no_timestamp"))
    parser.add_argument(
        "--excluded-output", type=Path, default=DEFAULT_EXCLUDED,
        help=msg("aide_excluded_output"),
    )
    parser.add_argument(
        "--exclude", nargs="+", metavar="CLIP", default=[], help=msg("aide_exclude"),
    )
    parser.add_argument(
        "--include", nargs="+", metavar="CLIP", default=[], help=msg("aide_include"),
    )
    parser.add_argument("--timezone", default="Europe/Paris")
    runtime.ajouter_boucle(parser)
    parser.add_argument("--date", help=msg("aide_date"))
    parser.add_argument("--camera", help=msg("aide_camera"))
    parser.add_argument("--force", action="store_true", help=msg("aide_force"))
    parser.add_argument("--font", type=Path, default=None, help=msg("aide_font"))
    parser.add_argument(
        "--font-size", type=_taille_police_valide, default=None,
        help=msg("aide_font_size"),
    )
    parser.add_argument(
        "--font-color", type=_couleur_ffmpeg_valide, default="white",
        help=msg("aide_font_color"),
    )
    parser.add_argument(
        "--box-opacity", type=_opacite_valide, default=0.55,
        help=msg("aide_box_opacity"),
    )
    parser.add_argument("--preset", default="veryfast", help=msg("aide_preset"))
    parser.add_argument("--crf", type=int, default=21, help=msg("aide_crf"))
    return parser.parse_args()


def journal(ligne: str) -> None:
    """Timestamped trace: launched by the scheduler, the script has no console
    (AUDIT-2026-08-23, silent merge loop stall)."""
    moment = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with (runtime.app_dir() / "merge.log").open("a", encoding="utf-8") as fichier:
            fichier.write(f"{moment}  {ligne}\n")
    except OSError:
        pass


def main() -> int:
    args = parse_args()
    def travail():
        try:
            with runtime.verrou("merge", "assemblage", stale_after=7200,
                                attente=30):
                try:
                    code = _executer(args)
                finally:
                    # Always clear progress marker, even after error: otherwise
                    # the UI would think work is still running.
                    runtime.fin_travail()
        except runtime.BusyError as erreur:
            # Assembly already in progress does the same work: doubling it would
            # re-encode the same clips and corrupt the registry.
            print(msg("assemblage_deja_en_cours", erreur=erreur))
            journal(f"assembly already in progress: {erreur}")
            return 0
        runtime.marquer("merge")
        return code

    return runtime.repeter(travail, args.loop, journal)


def _calculer_cibles_encodage(groups: dict, registry: dict, input_dir: Path,
                             ffmpeg: str) -> dict:
    """Compute one format per camera across all its clips, regardless of filters."""
    targets = {}
    for camera in sorted({key[0] for key in groups}):
        infos = [
            clip_info(ffmpeg, registry, clip_identity(input_dir, source), created, source)
            for key, clips in groups.items()
            if key[0] == camera
            for created, source in clips
        ]
        targets[camera] = camera_target(registry, camera, infos)
    return targets


def _planifier_normalisation(args, groups: dict, selected: dict, targets: dict,
                            registry: dict, *, ffmpeg: str, timezone: ZoneInfo,
                            input_dir: Path, normalized_dir: Path,
                            font_path: Path | None,
                            style: TimestampStyle = STYLE_PAR_DEFAUT) -> tuple:
    """List days to process, segments to keep, and clips to encode."""
    plan: dict = {}
    used_segments: set = set()
    pending: set = set()
    for (camera, day), clips in sorted(groups.items(), key=lambda item: item[0]):
        target = targets[camera]
        entries = []
        for created, source in clips:
            identity = clip_identity(input_dir, source)
            info = clip_info(ffmpeg, registry, identity, created, source)
            key = render_key(
                identity, info, target, wall_clock_epoch(created, timezone),
                font_path, args.preset, args.crf, style=style,
            )
            # Declared even when not selected, to survive cleanup.
            used_segments.add(normalized_dir / identity)
            if (camera, day) not in selected:
                continue
            entries.append((identity, info, key))
            known = (registry["clips"].get(identity) or {}).get("key")
            if args.force or known != key or not valid_mp4(normalized_dir / identity):
                pending.add(identity)
        if (camera, day) in selected:
            plan[(camera, day)] = (target, entries)
    return plan, used_segments, pending


def _normaliser_plan(args, plan: dict, pending: set, registry: dict,
                    registry_path: Path, *, ffmpeg: str, timezone: ZoneInfo,
                    normalized_dir: Path, font_path: Path | None,
                    style: TimestampStyle = STYLE_PAR_DEFAUT) -> tuple:
    """Encode the plan; return prepared days and failure count."""
    normalized: dict = {}
    encoded = reused = failed = 0
    position, total = 0, len(pending)
    if total:
        print(msg("normalisation_titre", total=total))
    for (camera, day), (target, entries) in sorted(plan.items(), key=lambda i: i[0]):
        keys, segments = [], []
        journee_incomplete = False
        for identity, info, key in entries:
            report = None
            if identity in pending:
                position += 1
                print(f"  [{position}/{total}] {identity}", flush=True)
                report = progress_printer(f"[{position}/{total}]")
                runtime.travail("Preparing clips", position - 1, total,
                                cle="phase.prepare_clips")
            ok, error, did_encode = normalize_clip(
                ffmpeg, timezone, registry, normalized_dir, identity, info,
                target, key, font_path, args.preset, args.crf, args.force,
                report, style=style,
            )
            if not ok:
                print(msg("echec_normalisation", erreur=error))
                failed += 1
                # A day where one clip fails to normalize must not have its
                # truncated segment list used for assembly (step 3): that would
                # replace a complete daily with a partial, silently shorter one.
                # journee_incomplete blocks that assembly.
                journee_incomplete = True
                continue
            encoded += did_encode
            reused += not did_encode
            keys.append(key)
            segments.append(normalized_dir / identity)
            if did_encode:
                # Save after each freshly encoded clip, not only at end of day:
                # a mid-run stop (restart, crash) should lose only the clip being
                # encoded, not progress on earlier clips the same day. Without
                # this, a day of dozens of clips restarted entirely on every
                # interruption even after most were encoded - seen in production:
                # several consecutive restarts on one ~thirty-clip day, relaunching
                # ffmpeg on all clips each time.
                save_json(registry_path, registry)

        normalized[(camera, day)] = (keys, segments, journee_incomplete)
        save_json(registry_path, registry)

    print(msg("normalisation_resume", encoded=encoded, reused=reused))
    return normalized, failed


def _executer(args) -> int:
    input_dir = args.input.resolve()
    output_dir = args.output.resolve()
    normalized_dir = args.normalized_output.resolve()
    excluded_dir = args.excluded_output.resolve()

    # Exclusions run before everything else; the rest of the pass then rebuilds
    # what depends on them naturally.
    if args.exclude or args.include:
        try:
            if args.exclude:
                set_excluded(input_dir, normalized_dir, excluded_dir, args.exclude, True)
            if args.include:
                set_excluded(input_dir, normalized_dir, excluded_dir, args.include, False)
        except RuntimeError as error:
            print(msg("erreur_generique", erreur=error))
            return 1

    style = TimestampStyle(args.font_size, args.font_color, args.box_opacity)
    try:
        timezone = ZoneInfo(args.timezone)
        ffmpeg = find_ffmpeg()
        font_path = None
        if not args.no_timestamp:
            check_drawtext_available(ffmpeg)
            font_path = find_font(args.font)
            check_timestamp_rendering(ffmpeg, font_path, style)
        groups = load_groups(input_dir, timezone)
        indisponibles, identites_indisponibles = journees_a_source_indisponible(
            input_dir, timezone
        )
    except (RuntimeError, ZoneInfoNotFoundError) as error:
        print(msg("erreur_generique", erreur=error))
        return 1

    selected = {
        key: clips
        for key, clips in groups.items()
        if (not args.date or key[1] == args.date)
        and (not args.camera or key[0].casefold() == args.camera.casefold())
    }
    if not selected:
        print(msg("aucun_groupe"))

    merge_state_path = output_dir / MERGE_STATE
    merge_state = load_json(merge_state_path, {"version": 1, "groups": {}})
    merge_state.setdefault("version", 1)
    merge_state.setdefault("groups", {})

    registry_path = normalized_dir / NORMALIZED_STATE
    registry = load_json(registry_path, {"version": 1, "cameras": {}, "clips": {}})
    registry.setdefault("version", 1)
    registry.setdefault("cameras", {})
    registry.setdefault("clips", {})

    legacy = output_dir / LEGACY_SEGMENT_DIR
    if legacy.is_dir():
        shutil.rmtree(legacy, ignore_errors=True)
        print(msg("cache_supprime", chemin=legacy))

    # Step 1: encoding targets. Computed over all clips for a camera, including
    # those a --date or --camera filter excludes from building, or the target
    # would depend on the filter used.
    try:
        targets = _calculer_cibles_encodage(groups, registry, input_dir, ffmpeg)
    except RuntimeError as error:
        print(msg("erreur_generique", erreur=error))
        return 1
    save_json(registry_path, registry)

    # Step 2: normalization plan. Build the full list before encoding anything
    # so we can show "[3/24]": a counter without a known total says nothing about
    # time left, and that total feeds serve.py's progress bar.
    plan, used_segments, pending = _planifier_normalisation(
        args, groups, selected, targets, registry, ffmpeg=ffmpeg,
        timezone=timezone, input_dir=input_dir, normalized_dir=normalized_dir,
        font_path=font_path, style=style,
    )
    save_json(registry_path, registry)

    # Step 2b: encode only missing or stale segments.
    normalized, failed = _normaliser_plan(
        args, plan, pending, registry, registry_path, ffmpeg=ffmpeg,
        timezone=timezone, normalized_dir=normalized_dir, font_path=font_path,
        style=style,
    )

    # Step 3: assemble dailies by stream copy.
    built = skipped = 0
    todo = sorted(normalized.items(), key=lambda item: item[0])
    for index, ((camera, day), (keys, segments, journee_incomplete)) in enumerate(todo, start=1):
        camera_path = safe_name(camera)
        destination = output_dir / camera_path / f"{day}_{camera_path}.mp4"
        state_key = f"{camera}|{day}"
        if (camera, day) in indisponibles:
            # load_groups() omits missing/invalid raw files. The list can be
            # non-empty AND incomplete: never replace a complete daily with
            # only still-accessible clips.
            print(msg("jour_reportee_source", nom=destination.name))
            failed += 1
            continue
        if not segments:
            # Nothing to assemble: either the day has no valid clips left (all
            # excluded), or encoding failed for those that existed. Only the
            # first case should remove an already assembled video - the second
            # must not delete it on a transient failure (network, ffmpeg): it
            # stays valid and will be retried next pass.
            _, entries = plan.get((camera, day), (None, ()))
            # (camera, day) in indisponibles: a clip for this day still exists
            # in the registry but its file is gone - distinct from exclusion by
            # choice (see journees_a_source_indisponible()). Keep the already
            # assembled daily rather than deleting it for this absence alone.
            if not entries and (camera, day) not in indisponibles and destination.exists():
                destination.unlink()
                merge_state["groups"].pop(state_key, None)
                save_json(merge_state_path, merge_state)
                print(msg("jour_supprimee", nom=destination.name))
            continue
        if journee_incomplete:
            # At least one clip for this day failed normalization: segments is
            # shorter than clips actually present; assembling would replace a
            # complete daily with a shorter partial silently. failed already
            # counted (step 2); the day stays as-is and will be retried next
            # pass, like a total failure (see above).
            print(msg("jour_reportee_partielle", nom=destination.name))
            continue
        fingerprint = group_fingerprint(keys)
        previous = merge_state["groups"].get(state_key, {})

        if (
            not args.force
            and previous.get("fingerprint") == fingerprint
            and valid_mp4(destination)
        ):
            print(msg("jour_deja_a_jour", nom=destination.name, clips=len(segments)))
            skipped += 1
            continue

        print(msg("jour_assemblage", index=index, total=len(todo), jour=day,
                  camera=camera, clips=len(segments)))
        runtime.travail("Assembling videos", index - 1, len(todo),
                        cle="phase.assemble_videos")
        ok, error = merge_group(ffmpeg, segments, destination)
        if not ok:
            print(msg("echec_generique", erreur=error))
            failed += 1
            continue

        merge_state["groups"][state_key] = {
            "fingerprint": fingerprint,
            "path": destination.relative_to(output_dir).as_posix(),
            "clips": len(segments),
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        save_json(merge_state_path, merge_state)
        print(msg("cree", destination=destination))
        built += 1

    if not args.date and not args.camera and not failed:
        # A segment already encoded for a clip whose raw file later disappeared
        # (USB unplugged, folder moved) stays valid: preserve it despite not being
        # in used_segments, or prune_normalized() would erase it like a clip
        # excluded by choice (see journees_a_source_indisponible()).
        keep = used_segments | {normalized_dir / identity
                                for identity in identites_indisponibles}
        removed = prune_normalized(normalized_dir, registry, keep)
        if removed:
            print(msg("stock_normalise", n=removed))
        save_json(registry_path, registry)

    print(msg("journalieres_resume", built=built, skipped=skipped, failed=failed))

    if indisponibles & selected.keys():
        # A postponed daily may itself be missing. Rebuilding its periods from
        # other days only would shorten them too. Keep aggregates until fixed.
        print(msg("agregats_reportes"))
        return 1

    for period, period_dir, desactive in (
        ("weekly", args.weekly_output.resolve(), args.no_weekly),
        ("monthly", args.monthly_output.resolve(), args.no_monthly),
    ):
        if desactive:
            continue
        label = msg("label_hebdomadaires") if period == "weekly" else msg("label_mensuelles")
        print(msg("periode_titre", label=label))
        p_built, p_skipped, p_failed = build_periods(
            ffmpeg, timezone, output_dir, period_dir, period,
            args.force, args.preset, args.crf,
            proteges=periodes_a_proteger(indisponibles, output_dir, period),
        )
        failed += p_failed
        print(msg("periode_resume", label=label, built=p_built, skipped=p_skipped,
                  failed=p_failed))

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
