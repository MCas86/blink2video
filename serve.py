"""Local interface to watch Blink clips and discard or restore them.

Why a small HTTP server instead of a standalone HTML file: a page opened
via file:// can neither move a file nor write a registry. It could only
produce a list to copy by hand — half a tool. So a local process is
needed, and once it exists, it might as well serve the videos too.

Why not a Tk window: Tk does not play video. You would have to hand off
to an external player, and the “watch, judge, next” loop breaks on every
clip. A browser <video> tag gives playback, scrubbing, speed and
fullscreen for free.

Nothing beyond the standard library is required, and all decisions
(exclude, restore) are delegated to merge_daily.py: this interface
reimplements no rules; it calls the same functions as the command line.
"""

from __future__ import annotations  # Python 3.8 (Windows 7 build): "X | None" annotations are evaluated only when stringified, never at runtime.

import argparse
import asyncio
import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import hmac
import http.server
import ipaddress
import json
import mimetypes
import os
import queue
import re
import select
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8 (Windows 7 build, see build-win7.yml): no zoneinfo in stdlib.
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Before any dependency import: an isolated environment is prepared here
# and the program is relaunched inside it if needed.
from nico579_commons import serveweb

import runtime

LIBELLES = {
    "en": {
        "aide_desc": "Local interface to watch Blink clips, discard some and bring them back.",
        "aide_hub": "Blink Sync Module name; all modules if omitted",
        "aide_thumbs": "thumbnail cache; disposable, rebuilt on demand",
        "aide_trusted_host":
            "extra hostnames, addresses or CIDR subnets, comma-separated, "
            "accepted as Host besides loopback: for a private tunnel "
            "(Tailscale, WireGuard) or a trusted LAN bound directly to this "
            "instance with BLINK_BIND, no reverse proxy in front. Also "
            "settable from the page (Settings)",
        "aide_open_browser": "open the page in the browser at startup",
        "vignette_erreur": "[thumbnail] {identity}: {type}: {erreur}",
        "ecarter_lot_erreur": "Exclude (batch): {erreur}",
        "erreur_generique": "Error: {erreur}",
        "echec_ecoute_port": "Could not listen on port {port}: {erreur}",
        "autre_instance_deja":
            "Another « blink2video serve » is probably already running. Stop it, "
            "or choose another port with --port.",
        "interface_disponible": "Interface available at {url}   (Ctrl+C to stop)",
        "attention_blink_bind":
            "Warning: BLINK_BIND={bind} - the interface is also reachable "
            "from the rest of the network on port {port}, with no "
            "authentication. Only do this on a trusted network.",
        "attention_trusted_host":
            "Warning: trusted host set to {hote} - the interface is also "
            "reachable under that name, with no authentication. Only do "
            "this for a private tunnel (Tailscale, WireGuard) bound to that "
            "address with BLINK_BIND.",
        "interruption_arret": "\nStopping.",
        "snapshot_commande_refusee": "Blink refused the picture command.",
        "snapshot_non_confirme": "Blink did not confirm the picture.",
        "snapshot_image_indisponible":
            "The camera took the picture, but Blink has not published it yet. "
            "Try again in a moment.",
        "webhook_camera_manquante": "Missing «camera» parameter.",
        "webhook_jeton_invalide": "Invalid or missing webhook token.",
        "webhook_camera_inconnue": "Unknown camera: {camera}.",
        "webhook_systeme_inconnu": "Unknown system: {systeme}.",
        "webhook_nom_ambigu": "Several items have this name: {nom}. Rename one of them in the Blink app.",
        "webhook_cible": "Give exactly one target: camera or system.",
        "webhook_armed_invalide": "Parameter armed must be true or false.",
        "webhook_camera_hors_ligne": "Camera \u201c{camera}\u201d is offline: arming not attempted.",
    },
}
LIBELLES["fr"] = dict(LIBELLES["en"])


def msg(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


runtime.bootstrap()

import access_control
import autostart
import blink_auth
import blink_engine
import blink_models
import blink_registre
import blink_webrtc
import maj
import merge_daily as md
import watch


BASE_DIR = runtime.app_dir()

# A clip identity is a relative path "camera/month/file.mp4".
# Everything that arrives from the browser is checked against the registry
# before opening anything: no hand-crafted path is ever served.
IDENTITY = re.compile(r"^[\w.\- ]+(/[\w.\- ]+)*\.mp4$")
# Photos (GitHub issue #9): a single level (camera/file.jpg), never
# nested like clips — no reason for them to be.
IDENTITY_SNAPSHOT = re.compile(r"^[\w.\- ]+/[\w.\- ]+\.jpg$")

# Progress announced by blink2video.py and merge_daily.py, and phase headings
# emitted by blink_engine.py ("=== LOCAL STORAGE ===", "=== SUBSCRIPTION CLOUD ===").
PROGRESS = re.compile(r"\[(\d+)/(\d+)\]")
# Progress line inside a clip: "[3/24] 45%", nothing else.
INNER = re.compile(r"^\s*\[\d+/\d+\]\s+(\d+)%\s*$")
HEADING = re.compile(r"^=== (.+?) ===$")

# Caps concurrent thumbnail extractions (see send_thumb): the browser
# requests them all at once on every filter change; a fixed limit avoids
# saturating the machine with that many ffmpeg processes at once.
# Originally fixed at 2, far too low for a modern multi-core machine:
# switching from a wide preset (month, already cached) to a narrower one
# covering never-seen clips (today, this week) queued every missing
# thumbnail behind only two extractions at a time, the player's native
# spinner spinning on each until its turn (observed in practice, 2026-08-27).
# An extraction is much lighter than a full encode (one image, not a whole
# video): the core count is a reasonable ceiling, not a comparable risk.
THUMB_SLOTS = threading.Semaphore(min(8, os.cpu_count() or 4))

# One lock per thumbnail, not a single global that would serialize every
# thumbnail of every camera against each other: without it, two concurrent
# requests for THE SAME thumbnail (two tabs on the same page) share the
# same temporary file clip.tmp.jpg, and one can rename it to the final
# destination before the other has finished using it
# (FileNotFoundError, observed in practice).
_VERROUS_MINIATURE: dict = {}
_VERROU_CREATION_MINIATURE = threading.Lock()


def _verrou_miniature(cle: str) -> threading.Lock:
    with _VERROU_CREATION_MINIATURE:
        return _VERROUS_MINIATURE.setdefault(cle, threading.Lock())


# The Sync Module handles only one command at a time and rejects the next
# with "System is busy". No point asking it: measured, its published state
# is identical at rest and during a live view (busy stays empty, status stays
# "online"), and asking costs thirteen seconds — more expensive than trying
# the command. So we track occupancy locally, since we are the ones using
# it: this token is taken by a live view and by a refresh alike, which
# keeps them from stepping on each other.
#
# It obviously does not cover another client, e.g. the mobile app: there
# only the rejection informs us, hence the retry with growing backoff in
# blink2video.py.
MODULE_SLOT = threading.Semaphore(1)
# Diagnostic only: says who holds MODULE_SLOT and since when, so a 409 can
# distinguish "busy, will free soon" from "busy for 20 minutes, smells like
# a leak" without restarting the server to find out.
MODULE_SLOT_INFO: dict = {}
# /api/attente-module (below): how long a client may wait for confirmation
# that MODULE_SLOT is free before being told it is taking longer than
# expected. A bit beyond the internal ceiling of _stop_stream
# (asyncio.wait_for(feed, timeout=20)): no reason to cut off before the
# server-side cleanup itself gives up.
ATTENTE_MODULE_MAX_SECONDS = 25

# hub_lock("direct") (send_live_mse/send_offer_webrtc, below) used to fail
# instantly here (implicit wait=0) if it hit the background download
# (USB/cloud clips) already talking to the Sync Module — even though the
# Module often only holds it for a few seconds. An instant failure then
# cost a full retry cycle on the page (failure, 3s wait, entire WebRTC
# negotiation redone), rather than a simple wait here on the same request —
# exactly the case runtime.verrou() documents for its wait parameter
# (observed in practice, 2026-09-04: camera switches felt "unstable").
ATTENTE_HUB_MAX_SECONDS = 20

# The confirmed photo may still take a little time to appear at Blink.
# MODULE_SLOT stays held during this wait, shared with live views and
# downloads: do not extend polling beyond this ceiling.
ATTENTE_MEDIA_SNAPSHOT_SECONDS = 18
INTERVALLE_MEDIA_SNAPSHOT_SECONDS = 2

# Toggle driven by the page's record button (JS, /api/direct-enregistrement
# below): only one live view active at a time (MODULE_SLOT), so a single
# global flag is enough, read by send_live_mse and blink_webrtc._PisteH264
# to know whether to write to disk now. Cleared at the start of each new
# live view (send_live_mse, send_offer_webrtc): opening a live view must
# never silently inherit the state left by the previous one.
ENREGISTREMENT_DIRECT_ACTIF = threading.Event()

# The session exists from HTTP admission, before wake-up and pc creation.
# The random id comes from the tab and changes on every attempt.
# The lock protects transitions between the HTTP threads and BLINK.loop.
DIRECT_WEBRTC_SESSION: dict = {}
DIRECT_WEBRTC_SESSION_LOCK = threading.Lock()
# Same role for MSE, simpler: send_live_mse() runs synchronous code in its
# own HTTP thread, not on an asyncio loop — no "loop"/"interrompre" to keep,
# just the Event that its send loop checks itself each turn (see
# _demander_arret_direct). Without that, a browser closed during a camera
# silence was only detected on the next write — never if the camera stayed
# mute, so the module could stay busy until LIVE_MAX_SECONDS (300 s, real bug).
DIRECT_MSE_SESSION: dict = {}
DIRECT_ARRETS_RECENTS: dict = {}
DIRECT_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{16,64}\Z")
DIRECT_ARRET_TTL_SECONDS = 180
BLINK_OPEN_MAX_SECONDS = 45
WEBRTC_START_MAX_SECONDS = (
    BLINK_OPEN_MAX_SECONDS + blink_webrtc.PREMIERE_IMAGE_MAX_SECONDS
    + blink_webrtc.NEGOCIATION_MAX_SECONDS + 5
)


def _purger_arrets_direct() -> None:
    """Called under DIRECT_WEBRTC_SESSION_LOCK; bounded memory."""
    maintenant = time.monotonic()
    for identifiant, expiration in list(DIRECT_ARRETS_RECENTS.items()):
        if expiration <= maintenant:
            DIRECT_ARRETS_RECENTS.pop(identifiant, None)
    while len(DIRECT_ARRETS_RECENTS) > 256:
        DIRECT_ARRETS_RECENTS.pop(next(iter(DIRECT_ARRETS_RECENTS)))


def _demander_arret_direct(session_id: str) -> bool:
    """Cancel the designated WebRTC or MSE attempt, without waiting for BLINK.lock.

    A WebRTC cancellation can arrive before its offer on another HTTP
    connection; keeping it briefly (DIRECT_ARRETS_RECENTS) prevents that
    late offer from waking the camera. MSE has no such window: its GET
    request is the attempt itself, so setting its Event is enough — the
    send loop (send_live_mse) checks it each turn, with no "loop"/
    "interrompre" to wake since it does not run on asyncio."""
    if not DIRECT_SESSION_ID.fullmatch(session_id):
        return False
    with DIRECT_WEBRTC_SESSION_LOCK:
        DIRECT_ARRETS_RECENTS[session_id] = time.monotonic() + DIRECT_ARRET_TTL_SECONDS
        _purger_arrets_direct()
        webrtc_holder = DIRECT_WEBRTC_SESSION.get("session")
        if webrtc_holder is not None and webrtc_holder["session_id"] != session_id:
            webrtc_holder = None
        mse_holder = DIRECT_MSE_SESSION.get("session")
        if mse_holder is not None and mse_holder["session_id"] != session_id:
            mse_holder = None
        if webrtc_holder is None and mse_holder is None:
            return False
        deja_demande = webrtc_holder is not None and webrtc_holder["arret"].is_set()
        if webrtc_holder is not None:
            webrtc_holder["arret"].set()
        if mse_holder is not None:
            mse_holder["arret"].set()
        loop = webrtc_holder.get("loop") if webrtc_holder is not None else None
    if loop is not None and not deja_demande:
        loop.call_soon_threadsafe(webrtc_holder["interrompre"])
    return True

# Web page setting (webrtc by default) since 2026-09-03 — replaced the
# experimental BLINK_DIRECT_WEBRTC environment variable once WebRTC was
# validated in real use (BACKLOG.md). Read once at startup, like all other
# settings (port, timezone...): a change already restarts the server
# (/api/reglages on the JS side). Falls back to MSE if aiortc is not
# installed, even when "webrtc" is chosen.
WEBRTC_ACTIF = (runtime.lire_reglages()["live_protocol"] == "webrtc") and blink_webrtc.DISPONIBLE


def _slot_pris(quoi: str, name: str = "") -> None:
    MODULE_SLOT_INFO.clear()
    MODULE_SLOT_INFO.update({"quoi": quoi, "camera": name, "depuis": time.monotonic()})


def _slot_rendu() -> None:
    MODULE_SLOT_INFO.clear()


def _slot_occupe_message() -> str:
    quoi = MODULE_SLOT_INFO.get("quoi")
    if not quoi:
        return "The module is already busy (live view or refresh)."
    depuis = time.monotonic() - MODULE_SLOT_INFO.get("depuis", time.monotonic())
    camera = MODULE_SLOT_INFO.get("camera") or ""
    cible = f" ({camera})" if camera else ""
    return (f"The module is already busy: {quoi}{cible}, for "
            f"{depuis:.0f}s.")
# Ceiling for a live-view session. Blink itself closes it after about six
# minutes of video: its relay cuts the connection, measured 2026-09-28 on a
# Mini and an Outdoor (356 to 375 s of stream, 6:01 per ffmpeg), without
# ever needing "Continuer". Seven minutes lets it run to the end, and remains
# a safety net if that relay never closed. Beyond that, the page's
# "Continuous live" checkbox opens a new session.
LIVE_MAX_SECONDS = 420
# Time allowed for the first frame. A battery camera must wake up, so we are
# patient; beyond that we consider it will not respond.
LIVE_FIRST_FRAME_SECONDS = 40
# Ceiling for each wait in the MSE send loop, so an explicit stop requested
# via DIRECT_MSE_SESSION (see _demander_arret_direct) is rechecked at this
# cadence rather than only once LIVE_MAX_SECONDS has elapsed — without
# changing behavior of an active stream (blocks arrive well before this
# second anyway).
LIVE_MSE_ARRET_POLL_SECONDS = 1.0

# No thumbnail is re-requested on its own: it is fetched once, then kept
# until Actualiser is clicked. An image that changes by itself under your
# eyes, without being asked for, is not a service rendered.


# md.safe_name directly: safe_file() was a third copy of the same cleanup,
# derived from the other two (code review of 0eab463, bug #5).
safe_file = md.safe_name

# Where to save a copy of a live view (WebRTC or MSE) while it plays, next to
# Blink_Clips and related folders (runtime.DOSSIERS_SORTIES).
DOSSIER_DIRECT = runtime.dossier_sorties() / "Blink_Direct"

# On-demand photos (button, or external webhook — GitHub issue #9), distinct
# from detection clips: a camera disarmed during the day generates none, but
# can still respond to an explicit snapshot.
DOSSIER_SNAPSHOTS = runtime.dossier_sorties() / "Blink_Snapshots"
WEBHOOK_SNAPSHOT_ROUTE = "/webhook/snapshot"
# Camera and arming state for scripts (issue #40), same secret as the photo:
# only one to remember, decision by Nico and Markus on 2026-09-30.
WEBHOOK_STATUS_ROUTE = "/webhook/status"
WEBHOOK_ARM_ROUTE = "/webhook/arm"
# A POST on /webhook/arm has no useful body (everything goes through the URL):
# we still read it so as not to desynchronize a persistent connection, up to
# this size; beyond that we close the connection.
_CORPS_WEBHOOK_MAX = 65536


def _booleen_webhook(valeur):
    """true/false, 1/0, on/off, yes/no, oui/non (case-insensitive), None otherwise."""
    v = (valeur or "").strip().casefold()
    if v in ("true", "1", "on", "yes", "oui"):
        return True
    if v in ("false", "0", "off", "no", "non"):
        return False
    return None


def _camera_pour_webhook(camera: dict, systeme) -> dict:
    """What a script sees of a camera: the fields Blink provides, as-is, and
    null when a camera does not report them (not all models have battery,
    voltage, or wifi). No serial number."""
    return {
        "name": camera.get("name"),
        "system": systeme,
        "armed": camera.get("armed"),
        "online": not camera.get("offline"),
        "status": camera.get("status") or None,
        "battery": camera.get("battery"),
        "battery_signal": camera.get("battery_signal"),
        "voltage": camera.get("voltage"),
        "temperature_c": camera.get("temperature"),
        "wifi": camera.get("wifi"),
        "firmware": camera.get("firmware"),
        "model": camera.get("model"),
        "age_seconds": camera.get("age_seconds"),
    }


def etat_webhook(etat: dict, camera: str = ""):
    """System and camera state for /webhook/status, from the same
    system_state() as the page. With `camera`, only that one; None if the
    name matches no camera."""
    systemes = []
    trouvee = False
    for systeme in etat.get("systems") or []:
        cameras = [
            _camera_pour_webhook(c, systeme.get("name"))
            for c in systeme.get("cameras") or []
            if not camera or str(c.get("name") or "").strip() == camera
        ]
        if camera and not cameras:
            continue
        trouvee = trouvee or bool(cameras)
        systemes.append({
            "name": systeme.get("name"),
            "armed": systeme.get("armed"),
            "module": systeme.get("module"),
            "firmware": systeme.get("module_firmware"),
            "cameras": cameras,
        })
    if camera and not trouvee:
        return None
    return {"systems": systemes}


def _chemin_enregistrement_direct(name: str) -> Path:
    """Path of the file where to save the live view of a camera in progress.

    Same convention as target_path() (blink_models.py) for detection clips:
    camera/month as folders, then
    timestamp_camera_id_fingerprint.mp4 — requested explicitly so a live
    recording is not visually distinct from a detection clip in the tree
    (2026-09-04). target_path() itself is not reused: it is built around a
    clip.id from the Blink API, which a live view does not have.
    `identifiant` stands in for it, without claiming to come from the API:
    seconds since the UTC epoch, an integer of a length close to a real Blink
    id rather than a timestamp that would duplicate the first name segment.
    `suffixe` reuses target_path()'s mechanism exactly (JSON fingerprint,
    SHA256, 12 characters): a camera can only have one live view open at a
    time anyway (MODULE_SLOT), so collision is not the motivation here —
    only visual consistency with clips."""
    maintenant = dt.datetime.now(dt.timezone.utc)
    horodatage = maintenant.strftime("%Y-%m-%d_%H-%M-%SZ")
    camera = safe_file(name)
    identifiant = str(int(maintenant.timestamp()))
    empreinte = json.dumps(
        [camera, horodatage, identifiant], ensure_ascii=False, separators=(",", ":")
    )
    suffixe = hashlib.sha256(empreinte.encode("utf-8")).hexdigest()[:12]
    nom_fichier = f"{horodatage}_{camera}_{identifiant}_{suffixe}.mp4"
    return DOSSIER_DIRECT / camera / maintenant.strftime("%Y-%m") / nom_fichier


def read_entries(paths: dict) -> dict:
    return md.read_registry(paths["input"] / md.DOWNLOAD_STATE)


def nombre_clips_connus(entries: dict) -> int:
    """Registry clips counted as collect()'s total_known["clip"]:
    the page compares the two to announce new clips (audit of
    26/09/2026, B03). Also counting an unreadable entry that the gallery
    ignores would announce a "new" clip on every poll and restart
    automatic refresh forever."""
    total = 0
    for entry in entries.values():
        try:
            entry["path"]
            md.parse_created_at(entry["created_at"])
        except (KeyError, TypeError, ValueError):
            continue
        total += 1
    return total


ETIQUETTES_SOURCE = {"usb": "local", "cloud": "cloud"}

# Only one reassembly at a time: two simultaneous assemblies of the same
# day would write the same file.
REASSEMBLAGE = threading.Lock()

# Excluding a clip only touches the registry. Give it its own lock rather
# than the refresh one: otherwise a click during a download waits for that
# download to finish, and the button appears stuck for a full minute.
REGISTRE = threading.Lock()


def provenances(entrees: dict) -> dict:
    """Where each camera's clips are stored, per camera.

    Observed rather than deduced: it is the provenance of clips actually
    retrieved, not a reading of account settings. One camera covered by a
    "cloud" subscription, another "USB", and the absence of both also shows,
    which explains a camera that produces nothing. Entries earlier than this
    distinction necessarily come from the key, the only source then."""
    vues = {}
    for entree in entrees.values():
        camera = str(entree.get("camera") or "").strip()
        if camera:
            vues.setdefault(camera, set()).add(str(entree.get("source") or "usb"))
    return {camera: " + ".join(ETIQUETTES_SOURCE.get(s, s) for s in sorted(sources))
            for camera, sources in vues.items()}


def suppression_auto_choices(entrees: dict) -> list:
    """Configurable cameras, indexed by their persisted identity not their name.

    Internal use only (name -> key(s) resolution for the
    /api/suppression-auto route): the page never shows these keys, only the
    names of current cameras (watch.py state)."""
    choices = {}
    for entry in entrees.values():
        if not isinstance(entry, dict) or not entry.get("camera"):
            continue
        key = blink_registre.camera_setting_key_from_entry(entry)
        choices.setdefault(key, {
            "key": key,
            "name": str(entry.get("camera") or "camera").strip() or "camera",
        })
    result = list(choices.values())
    return sorted(result, key=lambda item: (item["name"].casefold(), item["key"]))


def suppression_auto_keys() -> set:
    """Ignore old ambiguous names until their explicit migration."""
    return {
        value for value in runtime.lire_suppression_auto()
        if value.startswith("camera-v2-")
    }


# (registry file key, identities) from the last read: see
# known_identities(). A tuple replaced as a whole, never mutated, is enough
# between server threads.
_IDENTITES_CONNUES: tuple = (None, frozenset())


def known_identities(paths: dict) -> frozenset:
    """Clip paths the registry recognizes, without measuring anything.

    Separated from collect() on purpose: validating a media request goes
    through here and must stay instant, while the full inventory may need
    to run ffmpeg.

    Cached as long as the registry file does not change: every thumbnail
    and every video player's Range request goes through here, and rereading
    the full JSON each time cost ~27 ms per request for 10,000 clips
    (5.4 MB, measured on a fast machine; much more under Windows 7),
    i.e. several seconds of CPU for a single thumbnail page (audit of
    2026-09-24). The registry is never rewritten in place (atomic
    replacement): inode, size or date therefore change on every write."""
    global _IDENTITES_CONNUES
    registre = paths["input"] / md.DOWNLOAD_STATE
    try:
        etat = registre.stat()
        cle = (os.fspath(registre), etat.st_mtime_ns, etat.st_size, etat.st_ino)
    except OSError:
        cle = None
    memorise = _IDENTITES_CONNUES
    if cle is not None and memorise[0] == cle:
        return memorise[1]
    identites = frozenset(
        entry["path"]
        for entry in read_entries(paths).values()
        if isinstance(entry, dict) and entry.get("path")
    )
    if cle is not None:
        _IDENTITES_CONNUES = (cle, identites)
    return identites


# Measured duration of excluded clips, which no longer have an entry in the
# normalized registry (see collect): without this cache, ffmpeg -i was
# relaunched for each of them on every page open.
EXCLUDED_DURATIONS = "excluded_durations.json"
# Same idea as EXCLUDED_DURATIONS, for assembled videos (collect_videos):
# without it, every server restart relaunched ffmpeg -i for ALL existing
# daily/weekly/monthly videos on the first open of the Clips tab — the
# in-memory probe_duration cache (_DURATIONS below) does not survive a
# restart (general audit requested by the user, 2026-09-03).
ASSEMBLED_DURATIONS = "assembled_durations.json"
# Exclude/restore a detection clip writes in the download registry
# (blink_registre.py): a live recording never has an entry there, so
# "exclude" cannot rely on the same mechanism. Separate file, as simple as
# a set of identities — no per-clip status to merge with anything else,
# unlike the registry.
DIRECT_EXCLUSION = "direct_exclusion.json"


def _identite_selection_valide(identity: str) -> bool:
    # fullmatch notably forbids a trailing newline after the extension.
    # Space/dot suffixes are normalized by Windows: rejecting them also
    # blocks ".." variants after native normalization.
    return bool(IDENTITY.fullmatch(identity)) and all(
        morceau not in (".", "..") and morceau.rstrip(" .") == morceau
        for morceau in identity.split("/"))


def _chemin_direct_confine(racine: Path, identity: str) -> Path:
    if not _identite_selection_valide(identity):
        raise ValueError("Invalid video identity")
    try:
        racine = racine.resolve()
        chemin = (racine / identity).resolve()
    except (OSError, RuntimeError) as erreur:
        raise ValueError("Video path inaccessible") from erreur
    if not runtime.est_relatif_a(chemin, racine):
        raise ValueError("Video outside the live-recording folder")
    return chemin


# Serializes read-modify-write of DIRECT_EXCLUSION across server threads:
# see Handler._appliquer_selection_directs().
_VERROU_EXCLUSION_DIRECTE = threading.Lock()


def _lire_exclusion_directe(paths: dict) -> set:
    # {"excluded": [...]}, not a bare list: md.load_json/save_json (already
    # used for ASSEMBLED_DURATIONS...) require a JSON object — a bare list
    # writes without complaint but rereads silently as empty
    # (isinstance(..., dict) rejects the list, load_json then returns its
    # default), observed when verifying this mechanism (2026-09-04).
    valeurs = md.load_json(paths["thumbs"] / DIRECT_EXCLUSION, {}).get("excluded")
    return {str(v) for v in valeurs} if isinstance(valeurs, list) else set()


def _ecrire_exclusion_directe(paths: dict, identites: set) -> None:
    md.save_json(paths["thumbs"] / DIRECT_EXCLUSION, {"excluded": sorted(identites)})


# Default window for /api/clips: beyond it, the thumbnail count only grows
# day by day without ever being consulted. Full history remains available
# on explicit request (?all=1 parameter), never lost.
DEFAULT_WINDOW_DAYS = 30

# Range-filter presets (quick range button on the clips page), in hours
# rather than days: "today" means the last 24 rolling hours, not since
# midnight, so it stays useful at any hour. "month" reuses
# DEFAULT_WINDOW_DAYS: same button as the default window, not a second
# value to keep in sync. "2months" follows Blink cloud storage retention
# in the United States, beyond which clips are deleted there (reported on
# Reddit, 2026-08-27).
RANGE_PRESETS_HOURS = {
    "today": 24,
    "week": 7 * 24,
    "month": DEFAULT_WINDOW_DAYS * 24,
    "2months": 60 * 24,
}

# The API only returns Amazon internal code names, never a commercial
# reference. Only two are established: "owl", documented as the Blink Mini
# in blinkpy's code, and "catalina", identified by the hardware owner as a
# Blink Outdoor. Any other code name stays displayed as-is, announced as
# internal, rather than guessed at.
CAMERA_MODELS = {"owl": "Blink Mini", "catalina": "Blink Outdoor"}
# The module carries its generation in its type: sm2 = Sync Module 2.
MODULE_MODELS = {"sm": "Sync Module", "sm2": "Sync Module 2"}


@dataclass(frozen=True)
class _SelectionVideos:
    """Local identities requested for each of the three actions."""

    exclure: list[str]
    inclure: list[str]
    supprimer: list[str]


@dataclass(frozen=True)
class _CibleSuppressionBlink:
    """Links a local file to its registry entry and remote ID."""

    identite: str
    entree: dict
    id_distant: str


def _lire_selection_videos(payload: dict) -> _SelectionVideos:
    # Keep absent or empty fields accepted by the historical API.
    listes = [payload.get(champ) or [] for champ in ("exclure", "inclure", "supprimer")]
    for identites in listes:
        if not isinstance(identites, list):
            raise ValueError("Invalid video selection.")
        for identite in identites:
            if not isinstance(identite, str) or not _identite_selection_valide(identite):
                raise ValueError("Invalid video selection.")
    return _SelectionVideos(*listes)


def _separer_clips_et_directs(selection: _SelectionVideos, racine_direct: Path | None
                            ) -> tuple[_SelectionVideos, _SelectionVideos, dict]:
    """Identify live-recording files before any registry read or mutation."""
    chemins_directs = {}
    if racine_direct:
        for identite in selection.exclure + selection.inclure + selection.supprimer:
            chemin = _chemin_direct_confine(racine_direct, identite)
            if chemin.is_file():
                chemins_directs[identite] = chemin

    clips = _SelectionVideos([], [], [])
    directs = _SelectionVideos([], [], [])
    for demandes, liste_clips, liste_directs in (
            (selection.exclure, clips.exclure, directs.exclure),
            (selection.inclure, clips.inclure, directs.inclure),
            (selection.supprimer, clips.supprimer, directs.supprimer)):
        for identite in demandes:
            if identite in chemins_directs:
                liste_directs.append(identite)
            else:
                liste_clips.append(identite)
    return clips, directs, chemins_directs


def _trouver_entree_selection(entrees: dict, identite: str) -> dict | None:
    # On a path duplicate, keep the first registry entry.
    return next((entree for entree in entrees.values()
                 if isinstance(entree, dict) and entree.get("path") == identite), None)


def _jours_selection_a_reconstruire(identites: list[str], entrees: dict,
                                  reglages: dict) -> set[tuple[str, str]]:
    """One (camera identity, local day) pair per affected day."""
    jours = set()
    cles_collision = md._cles_camera_par_collision(entrees)
    fuseau_fusion = ZoneInfo(reglages["timezone"])
    for identite in identites:
        entree = _trouver_entree_selection(entrees, identite)
        camera = str((entree or {}).get("camera") or "").strip()
        try:
            jour = md.parse_created_at(
                str((entree or {}).get("created_at"))
            ).astimezone(fuseau_fusion).date().isoformat()
        except (TypeError, ValueError):
            jour = None
        if camera and jour:
            jours.add((md._camera_key(cles_collision, entree, camera), jour))
    return jours


def _preparer_suppressions_blink(identites: list[str], entrees: dict,
                               resultats: dict) -> list[_CibleSuppressionBlink]:
    """Resolve known targets and note refusals without contacting Blink."""
    cibles = []
    for identite in identites:
        entree = _trouver_entree_selection(entrees, identite)
        if entree is None:
            resultats[identite] = "inconnu"
            continue
        # blink_models.target_path convention; remote_id serves older clips.
        correspondance = re.search(r"_(\d+)_[0-9a-f]{12}\.mp4$", identite)
        id_distant = correspondance.group(1) if correspondance else str(
            entree.get("remote_id") or "")
        if not id_distant:
            resultats[identite] = "identifiant_introuvable"
            continue
        cibles.append(_CibleSuppressionBlink(identite, entree, id_distant))
    return cibles


def camera_key(sync, name: str, camera) -> str:
    """Opaque stable identifier of a camera for the web interface."""
    attributes = getattr(camera, "attributes", None) or {}
    network_id = (
        getattr(camera, "network_id", None)
        or getattr(sync, "network_id", None)
        or ""
    )
    device_id = ""
    for value in (
        getattr(camera, "device_id", None),
        getattr(camera, "camera_id", None),
        attributes.get("device_id"),
        attributes.get("camera_id"),
        attributes.get("id"),
    ):
        if value not in (None, ""):
            device_id = str(value)
            break
    material = (
        ["device", str(network_id), device_id]
        if device_id
        else [
            "legacy", str(network_id), str(getattr(sync, "sync_id", "")),
            str(name).strip().casefold(),
        ]
    )
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"camera-{digest[:24]}"


def system_key(name: str, sync) -> str:
    material = [
        str(getattr(sync, "network_id", "")),
        str(getattr(sync, "sync_id", "")),
        str(name).strip().casefold(),
    ]
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"system-{digest[:24]}"


def model_name(kind: str | None) -> str | None:
    """Camera model name for the technical line of its card: the product
    name when known, otherwise the code Blink gives the type ("hawk",
    "sedona", "lotus"), as-is. Never a sentence: this text goes to a page
    shown in French or English, and "type Blink 'hawk'" stayed French
    under an English page (issue #37)."""
    if not kind:
        return None
    return CAMERA_MODELS.get(kind) or kind


def horodatage_releve(moment: dt.datetime, fuseau, maintenant: dt.datetime | None = None):
    """(ISO date to the minute, server time; is it today?) of a camera
    reading. No word or date format from a language: the page composes
    French "releve a 14:30" or English "measured on 09/28 at 14:30"
    (issue #37: "28/09 a 14:30" went out as-is under an English page)."""
    local = moment.astimezone(fuseau)
    maintenant = dt.datetime.now(fuseau) if maintenant is None else maintenant.astimezone(fuseau)
    return local.isoformat(timespec="minutes"), maintenant.date() == local.date()


def load_excluded_durations(paths: dict) -> dict:
    cache = md.load_json(paths["thumbs"] / EXCLUDED_DURATIONS, {})
    return cache if isinstance(cache, dict) else {}


def load_assembled_durations(paths: dict) -> dict:
    cache = md.load_json(paths["thumbs"] / ASSEMBLED_DURATIONS, {})
    return cache if isinstance(cache, dict) else {}


def probe_duration_cached(ffmpeg: str, source: Path, identity: str, cache: dict) -> float:
    """Duration of an excluded clip, measured once then cached.

    The key retains size and modification date of the source file: a
    replaced clip (repair of a missing media, for example) is thus
    re-measured rather than keeping a stale duration."""
    stat = source.stat()
    empreinte = [stat.st_size, stat.st_mtime]
    entree = cache.get(identity)
    if entree and entree.get("empreinte") == empreinte:
        return entree["duration"]
    duration = probe_duration(ffmpeg, source)
    cache[identity] = {"duration": duration, "empreinte": empreinte}
    return duration


def collect(paths: dict, timezone: ZoneInfo, ffmpeg: str = "",
            depuis: "dt.datetime | None" = None,
            jusqua: "dt.datetime | None" = None) -> dict:
    """Inventory clips known to the download registry.

    Unlike merge_daily.load_groups, excluded clips are kept: that is
    precisely what we want to be able to review and restore.

    `depuis`/`jusqua` bound the returned window, each optional: the stock
    grows every day, and without a lower bound the page would end up
    transmitting and drawing a thumbnail count unrelated to what is
    actually being watched. Full history remains explicitly accessible
    (see DEFAULT_WINDOW_DAYS). Both are full timestamps, not bare dates:
    the page's custom range filter targets a period to the hour, to find
    a specific incident without scrolling a whole always-armed camera's
    history (reported on Reddit, 2026-08-27)."""
    entries = read_entries(paths)
    # Durations already measured by merge_daily are reused as-is;
    # only those of excluded clips, whose entry was swept from the
    # normalized registry, still need measuring, via their own cache (see
    # load_excluded_durations) so ffmpeg is not relaunched on every request.
    probed = md.load_json(paths["normalized"] / md.NORMALIZED_STATE, {}).get("clips")
    probed = probed if isinstance(probed, dict) else {}
    duration_cache = None

    clips = []
    # Counted by kind: the filter summary ("X/Y clips") must stay specific
    # to the displayed view (Detection Clip or Live Recording), not mix
    # both since they share this list (2026-09-04).
    total_clips = 0
    total_direct = 0
    for entry in entries.values():
        try:
            identity = entry["path"]
            created = md.parse_created_at(entry["created_at"])
            camera = str(entry.get("camera") or "camera").strip() or "camera"
        except (KeyError, TypeError, ValueError):
            continue

        local = created.astimezone(timezone)
        total_clips += 1
        if depuis is not None and local < depuis:
            continue
        if jusqua is not None and local > jusqua:
            continue
        excluded = bool(entry.get("excluded"))
        # Prefer the normalized version: it carries the burned-in timestamp,
        # so it is the one that really helps judging.
        source = None
        for kind in ("normalized", "input", "excluded"):
            if (paths[kind] / identity).is_file():
                source = kind
                break

        probe = (probed.get(identity) or {}).get("probe") or {}
        seconds = probe.get("duration")
        # An excluded clip no longer has an entry in the normalized registry:
        # measure it once from Blink_Excluded, then remember.
        if seconds is None and source and ffmpeg:
            if duration_cache is None:
                duration_cache = load_excluded_durations(paths)
            seconds = probe_duration_cached(
                ffmpeg, paths[source] / identity, identity, duration_cache)

        clips.append({
            "identity": identity,
            "kind": "clip",
            "camera": camera,
            "cameraKey": blink_registre.camera_setting_key_from_entry(entry),
            "day": local.date().isoformat(),
            "time": local.strftime("%H:%M:%S"),
            "excluded": excluded,
            "source": source,
            "origine": ETIQUETTES_SOURCE.get(str(entry.get("source") or "usb"),
                                             str(entry.get("source"))),
            "sourceDeleted": bool(entry.get("source_deleted")),
            "duration": float(seconds or 0.0),
        })

    if duration_cache is not None:
        md.save_json(paths["thumbs"] / EXCLUDED_DURATIONS, duration_cache)

    # Live recordings (Record button during Live, Blink_Direct): same
    # presentation as detection clips, requested explicitly (2026-09-04)
    # rather than a separate view with its own rules — merged into the same
    # list, "kind" is the only field that truly distinguishes them
    # (day/time/duration/exclude/delete then work identically). "clip"/
    # "direct" instead of source/origine (tied to a clip's cloud/USB
    # provenance, meaningless for a live recording) — see
    # _chemin_enregistrement_direct for the naming convention from which
    # "day"/"time" are extracted here.
    exclusion_directe = _lire_exclusion_directe(paths)
    duration_cache_direct = None
    racine_direct = paths.get("direct")
    if racine_direct is not None and racine_direct.is_dir():
        for camera_dir in sorted(
            p for p in racine_direct.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        ):
            for video in sorted(camera_dir.rglob("*.mp4"), reverse=True):
                if not md.valid_mp4(video):
                    continue
                identity = f"{camera_dir.name}/{video.relative_to(camera_dir).as_posix()}"
                try:
                    created = dt.datetime.strptime(
                        video.stem[:20], "%Y-%m-%d_%H-%M-%SZ"
                    ).replace(tzinfo=dt.timezone.utc)
                except ValueError:
                    created = dt.datetime.fromtimestamp(
                        video.stat().st_mtime, dt.timezone.utc
                    )
                local = created.astimezone(timezone)
                total_direct += 1
                if depuis is not None and local < depuis:
                    continue
                if jusqua is not None and local > jusqua:
                    continue
                if duration_cache_direct is None:
                    duration_cache_direct = load_assembled_durations(paths)
                clips.append({
                    "identity": identity,
                    "kind": "direct",
                    "camera": camera_dir.name,
                    "cameraKey": safe_file(camera_dir.name),
                    "day": local.date().isoformat(),
                    "time": local.strftime("%H:%M:%S"),
                    "excluded": identity in exclusion_directe,
                    "source": "direct",
                    "origine": "Direct",
                    "sourceDeleted": False,
                    "duration": probe_duration_cached(
                        ffmpeg, video, f"direct/{identity}", duration_cache_direct
                    ),
                })
    if duration_cache_direct is not None:
        md.save_json(paths["thumbs"] / ASSEMBLED_DURATIONS, duration_cache_direct)

    # Newest to oldest: that is what you came to watch. One global sort is
    # enough; sorting by camera afterward would put an older camera ahead of
    # recent clips from another camera.
    clips.sort(key=lambda clip: (clip["day"], clip["time"]), reverse=True)
    return {
        "clips": clips,
        "cameras": sorted({clip["camera"] for clip in clips}),
        "passages": runtime.passages(),
        "sources": provenances(entries),
        # Lets the page say "X clips of Y known" and explicitly offer to load
        # the rest: which exact range is active (preset or custom) is already
        # known on the page side — it alone asked for it, no need to restate.
        "filtered": depuis is not None or jusqua is not None,
        # By kind (clip/direct), not a single total: otherwise the Detection
        # Clip summary would also count live recordings, and vice versa,
        # since they share "clips" (2026-09-04).
        "total_known": {"clip": total_clips, "direct": total_direct},
        # The gallery does not offer the Delete checkbox for a camera already
        # on automatic deletion (GitHub issue #1): redundant, the clip will
        # be removed from its source on the next successful download anyway.
        "suppressionAuto": sorted(suppression_auto_keys()),
    }


def collect_videos(paths: dict, ffmpeg: str) -> dict:
    """Inventory assembled videos by period (daily/weekly/monthly).

    Live recordings (Blink_Direct) are no longer part of this since they
    share the detection-clip presentation (2026-09-04): see collect(),
    which now reads them — same recursive walk root/camera/**/*.mp4, but
    merged into "clips" rather than exposed separately here.

    We read the disk rather than a registry: these files are the visible
    product of the pipeline (or the record button), and listing them as
    they exist avoids showing an entry for a video deleted by hand.

    Duration cached on disk (probe_duration_cached, same mechanism as for
    excluded clips) rather than the in-memory-only probe_duration used
    until then: without that, a restart relaunched ffmpeg -i for every
    existing video on the first tab open (general audit requested by the
    user, 2026-09-03)."""
    result = {}
    duration_cache = None
    for kind in ("daily", "weekly", "monthly"):
        items = []
        root = paths[kind]
        if root.is_dir():
            for camera_dir in sorted(
                p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
            ):
                # rglob, not glob: daily/weekly/monthly place their files flat under
                # camera_dir, but live also nests by month
                # (camera_dir/YYYY-MM/*.mp4, same convention as target_path()
                # for detection clips — see _chemin_enregistrement_direct).
                # One recursive walk covers both shapes without branching on
                # kind here.
                for video in sorted(camera_dir.rglob("*.mp4"), reverse=True):
                    if not md.valid_mp4(video):
                        continue
                    if duration_cache is None:
                        duration_cache = load_assembled_durations(paths)
                    relatif = video.relative_to(camera_dir).as_posix()
                    # kind in the identity: daily/weekly/monthly may each contain a
                    # file of the same name for the same camera, unrelated
                    # to each other.
                    identity = f"{kind}/{camera_dir.name}/{relatif}"
                    items.append({
                        "kind": kind,
                        "path": f"{camera_dir.name}/{relatif}",
                        "camera": camera_dir.name,
                        "label": video.stem.replace(f"_{camera_dir.name}", ""),
                        "bytes": video.stat().st_size,
                        "duration": probe_duration_cached(
                            ffmpeg, video, identity, duration_cache),
                    })
        result[kind] = items
    if duration_cache is not None:
        md.save_json(paths["thumbs"] / ASSEMBLED_DURATIONS, duration_cache)
    return result


_DURATIONS: dict = {}


def probe_facts(ffmpeg: str, path: Path) -> tuple:
    """Duration and resolution of a video, remembered as long as it does not change.

    Each measurement costs an ffmpeg launch; without this cache, opening the
    monthly tab would re-run analysis on every display."""
    key = (str(path), path.stat().st_mtime_ns)
    if key not in _DURATIONS:
        try:
            duration, width, height, _, _ = md.probe_clip_info(ffmpeg, path)
            _DURATIONS[key] = (duration, width, height)
        except RuntimeError:
            _DURATIONS[key] = (0.0, 0, 0)
    return _DURATIONS[key]


def probe_duration(ffmpeg: str, path: Path) -> float:
    return probe_facts(ffmpeg, path)[0]


class LecteurTube:
    """Read a blocking pipe continuously in a single thread, to give each
    read a real deadline without ever creating a second concurrent reader
    on the same pipe.

    `pipe.read(n)` is blocking and cannot be interrupted. Launching it in
    a thread on *every* call (as the old `read_with_deadline` did, reused
    in a loop by live view) leaves an orphan thread as soon as a call
    exceeds its deadline: `pipe.read()` keeps waiting in the background,
    and the next call starts a second one, concurrent with the first on
    the same pipe. The next arriving byte can then land in the orphan
    thread, invisible to the caller (real bug, see AUDIT-2026-08-13.md
    section 28.22 for `/live-mse`, and 28.26 for the main send loop of
    both live modes — a global deadline checked only *between* two reads
    bounds nothing if an individual read can itself block forever)."""

    def __init__(self, pipe, taille_bloc: int = 16384):
        self._file: queue.Queue = queue.Queue()
        self._fin = False

        def lire_en_continu():
            while True:
                morceau = pipe.read(taille_bloc)
                self._file.put(morceau)
                if not morceau:
                    return

        threading.Thread(target=lire_en_continu, daemon=True).start()

    def lire(self, delai: float):
        """One data block; ``b""`` if the pipe is truly at EOF;
        ``None`` if nothing arrived before `delai` (the stream is
        probably just slow — the thread itself keeps waiting, a later
        call may still receive something)."""
        if self._fin:
            return b""
        try:
            morceau = self._file.get(timeout=max(0.01, delai))
        except queue.Empty:
            return None
        if not morceau:
            self._fin = True
        return morceau


class DetecteurDebutFragment:
    """True if the received block starts exactly on an fMP4 fragment ("moof"),
    never in the middle of an already-started moof/mdat.

    Blocks that LecteurTube.lire() returns (16 KB) NEVER align on ffmpeg's
    moof+mdat fragment boundaries (verified empirically: a multi-KB mdat
    can be split mid-way by a block). send_live_mse() uses this to start
    a recording only on a block that opens a real fragment — hence a
    keyframe, guaranteed by -movflags frag_keyframe: starting on an
    arbitrary block could split an mdat in two, producing a fragment
    without its moof, unreadable on playback ("Invalid NAL unit size",
    real bug).

    Only looks at the very START of each block rather than locating a
    precise offset: the only consequence of a missed fragment (box header
    straddling two blocks, rare) is waiting for the next one, a few
    hundred ms at worst — incomparable to a corrupt file. Must be fed
    EVERY stream block, in order, including the init segment ("first" in
    send_live_mse: ftyp and moov are boxes like any other for this
    tracking, and it may already contain the first fragments), or its
    box-size tracking desynchronizes."""

    def __init__(self):
        self._restant_boite = 0
        self._entete = b""

    def bloc_ouvre_un_fragment(self, chunk: bytes) -> bool:
        sur_frontiere_avant = self._restant_boite == 0 and not self._entete
        resultat = (
            sur_frontiere_avant and len(chunk) >= 8 and chunk[4:8] == b"moof"
        )
        position, n = 0, len(chunk)
        while position < n:
            if self._restant_boite:
                pris = min(self._restant_boite, n - position)
                position += pris
                self._restant_boite -= pris
                continue
            manque = 8 - len(self._entete)
            pris = min(manque, n - position)
            self._entete += chunk[position:position + pris]
            position += pris
            if len(self._entete) < 8:
                break
            taille = int.from_bytes(self._entete[:4], "big")
            self._entete = b""
            if taille < 8:
                self._restant_boite = 0  # degenerate box: abandon tracking
                break
            self._restant_boite = taille - 8
        return resultat


_MOOV_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


def h264_mime_codec_from_moov(segment: bytes) -> str:
    """Look for avcC in an MP4 init segment (ftyp+moov) to get the exact MSE
    codec string (avc1.PPCCLL: profile, constraints, level). Guessing that
    string crashes addSourceBuffer as soon as the camera encodes in a
    different profile or level than assumed; reading it from the real
    stream is the only reliable way.

    Only follows the useful path: moov > trak > mdia > minf > stbl > stsd >
    avc1 > avcC. avcC already carries profile/constraints/level in clear
    (AVCDecoderConfigurationRecord); no need to dig down to the SPS it
    contains."""
    def walk(buf: bytes) -> bytes | None:
        i, n = 0, len(buf)
        while i + 8 <= n:
            size = int.from_bytes(buf[i:i + 4], "big")
            kind = buf[i + 4:i + 8]
            if size < 8 or i + size > n:
                break
            payload = buf[i + 8:i + size]
            if kind == b"avcC":
                return payload
            if kind in _MOOV_CONTAINERS:
                found = walk(payload)
            elif kind == b"stsd":
                found = walk(payload[8:])  # version+flags+entry_count
            elif kind == b"avc1":
                # SampleEntry (8) + fixed VisualSampleEntry fields (70) before
                # child boxes (avcC, ...).
                found = walk(payload[78:])
            else:
                found = None
            if found is not None:
                return found
            i += size
        return None

    avcc = walk(segment)
    if avcc is None or len(avcc) < 4:
        raise RuntimeError(
            "avcC not found in the MP4 initialization segment (unreadable profile).")
    profile_idc, constraint, level_idc = avcc[1], avcc[2], avcc[3]
    return f"avc1.{profile_idc:02x}{constraint:02x}{level_idc:02x}"


def read_mp4_init_segment(lecteur: LecteurTube, seconds: float) -> bytes:
    """Accumulate ffmpeg output until a complete avcC can be read, or give
    up after `seconds`. A single 4096-byte block (as for MJPEG) is not
    always enough: moov may spill past the first block.

    Takes an already-created `LecteurTube`, not a raw pipe: the same one
    must then serve the caller's main send loop. Creating a second on the
    same pipe would reintroduce the concurrent-reader bug that
    `LecteurTube` exists to prevent."""
    deadline = time.monotonic() + seconds
    buf = bytearray()
    while time.monotonic() < deadline and len(buf) < 65536:
        morceau = lecteur.lire(deadline - time.monotonic())
        if not morceau:
            break  # b"" (EOF) or None (global deadline exhausted): nothing more to try
        buf += morceau
        try:
            h264_mime_codec_from_moov(bytes(buf))
            break  # found: no need to read more before sending headers
        except RuntimeError:
            continue
    return bytes(buf)


# Last live-view failure, reread by the page: an <img> tag cannot read
# the body of an error response, it only sees a load failure.
LAST_LIVE_ERROR: dict = {}
LAST_LIVE_ERROR_LOCK = threading.Lock()


def _memoriser_erreur_direct(camera: str, message: str, status: int) -> None:
    """Publish in one shot the failure the browser will come reread."""
    with LAST_LIVE_ERROR_LOCK:
        LAST_LIVE_ERROR.clear()
        LAST_LIVE_ERROR.update({
            "camera": camera, "message": message, "status": status,
        })


def _effacer_erreur_direct() -> None:
    """A newly accepted attempt makes any prior failure obsolete."""
    with LAST_LIVE_ERROR_LOCK:
        LAST_LIVE_ERROR.clear()


def _derniere_erreur_direct() -> dict:
    """Consistent copy for the HTTP thread that serves /api/live-error."""
    with LAST_LIVE_ERROR_LOCK:
        return dict(LAST_LIVE_ERROR)


def _texte_stderr_ffmpeg(errors: list) -> str:
    """Normalize stderr, which subprocess delivers as bytes in this pipeline.

    _drainer_stderr feeds `errors` line by line (temporary diagnostic),
    no longer a single block as before: join everything, that covers both
    forms."""
    raw = b"".join(e for e in errors if isinstance(e, bytes))
    return raw.decode("utf-8", "replace").strip()[:300]


def _journal_direct(name: str, message: str) -> None:
    """Un journal indisponible ne doit jamais interrompre un direct.

    Commun a MSE et WebRTC (renomme le 2026-09-03 : le prefixe "direct-mse"
    laissait croire a une bascule vers MSE meme sur une ligne concernant
    WebRTC - source de confusion reelle, cf. BACKLOG.md). Le protocole en
    cause reste lisible dans le texte de chaque message ("webrtc"/"MSE"
    explicite), jamais dans ce prefixe generique. Ecrit aussi dans
    direct.log (runtime.ajouter_ligne) : le lancement normal tourne sous
    pythonw (autostart), donc sans console ou lire un print()."""
    horodatage = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    ligne = f"[direct] {horodatage} {name} : {message}"
    try:
        print(ligne, flush=True)
    except Exception:
        pass
    runtime.ajouter_ligne("direct.log", ligne)


async def _stop_stream(stream, feed) -> str:
    """Close sockets, wait for polling, and ensure its "done" fallback.

    init_livestream has already created a command, even if start fails or
    relay authentication fails before blinkpy's polling. Those cases must
    also send done. This coroutine is protected by the session cleanup
    task against cancellations from the HTTP requester.
    """
    erreurs = []
    try:
        stream.stop()
    except Exception as error:
        erreurs.append(f"stop : {type(error).__name__}: {error}")
    if feed is not None:
        try:
            # wait() must not stay blocked in the finally of a cancelled
            # task, unlike wait_for(feed, ...).
            terminees, _ = await asyncio.wait({feed}, timeout=20)
            if feed in terminees:
                await feed
                if not erreurs:
                    return "session released normally"
            else:
                feed.cancel()
                # Laisse au finally du polling une chance d'envoyer done.
                await asyncio.wait({feed}, timeout=5)
                if feed.done():
                    try:
                        feed.result()
                    except (Exception, asyncio.CancelledError):
                        pass
                else:
                    feed.add_done_callback(_observer_fin_flux)
        except asyncio.CancelledError:
            # An already-cancelled feed must not cancel the cleanup that
            # awaits it. Cancellation of the cleaner itself propagates.
            if not feed.cancelled():
                raise
            erreurs.append("stream cancelled before end confirmation")
        except Exception as error:
            erreurs.append(f"flux : {type(error).__name__}: {error}")

    try:
        from blinkpy import api

        await asyncio.wait_for(
            api.request_command_done(
                stream.camera.sync.blink, stream.camera.network_id, stream.command_id
            ), timeout=10,
        )
        return "session force-released" + (" | " + " ; ".join(erreurs) if erreurs else "")
    except Exception as error:
        return f"session may still be open: {type(error).__name__}"


def _observer_fin_flux(tache) -> None:
    """Collect exceptions from a close that finishes after its budget."""
    try:
        tache.result()
    except (Exception, asyncio.CancelledError):
        pass


def _erreur_boucle_asyncio(loop, contexte: dict) -> None:
    """Safety net for any uncaught exception in a BLINK.loop task/callback
    (RTCPeerConnection, connectionstatechange...).

    Without it, asyncio's default behavior (logging.error) goes to a stderr
    that does not exist under pythonw (normal launch, autostart): no trace
    anywhere. Lived in practice (2026-09-04): a stuck WebRTC session,
    MODULE_SLOT never released, without the slightest hint in direct.log
    or serve_erreurs.log (the latter only covers HTTP requests, not aiortc
    callbacks on this loop)."""
    exception = contexte.get("exception")
    detail = f"{type(exception).__name__}: {exception}" if exception else contexte.get(
        "message", "unknown error"
    )
    _journal_direct("asyncio", f"uncaught exception on BLINK.loop, {detail}")


# Wait for Blink confirmation after arming a camera (see
# _confirmer_armement_camera) ; BLINK.call() borne l'ensemble a 60 s.
DELAI_CONFIRMATION_ARMEMENT = 45


async def _confirmer_armement_camera(blink, camera, reponse, armed: bool) -> None:
    """Wait until Blink has applied a camera's arming, then retain it.

    blinkpy posts the command (camera.async_arm) and returns its response
    without following it, and does not update camera.motion_enabled, the
    attribute describe_camera() shows: since system_state() was lightened
    (2026-09-03, it only rereads the home screen and hub arming), that
    attribute kept its value from connect. Arming a camera from the page
    thus left its button red, while the hub's, reread each time, turned
    green (observed in practice 2026-10-05, camera "Salon"), and the status
    webhook announced stale arming.

    So we follow the command, like reveiller_camera(), and set the confirmed
    value. A refusal or missing confirmation becomes a readable error instead
    of a silent failure."""
    from blinkpy import api

    if not isinstance(reponse, dict) or not reponse.get("id"):
        raise RuntimeError("Blink refused the camera arming command.")
    commande = dict(reponse)
    commande.setdefault("network_id", camera.network_id)
    try:
        confirme = await asyncio.wait_for(
            api.wait_for_command(blink, commande), DELAI_CONFIRMATION_ARMEMENT)
    except asyncio.TimeoutError:
        confirme = False
    if not confirme:
        raise RuntimeError("Blink did not confirm the camera arming.")
    camera.motion_enabled = bool(armed)


class BlinkSession:
    """Shared Blink session, living on its own asyncio loop.

    Arming a camera or opening a live view requires an authenticated session.
    Reopening it on every click would cost two seconds and a useless round
    trip to Amazon: so we keep it open, and talk to it from the HTTP server
    threads via `call`."""

    def __init__(self):
        self.lock = threading.Lock()
        self.loop = None
        self.session = None
        self.blink = None

    def _ensure_loop(self):
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
            self.loop.set_exception_handler(_erreur_boucle_asyncio)
            threading.Thread(
                target=lambda: (asyncio.set_event_loop(self.loop),
                                self.loop.run_forever()),
                daemon=True,
            ).start()
        return self.loop

    def call(self, coroutine_factory, timeout: float = 60.0):
        """Run `coroutine_factory(blink)` on the loop, connecting if needed.
        Raises RuntimeError if no valid session exists."""
        echeance = time.monotonic() + timeout
        if not self.lock.acquire(timeout=max(0.0, timeout)):
            raise concurrent.futures.TimeoutError("The Blink session is busy.")
        try:
            loop = self._ensure_loop()
            finished = threading.Event()

            async def run():
                try:
                    if self.blink is None:
                        self.session = blink_auth.session_http()
                        self.blink = await blink_auth.connect_saved(self.session)
                    if self.blink is None:
                        raise RuntimeError(
                            "Blink session missing or expired. Reconnect "
                            "from the Refresh button."
                        )
                    return await coroutine_factory(self.blink)
                finally:
                    finished.set()

            future = asyncio.run_coroutine_threadsafe(run(), loop)
            try:
                return future.result(max(0.0, echeance - time.monotonic()))
            except concurrent.futures.TimeoutError:
                # result(timeout) ne borne que l'attente du thread appelant :
                # without cancellation, the coroutine continues on the Blink loop and
                # can arm a camera or delete a clip after the API has already
                # announced failure. Keeping it at least lets us stop anything
                # not yet sent to the remote service.
                future.cancel()
                try:
                    future.result(timeout=5)
                except (concurrent.futures.CancelledError,
                        concurrent.futures.TimeoutError):
                    pass
                # The concurrent Future becomes "cancelled" as soon as the
                # request is handed to the loop, before the coroutine's finally
                # has necessarily released its resources. A separate barrier
                # avoids releasing self.lock too early.
                finished.wait(timeout=5)
                raise
        finally:
            self.lock.release()

    def forget(self):
        """Oublie la session courante : la prochaine demande se reconnectera."""
        with self.lock:
            self.blink = None
            if self.session is not None and self.loop is not None:
                asyncio.run_coroutine_threadsafe(self.session.close(), self.loop)
            self.session = None

    def find_camera(self, blink, identity: str):
        by_key = []
        by_name = []
        for sync in blink.sync.values():
            for camera_name, camera in (getattr(sync, "cameras", None) or {}).items():
                if camera_key(sync, camera_name, camera) == identity:
                    by_key.append((sync, camera))
                if camera_name.strip() == identity.strip():
                    by_name.append((sync, camera))
        if len(by_key) == 1:
            return by_key[0]
        if len(by_key) > 1:
            raise RuntimeError("Duplicate camera identifier in the Blink account.")
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            raise RuntimeError(
                f"Ambiguous camera name: {identity}. Use its stable identifier."
            )
        raise RuntimeError(f"Unknown camera: {identity}")

    def find_system(self, blink, identity: str):
        by_name = []
        for sync_name, sync in blink.sync.items():
            if system_key(sync_name, sync) == identity:
                return sync
            if sync_name.strip() == identity.strip():
                by_name.append(sync)
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            raise RuntimeError(f"Ambiguous system name: {identity}")
        raise RuntimeError(f"Unknown system: {identity}")

    def find_sync_module(self, blink, entry: dict):
        """Resolve a clip's module from its persisted IDs.

        XR support builds the real Sync Module from the homescreen: it may
        therefore be absent from ``blink.sync``, which still contains, on some
        accounts, a Mini or Doorbell carrying the device ID. The camera name
        is kept only as fallback for old registry entries lacking identifiers.
        """
        network_id = str(entry.get("network_id") or "")
        sync_id = str(entry.get("sync_id") or "")
        modules = [sync for _name, sync in blink_models.select_sync_modules(blink, None)]
        if network_id or sync_id:
            matches = [
                sync for sync in modules
                if (not network_id
                    or str(getattr(sync, "network_id", "")) == network_id)
                and (not sync_id
                     or str(getattr(sync, "sync_id", "")) == sync_id)
            ]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise RuntimeError(
                    f"Sync Module not found (network {network_id or '?'}, "
                    f"module {sync_id or '?'})."
                )
            raise RuntimeError(
                f"Ambiguous Sync Module (network {network_id or '?'}, "
                f"module {sync_id or '?'})."
            )

        camera = str(entry.get("camera") or "").strip()
        sync, _camera = self.find_camera(blink, camera)
        return sync


class LoginFlow:
    """Drive a Blink connection whose answers arrive from the browser.

    The difficulty: credentials and the verification code arrive in two
    distinct HTTP requests, but must talk to the same Blink session, open
    between them. So we keep a live asyncio loop in a thread, and the
    connect task suspends there waiting for the code instead of reading it
    from the keyboard.

    The password never leaves this process's memory: blink2video.py only
    writes session tokens, never the password."""

    def __init__(self):
        self.loop = None
        self.future = None
        self.queue = None
        self._ferme = None
        self.asked = threading.Event()
        # Singleton on Handler (ThreadingHTTPServer, one thread per request):
        # two overlapping /api/login (page refreshed out of impatience
        # while a prior login still waits for its code, up to 120 s)
        # mutated loop/future/queue without synchronization, with a close()
        # could stop the loop another thread just created.
        self._verrou = threading.Lock()

    def start(self, username: str, password: str) -> dict:
        with self._verrou:
            self._arreter_session_courante()
            self.loop = asyncio.new_event_loop()
            threading.Thread(target=self._serve, daemon=True).start()
            self.asked.clear()
            self._ferme = threading.Event()
            self.future = asyncio.run_coroutine_threadsafe(
                self._login(username, password, self._ferme), self.loop
            )
            future = self.future
        return self.wait(future)

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _login(self, username: str, password: str, ferme: threading.Event) -> dict:
        self.queue = asyncio.Queue()

        async def ask_code(attempt: int) -> str:
            self.asked.set()
            return await self.queue.get()

        try:
            try:
                async with blink_auth.session_http_temporaire() as session:
                    blink = await blink_auth.login(session, username, password, ask_code)
            except Exception as error:  # surface the cause rather than crash the server
                return {"status": "error", "message": f"{type(error).__name__}: {error}"}
            if blink is None:
                return {"status": "error", "message": "Credentials or code refused."}
            return {"status": "ok"}
        finally:
            # Signals the REAL end of cleanup (including HTTP session close),
            # independent of the concurrent.futures.Future returned by
            # run_coroutine_threadsafe: the latter becomes "cancelled"
            # synchronously on .cancel(), even before the loop has given
            # control back to the cancelled task to run this block (observed
            # empirically: cancelled() becomes True in 0 ms, this finally
            # runs a fraction of a second later).
            # _arreter_session_courante() waits for this event rather than
            # future.result() to know when to cut the loop without
            # cutting this cleanup mid-way.
            ferme.set()

    def submit_code(self, code: str) -> dict:
        with self._verrou:
            if self.loop is None or self.queue is None:
                return {"status": "error", "message": "No connection in progress."}
            self.asked.clear()
            loop, queue, future = self.loop, self.queue, self.future
        loop.call_soon_threadsafe(queue.put_nowait, code)
        return self.wait(future)

    def wait(self, future, timeout: float = 120.0) -> dict:
        """Yield as soon as a code is requested or the connection succeeds.

        ``future`` is the one the caller captured under lock, never reread
        from ``self``: if another login session has meanwhile replaced this
        one, we keep waiting for ours without being trapped by a shared
        attribute that changed under our feet."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if future.done():
                result = future.result()
                self._fermer_si_courante(future)
                return result
            if self.asked.is_set():
                return {"status": "2fa"}
            time.sleep(0.1)
        self._fermer_si_courante(future)
        return {"status": "error", "message": "Timed out."}

    def close(self) -> None:
        with self._verrou:
            self._arreter_session_courante()

    def _fermer_si_courante(self, future) -> None:
        """Stop the loop only if ``future`` is still the active session,
        not one a more recent concurrent login has already replaced."""
        with self._verrou:
            if self.future is future:
                self._arreter_session_courante()

    def _arreter_session_courante(self) -> None:
        if self.future is not None and not self.future.done():
            ferme = self._ferme
            self.future.cancel()
            # future.done()/.cancelled() becomes True SYNCHRONOUSLY on
            # this call, well before the underlying asyncio task could
            # run its cancellation (it only resumes on the next turn of
            # the loop, in the other thread): waiting for future.result() here
            # therefore guarantees nothing about the coroutine's real state (observed
            # empirically: cancelled() is True in 0 ms, while the
            # ``finally`` of _login, which closes the HTTP session, only runs
            # only a fraction of a second later). ``ferme`` is the only
            # reliable signal of that real end; without waiting for it, loop.stop()
            # cut this cleanup mid-way, leaving the HTTP session
            # open and the task never truly finished (observed in practice:
            # restarting a connection while a previous one still waits for its
            # verification code).
            if ferme is not None:
                ferme.wait(timeout=5)
            try:
                self.future.result(timeout=0)
            except (Exception, asyncio.CancelledError):
                pass
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop, self.future, self.queue, self._ferme = None, None, None, None


BLINK = BlinkSession()


def _choisir_dossier_windows(initial: str) -> str:
    """Native folder picker via PowerShell/WinForms, empty string if cancelled.

    tkinter depends on Tcl/Tk, not always correctly embedded in a PyInstaller
    bundle (observed in real conditions on the Windows 7 build: "Folder
    picker unavailable"). System.Windows.Forms is part of .NET Framework,
    present by default since Windows Vista (hence Windows 7 too), and needs
    nothing packaged. An invisible TopMost owner serves the same role as
    tkinter's -topmost: without it, the dialog can open behind the browser.

    The owner must actually be shown (Show(), Opacity=0, off-screen) rather
    than just built with WindowState='Minimized' without ever displaying it:
    that second form, tried first, made ShowDialog() enter a silent hang
    (no error, no visible dialog, the PowerShell process stayed alive
    indefinitely — observed in real conditions). -STA is already
    powershell.exe's default mode (verified: GetApartmentState() returns STA
    without the option), kept explicit so we do not silently depend on it."""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$proprietaire = New-Object System.Windows.Forms.Form; "
        "$proprietaire.ShowInTaskbar = $false; "
        "$proprietaire.Opacity = 0; "
        "$proprietaire.StartPosition = 'Manual'; "
        "$proprietaire.Location = New-Object System.Drawing.Point(-2000, -2000); "
        "$proprietaire.Size = New-Object System.Drawing.Size(1, 1); "
        "$proprietaire.Show(); "
        "$proprietaire.TopMost = $true; "
        "$dialogue = New-Object System.Windows.Forms.FolderBrowserDialog; "
        f"$dialogue.SelectedPath = '{initial.replace(chr(39), chr(39) * 2)}'; "
        "if ($dialogue.ShowDialog($proprietaire) -eq "
        "[System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Out.Write($dialogue.SelectedPath) }"
    )
    resultat = runtime.lancer(
        ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", check=False,
    )
    if resultat.returncode != 0:
        raise RuntimeError((resultat.stderr or "").strip() or "PowerShell failed")
    return resultat.stdout.strip()


class _ReglagesInvalides(ValueError):
    """Form rejection meant for an HTTP 400 response."""


# What a host name (including NetBIOS, hence "_"), an IPv4/IPv6 or a
# CIDR subnet may contain, and nothing else: each entry also ends up in
# the CSP header (frame-ancestors, see end_headers), where a ";" would open
# one more directive.
_ENTREE_CONFIANCE_RE = re.compile(r"^[A-Za-z0-9._\-:\[\]/]+$")


def normaliser_hotes_confiance(valeur: str) -> str:
    """Validate trusted_host and return it in canonical form ("a,b"), or raise
    ValueError with a displayable message. Applied on save from the settings
    page, never at startup: a setting accepted by a prior version and rejected
    by this one would otherwise prevent the server from starting after an
    update. In use, hote_autorise() and end_headers() ignore invalid entries
    anyway (default deny)."""
    entrees = serveweb.entrees_confiance(valeur)
    for entree in entrees:
        # A leading "-" belongs to no host name or address, and
        # argparse would read "--trusted-host -x" as an option without a value:
        # serve would then refuse to start on the next launch.
        if not _ENTREE_CONFIANCE_RE.match(entree) or entree.startswith("-"):
            raise ValueError(
                f"Invalid trusted host: « {entree} ». One or more "
                "hostnames, IP addresses or CIDR subnets (192.168.1.0/24), "
                "comma-separated.")
        if "/" in entree:
            try:
                ipaddress.ip_network(entree, strict=False)
            except ValueError as erreur:
                raise ValueError(
                    f"Invalid subnet: « {entree} ». Expected format: "
                    "192.168.1.0/24.") from erreur
    return ",".join(entrees)


def _verifier_dossier_reglages(dossier: str) -> None:
    """Probe writing without changing the active storage folder."""
    if not dossier:
        return
    try:
        candidat = Path(dossier).expanduser()
        candidat.mkdir(parents=True, exist_ok=True)
        # Exclusive creation: neither an existing file overwritten, nor a collision between
        # two requests. The context closes and deletes only its probe,
        # including if the write or buffer flush fails.
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=".blink_ecriture_test-",
                dir=candidat) as sonde:
            sonde.write("blink2video")
            sonde.flush()
    except OSError as erreur:
        raise _ReglagesInvalides(f"Storage folder inaccessible: {erreur}") from erreur


def _preparer_reglages_web(payload: dict) -> tuple[str, dict]:
    """Return the requested folder and normalized settings, without saving them.

    The folder probe is a real side effect: keep the order of checks, which
    also determines the first error shown to the user.
    """
    try:
        usb_minutes = int(payload.get("usb_minutes"))
        cloud_minutes = int(payload.get("cloud_minutes"))
        port = int(payload.get("port"))
        if usb_minutes < 1 or cloud_minutes < 1:
            raise ValueError
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError) as erreur:
        raise _ReglagesInvalides(
            "Cadences must be minute counts of at least "
            "1, and the port a number between 1 and 65535.") from erreur

    dossier = str(payload.get("storage_dir", "")).strip()
    _verifier_dossier_reglages(dossier)

    timezone = str(payload.get("timezone", "")).strip()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as erreur:
        raise _ReglagesInvalides(f"Unknown timezone: « {timezone} ».") from erreur

    reglages = {
        "usb_minutes": usb_minutes, "cloud_minutes": cloud_minutes, "port": port,
        "timestamp": bool(payload.get("timestamp", False)), "timezone": timezone,
        "merge_jour": bool(payload.get("merge_jour", True)),
        "merge_semaine": bool(payload.get("merge_semaine", True)),
        "merge_mois": bool(payload.get("merge_mois", True)),
        "download_auto": bool(payload.get("download_auto", True)),
    }
    protocole = str(payload.get("live_protocol", "webrtc"))
    if protocole not in runtime.PROTOCOLES_LIVE_VALIDES:
        raise _ReglagesInvalides(f"Unknown live protocol: « {protocole} ».")
    reglages["live_protocol"] = protocole

    font_size = payload.get("font_size")
    if isinstance(font_size, str):
        font_size = font_size.strip() or None
    if font_size is not None:
        try:
            font_size = int(font_size)
            if not 8 <= font_size <= 500:
                raise ValueError
        except (TypeError, ValueError) as erreur:
            raise _ReglagesInvalides(
                "Font size must be a number between 8 and 500, "
                "or empty for automatic size.") from erreur
    reglages["font_size"] = font_size

    font_color = str(payload.get("font_color") or "white").strip() or "white"
    if not md.COULEUR_FFMPEG_RE.match(font_color):
        raise _ReglagesInvalides(f"Invalid timestamp color: « {font_color} ».")
    reglages["font_color"] = font_color

    try:
        box_opacity = float(payload.get("box_opacity", 0.55))
        if not 0.0 <= box_opacity <= 1.0:
            raise ValueError
    except (TypeError, ValueError) as erreur:
        raise _ReglagesInvalides(
            "Banner opacity must be a number between 0.0 and 1.0.") from erreur
    reglages["box_opacity"] = box_opacity

    try:
        reglages["trusted_host"] = normaliser_hotes_confiance(
            str(payload.get("trusted_host") or ""))
    except ValueError as erreur:
        raise _ReglagesInvalides(str(erreur)) from erreur

    webhook_notif_url = str(payload.get("webhook_notif_url") or "").strip()
    if webhook_notif_url:
        try:
            schema_valide = urlparse(webhook_notif_url).scheme in ("http", "https")
        except ValueError:
            # urlparse raises on some clearly invalid forms
            # (badly closed IPv6, e.g. "http://[abc") rather than returning an
            # empty scheme as for other malformed URLs — a case
            # found while wondering whether that same raise also hit
            # hote_autorise() (Origin), which calls urlparse the same
            # way with no more protection than here before this fix.
            schema_valide = False
        if not schema_valide:
            raise _ReglagesInvalides(
                f"Invalid notification URL: « {webhook_notif_url} ». A full "
                "http:// or https:// address, or empty to disable.")
    reglages["webhook_notif_url"] = webhook_notif_url

    # Capped at LIVE_MAX_SECONDS: the server cuts every
    # live view at that delay anyway, a value beyond would be inert (capped at 24 h
    # in 0.13.0, fixed on noticing this ceiling for issue #15).
    try:
        live_auto_stop_seconds = int(payload.get("live_auto_stop_seconds", 0) or 0)
        if not 0 <= live_auto_stop_seconds <= LIVE_MAX_SECONDS:
            raise ValueError
    except (TypeError, ValueError) as erreur:
        raise _ReglagesInvalides(
            "Live auto-stop must be a number of seconds "
            f"between 0 (disabled) and {LIVE_MAX_SECONDS} (the ceiling for any "
            "live view).") from erreur
    reglages["live_auto_stop_seconds"] = live_auto_stop_seconds

    return dossier, reglages


class Handler(serveweb.Handler):
    # HTTP/1.1 to keep the connection open: a browser scrubbing a video
    # chains Range requests, one per seek. On HTTP/1.0 it would reopen a
    # connection each time.
    protocol_version = "HTTP/1.1"
    paths: dict = {}
    timezone: ZoneInfo = ZoneInfo("Europe/Paris")
    hub: str | None = None
    ffmpeg: str = ""
    trusted_host: str = ""
    # Temporary server for the very first start: settings are saved without
    # this process launching a restart itself. The ``start`` parent waits for
    # their marker, stops this server, then only then creates the workers.
    initial_setup: bool = False
    lock = threading.Lock()
    login_flow = LoginFlow()

    def log_message(self, fmt, *args):
        pass  # the access log learns nothing here

    # ------------------------------------------------------------ protection

    # The host guard (Host, Origin, trusted_host as list or CIDR, local proxy)
    # is that of nico579_commons.serveweb, the same for all four apps; only
    # what is specific to blink2video remains here.
    variable_proxy_local = "BLINK_TRUSTED_LOOPBACK_PROXY"

    def _journaliser_acces_refuse(self, raison: str) -> None:
        """Log a hote_autorise() 403 in serve_erreurs.log (same file as
        handle_error, same tolerance of write failure).

        Without this, a misconfigured trusted_host (case, forgotten port in
        the field, Tailscale MagicDNS instead of the IP...) leaves no usable
        trace: just a generic 403 in the browser. Issue #10, BLINK_BIND +
        trusted_host always giving 403 without knowing which of the three
        conditions (Host, local loop/tunnel, Origin) had failed."""
        try:
            with (runtime.app_dir() / "serve_erreurs.log").open(
                    "a", encoding="utf-8") as journal:
                journal.write(
                    f"\n--- {dt.datetime.now().isoformat()} access denied ---\n"
                    f"raison: {raison}\n"
                    f"Host received: {self.headers.get('Host')!r}\n"
                    f"Origin received: {self.headers.get('Origin')!r}\n"
                    f"IP cliente: {getattr(self, 'client_address', ('?', 0))[0]}\n"
                    f"trusted_host configured: {self.trusted_host!r}\n")
        except OSError:
            pass

    def jeton_valide(self) -> bool:
        """The process token (TOKEN) must accompany every request that
        changes something.

        Distinct from `hote_autorise()`: Host is hard to forge but can be
        bypassed by DNS rebinding (a public domain pointed at 127.0.0.1);
        a custom header, by contrast, can only be set by code that has read
        the page served here, which a foreign origin cannot do (browser
        same-origin policy)."""
        if self.headers.get("X-Blink-Token") == TOKEN:
            return True
        # EventSource, <video> and <img> cannot add a custom
        # header. Their URL therefore carries the same secret; no-referrer and the
        # same-origin policy below prevent its leak to a third party.
        valeurs = parse_qs(urlparse(self.path).query).get("token") or []
        return any(value == TOKEN for value in valeurs)

    def identite_acces(self) -> dict:
        """Cloudflare Access + allowlist profile for this request (cached)."""
        cache = getattr(self, "_identite_acces_cache", None)
        if cache is not None:
            return cache
        cache = access_control.resoudre_identite(self.headers)
        self._identite_acces_cache = cache
        return cache

    def _refuser_acces(self, code: int = 403, message: str = "Forbidden") -> None:
        try:
            self.send_json({"error": message, "role": self.identite_acces().get("role")},
                           code)
        except Exception:
            self.send_error(code)

    def exiger_acces_authentifie(self) -> bool:
        """False already answered the client. True means identity is usable.

        Passing a Cloudflare Access policy (valid JWT) is enough to enter as
        guest/admin; camera allowlists are enforced separately per route.
        """
        profil = self.identite_acces()
        if profil.get("role") == "unauthenticated":
            self._refuser_acces(401, profil.get("error") or "Cloudflare Access login required.")
            return False
        return True

    def exiger_admin(self) -> bool:
        if not self.exiger_acces_authentifie():
            return False
        if self.identite_acces().get("role") != "admin":
            self._refuser_acces(403, "Admin only.")
            return False
        return True

    def exiger_camera(self, *identifiants: str | None) -> bool:
        if not self.exiger_acces_authentifie():
            return False
        email = self.identite_acces().get("email") or ""
        if access_control.camera_autorisee(email, *identifiants):
            return True
        self._refuser_acces(403, "Camera not allowed for this user.")
        return False

    def camera_depuis_clip_identity(self, identity: str) -> tuple[str | None, str | None]:
        """Return (camera name, camera key) for a clip identity, if known."""
        entry = read_entries(self.paths).get(identity)
        if not isinstance(entry, dict):
            return None, None
        nom = str(entry.get("camera") or "").strip() or None
        cle = blink_registre.camera_setting_key_from_entry(entry)
        return nom, cle

    def media_autorise(self, route: str) -> bool:
        kind, _, relative = route.partition("/")
        if not relative:
            return False
        email = self.identite_acces().get("email") or ""
        if kind == "clip":
            nom, cle = self.camera_depuis_clip_identity(relative)
            return access_control.camera_autorisee(email, nom, cle)
        camera = relative.split("/", 1)[0]
        return access_control.camera_autorisee(email, camera)

    def end_headers(self) -> None:
        # 'none' by default: even a page of this site must not be able to
        # display in an <iframe>, last rampart against
        # clickjacking (clicking a button you think is elsewhere).
        # Relaxed to trusted_host entries (issue #12: embed the page
        # in a home-automation dashboard like ioBroker) — only the
        # exact hosts/IPs, never a CIDR subnet: frame-ancestors has
        # no syntax for a subnet, and silently accepting one
        # would suggest a protection that does not apply. Refiltered here
        # even though settings are validated on write: a settings
        # file edited by hand does not go through that, and a ";" would add
        # a directive to this header.
        # ":*" required: without a port, a CSP source only applies to the
        # default port (80), and a dashboard served on 8081/8082
        # (ioBroker) was refused, verified in Chrome. Any port of the host,
        # like hote_autorise() which already ignores the port. No IPv6 entry:
        # CSP syntax has no form for a literal IPv6 address.
        exacts = [entree for entree in serveweb.entrees_confiance(self.trusted_host)
                  if "/" not in entree and ":" not in entree
                  and _ENTREE_CONFIANCE_RE.match(entree)]
        frame_ancestors = ("'self' " + " ".join(f"{entree}:*" for entree in exacts)
                           if exacts else "'none'")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            f"script-src 'nonce-{SCRIPT_NONCE}'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "media-src 'self' blob:; connect-src 'self'; object-src 'none'; "
            f"base-uri 'none'; form-action 'self'; frame-ancestors {frame_ancestors}",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        super().end_headers()

    # ------------------------------------------------------------------ envoi

    def repondre_puis_redemarrer(self, commande_restart: list) -> None:
        """Detach a command able to stop THIS process, then confirm to the
        browser that it was created. Factors /api/reglages and /api/stop,
        which share exactly this need.

        wfile.write()/flush() only guarantee delivery to the OS, not real
        delivery to the browser: a too-soon taskkill can abort the connection
        before the network stack finishes transmitting, the window being
        wider on a loaded or slow machine (reported by a user on Windows
        7/Python 3.8: JSON.parse failing on the page, disappearing once
        camera watching was stopped — less contention). A short pause after
        flush gives the network stack time to actually empty its buffer
        before kill."""
        # Verify the relay could really be created BEFORE announcing
        # acceptance. The finalizer then waits briefly before stopping,
        # which gives this response time to reach the browser without
        # having to hope a sleep after the flush is enough.
        commande = [*commande_restart, "--delai", "0.75"]
        try:
            runtime.demarrer(
                runtime.self_command(*commande), cwd=str(runtime.app_dir()),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))
        except OSError as erreur:
            self.send_json({"error": f"Could not start shutdown: {erreur}"}, 500)
            return
        self.send_json({"ok": True, "accepted": True})

    def send_media(self, path: Path) -> None:
        """Serve a file while handling Range requests.

        Without Range, a browser cannot scrub a video: it has no choice but
        to download everything from the start. That is the only reason this
        server is not three lines."""
        size = path.stat().st_size
        start, end = 0, size - 1
        partial = False
        header = self.headers.get("Range", "")
        match = re.match(r"bytes=(\d*)-(\d*)", header)
        if match:
            first, last = match.group(1), match.group(2)
            if first:
                # Do NOT clamp start here: a start past the file (bytes=20-
                # of 10 bytes) must be rejected (416), not silently
                # clamped to the last valid byte — RFC 7233 only allows
                # that for the END bound, never for the start bound.
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            elif last:  # suffixe : les N derniers octets
                n = int(last)
                start = max(size - n, 0) if n > 0 else size
                end = size - 1
            else:  # "bytes=-", neither start nor end: nothing to serve
                start = size
                end = size - 1
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header(
            "Content-Type", mimetypes.guess_type(path.name)[0] or "video/mp4"
        )
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining > 0:
                chunk = source.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # the user changed clip, that is normal
                remaining -= len(chunk)

    def _client_parti(self) -> bool:
        """True if the browser has already closed its connection (cancelled request).

        A fast-scrolling page releases its players, and the browser cancels
        the thumbnails it had requested (359 statusless requests in Joel's
        cold test, PR #59); the server does not know, still built each via
        ffmpeg, and those requests kept their place in the extraction queue.
        A readable byte that is the next request on a persistent connection
        is not a departure: only a closed stream (empty read) is."""
        try:
            lisibles, _, _ = select.select([self.connection], [], [], 0)
            if not lisibles:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError, TypeError, AttributeError):
            return True

    def send_thumb(self, route: str, source: Path) -> None:
        """Serve a clip's thumbnail, building it on first request.

        It is taken a little after the start, never on the first frame: a
        camera that just triggered often delivers one or two black or
        overexposed frames while exposure adjusts. And it is extracted from
        the normalized version when that exists, so the burned-in timestamp
        appears in the thumbnail."""
        thumb = (self.paths["thumbs"] / route).with_suffix(".jpg")

        def fraiche() -> bool:
            return (
                thumb.is_file()
                and thumb.stat().st_size > 0
                and thumb.stat().st_mtime >= source.stat().st_mtime
            )

        if not fraiche():
            # Per-thumbnail lock: see _verrou_miniature(). Re-checks
            # fraiche() once the lock is obtained — the thread that
            # wait may find it already built by whoever just
            # released it, otherwise a second useless extraction
            # would overwrite the first's temporary file mid-send.
            with _verrou_miniature(str(thumb)):
                if not fraiche():
                    thumb.parent.mkdir(parents=True, exist_ok=True)
                    pending = thumb.with_suffix(".tmp.jpg")
                    # At most two extractions at once: the browser requests
                    # all page thumbnails at once, and that many
                    # concurrent ffmpeg would saturate the machine for nothing.
                    with THUMB_SLOTS:
                        # After waiting for the slot, before the work: the
                        # client may have left during the queue (see _client_parti).
                        if self._client_parti():
                            self.close_connection = True
                            return
                        md.extraire_vignette(self.ffmpeg, source, pending)
                    if not pending.is_file() or pending.stat().st_size == 0:
                        pending.unlink(missing_ok=True)
                        self.send_error(404)
                        return
                    pending.replace(thumb)

        # Every filter change rebuilds the whole grid (see
        # renderClips on the page), so each already-known <video poster=...>
        # re-requests the same image: without validation, the browser
        # fully re-downloaded it every time, a black flash while
        # it came back for all thumbnails at once (observed in
        # practice, 2026-08-27). The thumbnail itself only changes on the next
        # re-download of the source clip (see `fresh` above): a
        # conditional revalidation is therefore safe, not just faster.
        derniere_modif = email.utils.formatdate(thumb.stat().st_mtime, usegmt=True)
        depuis = self.headers.get("If-Modified-Since")
        if depuis:
            try:
                pas_change = email.utils.parsedate_to_datetime(depuis) \
                    >= email.utils.parsedate_to_datetime(derniere_modif)
            except (TypeError, ValueError):
                pas_change = False
            if pas_change:
                self.send_response(304)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return

        body = thumb.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Last-Modified", derniere_modif)
        self.end_headers()
        self.wfile.write(body)

    def describe_camera(self, camera_name: str, camera, raw: dict) -> dict:
        """Describe a camera, dating its readings.

        An unreachable camera keeps returning its last known temperature.
        Presenting it without a date would announce as current a value that
        may be weeks old."""
        candidates = raw.get(camera_name.strip(), [])
        if isinstance(candidates, dict):
            candidates = [candidates]
        attributes = getattr(camera, "attributes", None) or {}
        network_id = str(getattr(camera, "network_id", "") or "")
        device_id = str(
            getattr(camera, "device_id", None)
            or getattr(camera, "camera_id", None)
            or attributes.get("device_id")
            or attributes.get("camera_id")
            or attributes.get("id")
            or ""
        )
        info = next(
            (item for item in candidates
             if device_id and str(item.get("id") or item.get("device_id")
                                  or item.get("camera_id") or "") == device_id),
            None,
        )
        if info is None:
            info = next(
                (item for item in candidates
                 if network_id and str(item.get("network_id")
                                       or item.get("network") or "") == network_id),
                candidates[0] if candidates else {},
            )
        status = str(info.get("status") or "").strip()
        measured = info.get("updated_at")
        age = None
        measured_today = False
        if measured:
            try:
                moment = md.parse_created_at(str(measured))
                age = (dt.datetime.now(dt.timezone.utc) - moment).total_seconds()
                # A reading from today reads better as time alone; beyond that the
                # date becomes necessary to judge its freshness. The page
                # composes the text, in its language (issue #37).
                measured, measured_today = horodatage_releve(moment, self.timezone)
            except (ValueError, TypeError):
                measured, age = None, None

        # Homescreen "signals" are the gauges the
        # the Blink app. We pass them through as-is, without inventing
        # scale: nothing in the API documents their maximum, and on this
        # installation a camera that works very well shows lfr 1.
        signals = info.get("signals") or {}

        # Do not use camera.arm: for an indoor mini, blinkpy
        # returns the sync module's arming instead of the
        # camera's own detection (BlinkCameraMini.arm returns sync.arm).
        # Armed system plus detection off thus showed as "active".
        # motion_enabled is correct for all models, and the
        # homescreen `enabled` field serves as a cross-check.
        enabled = camera.motion_enabled
        if enabled is None:
            enabled = info.get("enabled")

        # Raw homescreen temperature (Fahrenheit, as Blink
        # reports it): same conversion as camera.temperature_c (blinkpy,
        # camera.py) for an identical result, without depending on that
        # propriete elle-meme.
        temp_brute = signals.get("temp")
        try:
            temperature = round((temp_brute - 32) / 9.0 * 5.0, 1)
        except TypeError:
            temperature = None

        return {
            "name": camera_name.strip(),
            "armed": bool(enabled),
            # battery/temperature/wifi/firmware/kind : lus depuis l'ecran
            # homescreen (info/signals), not from the camera object of
            # blinkpy (attributes) — that one is only updated by
            # sync_module.refresh(), no longer called from system_state()
            # depuis l'allegement du 2026-09-03 (cf. BACKLOG.md) : y lire
            # those fields silently froze them at the value from
            # server startup, never refreshed afterward. Verified on
            # a real homescreen before this fix (not assumed):
            # tous presents (battery, fw_version, type, signals.wifi/temp).
            # voltage alone has no equivalent on the homescreen,
            # laisse tel quel : jamais affiche cote page (verifie),
            # going stale after startup has no visible effect.
            "battery": info.get("battery"),
            "battery_signal": signals.get("battery"),
            "voltage": attributes.get("battery_voltage"),
            "temperature": temperature,
            "wifi": signals.get("wifi"),
            "lfr": signals.get("lfr"),
            "firmware": info.get("fw_version"),
            "kind": info.get("type"),
            "model": model_name(info.get("type")),
            "serial": info.get("serial") or attributes.get("serial"),
            "status": status,
            "offline": status == "offline",
            "measured_at": measured,
            "measured_today": measured_today,
            "age_seconds": age,
        }

    def system_state(self) -> dict:
        """Arming state of the system and cameras.

        Blink arming has two levels, and confusing them leads to surprises:
        the sync module commands the whole, each camera also has its own
        switch. An armed camera in a disarmed system detects nothing, hence
        showing both."""
        # Read once for all cameras: it is a file, not a
        # question asked of Blink.
        venues = provenances(read_entries(self.paths))

        def read(blink):
            async def run(_blink=blink):
                # _blink.refresh(force=True) also chained, per camera,
                # get_camera_info() + update() (sync_module.py refresh()),
                # plus update_local_storage_manifest()/check_new_videos() -
                # none of that is read below: this display does
                # vient que de get_homescreen() (nom/batterie/temperature/
                # statut) et de network_info par module (armement). Verifie
                # in blinkpy's source before cutting (not
                # assumed): sync.cameras is populated once, at
                # connexion initiale (update_cameras(), appele par start(),
                # never by refresh()) — its identifiers (device_id/
                # network_id, used below to match each
                # camera from its homescreen entry) are therefore
                # already stable here, no need to refresh them on every
                # appel. Mesure en reel (2026-09-03) : la sequence complete
                # (1 + 1 per module + 1 per camera, in series) made
                # "Interrogation du systeme Blink..." last far longer than
                # necessary for this specific page.
                await _blink.get_homescreen()
                for sync in _blink.sync.values():
                    await sync.get_network_info()
                # blinkpy attributes do not say *when* a reading was
                # been taken. The homescreen carries a `status` and an
                # `updated_at` per device: without them, the temperature of a
                # out-of-range camera shows as if it were current
                # alors qu'elle peut dater de plusieurs semaines.
                raw = {}
                home = getattr(_blink, "homescreen", None) or {}
                for group in ("cameras", "owls", "doorbells"):
                    for item in home.get(group) or []:
                        raw.setdefault(str(item.get("name") or "").strip(), []).append(item)

                modules = {str(m.get("network_id")): m
                           for m in (home.get("sync_modules") or [])}
                systems = []
                for name, sync in _blink.sync.items():
                    module = modules.get(str(sync.network_id)) or {}
                    systems.append({
                        "name": name.strip(),
                        "key": system_key(name, sync),
                        "armed": bool(sync.arm),
                        "module": MODULE_MODELS.get(module.get("type"))
                                  or (f"module « {module.get('type')} »"
                                      if module.get("type") else None),
                        "module_firmware": module.get("fw_version"),
                        "module_serial": module.get("serial"),
                        "cameras": [
                            dict(self.describe_camera(camera_name, camera, raw),
                                 key=camera_key(sync, camera_name, camera),
                                 clips_source=venues.get(camera_name.strip()))
                            for camera_name, camera in sync.cameras.items()
                        ],
                    })
                return {"systems": systems, "passages": runtime.passages()}
            return run()

        state = BLINK.call(read, timeout=60)
        state["webrtc"] = WEBRTC_ACTIF
        # Relu a chaque appel, contrairement a WEBRTC_ACTIF (fige a l'import
        # of the process): a change from Settings already restarts the
        # server, but watchLive() reads this field during a live view already in
        # progress, well after that restart.
        state["live_auto_stop_seconds"] = runtime.lire_reglages()["live_auto_stop_seconds"]
        return state

    def set_armed(self, scope: str, identity: str, armed: bool) -> None:
        def apply(blink):
            async def run(_blink=blink):
                if scope == "system":
                    sync = BLINK.find_system(_blink, identity)
                    await sync.async_arm(armed)
                    return
                _, camera = BLINK.find_camera(_blink, identity)
                reponse = await camera.async_arm(armed)
                await _confirmer_armement_camera(_blink, camera, reponse, armed)
            return run()

        BLINK.call(apply, timeout=60)

    def reveiller_camera(self, identity: str) -> None:
        """Reveille une camera pour de bon, pas une simple relecture du cache.

        `system_state()`/`/api/system` relit deja le compte a chaque fois,
        mais c'est une lecture passive : les deux GET que blinkpy y fait
        (config, capteurs) renvoient ce que Blink a deja en archive cote
        serveur, jamais une valeur plus fraiche qu'un appareil endormi n'a
        pas encore renvoyee. `snap_picture()` poste une vraie commande, que
        Blink relaie a la camera physique et attend qu'elle confirme avant
        de repondre (jusqu'a 120 s dans le pire cas) - le seul mecanisme qui
        force reellement un reveil, au prix reel d'une photo prise et d'un
        peu de batterie a chaque clic. Voir AUDIT 28.51 (suite) pour la
        recherche qui a mene ici."""
        def demander(blink):
            async def run(_blink=blink):
                from blinkpy import api

                _, camera = BLINK.find_camera(_blink, identity)
                reponse = await camera.snap_picture()
                if not isinstance(reponse, dict) or not reponse.get("id"):
                    raise RuntimeError("Blink a refuse la commande de reveil.")
                # blinkpy already waits for the command, but ignores the result
                # of wait_for_command() and returns the initial response even
                # on a refusal or timeout. A single GET confirms
                # its outcome without triggering a second photo.
                statut = await api.request_command_status(
                    _blink, reponse.get("network_id") or camera.network_id,
                    reponse["id"],
                )
                if (not isinstance(statut, dict)
                        or statut.get("status_code") != 908
                        or statut.get("complete") is not True):
                    raise RuntimeError("Blink n'a pas confirme le reveil de la camera.")
            return run()

        if not MODULE_SLOT.acquire(blocking=False):
            raise blink_engine.BusyError(_slot_occupe_message())
        try:
            _slot_pris("reveil", identity)
            # The in-memory lock protects this server's live views; the
            # on-disk one also protects downloads in another
            # process. Both stay held until confirmation.
            with blink_engine.hub_lock("reveil", attente=ATTENTE_HUB_MAX_SECONDS):
                BLINK.call(demander, timeout=130)
        finally:
            try:
                _slot_rendu()
            finally:
                MODULE_SLOT.release()

    def declencher_snapshot(self, identity: str) -> Path:
        """Take an on-demand photo and keep it, unlike reveiller_camera() which
        only confirms wake-up (GitHub issue #9: manual button and webhook
        share this same method).

        Command confirmation does not update the thumbnail in blinkpy.
        The camera state must be reread and the image verified to have
        changed; otherwise get_media() may return the old photo."""
        resultat = {}

        def demander(blink):
            async def run(_blink=blink):
                from blinkpy import api

                sync, camera = BLINK.find_camera(_blink, identity)
                ancienne_url = camera.thumbnail
                ancien_corps = getattr(camera, "_cached_image", None)
                reponse = await camera.snap_picture()
                if not isinstance(reponse, dict) or not reponse.get("id"):
                    raise RuntimeError(msg("snapshot_commande_refusee"))
                statut = await api.request_command_status(
                    _blink, reponse.get("network_id") or camera.network_id,
                    reponse["id"],
                )
                if (not isinstance(statut, dict)
                        or statut.get("status_code") != 908
                        or statut.get("complete") is not True):
                    raise RuntimeError(msg("snapshot_non_confirme"))
                dernier_essai = (ATTENTE_MEDIA_SNAPSHOT_SECONDS
                                 // INTERVALLE_MEDIA_SNAPSHOT_SECONDS)
                async def attendre_media():
                    for tentative in range(dernier_essai + 1):
                        await self._actualiser_media_camera(_blink, sync, camera)
                        media = await camera.get_media()
                        if media is not None and media.status == 200:
                            corps = await media.read()
                        else:
                            corps = b""
                        image_changee = (camera.thumbnail != ancienne_url or
                                         (ancien_corps is not None and corps != ancien_corps))
                        if corps and image_changee:
                            return corps
                        if tentative < dernier_essai:
                            await asyncio.sleep(INTERVALLE_MEDIA_SNAPSHOT_SECONDS)
                    raise RuntimeError(msg("snapshot_image_indisponible"))

                try:
                    corps = await asyncio.wait_for(
                        attendre_media(), timeout=ATTENTE_MEDIA_SNAPSHOT_SECONDS)
                except asyncio.TimeoutError:
                    raise RuntimeError(msg("snapshot_image_indisponible"))
                resultat["camera"] = camera_key(sync, camera.name, camera)
                resultat["nom"] = camera.name
                resultat["corps"] = corps
            return run()

        if not MODULE_SLOT.acquire(blocking=False):
            raise blink_engine.BusyError(_slot_occupe_message())
        try:
            _slot_pris("snapshot", identity)
            with blink_engine.hub_lock("snapshot", attente=ATTENTE_HUB_MAX_SECONDS):
                BLINK.call(demander, timeout=130)
        finally:
            try:
                _slot_rendu()
            finally:
                MODULE_SLOT.release()

        dossier = self.paths["snapshots"] / safe_file(resultat["nom"])
        dossier.mkdir(parents=True, exist_ok=True)
        horodatage = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d_%H-%M-%SZ")
        # Content fingerprint, not a counter: two triggers in the
        # same second (double click, close webhooks) would otherwise silently
        # overwrite one of the two shots (observed in testing).
        empreinte = hashlib.sha256(resultat["corps"]).hexdigest()[:8]
        cible = dossier / f"{horodatage}_{empreinte}.jpg"
        cible.write_bytes(resultat["corps"])
        runtime.notifier_nouveau_media(resultat["nom"], cible, "snapshot")
        # The Live thumbnail (send_camera_thumb) comes from a distinct cache.
        # An on-demand photo already has the image at hand; it avoids showing
        # a view older than what was just captured
        # (requested on GitHub issue #10). resultat["camera"], not
        # `identity`: the webhook receives the display name (see the
        # settings URL template), but the Live tile reads its thumbnail under
        # the stable key (c.key on the JS side) — the two only coincide by
        # chance for the manual button, which already sends that key. A real
        # conditions test (webhook, camera "Salon") showed the
        # wrong file written before this fix.
        self._ecrire_vignette_camera(resultat["camera"], resultat["corps"])
        return cible

    def _ecrire_vignette_camera(self, identity: str, corps: bytes) -> None:
        """Same atomic replacement as send_camera_thumb, reusable to republish
        an image already at hand without going back through Blink."""
        cache = self.paths["thumbs"] / "cameras" / f"{safe_file(identity)}.jpg"
        cache.parent.mkdir(parents=True, exist_ok=True)
        attente = cache.with_suffix(".tmp.jpg")
        attente.write_bytes(corps)
        attente.replace(cache)

    async def _actualiser_media_camera(self, blink, sync, camera) -> None:
        """Reread the photo URL from Blink's current state."""
        await blink.get_homescreen()
        info = await sync.get_camera_info(
            camera.camera_id, unique_info=sync.get_unique_info(camera.name))
        if not isinstance(info, dict) or not info.get("thumbnail"):
            raise RuntimeError(msg("snapshot_image_indisponible"))
        await camera.update_images(info)

    def lister_snapshots(self) -> list:
        """Newest first; no separate registry, the file name (UTC timestamp)
        already suffices to sort and the folder contents to enumerate, as
        for assembled videos."""
        racine = self.paths["snapshots"]
        if not racine.is_dir():
            return []
        entrees = []
        for dossier_camera in racine.iterdir():
            if not dossier_camera.is_dir():
                continue
            for fichier in dossier_camera.glob("*.jpg"):
                if not fichier.is_file():
                    continue
                entrees.append({
                    "camera": dossier_camera.name,
                    "fichier": f"{dossier_camera.name}/{fichier.name}",
                    "horodatage": fichier.stem,
                })
        entrees.sort(key=lambda entree: entree["horodatage"], reverse=True)
        return entrees

    def gerer_webhook_snapshot(self) -> None:
        """Trigger a snapshot from outside (GitHub issue #9), authenticated by
        a dedicated secret rather than the session token or origin: a third
        party (smarthome) has neither, and cannot set a custom header on a
        plain webhook URL — so the secret travels as a parameter, as the
        session token already does for <video>/<img> (see jeton_valide())."""
        requete = parse_qs(urlparse(self.path).query)
        fourni = (requete.get("token") or [""])[0]
        attendu = runtime.lire_jeton_webhook()
        if not attendu or not hmac.compare_digest(fourni, attendu):
            self.send_error(403)
            return
        camera = (requete.get("camera") or [""])[0].strip()
        if not camera:
            self.send_json({"error": msg("webhook_camera_manquante")}, 400)
            return
        try:
            chemin = self.declencher_snapshot(camera)
            # "chemin", not "fichier": same key and same form (full
            # absolute path) as the outbound webhook (notifier_nouveau_media),
            # so the same home-automation script can read both the
            # same way (issue #16: the response previously only gave the
            # relative path under Pictures/, under a different key).
            self.send_json({"ok": True, "chemin": str(chemin)})
        except blink_engine.BusyError as erreur:
            self.send_json({"error": str(erreur)}, 409)
        except Exception as erreur:
            self.send_json({"error": f"{type(erreur).__name__}: {erreur}"}, 503)

    def _secret_webhook_valide(self, requete: dict) -> bool:
        """Meme secret que le webhook de photo, compare en temps constant (en
        octets : un jeton non ASCII ne doit pas lever TypeError)."""
        fourni = (requete.get("token") or [""])[0].encode("utf-8")
        attendu = runtime.lire_jeton_webhook().encode("utf-8")
        return bool(attendu) and hmac.compare_digest(fourni, attendu)

    def _etat_pour_webhook(self):
        """system_state() ou (None, message) : jamais une exception."""
        try:
            etat = self.system_state()
        except Exception as erreur:
            return None, f"{type(erreur).__name__}: {erreur}"
        if isinstance(etat, dict) and etat.get("error"):
            return None, str(etat["error"])
        return etat, ""

    def gerer_webhook_status(self) -> None:
        """Etat des cameras en JSON pour un script (issue #40) : en ligne,
        armee, batterie, temperature, wifi, firmware, modele. Meme authentification
        que gerer_webhook_snapshot(), avant les gardes-fous du navigateur."""
        requete = parse_qs(urlparse(self.path).query)
        if not self._secret_webhook_valide(requete):
            self.send_error(403)
            return
        camera = (requete.get("camera") or [""])[0].strip()
        etat, erreur = self._etat_pour_webhook()
        if etat is None:
            self.send_json({"error": erreur}, 503)
            return
        reponse = etat_webhook(etat, camera)
        if reponse is None:
            self.send_json({"error": msg("webhook_camera_inconnue", camera=camera)}, 404)
            return
        self.send_json(reponse)

    def gerer_webhook_arm(self) -> None:
        """Arme ou desarme une camera ou un systeme depuis un script (issue
        #40) : /webhook/arm?camera=<nom>|system=<nom>&armed=true|false&token=...

        Une camera hors ligne n'est pas tentee : la reponse rend son etat et une
        erreur nette plutot que d'attendre le delai d'une commande qui ne
        reviendra pas. Chaque changement est journalise (armement-webhook.log)
        avec l'adresse de l'appelant : desarmer des cameras a distance est plus
        sensible qu'une photo."""
        if self.command == "POST":
            try:
                longueur = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                longueur = 0
            if longueur > _CORPS_WEBHOOK_MAX:
                self.close_connection = True
            elif longueur > 0:
                self.rfile.read(longueur)
        requete = parse_qs(urlparse(self.path).query)
        if not self._secret_webhook_valide(requete):
            self.send_error(403)
            return
        # Several cameras at once by repeating the parameter (camera=A&camera=B,
        # issue #40); duplicates are armed only once.
        cameras = list(dict.fromkeys(
            nom.strip() for nom in requete.get("camera") or [] if nom.strip()))
        systeme = (requete.get("system") or [""])[0].strip()
        if bool(cameras) == bool(systeme):
            self.send_json({"error": msg("webhook_cible")}, 400)
            return
        voulu = _booleen_webhook((requete.get("armed") or [""])[0])
        if voulu is None:
            self.send_json({"error": msg("webhook_armed_invalide")}, 400)
            return
        etat, erreur = self._etat_pour_webhook()
        if etat is None:
            self.send_json({"error": erreur}, 503)
            return
        if systeme:
            corps, code = self._armer_une_cible(etat, "system", systeme, voulu)
            self.send_json(corps, code)
            return
        if len(cameras) == 1:
            corps, code = self._armer_une_cible(etat, "camera", cameras[0], voulu)
            self.send_json(corps, code)
            return
        # One response per camera: an offline or unknown camera does not block
        # the others. Global "ok" is true only if all succeeded.
        resultats = []
        for nom in cameras:
            corps, code = self._armer_une_cible(etat, "camera", nom, voulu)
            resultat = {"name": nom, "status_code": code}
            resultat.update(corps)
            resultat["ok"] = code == 200
            resultats.append(resultat)
        self.send_json({"ok": all(r["ok"] for r in resultats), "scope": "cameras",
                        "requested": voulu, "cameras": resultats})

    def _armer_une_cible(self, etat: dict, portee: str, nom: str, voulu: bool):
        """Arm or disarm a named camera or system; return (response, HTTP code).

        `changed` says whether blink2video's known state before the call differed
        from the request; `applied`, whether the state reread after the call
        matches the request (for a camera, blink2video follows the command until
        Blink confirms it)."""
        if portee == "camera":
            candidats = [(s, c) for s in etat.get("systems") or [] for c in s.get("cameras") or []
                         if str(c.get("name") or "").strip() == nom]
        else:
            candidats = [(s, None) for s in etat.get("systems") or []
                         if str(s.get("name") or "").strip() == nom]
        if not candidats:
            cle = "webhook_camera_inconnue" if portee == "camera" else "webhook_systeme_inconnu"
            return {"error": msg(cle, camera=nom, systeme=nom)}, 404
        if len(candidats) > 1:
            return {"error": msg("webhook_nom_ambigu", nom=nom)}, 409
        systeme_trouve, camera_trouvee = candidats[0]
        if camera_trouvee is not None and camera_trouvee.get("offline"):
            return {
                "error": msg("webhook_camera_hors_ligne", camera=nom),
                "camera": _camera_pour_webhook(camera_trouvee, systeme_trouve.get("name")),
            }, 409
        cible = camera_trouvee or systeme_trouve
        avant = cible.get("armed")
        try:
            self.set_armed(portee, cible["key"], voulu)
        except RuntimeError as erreur_blink:
            return {"error": str(erreur_blink)}, 503
        except Exception as erreur_blink:
            return {"error": f"{type(erreur_blink).__name__}: {erreur_blink}"}, 503
        horodatage = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        client = self.client_address[0] if getattr(self, "client_address", None) else "?"
        runtime.ajouter_ligne(
            "armement-webhook.log",
            f"{horodatage} {portee} \u00ab {nom} \u00bb armed={str(voulu).lower()} depuis {client}")
        reponse = {"ok": True, "scope": portee, "requested": voulu,
                   "changed": None if avant is None else bool(avant) != voulu}
        apres, _erreur = self._etat_pour_webhook()
        if apres is not None:
            if portee == "camera":
                lu = etat_webhook(apres, nom)
                if lu:
                    reponse["camera"] = lu["systems"][0]["cameras"][0]
                    reponse["applied"] = reponse["camera"]["armed"] == voulu
            else:
                courant = next((s for s in apres.get("systems") or []
                                if str(s.get("name") or "").strip() == nom), None)
                if courant is not None:
                    reponse["system"] = {"name": courant.get("name"), "armed": courant.get("armed")}
                    reponse["applied"] = courant.get("armed") == voulu
        return reponse, 200

    def send_camera_thumb(self, identity: str, refresh: bool = False) -> None:
        """Serve a camera's last known thumbnail.

        It replaces the black frame before starting a live view: you see at
        once what the camera is looking at, including those out of range whose
        live view will fail. It is the image Blink keeps on its side, not a
        fresh capture: requesting that would wake the camera and drain its
        battery on every page display.

        An explicit refresh request rereads the image Blink kept. On failure,
        the last local image remains available."""
        cached = (self.paths["thumbs"] / "cameras" / f"{safe_file(identity)}.jpg")
        if refresh or not (cached.is_file() and cached.stat().st_size > 0):
            def fetch(blink):
                async def run(_blink=blink):
                    sync, camera = BLINK.find_camera(_blink, identity)
                    await self._actualiser_media_camera(_blink, sync, camera)
                    response = await camera.get_media()
                    if response is None or response.status != 200:
                        return b""
                    return await response.read()
                return run()
            try:
                body = BLINK.call(fetch, timeout=45)
            except Exception as error:
                print(msg("vignette_erreur", identity=identity,
                          type=type(error).__name__, erreur=error), flush=True)
                body = b""
            if not body and not cached.is_file():
                self.send_error(404)
                return
            if body:
                self._ecrire_vignette_camera(identity, body)

        body = cached.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def live_failure_reason(self, stream) -> str:
        """Ask Blink why no image is arriving.

        The command-follow-up response carries a readable reason ("Live view
        failed" when the camera is out of range or asleep). blinkpy logs it
        but does not surface it: so we query ourselves."""
        if stream is None:
            return "The camera sent no image."
        def ask(blink):
            async def run(_blink=blink):
                from blinkpy import api
                return await api.request_command_status(
                    _blink, stream.camera.network_id, stream.command_id
                )
            return run()
        try:
            status = BLINK.call(ask, timeout=30) or {}
        except Exception:
            return "The camera sent no image."

        message = str(status.get("status_msg") or "").strip()
        commands = status.get("commands") or []
        condition = ""
        for command in commands:
            if command.get("id") == stream.command_id:
                condition = str(command.get("state_condition") or "")
        if message.lower() == "live view failed" or condition == "error":
            return ("The camera did not respond (Blink reports « "
                    f"{message or condition} »). Out of range of the module, "
                    "asleep, or already busy with another session.")
        return message or "The camera sent no image."

    def send_attente_module(self) -> None:
        """Wait until MODULE_SLOT is actually free, without holding it.

        Triggers nothing: watchLive() (serve_app.js) has already requested
        stop of the active session (stopWatch(), pc.close()/fetch abort)
        before this call — asking again here would be redundant. This route's
        role is only to say WHEN that cleanup, already in progress, finishes,
        rather than letting the client guess a delay (blind 10s, replaced by
        this on 2026-09-03 — BACKLOG.md: measured 8.8s then 1.65s on two real
        switches, far too variable for a fixed delay). What really takes time
        is outside this server: blinkpy (LiveStreamAPI.poll(), livestream.py)
        only sends the "done" command to Blink once it has seen the TCP
        connection to the Blink relay itself end — already fully awaited by
        _stop_stream before MODULE_SLOT is released, so nothing to speed up
        here, only to make visible."""
        libre = MODULE_SLOT.acquire(blocking=True, timeout=ATTENTE_MODULE_MAX_SECONDS)
        if libre:
            MODULE_SLOT.release()
            self.send_json({"libre": True})
        else:
            self.send_json({"libre": False, "error": _slot_occupe_message()})

    def send_offer_webrtc(self, name: str, payload: dict) -> None:
        """An attempt owns its wake-up, its pc and a unique cleanup."""
        offer_sdp = str(payload.get("sdp") or "")
        offer_type = str(payload.get("type") or "")
        session_id = str(payload.get("session_id") or "")
        essai = payload.get("essai")
        if not offer_sdp or offer_type != "offer" or not DIRECT_SESSION_ID.fullmatch(session_id):
            self.send_json({"error": "Invalid SDP offer or session identifier."}, 400)
            return

        if not MODULE_SLOT.acquire(blocking=False):
            message = _slot_occupe_message()
            _memoriser_erreur_direct(name, message, 409)
            self.send_json({"error": message}, 409)
            return

        holder: dict = {"session_id": session_id, "arret": threading.Event(),
                        "rendu": False, "nettoyage": None}

        def journal(message):
            _journal_direct(name, f"session {session_id[:8]}, attempt {essai}: {message}")

        def _rendre_verrous() -> None:
            # Never release the semaphore twice, even if HTTP and a
            # pc callback both request cleanup.
            with DIRECT_WEBRTC_SESSION_LOCK:
                if holder["rendu"]:
                    return
                holder["rendu"] = True
            verrou = holder.get("lock")
            try:
                if verrou is not None and holder.get("lock_entered"):
                    verrou.__exit__(None, None, None)
            except Exception as error:
                journal(f"failed to release disk lock, {type(error).__name__}: {error}")
            finally:
                with DIRECT_WEBRTC_SESSION_LOCK:
                    if DIRECT_WEBRTC_SESSION.get("session") is holder:
                        DIRECT_WEBRTC_SESSION.clear()
                    ENREGISTREMENT_DIRECT_ACTIF.clear()
                    try:
                        _slot_rendu()
                    finally:
                        MODULE_SLOT.release()

        async def _nettoyer() -> None:
            try:
                stream = holder.get("stream")
                if stream is not None:
                    journal(await _stop_stream(stream, holder.get("feed")))
            except Exception as error:
                journal(f"failed to close Blink, {type(error).__name__}: {error}")
            finally:
                _rendre_verrous()

        async def _fermer() -> None:
            # All callers await the same task. Their cancellation
            # cuts neither done nor lock release.
            if holder["nettoyage"] is None:
                holder["nettoyage"] = asyncio.ensure_future(_nettoyer())
            await asyncio.shield(holder["nettoyage"])

        def _interrompre() -> None:
            # Run on BLINK.loop, without going through BLINK.call: the latter
            # may be waiting on the wake-up the user is cancelling.
            if holder["nettoyage"] is not None or holder["rendu"]:
                return
            pc = holder.get("pc")
            if pc is not None:
                asyncio.ensure_future(blink_webrtc.fermer_connexion(pc))
            else:
                task = holder.get("demarrage")
                if task is not None:
                    task.cancel()

        holder["interrompre"] = _interrompre
        with DIRECT_WEBRTC_SESSION_LOCK:
            _purger_arrets_direct()
            annulee = session_id in DIRECT_ARRETS_RECENTS
            if not annulee:
                DIRECT_WEBRTC_SESSION["session"] = holder
        if annulee:
            MODULE_SLOT.release()
            self.send_json({"error": "Attempt cancelled."}, 409)
            return

        def _attendre_nettoyage_http():
            loop = holder.get("loop")
            if loop is None:
                _rendre_verrous()
            else:
                # No reconnect/authentication required to close.
                future = asyncio.run_coroutine_threadsafe(_fermer(), loop)
                future.result(timeout=45)

        try:
            _slot_pris("direct WebRTC", name)
            journal("WebRTC live starting")
            ENREGISTREMENT_DIRECT_ACTIF.clear()
            _effacer_erreur_direct()
            holder["lock"] = blink_engine.hub_lock("direct", attente=ATTENTE_HUB_MAX_SECONDS)
            holder["lock"].__enter__()
            holder["lock_entered"] = True
            if holder["arret"].is_set():
                raise concurrent.futures.CancelledError()

            def start(blink):
                async def run(_blink=blink):
                    holder["loop"] = asyncio.get_running_loop()
                    holder["demarrage"] = asyncio.current_task()
                    try:
                        if holder["arret"].is_set():
                            raise asyncio.CancelledError()
                        _, camera = BLINK.find_camera(_blink, name)

                        async def ouvrir():
                            t_flux = time.monotonic()
                            try:
                                stream = await camera.init_livestream()
                            except KeyError as error:
                                raise RuntimeError(
                                    "Blink provided no stream address. "
                                    "The camera is unavailable or the module is busy."
                                ) from error
                            except NotImplementedError as error:
                                raise RuntimeError(f"Unsupported live format ({error}).") from error
                            holder["stream"] = stream
                            journal(f"init_livestream en {time.monotonic() - t_flux:.1f}s")
                            t_start = time.monotonic()
                            await stream.start()
                            journal(f"stream.start() en {time.monotonic() - t_start:.1f}s")
                            return stream

                        stream = await asyncio.wait_for(ouvrir(), timeout=BLINK_OPEN_MAX_SECONDS)
                        holder["feed"] = asyncio.ensure_future(stream.feed())
                        negociation = asyncio.ensure_future(blink_webrtc.negocier(
                            stream.url, offer_sdp, offer_type, _fermer,
                            ffmpeg=self.ffmpeg,
                            chemin_enregistrement=lambda: _chemin_enregistrement_direct(camera.name),
                            enregistrement_actif=ENREGISTREMENT_DIRECT_ACTIF.is_set,
                            journal=journal,
                        ))
                        try:
                            terminees, _ = await asyncio.wait(
                                {negociation, holder["feed"]},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if negociation not in terminees and holder["nettoyage"] is None:
                                # feed.auth can fail before its finally
                                # upstream, leaving the local TCP open. Do
                                # not wait 40 s for an SPS that will not arrive.
                                raise RuntimeError("The Blink relay stopped before the first frame.")
                            # A failed negotiation closes feed itself
                            # via on_close before raising its exception.
                            # Waiting for this cleanup preserves the real cause.
                            pc, answer_sdp, answer_type = await negociation
                        finally:
                            if not negociation.done():
                                negociation.cancel()
                                await asyncio.gather(negociation, return_exceptions=True)
                        holder["pc"] = pc
                        if holder["arret"].is_set() or holder["rendu"]:
                            await blink_webrtc.fermer_connexion(pc)
                            raise asyncio.CancelledError()
                        return answer_sdp, answer_type
                    except BaseException:
                        await _fermer()
                        raise
                    finally:
                        holder["demarrage"] = None
                return run()

            answer_sdp, answer_type = BLINK.call(start, timeout=WEBRTC_START_MAX_SECONDS)
            journal("WebRTC live negotiated")
        except Exception as error:
            message = "Attempt cancelled." if isinstance(error, concurrent.futures.CancelledError) else (
                str(error) if isinstance(error, RuntimeError) else f"{type(error).__name__}: {error}"
            )
            _memoriser_erreur_direct(name, message, 503)
            journal(f"failed (webrtc), {message}")
            try:
                _attendre_nettoyage_http()
            except Exception as error:
                journal(f"nettoyage encore en cours, {type(error).__name__}: {error}")
            try:
                self.send_json({"error": message}, 503)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return

        try:
            self.send_json({"sdp": answer_sdp, "type": answer_type, "session_id": session_id})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            _demander_arret_direct(session_id)

    def send_live_mse(self, name: str, session_id: str = "") -> None:
        """Stream a camera's live view as fragmented fMP4, for MediaSource.

        Versus /live (MJPEG): instead of having ffmpeg re-encode each frame
        to JPEG then an <img> re-decode it, we only change container
        (Annex B -> fragmented MP4 boxes), without touching the image. An
        ffmpeg subprocess does the remux, exactly like /live: a first version
        went through PyAV reading the Blink stream directly, simpler for
        reading the exact codec, but clearly less reliable on this precise
        stream (real camera trials) than the ffmpeg subprocess /live has used
        successfully from the start. The cost: reading the codec requires
        walking the MP4 init-segment boxes (moov > trak > mdia > minf > stbl >
        stsd > avc1 > avcC) rather than reading it via an API.

        Also triggers live recording into Blink_Direct (see
        _chemin_enregistrement_direct): the same bytes sent to the browser
        are duplicated to a file, best-effort — a failure never interrupts
        the displayed live view, see _ecrire below.

        `session_id` (optional, old client or bare test): registered in
        DIRECT_MSE_SESSION so /api/arreter-direct can interrupt the wait
        below without depending on passive closed-connection detection,
        which only happens on the next write — never if the camera stays
        mute (real bug: the module stayed busy until LIVE_MAX_SECONDS after
        a tab close during silence)."""
        if not DIRECT_SESSION_ID.fullmatch(session_id):
            # Graceful degradation rather than refusal: an old client or
            # a malformed id just loses the benefit of explicit
            # stop; the live view itself does not need session_id.
            session_id = ""
        if not MODULE_SLOT.acquire(blocking=False):
            message = _slot_occupe_message()
            _memoriser_erreur_direct(name, message, 409)
            _journal_direct(name, f"refused (MSE), {message}")
            # The detail may contain a non-latin-1 camera name or a
            # de ligne. Il reste disponible via /api/live-error ; la ligne de
            # HTTP status itself must always stay ASCII and well-formed.
            self.send_error(409, "Live stream busy")
            return

        holder: dict = {"session_id": session_id, "arret": threading.Event()}
        if session_id:
            with DIRECT_WEBRTC_SESSION_LOCK:
                DIRECT_MSE_SESSION["session"] = holder
        errors: list = []
        erreur_direct = None
        reponse_commencee = False
        journaux_nettoyage: list = []
        try:
            _slot_pris("direct MSE", name)
            ENREGISTREMENT_DIRECT_ACTIF.clear()
            # A truly admitted attempt replaces the old diagnostic.
            # In particular, a 409 must never make the browser reread
            # the 503 of a previous attempt on the same camera.
            _effacer_erreur_direct()
            holder["lock"] = blink_engine.hub_lock("direct", attente=ATTENTE_HUB_MAX_SECONDS)
            holder["lock"].__enter__()
            holder["lock_entered"] = True

            def start(blink):
                async def run(_blink=blink):
                    sync, camera = BLINK.find_camera(_blink, name)
                    # Timed like send_offer_webrtc (same opacity:
                    # init_livestream()/stream.start() sont de vrais appels
                    # to Blink cloud, the slowest and until now least
                    # visible part of this whole path — see its note,
                    # 2026-09-04).
                    t_flux = time.monotonic()
                    try:
                        stream = await camera.init_livestream()
                    except KeyError as error:
                        raise RuntimeError(
                            f"Blink provided no stream address for "
                            f"« {name} ». The camera is probably out of "
                            f"range of the Sync Module, powered off, or "
                            f"its battery is empty."
                        ) from error
                    except NotImplementedError as error:
                        raise RuntimeError(
                            f"This camera streams in an unsupported "
                            f"format ({error})."
                        ) from error
                    _journal_direct(
                        name, f"init_livestream en "
                              f"{time.monotonic() - t_flux:.1f}s"
                    )
                    # init_livestream() has already created a remote command. If
                    # start() fails, finally must still find this
                    # stream and send stop/done to Blink.
                    holder["stream"] = stream
                    # camera.name, not `name`: `name` here is the
                    # opaque camera_key() (stable across a rename,
                    # see its docstring), not the display name. Passed via
                    # holder rather than a direct closure, like
                    # "stream"/"feed": run() runs on BLINK.loop, this name
                    # must survive until _ecrire (much further down, outside
                    # this coroutine) to name the recording file.
                    holder["camera_name"] = camera.name
                    t_start = time.monotonic()
                    await stream.start()
                    _journal_direct(
                        name, f"stream.start() en "
                              f"{time.monotonic() - t_start:.1f}s"
                    )
                    holder["feed"] = asyncio.ensure_future(stream.feed())
                    return stream.url
                return run()

            url = BLINK.call(start, timeout=45)
            _journal_direct(name, f"flux Blink ouvert sur {url}")

            process = runtime.demarrer(
                # Temporary loglevel "info" (shared relay/ffmpeg diagnostic
                # in progress, see direct.log): switch back to "error" once
                # the Terrasse1/Salon gap is explained.
                [self.ffmpeg, "-hide_banner", "-loglevel", "info",
                 "-fflags", "nobuffer", "-flags", "low_delay",
                 # More generous than MJPEG (500000/300000): a frozen moov
                 # (empty_moov) must know dimensions *before*
                 # writing its header, while MJPEG decoding learns them
                 # on the fly without ever having to commit
                 # ahead of time. Seen in practice on a slower-to-deliver camera
                 # delivering its SPS (jardin): "dimensions not set / Could not
                 # write header" on the ffmpeg side, never on MJPEG with the
                 # same settings on the same camera. Still well
                 # under LIVE_FIRST_FRAME_SECONDS (40 s): a camera that
                 # responds quickly does not wait longer for that reason;
                 # these are only ceilings.
                 #
                 # Ongoing trial (direct.log, 2026-09-02): at 5000000, this
                 # ceiling is systematically almost fully consumed
                 # (~5-6 s) on Salon AND Terrasse1, two cameras unrelated
                 # to the original slow case (jardin): not just used
                 # when needed, always exhausted. Lowered back to 1500000
                 # (3x the old MJPEG threshold, 1/3 of the current) to see if
                 # jardin still holds without "dimensions not set". To
                 # raise again if jardin fails with this value.
                 "-analyzeduration", "1500000", "-probesize", "1500000",
                 "-i", url,
                 # copy: remux without re-encode, near-zero CPU cost.
                 "-c:v", "copy", "-an",
                 "-f", "mp4",
                 "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                 "-"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            holder["process"] = process

            def _drainer_stderr() -> None:
                # One line at a time (not a single blocking read until
                # the end): temporary diagnostic to date each internal
                # ffmpeg step (stream detection, keyframe...) while we
                # look into why Terrasse1 (12 s) exceeds Salon (8 s) between
                # the first relay byte and the init segment. errors
                # stays fed for _texte_stderr_ffmpeg (reason/end
                # diagnostic), otherwise unchanged.
                for ligne_brute in iter(process.stderr.readline, b""):
                    errors.append(ligne_brute)
                    texte = ligne_brute.decode("utf-8", "replace").rstrip()
                    if texte:
                        _journal_direct(name, f"ffmpeg : {texte}")

            holder["drain"] = threading.Thread(target=_drainer_stderr, daemon=True)
            holder["drain"].start()

            lecteur = LecteurTube(process.stdout)
            first = read_mp4_init_segment(lecteur, LIVE_FIRST_FRAME_SECONDS)
            _journal_direct(name, f"segment initial {len(first)} octets")
            if not first:
                reason = self.live_failure_reason(holder.get("stream"))
                # ffmpeg may have stopped before any usable byte (not
                # necessarily an out-of-range camera): its message, when it
                # there is one, often says more than Blink status alone. The
                # reader thread has only that to do since process
                # subprocess: if it has not finished, ffmpeg is still running.
                drain = holder.get("drain")
                if drain is not None:
                    drain.join(timeout=2)
                trace = _texte_stderr_ffmpeg(errors)
                if trace:
                    reason = f"{reason} | ffmpeg : {trace}"
                raise RuntimeError(reason)
            codec_str = h264_mime_codec_from_moov(first)
            # Pre-fed with `first` (verdict ignored: see _ecrire, this
            # first block is always written in full anyway)
            # so its box-size tracking stays synchronized on
            # the whole stream from its origin, `first` possibly already containing
            # more than just the init segment (see its docstring).
            detecteur_fragments = DetecteurDebutFragment()
            detecteur_fragments.bloc_ouvre_un_fragment(first)

            # From this point, a second HTTP status line cannot
            # be sent cleanly, even if end_headers() fails.
            reponse_commencee = True
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Codec", codec_str)
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            _journal_direct(name, f"en-tetes envoyes, codec {codec_str}")

            def _ecrire(data: bytes) -> bool:
                # Same tolerance on this first send as on later ones:
                # a tab already closed when this block leaves must not surface
                # as a failure (seen in practice: ConnectionAbortedError on this
                # very first write, even though that is a normal end).
                #
                # Driven by the button (ENREGISTREMENT_DIRECT_ACTIF, see
                # /api/direct-enregistrement): three states, never mixed —
                # start (opens a new file, timestamped at this instant, and
                # first writes `first` into it: without the MP4 init
                # segment at the head, the file would not be readable), stop
                # (closes, writes nothing more of this packet), steady state
                # (writes `data` normally). A failure here (disk full...)
                # stops recording only, never the on-screen live view.
                fichier_direct = holder.get("fichier_direct")
                desire = ENREGISTREMENT_DIRECT_ACTIF.is_set()
                # `data` must feed the detector even when desire is
                # still false or a file is already open: that is the
                # price for its box-size tracking to stay
                # synchronized on the whole stream (see its docstring). Not for
                # `first`, already pre-fed before the loop.
                fragment_complet = (
                    data is first or detecteur_fragments.bloc_ouvre_un_fragment(data)
                )
                if desire and fichier_direct is None and fragment_complet:
                    # A start requested mid-live (not from the
                    # first block) waits for this fragment rather than writing
                    # `data` as-is: blocks read from ffmpeg (16 KB,
                    # LecteurTube) never align on
                    # moof/mdat boundaries; starting on an arbitrary block could
                    # split an mdat in two — the resulting file, missing its
                    # leading moof, was no longer decodable (observed with
                    # real data: ffmpeg read invalid NAL units).
                    #
                    # holder["camera_name"] (the real name, set by run()
                    # above) rather than `name`: see the note on camera_key()
                    # where holder["camera_name"] is set.
                    chemin = _chemin_enregistrement_direct(
                        holder.get("camera_name", name)
                    )
                    try:
                        chemin.parent.mkdir(parents=True, exist_ok=True)
                        fichier_direct = chemin.open("wb")
                        fichier_direct.write(first)
                        if data is not first:
                            fichier_direct.write(data)
                        holder["fichier_direct"] = fichier_direct
                    except OSError as error:
                        fichier_direct = None
                        _journal_direct(
                            name, f"recording impossible, "
                                  f"{type(error).__name__}: {error}"
                        )
                elif not desire and fichier_direct is not None:
                    try:
                        fichier_direct.close()
                    except OSError as error:
                        _journal_direct(
                            name, f"failed to close recording, "
                                  f"{type(error).__name__}: {error}"
                        )
                    holder["fichier_direct"] = None
                elif fichier_direct is not None:
                    try:
                        fichier_direct.write(data)
                    except OSError as error:
                        holder["fichier_direct"] = None
                        _journal_direct(
                            name, f"recording interrupted, "
                                  f"{type(error).__name__}: {error}"
                        )
                try:
                    self.wfile.write(data)
                    return True
                except (BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError):
                    return False

            if not _ecrire(first):
                holder["sent"] = 0
                return
            sent = len(first)
            # deadline was only checked between two reads: a blocked
            # individual read (ffmpeg going silent without ever closing
            # stdout) never returned, and LIVE_MAX_SECONDS
            # therefore bounded nothing in this case (seen in practice: a live
            # view left open over 600 s, MODULE_SLOT never released). lire()
            # now itself carries a real timeout on each read.
            deadline = time.monotonic() + LIVE_MAX_SECONDS
            while time.monotonic() < deadline:
                if holder["arret"].is_set():
                    break  # /api/arreter-direct: do not wait for a block that may never come
                chunk = lecteur.lire(
                    min(LIVE_MSE_ARRET_POLL_SECONDS, deadline - time.monotonic())
                )
                if chunk is None:
                    continue  # just slow: the global deadline has not elapsed yet
                if not chunk:
                    break
                if not _ecrire(chunk):
                    break  # tab closed: that is the normal end of a live view
                sent += len(chunk)
            holder["sent"] = sent
        except Exception as error:
            erreur_direct = str(error) if isinstance(error, RuntimeError) \
                else f"{type(error).__name__}: {error}"
        finally:
            if session_id:
                with DIRECT_WEBRTC_SESSION_LOCK:
                    if DIRECT_MSE_SESSION.get("session") is holder:
                        DIRECT_MSE_SESSION.clear()
            process = holder.get("process")
            if process is not None:
                try:
                    process.terminate()
                except Exception as error:
                    journaux_nettoyage.append(
                        f"failed to terminate ffmpeg, {type(error).__name__}: {error}"
                    )
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except Exception as error:
                        journaux_nettoyage.append(
                            f"failed to kill ffmpeg, {type(error).__name__}: {error}"
                        )
                except Exception as error:
                    journaux_nettoyage.append(
                        f"failed waiting for ffmpeg, {type(error).__name__}: {error}"
                    )
            fichier_direct = holder.get("fichier_direct")
            if fichier_direct is not None:
                try:
                    fichier_direct.close()
                except OSError as error:
                    journaux_nettoyage.append(
                        f"failed to close recording, "
                        f"{type(error).__name__}: {error}"
                    )
            stream = holder.get("stream")
            if stream is not None:
                try:
                    verdict = BLINK.call(
                        lambda _b: _stop_stream(stream, holder.get("feed")),
                        timeout=45,
                    )
                except Exception as error:
                    verdict = f"failed to close, {type(error).__name__}: {error}"
                journaux_nettoyage.append(verdict)
            verrou = holder.get("lock")
            if verrou is not None and holder.get("lock_entered"):
                try:
                    verrou.__exit__(None, None, None)
                except Exception as error:
                    journaux_nettoyage.append(
                        f"failed to release disk lock, "
                        f"{type(error).__name__}: {error}"
                    )
            # Publishing the failure before releasing the token prevents a new
            # tentative d'effacer LAST_LIVE_ERROR puis de voir cet ancien
            # diagnostic from reappearing under its feet. The HTTP response stays
            # deferred until after the token is released.
            if erreur_direct is not None:
                try:
                    _memoriser_erreur_direct(name, erreur_direct, 503)
                except Exception as error:
                    journaux_nettoyage.append(
                        f"failure diagnosis unavailable, "
                        f"{type(error).__name__}: {error}"
                    )
            # These two memory releases are the last belt: no
            # ffmpeg/Blink/disk-lock close error, nor any
            # diagnostic, must be able to skip them.
            try:
                _slot_rendu()
            finally:
                MODULE_SLOT.release()

            # Diagnostic only after releasing all resources:
            # its read waits on a thread and previously treated bytes as str,
            # what could raise here and leave the module busy forever.
            sent = holder.get("sent")
            if sent is not None:
                try:
                    detail = ""
                    if not sent:
                        drain = holder.get("drain")
                        if drain is not None:
                            drain.join(timeout=3)
                        detail = " | ffmpeg : " + (
                            _texte_stderr_ffmpeg(errors) or "rien"
                        )
                    journaux_nettoyage.append(
                        f"termine, {sent} octets transmis{detail}"
                    )
                except Exception as error:
                    journaux_nettoyage.append(
                        f"final diagnosis unavailable, {type(error).__name__}: {error}"
                    )
            for journal in journaux_nettoyage:
                _journal_direct(name, journal)

        if erreur_direct is not None:
            # The client only sees this 503 once ffmpeg is stopped, the
            # Blink session closed and locks released. Its next attempt
            # can therefore no longer spend its retries on our own 409s.
            _journal_direct(name, f"failed, {erreur_direct}")
            if not reponse_commencee:
                try:
                    self.send_error(503, "Live stream unavailable")
                except Exception:
                    pass

    def resolve_media(self, route: str) -> Path | None:
        """Translate "clip/..." or "daily/..." into a real file, or nothing.

        Two independent checks: path shape, then that it falls under the
        announced root once resolved. The second is the one that matters; it
        neutralizes ".." whatever their spelling."""
        kind, _, relative = route.partition("/")
        if not relative or not IDENTITY.match(relative):
            return None

        if kind == "clip":
            if relative not in known_identities(self.paths):
                return None
            roots = [self.paths[name] for name in ("normalized", "input", "excluded")]
        elif kind in ("daily", "weekly", "monthly", "direct"):
            roots = [self.paths[kind]]
        else:
            return None

        for root in roots:
            candidate = (root / relative).resolve()
            if runtime.est_relatif_a(candidate, root.resolve()) and candidate.is_file():
                return candidate
        return None

    def resolve_snapshot(self, relative: str) -> Path | None:
        """Same safeguard as resolve_media() (well-formed path, then falls under
        the root once resolved), for "camera/file.jpg": a single root, no
        identity to check against a registry — an on-demand photo has none."""
        if not relative or not IDENTITY_SNAPSHOT.match(relative):
            return None
        root = self.paths["snapshots"]
        candidate = (root / relative).resolve()
        if runtime.est_relatif_a(candidate, root.resolve()) and candidate.is_file():
            return candidate
        return None

    # ------------------------------------------------------------------ routes

    def do_GET(self):
        # try/except: self.path comes as-is from the request line,
        # under full client control. urlparse raises on some
        # clearly invalid forms (badly closed IPv6, e.g. "//[abc") — same
        # trap already audited and fixed for Origin in hote_autorise() and
        # for the outbound webhook URL. On recent Python (3.9+, see
        # gh-87389) a "//..." path is already normalized before reaching
        # urlparse and this case no longer appears; the Windows 7 build
        # (pinned Python 3.8.10) lacks that stdlib fix, hence the
        # safety net here rather than relying on it.
        try:
            route = urlparse(self.path).path
        except ValueError:
            self.send_error(400)
            return
        if route == WEBHOOK_SNAPSHOT_ROUTE:
            # Deliberately BEFORE hote_autorise()/jeton_valide(): those are
            # the browser safeguards (same origin, same machine),
            # irrelevant for a legitimate external call (smarthome, issue
            # GitHub #9). gerer_webhook_snapshot() a son propre secret,
            # independent, never examined by those two functions.
            self.gerer_webhook_snapshot()
            return
        if route == WEBHOOK_STATUS_ROUTE:
            self.gerer_webhook_status()
            return
        if route == WEBHOOK_ARM_ROUTE:
            self.gerer_webhook_arm()
            return
        if not self.hote_autorise():
            self.send_error(403)
            return
        if route not in ("/", "/favicon.ico", "/logo.png") and not self.jeton_valide():
            self.send_error(403)
            return
        if route == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # The page is embedded in the script: it changes as soon as one edits
            # serve.py. Without this directive, the browser reuses its copy and
            # one thinks a change is lost when it is actually there.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if route == "/favicon.ico":
            chemin = runtime.resource_dir() / "assets" / "blink2video.ico"
            if not chemin.is_file():
                self.send_error(404)
                return
            corps = chemin.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/x-icon")
            # Static, never changes on the fly: might as well let the
            # browser stop re-requesting it on every page.
            self.send_header("Cache-Control", "public, max-age=604800")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)
            return

        if route == "/logo.png":
            chemin = runtime.resource_dir() / "assets" / "cas-cams-icon.png"
            if not chemin.is_file():
                self.send_error(404)
                return
            corps = chemin.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=604800")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)
            return

        # CSRF token alone is not enough once Cloudflare Access is on:
        # identity (email → allowlist) must resolve for every API/media call.
        if not self.exiger_acces_authentifie():
            return

        if route == "/api/me":
            self.send_json(self.identite_acces())
            return

        if route == "/api/access":
            if not self.exiger_admin():
                return
            self.send_json(access_control.lire_acces())
            return

        if route.startswith("/camthumb/"):
            # No name check here: it would cost a full account
            # refresh on every thumbnail. That is find_camera, below,
            # that rejects an unknown name, and safe_file that sanitizes the
            # fichier du cache.
            nom = unquote(route[len("/camthumb/"):])
            if not self.exiger_camera(nom):
                return
            refresh = parse_qs(urlparse(self.path).query).get("refresh", [""])[0] == "1"
            self.send_camera_thumb(nom, refresh=refresh)
            return

        if route.startswith("/live-mse/"):
            nom = unquote(route[len("/live-mse/"):])
            if not self.exiger_camera(nom):
                return
            session_id = parse_qs(urlparse(self.path).query).get("session_id", [""])[0]
            self.send_live_mse(nom, session_id)
            return

        if route == "/api/live-error":
            self.send_json(_derniere_erreur_direct())
            return

        if route == "/api/system":
            try:
                etat = self.system_state()
                self.send_json(access_control.filtrer_system_state(
                    etat, self.identite_acces().get("email") or ""))
            except RuntimeError as error:
                self.send_json({"error": str(error)}, 503)
            return

        if route == "/api/attente-module":
            self.send_attente_module()
            return

        if route == "/api/videos":
            self.send_json(access_control.filtrer_videos(
                collect_videos(self.paths, self.ffmpeg),
                self.identite_acces().get("email") or ""))
            return

        if route == "/api/status":
            self.send_json({
                "authenticated": (runtime.app_dir() / "blink_auth.json").is_file(),
                "initial_setup": self.initial_setup,
                "role": self.identite_acces().get("role"),
                "email": self.identite_acces().get("email"),
            })
            return

        if route == "/api/autostart":
            if not self.exiger_admin():
                return
            self.send_json({"actif": autostart.est_installe()})
            return

        if route == "/api/choisir-dossier":
            if not self.exiger_admin():
                return
            # The browser cannot return an absolute path (File System
            # Access API: just a handle, by design, for the
            # private to the web). Server and page being on the same machine here
            # (local tool, not a remote service), a dialog
            # native fills that gap. PowerShell/WinForms under Windows (see
            # _choisir_dossier_windows); tkinter as fallback elsewhere — imported
            # locally so as never to weigh on environments without
            # a display (headless Linux CI), which never take this
            # route.
            try:
                if os.name == "nt":
                    choisi = _choisir_dossier_windows(runtime.lire_dossier_stockage())
                else:
                    import tkinter
                    from tkinter import filedialog
                    racine = tkinter.Tk()
                    racine.withdraw()
                    racine.attributes("-topmost", True)
                    choisi = filedialog.askdirectory(initialdir=runtime.lire_dossier_stockage())
                    racine.destroy()
            except Exception as error:
                self.send_json({"error": str(error)}, 500)
                return
            self.send_json({"path": choisi or ""})
            return

        if route == "/api/reglages":
            if not self.exiger_admin():
                return
            self.send_json(
                {**runtime.lire_reglages(), "storage_dir": runtime.lire_dossier_stockage(),
                 "initial_setup": self.initial_setup,
                 "webhook_token": runtime.lire_jeton_webhook()})
            return

        if route == "/api/sourdine":
            if not self.exiger_admin():
                return
            # Known cameras come from the clip registry AND from
            # watch.py's last state (local file, no network call to
            # Blink here): a camera long out of range may have
            # never produced a clip and would otherwise stay absent from the list,
            # therefore impossible to mute from this page (case
            # lived: "Portail").
            etat = md.load_json(watch.WATCH_STATE, {})
            surveillees = etat.get("cameras") or {}
            noms_connus = {str(e.get("name") or nom).strip().casefold()
                           for nom, e in surveillees.items()}
            historiques = {nom for nom in provenances(read_entries(self.paths))
                           if nom.strip().casefold() not in noms_connus}
            cameras = sorted(historiques | set(surveillees))
            self.send_json({"cameras": cameras, "ignored": sorted(
                watch.normaliser_sourdines(etat.get("ignored") or [], surveillees))})
            return

        if route == "/api/suppression-auto":
            if not self.exiger_admin():
                return
            # Proposed cameras are those of current activity
            # (watch.py's last state), not the cumulative history of the
            # download registry: a camera whose internal key has
            # changed over time (Sync Module replaced, network_id
            # absent from old USB clips — blinkpy 0.25 does not expose a
            # device_id on USB) would otherwise appear twice, under
            # internal labels unreadable to the user. The
            # name -> key(s) resolution (camera-v2-*, suppression_auto_choices
            # above) remains an internal detail, never exposed here.
            etat = md.load_json(watch.WATCH_STATE, {})
            surveillees = etat.get("cameras") or {}
            noms = sorted(surveillees, key=str.casefold)
            groupes = watch.regrouper_entrees_cameras(surveillees, read_entries(self.paths))
            actives_keys = suppression_auto_keys()
            cameras = []
            actives = []
            for nom in noms:
                cles = [c["key"] for c in suppression_auto_choices(groupes[nom])]
                cameras.append({"name": nom})
                if any(cle in actives_keys for cle in cles):
                    actives.append(nom)
            self.send_json({"cameras": cameras, "actives": actives})
            return

        if route == "/api/clips":
            # ?all=1 explicitly lifts any bound: full history
            # stays one click away, never lost, just not loaded by default
            # (voir DEFAULT_WINDOW_DAYS). ?preset=today|week|month|2months
            # covers the page's quick-range buttons (RANGE_
            # PRESETS_HOURS); ?depuis/?jusqua (ISO, no timezone: read in the
            # configured timezone, like each clip's timestamp) cover its
            # custom range to the hour. With no parameter, the
            # window stays as before those buttons: DEFAULT_WINDOW_DAYS
            # last days, exactly what ?preset=month also returns.
            query = parse_qs(urlparse(self.path).query)
            depuis = jusqua = None
            if query.get("all", ["0"])[0] != "1":
                maintenant = dt.datetime.now(self.timezone)
                preset = (query.get("preset") or [""])[0]
                depuis_brut = (query.get("depuis") or [""])[0]
                jusqua_brut = (query.get("jusqua") or [""])[0]
                if preset in RANGE_PRESETS_HOURS:
                    depuis = maintenant - dt.timedelta(hours=RANGE_PRESETS_HOURS[preset])
                elif depuis_brut or jusqua_brut:
                    try:
                        if depuis_brut:
                            depuis = dt.datetime.fromisoformat(depuis_brut) \
                                .replace(tzinfo=self.timezone)
                        if jusqua_brut:
                            jusqua = dt.datetime.fromisoformat(jusqua_brut) \
                                .replace(tzinfo=self.timezone)
                    except ValueError:
                        self.send_json({"error": "Invalid date range."}, 400)
                        return
                else:
                    depuis = maintenant - dt.timedelta(hours=RANGE_PRESETS_HOURS["month"])
            try:
                data = collect(self.paths, self.timezone, self.ffmpeg, depuis, jusqua)
                self.send_json(access_control.filtrer_clips(
                    data, self.identite_acces().get("email") or ""))
            except RuntimeError as error:
                self.send_json({"error": str(error)}, 500)
            return

        if route.startswith("/thumb/"):
            target = unquote(route[len("/thumb/"):])
            if not self.media_autorise(target):
                self._refuser_acces(403, "Media not allowed for this user.")
                return
            source = self.resolve_media(target)
            if source is None:
                self.send_error(404)
                return
            self.send_thumb(target, source)
            return

        if route.startswith("/media/"):
            target = unquote(route[len("/media/"):])
            if not self.media_autorise(target):
                self._refuser_acces(403, "Media not allowed for this user.")
                return
            path = self.resolve_media(target)
            if path is None:
                self.send_error(404)
                return
            self.send_media(path)
            return

        if route.startswith("/snapshot/"):
            relatif = unquote(route[len("/snapshot/"):])
            camera = relatif.split("/", 1)[0] if relatif else ""
            if not self.exiger_camera(camera):
                return
            path = self.resolve_snapshot(relatif)
            if path is None:
                self.send_error(404)
                return
            self.send_media(path)
            return

        if route == "/api/snapshots":
            self.send_json({
                "snapshots": access_control.filtrer_snapshots(
                    self.lister_snapshots(),
                    self.identite_acces().get("email") or ""),
            })
            return

        if route == "/api/passages":
            # The registry clip count accompanies the hours: the page
            # compares to what it shows and reports the gap, without
            # redrawing anything itself.
            # The published version is read from cache, never from GitHub: the
            # page must not wait on a remote server. A background thread keeps
            # this cache up to date.
            self.send_json({"passages": runtime.passages(),
                            "clips": nombre_clips_connus(read_entries(self.paths)),
                            "maj": maj.disponible(reseau=False)})
            return

        if route == "/api/travail":
            # Frequent and deliberately tiny probe: not rereading here
            # the whole clip registry like /api/passages. On an old
            # Windows 7 PC, parsing this growing JSON every three seconds
            # would cost far more than reading just the progress state.
            self.send_json({"travail": runtime.travail_affichable()})
            return

        if route == "/api/refresh":
            if not self.exiger_admin():
                return
            if self.initial_setup:
                # Server-side defense, not only a modal dialog: even
                # a manual request cannot fetch a clip before the
                # validation du dossier et du fuseau.
                self.send_json({"error": "Validate the initial settings first."}, 409)
                return
            self.stream_refresh()
            return

        self.send_error(404)

    # ------------------------------------------------------- actualisation

    def send_event(self, payload: dict) -> bool:
        """Emit an SSE event. Returns False if the browser has disconnected."""
        body = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        try:
            self.wfile.write(body.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True

    def stream_refresh(self) -> None:
        """Chain download then merge, pushing output as it comes.

        Server-Sent Events rather than a single response at the end: that is
        the browser's mechanism for receiving a message stream (a simple
        data: tag, no protocol to invent), and nothing to install on the page.
        Flow is one-way, server to page, which is enough here: the page has
        nothing to answer.

        Two distinct self_commands ("download" then "merge"), not one verb
        that would chain them: those are the tool's only two hands, and
        calling them one after the other says exactly what is happening.
        See run_refresh()."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        if not self.lock.acquire(blocking=False):
            self.send_event({"line": "A refresh is already in progress."})
            self.send_event({"done": True, "ok": False})
            return
        # A job started by background loops already occupies ffmpeg and the
        # registry. Starting a second would only wait on the lock for
        # de longues minutes, en donnant l'impression d'un blocage.
        en_cours = runtime.travail_en_cours()
        if en_cours:
            self.send_event({"line": (
                f"{en_cours.get('quoi', 'A job')} is already in progress. "
                "Wait for the progress bar to finish, then try again."
            )})
            self.send_event({"done": True, "ok": False})
            self.lock.release()
            return
        # The page refreshes thumbnails at the end of this update.
        try:
            # Download talks to the Sync Module, like live view. Letting
            # them overlap would guarantee a "System is busy": better
            # to say so immediately than fail mid-way.
            if not MODULE_SLOT.acquire(blocking=False):
                self.send_event({"line": _slot_occupe_message()})
                self.send_event({"done": True, "ok": False})
                return
            _slot_pris("actualisation")
            try:
                # The ``download`` subprocess itself takes the
                # hub file lock during its inventory then transfers.
                # Taking it here too, in the parent, made it impossible to
                # reacquire: every "Actualiser" click then skipped the whole
                # USB part as if another program held the module.
                # MODULE_SLOT remains the in-memory guard against a live view started
                # by this server; the child lock covers other processes.
                self.run_refresh()
            except blink_engine.BusyError as error:
                self.send_event({"line": f"Module busy: {error}."})
                self.send_event({"done": True, "ok": False})
            finally:
                _slot_rendu()
                MODULE_SLOT.release()
        finally:
            self.lock.release()

    def run_refresh(self) -> None:
        """Download then assemble, pushing both outputs as they come.

        Two programs chained here rather than a verb that would chain them:
        "download" and "merge" are the tool's only two hands, and calling
        them one after the other says exactly what is happening."""
        auth = BASE_DIR / "blink_auth.json"
        hub_args = ["--hub", self.hub] if self.hub else []
        reglages = runtime.lire_reglages()
        # Same flags as runtime.standard() for the merge loop: a click
        # on Actualiser must produce what the automatic loop would produce
        # on its next turn, not a daily/weekly/monthly by
        # default that ignores the Settings page. As in standard(),
        # merge_jour is the general assembly switch: Actualiser
        # must not implicitly reactivate it.
        options_merge = runtime.options_fusion(reglages)
        etapes = [("Downloading", "phase.step_download",
                  runtime.self_command("download", *hub_args))]
        if reglages["merge_jour"]:
            etapes.append(("Merging", "phase.step_merge",
                           runtime.self_command("merge", *options_merge)))
        if not auth.is_file():
            # Download would ask for email, password and the
            # verification code on standard input, which does not exist here: the
            # process would stay blocked. We say so and settle for
            # assembling what is already there.
            self.send_event({"line": (
                f"Blink session missing ({auth.name}). Run « blink2video login » "
                "in a terminal to sign in. "
                + ("Video reconstruction only." if reglages["merge_jour"]
                   else "Nothing started: merging is disabled.")
            )})
            etapes = etapes[1:]

        # PYTHONUNBUFFERED: without it, output would arrive in
        # several kilobytes and the bar would jump in fits.
        # PYTHONIOENCODING avoids accents mangled by the Windows console.
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        for phase, cle, command in etapes:
            self.send_event({"phase": phase, "phase_key": cle, "line": f"$ {phase.lower()}"})
            if not self.suivre(command, env, phase):
                return
        self.send_event({"done": True, "ok": True})

    def suivre(self, command: list, env: dict, phase: str) -> bool:
        """Advance the bar as lines arrive. False if we must stop."""
        process = runtime.demarrer(
            command, cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1, env=env,
        )
        # Silence watchdog, same threshold and same reason as
        # run_ffmpeg_batch/concat_copy (merge_daily.py, AUDIT-2026-08-13
        # 28.82) : download comme merge peuvent rester silencieux longtemps
        # (Blink network not responding, ffmpeg stuck underneath) without
        # no line arrives on stdout, which would freeze
        # indefinitely the loop below and would keep self.lock held for
        # toute future Actualisation.
        dernier_signe = time.monotonic()
        signe_verrou = threading.Lock()
        termine = threading.Event()
        tue_par_silence = False

        def surveiller() -> None:
            nonlocal tue_par_silence
            while not termine.wait(10):
                with signe_verrou:
                    silence = time.monotonic() - dernier_signe
                if silence > md.SILENCE_MAX:
                    tue_par_silence = True
                    process.kill()
                    return

        chien_de_garde = threading.Thread(target=surveiller, daemon=True)
        chien_de_garde.start()
        try:
            for raw in process.stdout:
                with signe_verrou:
                    dernier_signe = time.monotonic()
                line = raw.rstrip("\n")
                event = {"line": line}
                # Both programs announce their progress in the form
                # "[3/24]". A single reading rule is enough, and each phase
                # repart naturellement de 1.
                counter = PROGRESS.search(line)
                if counter:
                    index, total = int(counter.group(1)), int(counter.group(2))
                    inner = INNER.match(line)
                    fraction = int(inner.group(1)) / 100 if inner else 0.0
                    if inner:
                        event.pop("line")
                    event["progress"] = {
                        "done": round(index - 1 + fraction, 3), "total": total
                    }
                heading = HEADING.match(line)
                if heading:
                    titre = heading.group(1).strip()
                    event["phase"] = titre.capitalize() if titre.isupper() else titre
                    # These two headings are the only ones blink_engine.py emits
                    # in this form (see traiter_cloud/un_passage): a stable
                    # key lets the page translate them, the hub name
                    # (user data, never translatable) passing separately.
                    if titre == "CLOUD DE L'ABONNEMENT":
                        event["phase_key"] = "phase.cloud_section"
                    elif titre.startswith("STOCKAGE LOCAL : "):
                        event["phase_key"] = "phase.usb_section"
                        event["phase_hub"] = titre[len("STOCKAGE LOCAL : "):].strip()
                if not self.send_event(event):
                    process.terminate()
                    process.wait()
                    return False
        finally:
            termine.set()
            chien_de_garde.join(timeout=5)
        process.wait()
        if tue_par_silence:
            self.send_event({"line": f"{phase}: stopped, silent for "
                                      f"more than {int(md.SILENCE_MAX)} s"})
            self.send_event({"done": True, "ok": False})
            return False
        if process.returncode != 0:
            self.send_event({"line": f"{phase}: failed (code {process.returncode})"})
            self.send_event({"done": True, "ok": False})
            return False
        return True

    def _post_reglages(self, payload: dict) -> None:
        """Validate settings, publish storage, then arrange the restart."""
        try:
            dossier, reglages = _preparer_reglages_web(payload)
        except _ReglagesInvalides as erreur:
            self.send_json({"error": str(erreur)}, 400)
            return

        try:
            # The lock covers the whole transaction: preparing the
            # destination, publishing the pointer and any initial marker.
            with runtime.verrou_configuration():
                runtime.ecrire_dossier_stockage(
                    dossier, reglages=reglages, configuration_initiale=self.initial_setup)
        except runtime.BusyError as erreur:
            self.send_json(
                {"error": f"A settings change is already in progress: {erreur}"},
                409)
            return
        except OSError as erreur:
            self.send_json(
                {"error": f"Could not save settings: {erreur}"}, 500)
            return
        if self.initial_setup:
            # The start parent still holds the startup lock: it sees
            # the marker and itself replaces the initial server after the response.
            self.send_json({"ok": True, "accepted": True,
                            "initial_setup": True})
            return
        # The restart will reread settings; its relay lets the HTTP
        # response leave before stopping this server.
        self.repondre_puis_redemarrer(["restart"])

    def _post_sourdine(self, payload: dict) -> None:
        """Delegate mute to watch, without restarting the server."""
        camera = str(payload.get("camera", "")).strip()
        ignored = bool(payload.get("ignored"))
        if not camera:
            self.send_json({"error": "Missing camera name."}, 400)
            return
        # watch rereads its state on each pass of its own loop
        # (see _controler): unlike other settings,
        # mute needs no restart, only letting
        # "watch --ignore/--unignore" write the shared file. Done
        # here in the background so the click stays immediate, on the
        # same principle as the Exclude button (28.33).
        option = "--ignore" if ignored else "--unignore"

        def travailler():
            runtime.lancer(
                runtime.self_command("watch", option, camera),
                cwd=str(runtime.app_dir()), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )

        threading.Thread(target=travailler, daemon=True).start()
        self.send_json({"ok": True})
        return

    def _post_suppression_auto(self, payload: dict) -> None:
        """Modifie les autorisations sous le verrou de configuration."""
        camera = str(payload.get("camera", "")).strip()
        actif = bool(payload.get("actif"))
        try:
            # Serialize read AND modification, including with a
            # bascule de stockage : replace seul perd les clics concurrents.
            with runtime.verrou_configuration("suppression-auto", attente=5):
                etat = md.load_json(watch.WATCH_STATE, {})
                surveillees = etat.get("cameras") or {}
                libelle = next((nom for nom in surveillees
                                if nom.strip().casefold() == camera.casefold()), None)
                if not camera or libelle is None:
                    self.send_json({"error": "Unknown camera."}, 400)
                    return
                # The display name resolves persisted internal identities.
                cles = {
                    choice["key"] for choice in suppression_auto_choices(
                        watch.camera_entries(libelle, surveillees, read_entries(self.paths)))
                }
                cameras = suppression_auto_keys()
                if actif:
                    cameras |= cles
                else:
                    cameras -= cles
                runtime.ecrire_suppression_auto(cameras)
        except runtime.BusyError as erreur:
            self.send_json({"error": f"A settings change is already in progress: {erreur}"}, 409)
            return
        except OSError as erreur:
            self.send_json({"error": f"Could not save automatic deletion: {erreur}"}, 500)
            return
        self.send_json({"ok": True})
        return

    def _post_appliquer_selection(self, payload: dict) -> None:
        """Valide le lot, lance les exclusions, puis applique les suppressions."""
        try:
            selection = _lire_selection_videos(payload)
            racine_direct = self.paths.get("direct")
            clips, directs, chemins_directs = _separer_clips_et_directs(
                selection, racine_direct)
        except (ValueError, OSError) as erreur:
            self.send_json({"error": str(erreur)}, 400)
            return

        # The same snapshot serves deletions and deferred work.
        entrees = read_entries(self.paths)
        if clips.exclure or clips.inclure:
            # Only exclusions/reconstructions are asynchronous: the
            # HTTP response does not wait on an already-held registry lock.
            threading.Thread(
                target=self._appliquer_exclusions_clips,
                args=(clips, entrees), daemon=True,
            ).start()

        resultats = {}
        if clips.supprimer:
            self._supprimer_sources_blink(clips.supprimer, entrees, resultats)
        if directs.exclure or directs.inclure or directs.supprimer:
            self._appliquer_selection_directs(
                directs, racine_direct, chemins_directs, resultats)
        self.send_json({"ok": True, "resultats": resultats})

    def _appliquer_exclusions_clips(self, selection: _SelectionVideos, entrees: dict) -> None:
        """Background work: exclude/restore, then rebuild if allowed."""
        # The registry is shared: serialize both actions and keep
        # their order. A restore wins over an exclusion of the same clip.
        with REGISTRE:
            for identites, exclu in ((selection.exclure, True), (selection.inclure, False)):
                if not identites:
                    continue
                try:
                    md.set_excluded(
                        self.paths["input"], self.paths["normalized"],
                        self.paths["excluded"],
                        [str(self.paths["input"] / identite) for identite in identites], exclu)
                except RuntimeError as erreur:
                    print(msg("ecarter_lot_erreur", erreur=erreur))

        reglages = runtime.lire_reglages()
        # Unchecking Daily also forbids reconstructions triggered
        # by a selection. Settings are reread after writing the registry.
        if not reglages["merge_jour"]:
            return
        jours = _jours_selection_a_reconstruire(
            selection.exclure + selection.inclure, entrees, reglages)
        options = runtime.options_fusion(reglages)
        # Same timezone as day calculation, and never two assemblies
        # concurrent that could write the same video.
        with REASSEMBLAGE:
            for camera, jour in jours:
                runtime.lancer(
                    runtime.self_command("merge", "--camera", camera, "--date", jour, *options),
                    cwd=str(runtime.app_dir()), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )

    def _supprimer_sources_blink(self, identites: list[str], entrees: dict,
                                resultats: dict) -> None:
        """Supprime un lot sous les verrous Blink, puis met à jour le registre."""
        cibles = _preparer_suppressions_blink(identites, entrees, resultats)
        nb_cameras_usb = len({
            str(cible.entree.get("camera") or "").strip() for cible in cibles
            if str(cible.entree.get("source") or "usb") != "cloud"
        })
        ids_presents_par_module = {}
        evenements_presents_par_module = {}

        slot_pris = False
        try:
            # Resource shared with live view and download: a
            # concurrent deletion would risk a partial manifest or batch.
            if not MODULE_SLOT.acquire(blocking=False):
                raise blink_engine.BusyError(_slot_occupe_message())
            slot_pris = True
            _slot_pris("suppression manuelle")
            with blink_engine.hub_lock("suppression manuelle"):
                BLINK.call(
                    lambda blink: self._supprimer_clips_blink(
                        blink, cibles, resultats, ids_presents_par_module,
                        evenements_presents_par_module),
                    timeout=30 + 90 * max(1, nb_cameras_usb),
                )
        except Exception as erreur:
            for cible in cibles:
                resultats.setdefault(cible.identite, f"echec: {type(erreur).__name__}")
        finally:
            if slot_pris:
                _slot_rendu()
                MODULE_SLOT.release()

        self._marquer_sources_supprimees(
            resultats, ids_presents_par_module, evenements_presents_par_module)

    async def _supprimer_clips_blink(self, blink, cibles: list[_CibleSuppressionBlink],
                                   resultats: dict, ids_presents_par_module: dict,
                                   evenements_presents_par_module: dict | None = None,
                                   ) -> None:
        """Exécute les appels distants, avec un seul manifeste par Sync Module.

        ``evenements_presents_par_module`` reçoit, par module, les couples
        (caméra, instant) encore présents sur son stockage : ceux du manifeste
        lu, moins ceux que ce lot vient de supprimer avec succès."""
        if evenements_presents_par_module is None:
            evenements_presents_par_module = {}
        manifestes = {}
        for cible in cibles:
            entree = cible.entree
            source = str(entree.get("source") or "usb")
            camera = str(entree.get("camera") or "").strip()
            try:
                if source == "cloud":
                    clip = blink_models.CloudClip({
                        "id": int(cible.id_distant), "device_name": camera,
                        "created_at": entree.get("created_at"),
                    })
                    resultats[cible.identite] = (
                        "supprime" if await clip.delete_video(blink) else "echec")
                    continue

                sync = BLINK.find_sync_module(blink, entree)
                cle_module = id(sync)
                cle_sync = str(getattr(sync, "sync_id", ""))
                if cle_module not in manifestes:
                    manifestes[cle_module] = await blink_models.read_local_manifest(sync)
                    ids_presents_par_module[cle_sync] = {
                        str(clip.id) for clip in manifestes[cle_module]}
                    evenements = {}
                    for clip in manifestes[cle_module]:
                        try:
                            evenements[id(clip)] = (
                                str(clip.name).strip().casefold(),
                                blink_models.clip_datetime_utc(clip))
                        except (AttributeError, TypeError, ValueError):
                            continue
                    evenements_presents_par_module[cle_sync] = evenements

                # USB numbers can be reassigned after reindexing.
                # Find the clip by camera/date; ambiguity forbids
                # any deletion, even if the old number seems valid.
                try:
                    attendu = md.parse_created_at(str(entree.get("created_at") or ""))
                except (ValueError, TypeError):
                    attendu = None
                candidats = [] if attendu is None else [
                    clip for clip in manifestes[cle_module]
                    if str(clip.name).casefold() == camera.casefold()
                    and abs((blink_models.clip_datetime_utc(clip)
                             - attendu).total_seconds()) <= 2
                ]
                if not candidats:
                    resultats[cible.identite] = "deja_absent"
                elif len(candidats) > 1:
                    resultats[cible.identite] = "ambigu"
                else:
                    supprime = await candidats[0].delete_video(blink)
                    if supprime:
                        evenements_presents_par_module.get(cle_sync, {}).pop(
                            id(candidats[0]), None)
                    resultats[cible.identite] = "supprime" if supprime else "echec"
            except Exception as erreur:
                resultats[cible.identite] = f"echec: {type(erreur).__name__}"

    @staticmethod
    def _evenement_encore_present(entree: dict, evenements) -> bool:
        """True if the manifest still shows this clip by camera and instant.

        Same matching as deletion itself (camera, +/-2 s): the USB number
        alone does not prove absence; the Sync Module renumbers on
        reindexing."""
        if not evenements:
            return False
        camera = str(entree.get("camera") or "").strip().casefold()
        try:
            attendu = md.parse_created_at(str(entree.get("created_at") or ""))
        except (TypeError, ValueError):
            return False
        return any(
            nom == camera and abs((instant - attendu).total_seconds()) <= 2
            for nom, instant in list(evenements.values())
        )

    def _marquer_sources_supprimees(self, resultats: dict,
                                   ids_presents_par_module: dict,
                                   evenements_presents_par_module: dict | None = None,
                                   ) -> None:
        """Mémorise les absences constatées pour éviter de redemander une suppression."""
        evenements_presents_par_module = evenements_presents_par_module or {}
        marques = {identite for identite, statut in resultats.items()
                   if statut in ("supprime", "deja_absent")}
        if not marques and not ids_presents_par_module:
            return

        etat = blink_registre.load_download_state(self.paths["input"])
        for entree in etat["clips"].values():
            if not isinstance(entree, dict) or entree.get("source_deleted"):
                continue
            if entree.get("path") in marques:
                entree["source_deleted"] = True
                continue
            # Reading the manifest also informs the other USB clips of
            # same module, without an extra network request.
            cle_sync = str(entree.get("sync_id") or "")
            ids_presents = ids_presents_par_module.get(cle_sync)
            if ids_presents is None or str(entree.get("source") or "usb") != "usb":
                continue
            correspondance = re.search(
                r"_(\d+)_[0-9a-f]{12}\.mp4$", str(entree.get("path") or ""))
            id_connu = correspondance.group(1) if correspondance else str(
                entree.get("remote_id") or "")
            # Absent by its number AND by camera/instant: an absent number
            # alone wrongly marked any still-present but renumbered clip,
            # which permanently hid its "Delete" checkbox in the
            # gallery (audit of 2026-09-24). A false negative stays benign: the
            # next request answers "deja_absent" and then marks it.
            if (id_connu and id_connu not in ids_presents
                    and not self._evenement_encore_present(
                        entree, evenements_presents_par_module.get(cle_sync))):
                entree["source_deleted"] = True
        blink_registre.save_download_state(self.paths["input"], etat)

    def _appliquer_selection_directs(self, selection: _SelectionVideos, racine_direct: Path,
                                    chemins_directs: dict, resultats: dict) -> None:
        """Modifie uniquement les fichiers directs et leurs exclusions locales."""
        # No clip registry, Blink call or reassembly for these files.
        # Locked read-modify-write: two simultaneous
        # selections (double-click, two tabs) reread the same list and
        # the last write erased the other (39 exclusions lost out of 40
        # launched together, measured 2026-09-24).
        with _VERROU_EXCLUSION_DIRECTE:
            exclusion_directe = _lire_exclusion_directe(self.paths)
            exclusion_directe |= set(selection.exclure)
            exclusion_directe -= set(selection.inclure)
            for identite in selection.supprimer:
                try:
                    chemin = _chemin_direct_confine(racine_direct, identite)
                    if chemin != chemins_directs[identite]:
                        raise ValueError("Video path changed during selection")
                    chemin.unlink()
                    resultats[identite] = "supprime"
                    exclusion_directe.discard(identite)
                except FileNotFoundError:
                    resultats[identite] = "deja_absent"
                except (OSError, ValueError) as erreur:
                    resultats[identite] = f"echec: {type(erreur).__name__}"
                    continue
                # Cosmetic cleanup of both parents, only if they are
                # empty. Never climb to the live-recording storage root
                # or beyond.
                for dossier in (chemin.parent, chemin.parent.parent):
                    if (dossier == racine_direct.resolve()
                            or not runtime.est_relatif_a(dossier, racine_direct.resolve())):
                        break
                    try:
                        dossier.rmdir()
                    except OSError:
                        break
            _ecrire_exclusion_directe(self.paths, exclusion_directe)

    # Beyond that, a refused request's body is not read: the connection is
    # closed without it; an abusive send must not occupy the server.
    _CORPS_REFUSE_MAX = 1 << 20

    def _refuser(self, code: int) -> None:
        """Answer an error to a POST after reading its body, never interpreting
        it. Closed with unread bytes, the connection goes RST under Windows
        and the client loses the response (WinError 10053): 25 token refusals
        lost out of 300 POSTs with body, measured 2026-09-26, i.e. the page
        keeping a stale token after a restart. Bounded read and bytes
        discarded: the audit that wanted to refuse "before reading" sought
        to process nothing from an unauthenticated client, which remains
        true. Same fix as the lidar2map and watch2notif servers."""
        try:
            longueur = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            longueur = 0
        if 0 < longueur <= self._CORPS_REFUSE_MAX:
            self.rfile.read(longueur)
        self.send_error(code)

    def do_POST(self):
        # Same reason as do_GET: an external caller (home-automation script)
        # has neither origin nor session token; the webhook secret is enough.
        try:
            route_webhook = urlparse(self.path).path
        except ValueError:
            route_webhook = ""
        if route_webhook == WEBHOOK_ARM_ROUTE:
            self.gerer_webhook_arm()
            return
        if not self.hote_autorise() or not self.jeton_valide():
            self._refuser(403)
            return
        # Same safety net as do_GET: after hote_autorise()/jeton_valide() here,
        # so only reachable by an already-trusted client, but
        # a malformed request must not crash its own either.
        try:
            route = urlparse(self.path).path
        except ValueError:
            self._refuser(400)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_error(400)
            return
        if length < 0:
            # rfile.read(-1) would read until connection close,
            # which a persistent HTTP/1.1 connection never does by itself:
            # the request thread stayed blocked (audit of 2026-09-24).
            self.send_error(400)
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:  # JSONDecodeError, ou UnicodeDecodeError (octets non UTF-8)
            self.send_json({"error": "corps JSON illisible"}, 400)
            return
        if not isinstance(payload, dict):
            # All routes read payload.get(...): a list or a
            # valid JSON numbers raised AttributeError, with no clean response.
            self.send_json({"error": "corps JSON : objet attendu"}, 400)
            return

        if not self.exiger_acces_authentifie():
            return

        if route.startswith("/live-webrtc/"):
            nom = unquote(route[len("/live-webrtc/"):])
            if not self.exiger_camera(nom):
                return
            self.send_offer_webrtc(nom, payload)
            return

        if route.startswith("/api/direct-signal/"):
            # Only link in the whole chain that the server cannot
            # observe itself: the real decode/display in the
            # <video> du navigateur. connectionstatechange (blink_webrtc.py)
            # says negotiation succeeded, never that an image is
            # effectivement arrivee a l'ecran - watchWebRTC()/watchMse()
            # (serve_app.js) know that, via loadeddata/playing or
            # giving up after retries. Best-effort, never blocking: a
            # lost signal must never fail the live view itself,
            # seul direct.log en patit.
            nom = unquote(route[len("/api/direct-signal/"):])
            if not self.exiger_camera(nom):
                return
            evenement = str(payload.get("evenement") or "signal").strip()
            detail = str(payload.get("detail") or "").strip()
            suffixe = f" : {detail}" if detail else ""
            session_id = str(payload.get("session_id") or "")
            session = f"session {session_id[:8]}, " if DIRECT_SESSION_ID.fullmatch(session_id) else ""
            _journal_direct(nom, f"{session}navigateur : {evenement}{suffixe}")
            self.send_json({"ok": True})
            return

        if route == "/api/access":
            if not self.exiger_admin():
                return
            try:
                self.send_json(access_control.ecrire_acces(payload))
            except ValueError as error:
                self.send_json({"error": str(error)}, 400)
            return

        if route == "/api/login":
            if not self.exiger_admin():
                return
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
            if not username or not password:
                self.send_json({"status": "error",
                                "message": "Adresse e-mail et mot de passe requis."})
                return
            self.send_json(self.login_flow.start(username, password))
            return

        if route == "/api/2fa":
            if not self.exiger_admin():
                return
            code = str(payload.get("code", "")).strip()
            if not code:
                self.send_json({"status": "error", "message": "Code requis."})
                return
            self.send_json(self.login_flow.submit_code(code))
            return

        if route == "/api/arm":
            if not self.exiger_admin():
                return
            name = str(payload.get("name", "")).strip()
            armed = bool(payload.get("armed"))
            scope = str(payload.get("scope", "camera"))
            try:
                self.set_armed(scope, name, armed)
                self.send_json(access_control.filtrer_system_state(
                    self.system_state(), self.identite_acces().get("email") or ""))
            except RuntimeError as error:
                self.send_json({"error": str(error)}, 503)
            return

        if route == "/api/reveiller":
            if not self.exiger_admin():
                return
            name = str(payload.get("name", "")).strip()
            try:
                self.reveiller_camera(name)
                self.send_json(access_control.filtrer_system_state(
                    self.system_state(), self.identite_acces().get("email") or ""))
            except blink_engine.BusyError as error:
                self.send_json({"error": str(error)}, 409)
            except Exception as error:
                # Delai large (jusqu'a 130 s, voir reveiller_camera) : outre
                # RuntimeError (unknown camera), a blinkpy-side timeout or
                # a network refusal must also surface a readable message
                # rather than a silent 500 error.
                self.send_json({"error": f"{type(error).__name__}: {error}"}, 503)
            return

        if route == "/api/snapshot":
            if not self.exiger_admin():
                return
            name = str(payload.get("name", "")).strip()
            try:
                chemin = self.declencher_snapshot(name)
                relatif = chemin.relative_to(self.paths["snapshots"]).as_posix()
                self.send_json({"ok": True, "fichier": relatif})
            except blink_engine.BusyError as error:
                self.send_json({"error": str(error)}, 409)
            except Exception as error:
                self.send_json({"error": f"{type(error).__name__}: {error}"}, 503)
            return

        if route == "/api/snapshot-supprimer":
            if not self.exiger_admin():
                return
            relatif = str(payload.get("fichier", ""))
            chemin = self.resolve_snapshot(relatif)
            if chemin is None:
                self.send_error(404)
                return
            chemin.unlink(missing_ok=True)
            self.send_json({"ok": True})
            return

        if route == "/api/webhook-regenerer":
            if not self.exiger_admin():
                return
            self.send_json({"token": runtime.regenerer_jeton_webhook()})
            return

        if route == "/api/direct-enregistrement":
            if not self.exiger_admin():
                return
            session_id = str(payload.get("session_id") or "")
            with DIRECT_WEBRTC_SESSION_LOCK:
                holder = DIRECT_WEBRTC_SESSION.get("session")
                autorise = (
                    holder is not None and holder["session_id"] == session_id
                    and not holder["arret"].is_set() and not holder["rendu"]
                ) or (
                    holder is None and not session_id
                    and MODULE_SLOT_INFO.get("quoi") == "direct MSE"
                )
                if autorise:
                    if bool(payload.get("actif")):
                        ENREGISTREMENT_DIRECT_ACTIF.set()
                    else:
                        ENREGISTREMENT_DIRECT_ACTIF.clear()
            if not autorise:
                self.send_json({"error": "This live session is no longer active."}, 409)
                return
            self.send_json({"actif": ENREGISTREMENT_DIRECT_ACTIF.is_set()})
            return

        if route == "/api/arreter-direct":
            session_id = str(payload.get("session_id") or "")
            if not DIRECT_SESSION_ID.fullmatch(session_id):
                self.send_json({"error": "Session identifier required."}, 400)
                return
            arrete = _demander_arret_direct(session_id)
            if arrete:
                _journal_direct("arreter-direct", f"session {session_id[:8]}, close requested")
            # Any Blink polling wait continues on its loop.
            # /api/attente-module remains the real-release confirmation.
            self.send_json({"ok": True, "stopped": arrete, "session_id": session_id})
            return

        if route == "/api/maj/verifier":
            if not self.exiger_admin():
                return
            # Settings "Check for updates" button: GitHub right
            # right away, without waiting for the background thread (an hour at most, see
            # maj.FRAICHEUR). "ok" false: GitHub did not answer, the cache
            # reste tel quel.
            neuve, ok = maj.verifier_maintenant()
            self.send_json({"maj": neuve, "ok": ok, "version": runtime.VERSION})
            return

        if route == "/api/update":
            if not self.exiger_admin():
                return
            neuve = maj.disponible(reseau=False)
            if not neuve:
                self.send_json({"error": "No newer version available."}, 409)
                return
            if not runtime.effacer_conclusion_travail("phase.update_noop"):
                self.send_json({"error": "The previous update conclusion is still busy. Try again."}, 409)
                return
            # Detached, and deliberately without waiting: this process is
            # part of what the update will stop. It reports in
            # maj.log, and the page simply waits for the server reply.
            runtime.demarrer(
                runtime.self_command("update"), cwd=str(runtime.app_dir()),
                stdin=subprocess.DEVNULL,
                stdout=(runtime.app_dir() / "maj.log").open("ab"),
                stderr=subprocess.STDOUT,
                start_new_session=(os.name != "nt"))
            self.send_json({"ok": True, "version": neuve["version"]})
            return

        if route == "/api/appliquer-selection":
            if not self.exiger_admin():
                return
            return self._post_appliquer_selection(payload)

        if route == "/api/autostart":
            if not self.exiger_admin():
                return
            code = autostart.appliquer("on" if payload.get("actif") else "off")
            if code != 0:
                self.send_json(
                    {"error": "Failed to change automatic startup."}, 500)
                return
            # Reread rather than returned as-is: if the
            # plateforme n'a pas vraiment pris (droits, service absent...),
            # interface shows the real state, not what was just requested.
            self.send_json({"actif": autostart.est_installe()})
            return

        if route == "/api/reglages":
            if not self.exiger_admin():
                return
            return self._post_reglages(payload)

        if route == "/api/sourdine":
            if not self.exiger_admin():
                return
            return self._post_sourdine(payload)

        if route == "/api/suppression-auto":
            if not self.exiger_admin():
                return
            return self._post_suppression_auto(payload)

        if route == "/api/stop":
            if not self.exiger_admin():
                return
            # --sans-relance so "restart" stops without coming back,
            # exactly what a Stop button must do.
            self.repondre_puis_redemarrer(["restart", "--sans-relance"])
            return

        if route == "/api/redemarrer":
            if not self.exiger_admin():
                return
            # Same call as the end of _post_reglages(), without going through
            # validation or saving: a plain restart,
            # to pick up freshly edited code or leave a stuck
            # state, without having to tweak a setting to get it.
            self.repondre_puis_redemarrer(["restart"])
            return

        if route == "/api/lang":
            runtime.ecrire_langue(str(payload.get("lang", "")))
            self.send_json({"ok": True})
            return

        self.send_error(404)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cas Cams</title>
<link rel="icon" href="/favicon.ico">
<style>
__CSS__
</style>
</head>
<body>
<header>
  <h1 class="brand">
  <img class="brand-icon" src="/logo.png" alt="" width="40" height="40">
  <span class="brand-text">
    Cas Cams
    <span class="v">__VERSION__ (<span id="pidServeur"></span>)</span>
  </span>
</h1>
  <select id="view">
    <option value="live" data-i18n="view.live">Live Views</option>
    <option value="alllive" data-i18n="view.alllive">All Live Views</option>
    <option value="rotation" data-i18n="view.rotation">Live View Rotation</option>
    <option value="direct" data-i18n="view.direct">Live Recordings</option>
    <option value="clips" data-i18n="view.clips">Detection Recordings</option>
    <option value="daily" data-i18n="view.daily">Detection Daily</option>
    <option value="weekly" data-i18n="view.weekly">Detection Weekly</option>
    <option value="monthly" data-i18n="view.monthly">Detection Monthly</option>
    <option value="pictures" data-i18n="view.pictures">Pictures</option>
  </select>
  <button id="filtreButton" data-i18n="filtre.button" data-i18n-title="filtre.button.title"
          title="Filter">🔍 Filter</button>
  <span class="filtreResume" id="filtreResume"></span>
  <span class="filtreResume" id="filtreCompte"></span>
  <span class="count" id="count"></span>
  <!-- Update controls stay together on the right: last-pass time,
       auto-reload checkbox, and the button. -->
  <div class="maj">
    <button id="update" hidden></button>
    <span class="sub tiny" id="passages"></span>
    <button class="danger" id="applyButton" hidden></button>
    <button class="primary" id="refresh" data-i18n="btn.refresh">↻ Refresh</button>
    <button id="reglagesButton" data-i18n="btn.reglages" data-i18n-title="btn.reglages.title" title="Settings">⚙ Settings…</button>
  </div>
  <span class="langGroup" title="Langue / Language">
    <button class="btn-lang" data-lang-btn="fr">FR</button>
    <button class="btn-lang" data-lang-btn="en">EN</button>
  </span>
  <div id="work"><span id="phase"></span><progress id="bar"></progress></div>
</header>
<main><div id="list"></div><pre id="log"></pre></main>

<dialog id="auth">
  <!-- Modal: the dimmed backdrop covers the header FR/EN buttons while
       showModal() is open. Minimal duplicate here is the only way to
       change language before signing in. -->
  <span class="langGroup langGroupAuth" title="Langue / Language">
    <button class="btn-lang" data-lang-btn="fr">FR</button>
    <button class="btn-lang" data-lang-btn="en">EN</button>
  </span>
  <h3 id="authTitle" data-i18n="auth.title">Blink login</h3>
  <p id="authHint" data-i18n="auth.hint">The password is only used to open the session; only
     the tokens are stored, never the password.</p>
  <p id="authError"></p>
  <div id="authCreds">
    <input id="user" type="email" data-i18n-placeholder="auth.email" placeholder="Email address" autocomplete="username">
    <div class="champMdp">
      <input id="pass" type="password" data-i18n-placeholder="auth.password" placeholder="Password"
             autocomplete="current-password">
      <button type="button" id="passToggle" data-i18n="auth.show"
              aria-label="Show password">Show</button>
    </div>
  </div>
  <div id="authCode" hidden>
    <input id="code" inputmode="numeric" data-i18n-placeholder="auth.code" placeholder="Code received by SMS or email"
           autocomplete="one-time-code">
  </div>
  <div class="row">
    <button id="authCancel" data-i18n="auth.cancel">Cancel</button>
    <button class="primary" id="authOk" data-i18n="auth.ok">Log in</button>
  </div>
</dialog>

<dialog id="reglages">
  <h3 data-i18n="reglages.title">Settings</h3>
  <p id="initialSetupHint" class="sub" hidden data-i18n="reglages.initial.hint">
    Check the data folder and time zone in particular. No clip
    will be downloaded until you apply these settings.
  </p>
  <div class="tabsReglages" role="tablist">
    <button type="button" id="tabGeneral" class="tabReglages"
            data-i18n="reglages.tab.general">General</button>
    <button type="button" id="tabVideo" class="tabReglages"
            data-i18n="reglages.tab.video">Video</button>
    <button type="button" id="tabAcces" class="tabReglages"
            data-i18n="reglages.tab.acces">Access &amp; automation</button>
    <button type="button" id="tabAlertes" class="tabReglages"
            data-i18n="reglages.tab.alertes">Alerts</button>
  </div>
  <div id="panelGeneral" class="panelReglages">
    <label id="autostartLabel"
           data-i18n-title="reglages.autostart.title"
           title="Starts the web server and clip processing in the background
                   when your session opens (at boot for a Linux service
                   with lingering); does not open this page by itself">
      <input type="checkbox" id="autostart"> <span data-i18n="reglages.autostart">Start monitoring
      automatically</span>
    </label>
    <label id="autoLabel" data-i18n-title="reglages.auto.title" title="Reload the list as soon as clips arrive">
      <input type="checkbox" id="auto"> <span data-i18n="reglages.auto">Automatic page refresh</span>
    </label>
    <div class="champCadence">
      <label for="tailleCartes" data-i18n="reglages.tailleCartes">Thumbnail size</label>
      <select id="tailleCartes">
        <option value="petites" data-i18n="reglages.tailleCartes.petites">Small</option>
        <option value="moyennes" data-i18n="reglages.tailleCartes.moyennes">Medium</option>
        <option value="grandes" data-i18n="reglages.tailleCartes.grandes">Large</option>
        <option value="tres_grandes" data-i18n="reglages.tailleCartes.tres_grandes">Extra large</option>
      </select>
    </div>
    <label id="barrePaginationLabel" data-i18n-title="reglages.barrePagination.title"
           title="When hidden, the bar at the bottom of the list stays available to change page. Specific to this browser.">
      <input type="checkbox" id="barrePagination"> <span data-i18n="reglages.barrePagination">Show the pagination
      bar at the top of the list</span>
    </label>
    <fieldset>
      <legend data-i18n="reglages.masquees">Hidden cameras</legend>
      <p class="sub tiny" data-i18n="reglages.masquees.hint">A hidden camera disappears from the lists on this page but keeps recording and downloading. This setting is specific to this browser.</p>
      <div id="camerasMasqueesListe" class="ligneCoches sub tiny"></div>
    </fieldset>
    <div class="champCadence">
      <label for="port" data-i18n="reglages.serveur">Server port</label>
      <input type="number" id="port" min="1" max="65535" step="1">
    </div>
    <div class="champDossier" data-i18n-title="reglages.storageDir.hint"
         title="Clips, assembled videos, lives and snapshots. Does not move those already saved at the old location: do it yourself if you change this path. Empty = default location, Documents/blink2video.">
      <label for="storageDir" data-i18n="reglages.storageDir">Videos folder</label>
      <input type="text" id="storageDir" data-i18n-placeholder="reglages.storageDir.placeholder"
             placeholder="C:/path/to/the/folder">
      <button type="button" id="storageDirBrowse" data-i18n="reglages.storageDir.browse">Browse…</button>
    </div>
  </div>
  <div id="panelVideo" class="panelReglages" hidden>
    <fieldset>
      <legend data-i18n="reglages.cadence">Camera polling interval</legend>
      <label id="downloadAutoLabel" data-i18n-title="reglages.downloadAuto.hint"
             title="Unchecked, no clip is fetched or stored anymore: useful to keep only the live view. The cadences below then have no effect.">
        <input type="checkbox" id="downloadAuto">
        <span data-i18n="reglages.downloadAuto">Download clips automatically</span>
      </label>
      <div class="champCadenceDouble">
        <label for="usbMinutes" data-i18n="reglages.usb">Local storage (minutes)</label>
        <input type="number" id="usbMinutes" min="1" step="1">
        <label for="cloudMinutes" data-i18n="reglages.cloud">Cloud (minutes)</label>
        <input type="number" id="cloudMinutes" min="1" step="1">
      </div>
    </fieldset>
    <fieldset>
      <legend data-i18n="reglages.video">Video</legend>
      <label id="timestampLabel">
        <input type="checkbox" id="timestamp"> <span data-i18n="reglages.timestamp">Burn the date and time
        into the image</span>
      </label>
      <p class="sub tiny" data-i18n="reglages.timestamp.style.hint">
        Empty = size automatically scaled to the video's height. Color: a name
        (white, yellow...) or hex code, optionally with transparency
        (e.g. white@0.8).
      </p>
      <div class="champCadenceDouble">
        <label for="fontSize" data-i18n="reglages.fontSize">Font size</label>
        <input type="number" id="fontSize" min="8" max="500" step="1" placeholder="auto">
        <label for="boxOpacity" data-i18n="reglages.boxOpacity">Box opacity</label>
        <input type="number" id="boxOpacity" min="0" max="1" step="0.05">
      </div>
      <div class="champCadence">
        <label for="fontColor" data-i18n="reglages.fontColor">Color</label>
        <input type="text" id="fontColor" list="couleursCourantes" placeholder="white">
      </div>
      <datalist id="couleursCourantes">
        <option value="white">
        <option value="yellow">
        <option value="black">
        <option value="red">
        <option value="lime">
        <option value="cyan">
        <option value="orange">
      </datalist>
      <div class="champCadence">
        <label for="timezone" data-i18n="reglages.timezone">Time zone</label>
        <input type="text" id="timezone" list="fuseauxCourants" placeholder="America/New_York">
      </div>
      <datalist id="fuseauxCourants">
        <option value="Europe/Paris">
        <option value="Europe/London">
        <option value="Europe/Brussels">
        <option value="Europe/Madrid">
        <option value="Europe/Berlin">
        <option value="America/Montreal">
        <option value="America/New_York">
        <option value="America/Chicago">
        <option value="America/Denver">
        <option value="America/Los_Angeles">
        <option value="Africa/Casablanca">
        <option value="Africa/Abidjan">
        <option value="Indian/Reunion">
        <option value="Asia/Tokyo">
        <option value="Australia/Sydney">
        <option value="UTC">
      </datalist>
      <div class="champCadence">
        <label for="liveProtocol" data-i18n="reglages.liveProtocol">Live view protocol</label>
        <select id="liveProtocol">
          <option value="webrtc" data-i18n="reglages.liveProtocol.webrtc">WebRTC (fast)</option>
          <option value="mse" data-i18n="reglages.liveProtocol.mse">MSE (compatible)</option>
        </select>
      </div>
      <div class="champCadence" data-i18n-title="reglages.liveAutoStop.hint"
           title="Stops the live view on its own after this delay, up to 420 s, even in continuous live. Empty or 0: no auto-stop; Blink closes every live view by itself after about 6 minutes, and a camera's &quot;Continuous live&quot; box starts it again.">
        <label for="liveAutoStopSeconds" data-i18n="reglages.liveAutoStop">Auto-stop live view (s)</label>
        <input type="number" id="liveAutoStopSeconds" min="0" max="420" placeholder="0">
      </div>
    </fieldset>
    <fieldset>
      <legend data-i18n="reglages.archivage"
              data-i18n-title="reglages.archivage.hint"
              title="Weekly and Monthly are assembled from the Daily: unchecking &quot;Daily&quot; also disables the other two.">Per-camera time-based video creation</legend>
      <div class="ligneCoches">
        <label id="mergeJourLabel">
          <input type="checkbox" id="mergeJour"> <span data-i18n="reglages.mergeJour">Daily</span>
        </label>
        <label id="mergeSemaineLabel">
          <input type="checkbox" id="mergeSemaine"> <span data-i18n="reglages.mergeSemaine">Weekly</span>
        </label>
        <label id="mergeMoisLabel">
          <input type="checkbox" id="mergeMois"> <span data-i18n="reglages.mergeMois">Monthly</span>
        </label>
      </div>
    </fieldset>
  </div>
  <div id="panelAcces" class="panelReglages" hidden>
    <fieldset>
      <legend data-i18n="reglages.sharedAccess">Shared camera access</legend>
      <p class="sub tiny" data-i18n="reglages.sharedAccess.hint">
        Friends authenticate with Cloudflare Access (their email). You keep the Blink login.
        Assign cameras below. Empty Cloudflare fields keep local-only mode (full admin on this machine).
      </p>
      <div class="champDossier">
        <label for="cfAud" data-i18n="reglages.cfAud">Access application AUD</label>
        <input type="text" id="cfAud" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx">
      </div>
      <div class="champDossier">
        <label for="accessAdmins" data-i18n="reglages.accessAdmins">Admin emails (comma-separated)</label>
        <input type="text" id="accessAdmins" placeholder="you@example.com">
      </div>
      <div id="accessUsersListe" class="accessUsers"></div>
      <div class="row" style="gap:8px;margin-top:8px">
        <input type="email" id="accessNewEmail" placeholder="friend@example.com" style="flex:1">
        <button type="button" id="accessAddUser" data-i18n="reglages.accessAdd">Add friend</button>
        <button type="button" id="accessSave" class="primary" data-i18n="reglages.accessSave">Save access</button>
      </div>
      <p id="accessSaveStatus" class="sub tiny" hidden></p>
    </fieldset>
    <fieldset>
      <legend data-i18n="reglages.accesDistant" data-i18n-title="reglages.accesDistant.hint"
              title="To reach this instance directly from a mesh VPN (Tailscale, WireGuard), no reverse proxy in front.">Remote access over a mesh VPN</legend>
      <p class="sub tiny" data-i18n="reglages.accesDistant.hint.text">
        Use together with the BLINK_BIND environment variable set to
        the listening address (see the README, "Reaching it remotely" section):
        this setting alone, without it, changes nothing about who can reach
        the interface. Several values allowed, comma-separated:
        hostnames, IP addresses or subnets (192.168.1.0/24). Only exact names
        and addresses also allow embedding the page in an
        iframe.
      </p>
      <div class="champDossier">
        <label for="trustedHost" data-i18n="reglages.trustedHost">Trusted host</label>
        <input type="text" id="trustedHost" placeholder="100.x.y.z, 192.168.1.0/24, my-pc">
      </div>
    </fieldset>
    <fieldset>
      <legend data-i18n="reglages.webhook" data-i18n-title="reglages.webhook.hint"
              title="Triggers a picture remotely (home automation) without opening this page.">Webhook picture</legend>
      <p class="sub tiny" data-i18n="reglages.webhook.hint.text">
        Call this URL with GET, replacing NOM_CAMERA with a camera's exact
        name, to trigger a picture remotely. The secret is part of
        the URL: keep it private.
      </p>
      <div class="champDossier">
        <label for="webhookUrl" data-i18n="reglages.webhook.url">URL</label>
        <input type="text" id="webhookUrl" readonly>
      </div>
      <button type="button" id="webhookRegenerer" data-i18n="reglages.webhook.regenerer">Regenerate the secret</button>
    </fieldset>
    <fieldset>
      <legend data-i18n="reglages.webhookNotif" data-i18n-title="reglages.webhookNotif.hint"
              title="Calls this URL with POST on every new clip or picture, once the file is actually done writing to disk.">Webhook notification</legend>
      <p class="sub tiny" data-i18n="reglages.webhookNotif.hint.text">
        Sends a JSON POST (camera, path, type) to the URL below every
        time a clip or picture is ready. Best-effort, no retry:
        a missed notification never holds up
        downloading.
      </p>
      <div class="champDossier">
        <label for="webhookNotifUrl" data-i18n="reglages.webhookNotifUrl">Notification URL</label>
        <input type="text" id="webhookNotifUrl" placeholder="https://...">
      </div>
    </fieldset>
  </div>
  <div id="panelAlertes" class="panelReglages" hidden>
    <fieldset>
      <legend data-i18n="reglages.alertes">Mute alerts</legend>
      <div id="sourdineListe" class="ligneCoches sub tiny" data-i18n="sourdine.loading">Loading…</div>
    </fieldset>
    <fieldset>
      <legend data-i18n="reglages.suppressionAuto" data-i18n-title="suppressionAuto.hint"
              title="Once a clip is successfully downloaded, it is deleted from its source (local USB/microSD storage or subscription cloud, depending on the camera).">Automatic deletion after download</legend>
      <div id="suppressionAutoListe" class="ligneCoches sub tiny" data-i18n="suppressionAuto.loading">Loading…</div>
    </fieldset>
  </div>
  <div class="row row-boutons">
    <button class="primary" id="reglagesApply" data-i18n="reglages.apply"
            data-i18n-title="reglages.hint"
            title="Settings only take effect on restart: &quot;Apply&quot; saves and restarts. Changing the port redirects this page to the new address.">Apply</button>
    <button id="verifierMajButton" data-i18n="reglages.checkUpdates"
            data-i18n-title="reglages.checkUpdates.hint"
            title="Asks GitHub right away whether a newer version is out. Without this button, blink2video checks by itself every hour.">Check for updates</button>
    <button id="redemarrerButton" data-i18n="reglages.restart"
            data-i18n-title="reglages.restart.hint"
            title="Restarts without changing any setting, for example to pick up an update already in place.">Restart</button>
    <button id="stopButton" data-i18n="reglages.stop">Stop camera monitoring</button>
    <button id="reglagesClose" data-i18n="reglages.close">Close</button>
  </div>
</dialog>

<dialog id="filtre">
  <h3 data-i18n="filtre.title">Filter</h3>
  <div class="champCadence">
    <label for="camera" data-i18n="filtre.camera">Camera</label>
    <select id="camera"></select>
  </div>
  <div class="champCadence" id="groupBySection" hidden>
    <label for="groupBy" data-i18n="filtre.groupBy">Group by</label>
    <select id="groupBy">
      <option value="camera" data-i18n="filtre.groupBy.camera">Camera</option>
      <option value="day" data-i18n="filtre.groupBy.day">Day</option>
    </select>
  </div>
  <label id="outLabel">
    <input type="checkbox" id="showOut"> <span data-i18n="reglages.showOut">Show discarded clips</span>
  </label>
  <label id="hiddenLabel" hidden>
    <input type="checkbox" id="showHidden"> <span data-i18n="filtre.showHidden">Show hidden cameras</span>
  </label>
  <div id="periodeSection">
    <p class="sub tiny" data-i18n="range.title">Period</p>
    <div class="presets">
      <button type="button" data-preset="today" data-i18n="range.today">Today (24h)</button>
      <button type="button" data-preset="week" data-i18n="range.week">This week (7d)</button>
      <button type="button" data-preset="month" data-i18n="range.month">This month</button>
      <button type="button" data-preset="2months" data-i18n="range.2months">Last 2 months</button>
      <button type="button" data-preset="all" data-i18n="range.all">All history</button>
    </div>
    <p class="sub tiny" data-i18n="range.custom.hint">Or a precise range, down to the hour:</p>
    <div class="champCadence">
      <label for="rangeFrom" data-i18n="range.from">From</label>
      <input type="datetime-local" id="rangeFrom">
    </div>
    <div class="champCadence">
      <label for="rangeTo" data-i18n="range.to">to</label>
      <input type="datetime-local" id="rangeTo">
    </div>
  </div>
  <div class="row row-boutons">
    <button class="primary" id="filtreApply" data-i18n="range.apply">Filter</button>
    <button id="filtreClose" data-i18n="reglages.close">Close</button>
  </div>
</dialog>
<script nonce="__SCRIPT_NONCE__">
__APP_JS__
</script>
</body>
</html>
"""

# CSS et JS vivent dans leurs propres fichiers (serve_style.css,
# serve_app.js): a .py that hardcoded them had neither highlighting nor
# lint corrects for either. Spliced here before template
# following so that __TOKEN__ (in serve_app.js) benefits like the rest
# substitutions. runtime.resource_dir() rather than Path(__file__).parent: only
# the former stays correct once the bundle is frozen (see assets/, same
# convention for the favicon).
PAGE = PAGE.replace(
    "__CSS__",
    (runtime.resource_dir() / "serve_style.css").read_text(encoding="utf-8"),
)
PAGE = PAGE.replace(
    "__APP_JS__",
    (runtime.resource_dir() / "serve_app.js").read_text(encoding="utf-8"),
)

# The page is a constant template, full of CSS and JavaScript braces:
# impossible to make it an f-string. A single substitution at load
# suffit, et laisse le gabarit lisible.
PAGE = PAGE.replace("__VERSION__", runtime.VERSION)
# The PID next to the version distinguishes at a glance a tab left
# open on the old process from one that just restarted after a
# restart: both show the same page until the tab
# reloads, but not the same PID (observed in practice, session of
# 2026-09-16: several restarts with no trace anywhere of what had
# actually been running).
PAGE = PAGE.replace("__PID__", str(os.getpid()))

# One token per process (28.60/28.59), not per request: generated once
# here at module load, like VERSION above. uuid4 rather than
# secrets to stay on the token generation already used elsewhere
# (runtime.py, verrou disque ; blink_auth.py, fichiers temporaires).
TOKEN = uuid.uuid4().hex
PAGE = PAGE.replace("__TOKEN__", TOKEN)
SCRIPT_NONCE = uuid.uuid4().hex
PAGE = PAGE.replace("__SCRIPT_NONCE__", SCRIPT_NONCE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blink2video serve",
        description=msg("aide_desc"),
    )
    parser.add_argument("--input", type=Path, default=md.DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=md.DEFAULT_OUTPUT)
    parser.add_argument("--weekly-output", type=Path, default=md.DEFAULT_WEEKLY)
    parser.add_argument("--monthly-output", type=Path, default=md.DEFAULT_MONTHLY)
    parser.add_argument("--normalized-output", type=Path, default=md.DEFAULT_NORMALIZED)
    parser.add_argument("--excluded-output", type=Path, default=md.DEFAULT_EXCLUDED)
    parser.add_argument("--timezone", default="Europe/Paris")
    parser.add_argument("--hub", help=msg("aide_hub"))
    # With the outputs: thumbnails and live-recording exclusions
    # (DIRECT_EXCLUSION) describe those videos, and follow them.
    parser.add_argument(
        "--thumbs", type=Path, default=runtime.dossier_sorties() / ".blink_thumbs",
        help=msg("aide_thumbs"),
    )
    parser.add_argument("--port", type=runtime.port_valide, default=8765)
    parser.add_argument("--trusted-host", default="", help=msg("aide_trusted_host"))
    parser.add_argument("--initial-setup", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument(
        # A server does not open a window by itself: that is the usage, and
        # this one spends most of its life launched at session start, where
        # popping up in the browser would be out of place. So we ask for it.
        "--open-browser", action="store_true", help=msg("aide_open_browser"),
    )
    return parser.parse_args()


def veiller_sur_les_versions() -> None:
    """Keep up to date, in the background, knowledge of the latest published version.

    A separate thread rather than a call in the page: GitHub can take ten
    seconds to answer, or not answer at all, and none of that must show in
    the UI. One visit per hour (maj.FRAICHEUR): at six hours, a publication
    could wait half a day before appearing (issue #35). The loop is the
    shared one (Verificateur.veiller), the same for all four apps: it does
    not re-request a still-fresh response from a recent startup's disk cache."""
    if runtime.build_windows7():
        return             # no automatic update for this edition
    try:
        maj._verificateur().veiller()
    except Exception:      # a failure does not stop the server
        pass


def main() -> int:
    args = parse_args()
    Handler.paths = {
        "input": args.input.resolve(),
        "normalized": args.normalized_output.resolve(),
        "excluded": args.excluded_output.resolve(),
        "thumbs": args.thumbs.resolve(),
        "daily": args.output.resolve(),
        "weekly": args.weekly_output.resolve(),
        "monthly": args.monthly_output.resolve(),
        "direct": DOSSIER_DIRECT.resolve(),
        "snapshots": DOSSIER_SNAPSHOTS.resolve(),
    }
    Handler.hub = args.hub
    Handler.initial_setup = args.initial_setup
    Handler.trusted_host = args.trusted_host.strip()
    try:
        Handler.ffmpeg = md.find_ffmpeg()
        Handler.timezone = ZoneInfo(args.timezone)
        collect(Handler.paths, Handler.timezone, Handler.ffmpeg)
    except (RuntimeError, ZoneInfoNotFoundError) as error:
        print(msg("erreur_generique", erreur=error))
        return 1

    # 127.0.0.1 and not 0.0.0.0 by default: the dashboard has no
    # authentication; it has no business on the local network without an
    # explicit choice. BLINK_BIND lets that be overridden for two uses:
    # direct access from other LAN machines (reserve for a trusted
    # home network, never exposed on the internet), or a Docker
    # container where 127.0.0.1 would mean the container's loopback,
    # unreachable from the host even with the published port (bridge network
    # routes to the container interface, not its loopback): the
    # security boundary is then set by the port publication
    # itself (see docker-compose.yml, hardcoded 127.0.0.1:).
    bind = os.environ.get("BLINK_BIND", "127.0.0.1")

    class Server(http.server.ThreadingHTTPServer):
        # http.server enables allow_reuse_address, whose semantics differ
        # under Windows: several servers can bind the same port without
        # any error, and which receives connections is indeterminate.
        # A forgotten instance then keeps serving its in-memory page, and
        # one thinks changes have no effect. So we prefer a clear
        # refusal here, keeping usual Unix behavior elsewhere (where
        # the option lets a restart without waiting out TIME_WAIT).
        allow_reuse_address = os.name != "nt"

        def handle_error(self, request, client_address):
            # socketserver default behavior: traceback.print_exc()
            # on stderr only — invisible with console=False (review of
            # 27/08: a storage-folder change did not restart
            # at all, without the slightest trace anywhere to say
            # why). Also logged to a file, never instead of:
            # remains useful even when a real console exists.
            import traceback
            try:
                with (runtime.app_dir() / "serve_erreurs.log").open(
                        "a", encoding="utf-8") as journal:
                    journal.write(f"\n--- {dt.datetime.now().isoformat()} "
                                  f"{client_address} ---\n")
                    traceback.print_exc(file=journal)
            except OSError:
                pass
            super().handle_error(request, client_address)

    try:
        server = Server((bind, args.port), Handler)
    except OSError as error:
        print(msg("echec_ecoute_port", port=args.port, erreur=error))
        print(msg("autre_instance_deja"))
        return 1
    url = f"http://127.0.0.1:{args.port}/"
    print(msg("interface_disponible", url=url))
    if bind not in ("127.0.0.1", "localhost"):
        # webbrowser.open() ci-dessous garde volontairement 127.0.0.1 (le
        # open browser is this machine's), but someone who
        # who set BLINK_BIND for LAN access must see, without having to
        # reread the docs, that they just opened the UI with no authentication
        # to the rest of the network.
        print(msg("attention_blink_bind", bind=bind, port=args.port))
    if Handler.trusted_host:
        print(msg("attention_trusted_host", hote=Handler.trusted_host))
    # The initial configuration server must create no background
    # work before validation. Even version watching, unrelated to
    # clips, therefore waits for the real start to keep this mode strictly
    # limited to the form.
    if not args.initial_setup:
        veiller_sur_les_versions()
    if args.open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()

    def surveiller_arret_demande():
        # An external stop (tray, stop) sets this flag rather than killing the
        # process directly: shutdown() lets any request already in
        # progress finish normally (review of 27/08, cooperative stop).
        while not runtime.arret_demande():
            time.sleep(1)
        server.shutdown()

    fil_arret = threading.Thread(target=surveiller_arret_demande, daemon=True)
    fil_arret.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(msg("interruption_arret"))
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
