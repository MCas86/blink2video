"""Runtime landmarks, the same whether running from sources or from a bundle.

Three questions change their answers once the tool is frozen by PyInstaller, and
each fails silently if not handled.

Where is the data? Next to the sources in development, next to the executable
once distributed. Never in the temporary extraction folder, which disappears on
exit: the archive would be lost there.

Where are bundled resources, ffmpeg in particular? In that extraction folder,
which PyInstaller exposes as sys._MEIPASS.

How does the program relaunch itself? In development we invoke the interpreter
with a script; in a bundle there is no script anymore, only the executable and
its verbs. That is the classic pitfall of the "launcher + bundle" pattern: a
program that relaunches via sys.executable + script path works perfectly in
development and fails once frozen.
"""

from __future__ import annotations  # Python 3.8 (Windows 7 build): "int | None" annotations are only stored as strings, never evaluated at runtime.

import argparse
import contextlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import NamedTuple

import _amorcage


# Tool version, and the sole source for the release tag: the release workflow
# rejects a tag that does not match. A binary must be able to say what it is,
# if only so a bug report is actionable.
VERSION = "0.23.0"
WINDOWS7_BUILD_MARKER = "windows7-build.txt"


# Single source of truth for verbs: their order in help, the program that runs
# them, and what they do. Three uses follow: displayed help, delegating a verb
# to the right program, and relaunching the tool on another verb. Keeping them
# in three places guaranteed drift, which happened: "autostart" was missing from
# one description, "list" and "login" did not appear in the verb list at all.
#
# The "blink" program is blink2video.py itself, which handles these verbs without
# delegating: the verb is then passed as an argument.
# Each verb carries two labels: CLI help uses French, docs.py picks one or the
# other depending on which README it fills. The English README long showed the
# verb list in French for lack of this column.
# Entry point name, without extension. Verbs it handles itself refer to this
# constant: inferring it from a hand-written marker survived a file rename and
# broke every relaunch silently.
ENTREE = "blink2video"


# Recommended configuration in one place: "start" runs it, "autostart on" schedules
# it. Each activity has its own cadence because costs differ: cloud inventory is
# a 0.13 s account call, the USB manifest wakes the sync module, merging does
# nothing when nothing changed. Verbose to read, never to type.
REGLAGES = "blink_reglages.json"
REGLAGES_DEFAUT = {"usb_minutes": 10, "cloud_minutes": 1, "port": 8765, "timestamp": False,
                   "timezone": "Europe/Paris", "merge_jour": True, "merge_semaine": False,
                   "merge_mois": False, "download_auto": True, "live_protocol": "webrtc",
                   "font_size": None, "font_color": "white", "box_opacity": 0.55,
                   "trusted_host": "", "webhook_notif_url": "",
                   "live_auto_stop_seconds": 0,
                   # Empty: default folder, see dossier_sorties().
                   "dossier_sorties": ""}
# Replaces BLINK_DIRECT_WEBRTC env var (experimental, BACKLOG.md 2026-09-03) once
# WebRTC is valid in real use: a real setting, not just a variable to set before
# launching the server. "mse" remains a deliberate fallback (web page setting);
# serve.py falls back to MSE anyway if aiortc is not installed, regardless of
# this choice. MJPEG was compared to MSE once (audit 28.15) and dead code removed
# since (commit 7339f85): not reintroduced as a third option without new reason.
PROTOCOLES_LIVE_VALIDES = ("webrtc", "mse")
# Weekly and monthly used to re-encode by default the same material as daily
# (each merge re-encodes its clips, never a simple file grouping): on a fresh
# install, that silent tripling of space and CPU time surprised more than it
# helped. Daily alone remains the default; the other two stay an explicit choice
# in settings (reported on Reddit, 2026-08-26).
# Number of elements in the fixed block at the head of standard(): serve, --port,
# value, --timezone, value, --trusted-host, value. blink_cli.route() relies on
# this length to graft the "start" extra right after (see standard()).
LONGUEUR_BLOC_SERVE = 7


def _entier_borne(valeurs: dict, champ: str, defaut: int, minimum: int,
                  maximum: int | None = None) -> int:
    """Read a bounded integer from the settings JSON, fall back to `defaut`
    when the value is missing, wrong type, or out of range.

    Same bounds as validation on write (`/api/reglages` in serve.py): these
    min/max limits are not new, only reading them here is — a hand-edited or
    corrupt file (code review 0eab463, bug #10) could bypass both checks unlike
    the web form."""
    try:
        nombre = int(valeurs.get(champ, defaut))
    except (TypeError, ValueError):
        return defaut
    if nombre < minimum or (maximum is not None and nombre > maximum):
        return defaut
    return nombre


def _booleen(valeurs: dict, champ: str, defaut: bool) -> bool:
    """Read a boolean, fall back to `defaut` if it is not one.

    `bool(valeur)` alone makes almost everything true (only 0, "", None, [], {}
    are false in Python): a "false" string — plausible when hand-editing JSON,
    since JSON requires unquoted `false` — became `True`, the opposite of intent
    (code review 0eab463, bug #10). Only a real JSON boolean (unquoted
    `true`/`false`) is accepted now; everything else falls back to the default
    rather than being misinterpreted silently."""
    valeur = valeurs.get(champ, defaut)
    return valeur if isinstance(valeur, bool) else defaut


def _entier_optionnel_borne(valeurs: dict, champ: str, defaut: int | None,
                            minimum: int, maximum: int) -> int | None:
    """Like `_entier_borne`, but `None` (missing or explicit) is a valid value
    on its own: font size auto-fit to video height (merge_daily.STYLE_PAR_DEFAUT),
    not an error."""
    brut = valeurs.get(champ, defaut)
    if brut is None:
        return None
    try:
        nombre = int(brut)
    except (TypeError, ValueError):
        return defaut
    if nombre < minimum or nombre > maximum:
        return defaut
    return nombre


def _flottant_borne(valeurs: dict, champ: str, defaut: float,
                    minimum: float, maximum: float) -> float:
    """Like `_entier_borne`, for a floating-point number (banner opacity)."""
    try:
        nombre = float(valeurs.get(champ, defaut))
    except (TypeError, ValueError):
        return defaut
    if nombre < minimum or nombre > maximum:
        return defaut
    return nombre


def lire_reglages() -> dict:
    """Current USB/cloud cadences, port, timestamping and timezone, editable from
    the web page.

    Missing or unreadable file: defaults, same as those hard-coded here before
    this setting existed."""
    try:
        valeurs = json.loads((app_dir() / REGLAGES).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(REGLAGES_DEFAUT)
    if not isinstance(valeurs, dict):
        # Valid JSON (`[]`, `"texte"`, `42`...) but not an object: as unusable
        # as unreadable (code review 0eab463, bug #10 — `.get()` on non-dict
        # raised AttributeError, never caught).
        return dict(REGLAGES_DEFAUT)
    return {
        "usb_minutes": _entier_borne(valeurs, "usb_minutes",
                                     REGLAGES_DEFAUT["usb_minutes"], 1),
        "cloud_minutes": _entier_borne(valeurs, "cloud_minutes",
                                       REGLAGES_DEFAUT["cloud_minutes"], 1),
        "port": _entier_borne(valeurs, "port", REGLAGES_DEFAUT["port"], 1, 65535),
        "timestamp": _booleen(valeurs, "timestamp", REGLAGES_DEFAUT["timestamp"]),
        "timezone": str(valeurs.get("timezone", REGLAGES_DEFAUT["timezone"])) or
        REGLAGES_DEFAUT["timezone"],
        "merge_jour": _booleen(valeurs, "merge_jour", REGLAGES_DEFAUT["merge_jour"]),
        "merge_semaine": _booleen(valeurs, "merge_semaine", REGLAGES_DEFAUT["merge_semaine"]),
        "merge_mois": _booleen(valeurs, "merge_mois", REGLAGES_DEFAUT["merge_mois"]),
        "download_auto": _booleen(valeurs, "download_auto", REGLAGES_DEFAUT["download_auto"]),
        "live_protocol": valeurs.get("live_protocol") if valeurs.get("live_protocol")
        in PROTOCOLES_LIVE_VALIDES else REGLAGES_DEFAUT["live_protocol"],
        "font_size": _entier_optionnel_borne(valeurs, "font_size",
                                             REGLAGES_DEFAUT["font_size"], 8, 500),
        "font_color": str(valeurs.get("font_color", REGLAGES_DEFAUT["font_color"])) or
        REGLAGES_DEFAUT["font_color"],
        "box_opacity": _flottant_borne(valeurs, "box_opacity",
                                       REGLAGES_DEFAUT["box_opacity"], 0.0, 1.0),
        "trusted_host": str(valeurs.get(
            "trusted_host", REGLAGES_DEFAUT["trusted_host"])).strip(),
        "webhook_notif_url": str(valeurs.get(
            "webhook_notif_url", REGLAGES_DEFAUT["webhook_notif_url"])).strip(),
        # 300 = LIVE_MAX_SECONDS (serve.py), the hard cap that ends every live
        # stream anyway: beyond that, the setting would have no effect.
        "live_auto_stop_seconds": _entier_borne(
            valeurs, "live_auto_stop_seconds",
            REGLAGES_DEFAUT["live_auto_stop_seconds"], 0, 300),
        "dossier_sorties": str(valeurs.get("dossier_sorties") or "").strip(),
    }


def _ecrire_texte_atomique(cible: Path, contenu: str) -> None:
    """Replace a small UTF-8 file after writing it completely alongside it.

    Per-call temporary file, even within the same process, and replace that
    retries transient Windows refusal (antivirus, indexer): nico579_commons.atomique,
    shared across the four applications. The caller keeps folder creation,
    serialization and transaction locks: atomic replace alone does not protect
    a concurrent read-modify-write. Errors propagate.
    """
    from nico579_commons import atomique

    temporaire = atomique.chemin_part(cible)
    try:
        temporaire.write_text(contenu, encoding="utf-8")
        atomique.remplacer(temporaire, cible)
    finally:
        temporaire.unlink(missing_ok=True)


def ecrire_reglages(usb_minutes: int, cloud_minutes: int, port: int, timestamp: bool,
                    timezone: str, merge_jour: bool, merge_semaine: bool,
                    merge_mois: bool, download_auto: bool, live_protocol: str, *,
                    font_size: int | None = None, font_color: str = "white",
                    box_opacity: float = 0.55, trusted_host: str = "",
                    webhook_notif_url: str = "",
                    live_auto_stop_seconds: int = 0,
                    dossier_sorties: str | None = None,
                    dossier: Path | None = None) -> None:
    cible = (app_dir() if dossier is None else dossier) / REGLAGES
    if dossier_sorties is None:
        # Omitted from the call: keep what is already saved. Resetting to empty
        # would silently send later clips to the default folder.
        try:
            actuel = json.loads(cible.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            actuel = {}
        dossier_sorties = str(actuel.get("dossier_sorties") or "") \
            if isinstance(actuel, dict) else ""
    _ecrire_texte_atomique(cible, json.dumps({
        "usb_minutes": int(usb_minutes), "cloud_minutes": int(cloud_minutes),
        "port": int(port), "timestamp": bool(timestamp),
        "timezone": str(timezone), "merge_jour": bool(merge_jour),
        "merge_semaine": bool(merge_semaine), "merge_mois": bool(merge_mois),
        "download_auto": bool(download_auto), "live_protocol": str(live_protocol),
        "font_size": int(font_size) if font_size is not None else None,
        "font_color": str(font_color), "box_opacity": float(box_opacity),
        "trusted_host": str(trusted_host).strip(),
        "webhook_notif_url": str(webhook_notif_url).strip(),
        "live_auto_stop_seconds": int(live_auto_stop_seconds),
        "dossier_sorties": str(dossier_sorties).strip(),
    }))


LANGUE = "blink_langue.txt"


def lire_langue() -> str:
    """Language of the last loaded web page, "fr" or "en".

    Missing or unreadable file (nothing loaded yet): "en". Used by the
    systray menu (tray.py) so it follows the page language."""
    try:
        contenu = (app_dir() / LANGUE).read_text(encoding="utf-8").strip()
    except OSError:
        return "en"
    return "en" if contenu == "en" else "fr"


def ecrire_langue(code: str) -> None:
    """Remember the language shown by the web page (POST /api/lang), on every
    load or explicit change — not only manual choice: without this, a first run
    never touched via FR/EN buttons would leave the systray menu at default even
    if the page already showed English (browser-detected language)."""
    _ecrire_texte_atomique(app_dir() / LANGUE, "en" if code == "en" else "fr")


def traduire(libelles: dict, cle: str, **valeurs) -> str:
    """Return libelles[langue][cle] in the page language (lire_langue()), formatted
    with valeurs when needed.

    Same bilingual dict with "fr"/"en" keys as LIBELLES in tray.py, reused here
    for messages that commands (download, login, merge...) print to their own
    stdout: those processes have no web page to delegate translation to, only
    `blink_langue.txt` left by the last page load (GitHub issue #6: those
    messages stayed all French)."""
    texte = libelles[lire_langue()][cle]
    return texte.format(**valeurs) if valeurs else texte


# Messages that runtime.py prints itself (bootstrap, --loop loop).
# Safe from bootstrap, before any dependency is installed: lire_langue() uses
# only the stdlib and falls back to "fr" if nothing is written yet.
_LIBELLES_RUNTIME = {
    "en": {
        "repetition": "Repeating every {minutes} min. Ctrl+C to stop.",
        "tour_interrompu": "Run interrupted by an error, retrying next time: {erreur}",
        "arret": "\nStopped.",
        "aide_loop": "repeat every N minutes instead of running once (default 10)",
    },
}
_LIBELLES_RUNTIME["fr"] = dict(_LIBELLES_RUNTIME["en"])


def _msg(cle: str, **valeurs) -> str:
    return traduire(_LIBELLES_RUNTIME, cle, **valeurs)


JETON_WEBHOOK = "blink_webhook_token.txt"


def lire_jeton_webhook() -> str:
    """Snapshot webhook secret (GitHub issue #9), generated once.

    Unlike the session token (uuid4, one per process, never shown), this one must
    survive restarts and be copyable into a third-party system (smarthome): it
    lives in a file, not in memory. secrets.token_urlsafe rather than uuid4: the
    API meant for a secret that must resist guessing, not just name a process."""
    chemin = app_dir() / JETON_WEBHOOK
    try:
        jeton = chemin.read_text(encoding="utf-8").strip()
    except OSError:
        jeton = ""
    if jeton:
        return jeton
    return regenerer_jeton_webhook()


def regenerer_jeton_webhook() -> str:
    """Invalidate the old secret (any system already configured must be updated):
    used both to create the first token and to revoke a leaked URL."""
    jeton = secrets.token_urlsafe(32)
    _ecrire_texte_atomique(app_dir() / JETON_WEBHOOK, jeton)
    return jeton


SUPPRESSION_AUTO = "blink_suppression_auto.json"


def lire_suppression_auto() -> set:
    """Cameras whose USB clips are deleted from the Sync Module once downloaded
    successfully (GitHub issue #1: free buffer memory).

    Per camera, not globally: an uncertain camera (little track record on
    download reliability) should stay in retention while another, proven one,
    frees its memory. Missing or unreadable file: empty set, nothing deleted by
    default."""
    import json

    try:
        contenu = (app_dir() / SUPPRESSION_AUTO).read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        cameras = json.loads(contenu)
    except json.JSONDecodeError:
        return set()
    if not isinstance(cameras, list):
        return set()
    return {str(c) for c in cameras if isinstance(c, str)}


def ecrire_suppression_auto(cameras: set) -> None:
    """Save the set of cameras in automatic deletion (USB)."""
    _ecrire_texte_atomique(app_dir() / SUPPRESSION_AUTO,
                           json.dumps(sorted(cameras), ensure_ascii=False))


def options_fusion(reglages: dict) -> list:
    """Options shared by automatic merges and those requested from the page.

    The caller decides whether to run merge based on merge_jour. Once started,
    it keeps the same timezone and enabled outputs everywhere.
    """
    options = ["--timezone", reglages["timezone"]]
    if not reglages["timestamp"]:
        options.append("--no-timestamp")
    else:
        # Only when this differs from merge_daily's own defaults: an install that
        # never touched these settings must run exactly the same command line as
        # before they existed.
        if reglages["font_size"] is not None:
            options += ["--font-size", str(reglages["font_size"])]
        if reglages["font_color"] != "white":
            options += ["--font-color", reglages["font_color"]]
        if reglages["box_opacity"] != 0.55:
            options += ["--box-opacity", str(reglages["box_opacity"])]
    if not reglages["merge_semaine"]:
        options.append("--no-weekly")
    if not reglages["merge_mois"]:
        options.append("--no-monthly")
    return options


def standard() -> tuple:
    """Recommended composition: same verbs as the old STANDARD constant, but
    USB/cloud cadences, port, timestamping and timezone come from `lire_reglages()`
    rather than being fixed here, so web settings apply on the next start.

    The first LONGUEUR_BLOC_SERVE elements (serve, --port, value, --timezone,
    value, --trusted-host, value) are a fixed block: blink_cli.route() treats
    them as the "verb" part that a hand-typed extra ("start --port 8899") grafts
    onto, so an explicit --port, --timezone or --trusted-host always wins over
    the saved value (argparse keeps the last occurrence of an option)."""
    c = lire_reglages()
    merge = []
    if c["merge_jour"]:
        merge = ["merge", "--loop", "5", *options_fusion(c)]
    download = []
    if c["download_auto"]:
        # One worker inventories both sources first: the progress bar then knows
        # the total clip count to fetch, especially right after a folder change.
        # It then keeps both cadences without waking USB as often as cloud.
        download = ["download", "--from", "all",
                    "--usb-loop", str(c["usb_minutes"]),
                    "--cloud-loop", str(c["cloud_minutes"])]
    return ("serve", "--port", str(c["port"]), "--timezone", c["timezone"],
            "--trusted-host", c["trusted_host"],
            "watch", "--loop", "10",
            *download, *merge)


class Verbe(NamedTuple):
    """One verb: the program that runs it, and its label in each language.

    A named tuple rather than a plain tuple because the table is read from five
    places, including a .spec file: when a third column appeared, everyone
    unpacking "module, text" broke at once. Naming fields removes that whole
    class of failures."""

    module: str
    fr: str
    en: str


VERBES = {
    "login": Verbe(ENTREE,
              "se connecter au compte Blink, vérification en deux étapes gérée",
              "sign in to the Blink account, two-step verification handled"),
    "list": Verbe(ENTREE,
             "ce que contient le module de synchronisation en ce moment",
             "what the Sync Module currently holds"),
    "download": Verbe(ENTREE,
                 "récupérer les nouveaux clips avant que la rotation ne les efface",
                 "fetch new clips before rotation erases them"),
    "merge": Verbe("merge_daily",
              "normaliser, horodater et assembler jour, semaine et mois",
              "normalize, stamp and assemble day, week and month"),
    "watch": Verbe("watch",
              "contrôler l'état de l'installation et alerter s'il se dégrade",
              "check the installation and alert when it degrades"),
    "serve": Verbe("serve",
              "servir l'interface web, pour regarder, écarter, voir en direct",
              "serve the web interface, to watch, discard, see live"),
    "start": Verbe(ENTREE,
             "tout lancer avec les réglages recommandés",
             "start everything with the recommended settings"),
    "open": Verbe(ENTREE,
            "ouvrir l'interface web dans le navigateur",
            "open the web interface in the browser"),
    "stop": Verbe(ENTREE,
            "arrêter l'instance qui tourne en fond",
            "stop the instance running in the background"),
    "restart": Verbe(ENTREE,
               "arrêter puis relancer avec les réglages actuels",
               "stop then relaunch with the current settings"),
    "update": Verbe("maj",
              "installer la dernière version publiée",
              "install the latest published release"),
    "autostart": Verbe("autostart",
                  "inscrire à l'ouverture de session la commande qui suit",
                  "register the command that follows with your session"),
    "smoketest": Verbe("smoketest",
                  "vérifier que l'installation fonctionne sur cette machine",
                  "check that the installation works on this machine"),
}

# Verbs delegated to another program, derived from the table above.
DELEGUES = {nom: verbe.module for nom, verbe in VERBES.items()
            if verbe.module != ENTREE}


def extraire_mode_bootstrap(argv: list) -> list:
    """Remove --bootstrap=... from `argv` if present, and store its value in
    BLINK_BOOTSTRAP. Returns `argv` without it.

    --bootstrap is not declared by any verb parser, only understood by
    bootstrap(): left in argv, it makes argparse fail with "unrecognized
    arguments" before bootstrap() could read it. Call BEFORE any parse_args(),
    not only inside bootstrap() itself — route() (blink_cli.py) uses it at the
    very top to cover verbs like download, whose arguments were not stripped
    before reaching argparse (review 27/08, bug 4)."""
    nettoye = []
    for argument in argv:
        if argument.startswith("--bootstrap="):
            os.environ["BLINK_BOOTSTRAP"] = argument.split("=", 1)[1]
        else:
            nettoye.append(argument)
    return nettoye


def bootstrap() -> None:
    """Prepare an isolated Python environment, then relaunch into it.

    Call at the very top of an entry point, BEFORE importing aiohttp or blinkpy:
    chicken-and-egg — you cannot check dependencies after failing to import them.

    The engine is _amorcage.py, a byte-for-byte copy of nico579_commons.amorcage,
    shared across the four applications (it runs before the common library is
    installed, so it cannot import it: test_amorcage_commun.py compares the two).
    Dependencies come from requirements.in, installed from lock file
    requirements.txt; there is no second list here. On Python 3.8 (Windows 7
    edition), the lock compiled for 3.11 does not apply: install requirements.in.

    Four modes, chosen by --bootstrap= or BLINK_BOOTSTRAP:
      auto (default): venv in ~/.blink2video/venv, created as needed, then relaunch;
                      if dependencies are already present and no venv exists
                      (a Docker image), the current environment is enough
      force         : like auto, even if another environment is active
      pip           : install into the current environment
      none          : no install, verify and explain

    No effect in a bundle, which already embeds everything."""
    if frozen():
        return
    sys.argv[1:] = extraire_mode_bootstrap(sys.argv[1:])
    amorcage().lancer()


def amorcage() -> "_amorcage.Amorcage":
    return _amorcage.Amorcage(
        "blink2video", Path(__file__).resolve().parent, variable="BLINK_BOOTSTRAP",
        verrou=None if sys.version_info < (3, 9) else "requirements.txt",
        reutiliser_environnement=True, langue=lire_langue)


def frozen() -> bool:
    """True when the program runs from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


POINTEUR_STOCKAGE = "blink_home.txt"
MARQUEUR_CONFIGURATION_INITIALE = ".blink_configuration_initiale_effectuee"
MARQUEUR_CONFIGURATION_EN_ATTENTE = ".blink_configuration_initiale_en_attente"
# Report of state migration from a version ≤ 0.13 (see _migrer_etat_historique):
# its presence also means migration is done.
MARQUEUR_MIGRATION = ".blink_etat_migre.json"
# State this migration copies. Literal names for those defined by other modules
# that runtime cannot import: test_runtime_dossier_stockage.py checks they match.
ETAT_HISTORIQUE = (REGLAGES, "blink_auth.json", LANGUE, JETON_WEBHOOK,
                   SUPPRESSION_AUTO, ".blink_passages.json",
                   ".blink_watch_state.json", ".blink_raccourci_cree",
                   ".blink_maj.json")
# What the program produces, stored under dossier_sorties().
DOSSIERS_SORTIES = ("Blink_Clips", "Blink_Daily", "Blink_Weekly", "Blink_Monthly",
                    "Blink_Normalized", "Blink_Excluded", "Blink_Direct",
                    "Blink_Snapshots")


def _dossier_ancre() -> Path:
    """Program directory: next to the executable (frozen) or the sources.

    Until 0.13, this was also the data directory, unless `POINTEUR_STOCKAGE`,
    written here, redirected elsewhere. It now only serves to find that data
    (migration, update from those versions) and what ships with the program."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def app_dir_depuis(ancre: Path) -> Path:
    """Data directory of a version ≤ 0.13 installed in `ancre`: target of its
    `blink_home.txt`, otherwise the anchor itself.

    Used for state migration and by maj.py, which must still understand roots
    passed by launchers of those versions. There, the freshly extracted version
    runs from a temporary folder whose natural anchor does not know the real
    install's `blink_home.txt`. It must tell the relaunched version the right
    folder without locking it to the anchor if the user redirected storage:
    following the pointer from the given anchor yields either correctly, unlike
    forcing the anchor without reading it (BLINK_HOME would then overwrite it,
    including explicitly redirected storage — in practice this reset the data
    folder to the executable's after every update, reported on Reddit, 2026-08-26)."""
    pointeur = ancre / POINTEUR_STOCKAGE
    try:
        cible = pointeur.read_text(encoding="utf-8").strip()
    except OSError:
        cible = ""
    if cible:
        return Path(cible).expanduser().resolve()
    return ancre


def _dossier_etat_standard() -> Path:
    """Standard OS state directory, the one platformdirs provides.

    runtime.py must stay importable before bootstrap() installs dependencies
    (sources launched with Python that lacks them). Without platformdirs, the
    same rules for the three supported systems: test_runtime_dossier_stockage.py
    compares both results, and CI runs it on each."""
    try:
        from platformdirs import user_data_dir
    except ImportError:
        if os.name == "nt":
            base = (os.environ.get("LOCALAPPDATA")
                    or str(Path.home() / "AppData" / "Local"))
        elif sys.platform == "darwin":
            base = str(Path.home() / "Library" / "Application Support")
        else:
            base = (os.environ.get("XDG_DATA_HOME", "").strip()
                    or str(Path.home() / ".local" / "share"))
        return (Path(base) / ENTREE).resolve()
    return Path(user_data_dir(ENTREE, appauthor=False)).resolve()


# State directories already created by this process.
_ETATS_CREES: set = set()


def app_dir() -> Path:
    """State directory: settings, Blink session, preferences, logs and files
    that drive processes. Never what the program produces, stored in
    dossier_sorties().

    BLINK_HOME overrides it (tests, Docker, portable install). Otherwise the
    standard OS folder (LOCALAPPDATA on Windows), same convention as lidar2map
    and watch2notif. It no longer depends on program location: sources and
    executable, or two copies of the executable, share the same state, and an
    update never touches it. preparer_etat(), at startup, migrates state from
    version ≤ 0.13."""
    force = os.environ.get("BLINK_HOME")
    if force:
        return Path(force).expanduser().resolve()
    etat = _dossier_etat_standard()
    if etat not in _ETATS_CREES:
        try:
            etat.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # The first write will explain why.
        else:
            _ETATS_CREES.add(etat)
    return etat


def preparer_etat() -> None:
    """Migrate once from a version ≤ 0.13 (see _migrer_etat_historique). No-op
    with BLINK_HOME.

    Called at startup (blink2video.py), before any module reads constants from
    app_dir() or dossier_sorties() at import. Explicit, like watch2notif's
    migrer_donnees_existantes(), rather than hidden in app_dir(): a simple path
    computation in a test must never copy state or move cards of a running
    instance. Never blocking: on failure, old state stays and migration retries
    on next launch."""
    if os.environ.get("BLINK_HOME"):
        return
    etat = app_dir()
    try:
        if (etat / MARQUEUR_MIGRATION).is_file():
            return
        # start and its verbs start together: one process copies, others wait
        # on its lock then find the marker.
        with verrou("migration-etat", "migration", attente=60, racine=etat):
            if not (etat / MARQUEUR_MIGRATION).is_file():
                _migrer_etat_historique(etat)
    except (OSError, BusyError) as erreur:
        ajouter_ligne("migration.log", f"State migration deferred: {erreur}")


def _copier_si_absent(source: Path, cible: Path) -> bool:
    """Atomic file copy, never overwriting the target."""
    import uuid

    if cible.exists() or not source.is_file():
        return False
    cible.parent.mkdir(parents=True, exist_ok=True)
    temporaire = cible.with_name(f".{cible.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        shutil.copy2(source, temporaire)
        temporaire.replace(cible)
    finally:
        temporaire.unlink(missing_ok=True)
    return True


def _migrer_etat_historique(etat: Path) -> None:
    """Migrate into `etat` from a version ≤ 0.13, once.

    Those versions kept everything in one place: next to the program, or in the
    target of blink_home.txt. Settings, Blink session, preferences and markers
    are copied without overwriting what `etat` already has, never moved: rolling
    back to 0.13 stays possible. Running-process cards are moved instead so
    stop finds them in one place, including from an instance started before the
    update. Clips do not move: if the old folder has them, dossier_sorties
    points there, nothing changes on disk.

    The marker is written only if something was migrated. With nothing to
    migrate, the check runs again on next launch, for a few disk accesses: an
    update finalizer launched from a temporary folder with no data must not
    close migration before the installed version could run it, and nothing a
    trial write put in `etat` can block that either."""
    import datetime as dt

    ancre = _dossier_ancre()
    ancien = app_dir_depuis(ancre)
    repris = []
    if ancien != etat:
        for nom in ETAT_HISTORIQUE:
            if _copier_si_absent(ancien / nom, etat / nom):
                repris.append(nom)
    # Cards and markers lived next to the program (old control root), even when
    # data was redirected.
    if ancre != etat:
        for nom in (MARQUEUR_CONFIGURATION_INITIALE, MARQUEUR_CONFIGURATION_EN_ATTENTE):
            if _copier_si_absent(ancre / nom, etat / nom):
                repris.append(nom)
        fiches = ancre / INSTANCES
        for fiche in sorted(fiches.glob("*.json")) if fiches.is_dir() else []:
            cible = etat / INSTANCES / fiche.name
            if not cible.exists():
                cible.parent.mkdir(parents=True, exist_ok=True)
                # move: copy then delete if state is on another volume.
                shutil.move(str(fiche), str(cible))
                repris.append(f"{INSTANCES.name}/{fiche.name}")

    sorties = ""
    if ancien != etat and any((ancien / nom).is_dir() for nom in DOSSIERS_SORTIES):
        chemin = etat / REGLAGES
        try:
            reglages = json.loads(chemin.read_text(encoding="utf-8"))
        except FileNotFoundError:
            reglages = {}
        except (OSError, ValueError):
            reglages = None  # Unreadable: do not overwrite.
        if isinstance(reglages, dict) and not str(reglages.get("dossier_sorties") or "").strip():
            reglages["dossier_sorties"] = sorties = str(ancien)
            _ecrire_texte_atomique(chemin, json.dumps(reglages))

    if not (repris or sorties):
        return
    _ecrire_texte_atomique(etat / MARQUEUR_MIGRATION, json.dumps({
        "version": VERSION,
        "date": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "depuis": str(ancien),
        "repris": repris,
        "dossier_sorties": sorties,
    }, ensure_ascii=False, indent=2))


def _sorties_par_defaut() -> Path:
    """BLINK_HOME when set, as before 0.14; otherwise a visible folder,
    Documents/blink2video."""
    force = os.environ.get("BLINK_HOME")
    if force:
        return Path(force).expanduser().resolve()
    try:
        from platformdirs import user_documents_dir
    except ImportError:
        # Same fallback as _dossier_etat_standard(), before bootstrap().
        documents = Path.home() / "Documents"
    else:
        documents = Path(user_documents_dir())
    return (documents / ENTREE).resolve()


def dossier_sorties() -> Path:
    """Folder for what the program produces: clips, assembled videos, live
    streams, snapshots (see DOSSIERS_SORTIES), with their registries and
    thumbnails.

    Configurable from the page (dossier_sorties setting); otherwise BLINK_HOME
    when set, else Documents/blink2video. Modules read constants at import: a
    change applies on restart triggered by the page after saving."""
    choisi = lire_reglages()["dossier_sorties"]
    if choisi:
        return Path(choisi).expanduser().resolve()
    return _sorties_par_defaut()


def ajouter_ligne(nom_fichier: str, ligne: str) -> None:
    """Append one line to a file in app_dir(), append-only, never fatal.

    For traces that must survive without a console: normal launch uses pythonw
    (autostart), which has no observable stdout or stderr. watch.py has its own
    journal() for the same reason; this is the shared equivalent for other
    callers (timestamp stays in the caller's format, not added here, to avoid
    duplicating a possible print() of the same line)."""
    try:
        with (app_dir() / nom_fichier).open("a", encoding="utf-8") as fichier:
            fichier.write(ligne + "\n")
    except OSError:
        pass


def lire_dossier_stockage() -> str:
    """Output folder as shown in the settings panel: effective value, including
    when it is the default."""
    return str(dossier_sorties())


def _publier_octets(cible: Path, contenu: bytes | None) -> None:
    """Publish bytes atomically, or delete the file if content is absent."""
    import uuid

    if contenu is None:
        cible.unlink(missing_ok=True)
        return
    temporaire = cible.with_name(f".{cible.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temporaire.write_bytes(contenu)
        temporaire.replace(cible)
    finally:
        temporaire.unlink(missing_ok=True)


def ecrire_dossier_stockage(chemin: str, *, reglages: dict | None = None,
                            configuration_initiale: bool = False) -> None:
    """Change the output folder; an empty path reverts to default.

    Nothing is moved: existing files stay where they are, new ones go to the new
    folder. State (session, settings) no longer follows, it stays in app_dir().
    The folder is created first: unusable locations (missing drive, permissions)
    are rejected before saving.
    """
    chemin = chemin.strip()
    valeur = str(Path(chemin).expanduser().resolve()) if chemin else ""
    (Path(valeur) if valeur else _sorties_par_defaut()).mkdir(parents=True, exist_ok=True)

    # The start parent watches the initial marker to launch workers.
    # Keeping exact settings allows restoring them if this last write fails.
    fichier = app_dir() / REGLAGES
    precedents = None
    if configuration_initiale:
        try:
            precedents = fichier.read_bytes()
        except FileNotFoundError:
            pass

    ecrire_reglages(**{**(lire_reglages() if reglages is None else reglages),
                       "dossier_sorties": valeur})
    if configuration_initiale:
        try:
            marquer_configuration_initiale()
        except OSError as erreur:
            try:
                _publier_octets(fichier, precedents)
            except OSError as restauration:
                raise OSError(
                    "Failed to write initial marker and restore settings: "
                    f"{restauration}") from erreur
            raise


def resource_dir() -> Path:
    """Directory of resources bundled in the executable."""
    if frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    return Path(__file__).resolve().parent


def build_windows7() -> bool:
    """True only in the legacy bundle marked at build time."""
    return frozen() and (resource_dir() / WINDOWS7_BUILD_MARKER).is_file()


def version_affichee() -> str:
    """Stable numeric version, plus legacy bundle flavor when applicable."""
    suffixe = " (Windows 7 legacy)" if build_windows7() else ""
    return VERSION + suffixe


def est_relatif_a(chemin: Path, racine: Path) -> bool:
    """Equivalent of Path.is_relative_to, missing before Python 3.9 (Windows 7
    build, Python 3.8: see build-win7.yml)."""
    try:
        chemin.relative_to(racine)
    except ValueError:
        return False
    return True


# One card per running instance, named after the process that holds it.
# A single file would suffice while only one instance runs, but nothing forbids
# two: autostart and a manual launch coexist fine, and "stop" must stop all.
INSTANCES = Path(".blink_run")
INSTANCE_PID_ENV = "BLINK_INSTANCE_PID"


def _dossier_controle() -> Path:
    """Root of files that drive running processes: ``.blink_run`` cards,
    shutdown request, initial-setup markers.

    Until 0.13, the install anchor: the data folder could change from the page,
    and cards stored with it became invisible to ``stop`` at the switch. Since
    0.14, only the output folder changes, and state is shared across all of the
    user's installs, Blink session included: cards live there too so only one
    server at a time uses them, whether launched from sources or the executable.

    ``BLINK_CONTROL_HOME`` is set only by update: its finalizer must find cards
    from the version it replaces, where that version stored them.
    """
    force = os.environ.get("BLINK_CONTROL_HOME") or os.environ.get("BLINK_HOME")
    if force:
        return Path(force).expanduser().resolve()
    return app_dir()


def _ecrire_marqueur_configuration(nom: str) -> None:
    """Write a small marker atomically in the control root.

    Changing the output folder to an empty destination must never look like a
    fresh install: the marker stays with state, which does not follow outputs.
    """
    dossier = _dossier_controle()
    dossier.mkdir(parents=True, exist_ok=True)
    _ecrire_texte_atomique(dossier / nom, VERSION)


def _traces_installation_existante() -> bool:
    """Detect a prior install lacking our marker.

    The marker is new; without migration, every update would open initial setup
    for already-installed users. A session, settings, storage pointer or non-empty
    data folder is enough proof of prior use. This detection runs once: it is
    immediately materialized by the definitive marker. Outputs are also searched
    in the old folder of a version ≤ 0.13, in case its migration (see
    preparer_etat()) could not run.
    """
    racine = app_dir()
    if (racine / REGLAGES).is_file() or (racine / "blink_auth.json").is_file():
        return True
    ancre = _dossier_ancre()
    if (ancre / POINTEUR_STOCKAGE).is_file():
        return True
    bases = [dossier_sorties()]
    if not os.environ.get("BLINK_HOME"):
        bases.append(app_dir_depuis(ancre))
    for base in dict.fromkeys(bases):
        for nom in DOSSIERS_SORTIES:
            dossier = base / nom
            try:
                if dossier.is_dir() and next(dossier.iterdir(), None) is not None:
                    return True
            except OSError:
                # A temporarily unavailable root proves neither fresh nor old
                # install. Other traces may still decide; otherwise initial setup
                # remains the safe choice.
                continue
    return False


def configuration_initiale_effectuee() -> bool:
    """True after explicit validation of the first settings panel."""
    return (_dossier_controle() / MARQUEUR_CONFIGURATION_INITIALE).is_file()


def configuration_initiale_requise() -> bool:
    """True only for a genuinely new install.

    A "pending" marker is written before Blink login. Closing the window after
    signing in but before applying settings then does not turn that fresh session
    into a false historical install on next launch.
    """
    if configuration_initiale_effectuee():
        return False
    attente = _dossier_controle() / MARQUEUR_CONFIGURATION_EN_ATTENTE
    if attente.is_file():
        return True
    if _traces_installation_existante():
        try:
            marquer_configuration_initiale()
        except OSError:
            # Migration is a compatibility optimization. Traces will remain on
            # next startup and will still avoid forcing initial setup even if
            # the marker write is refused.
            pass
        return False
    try:
        _ecrire_marqueur_configuration(MARQUEUR_CONFIGURATION_EN_ATTENTE)
    except OSError:
        # Never start downloads solely because a marker could not be written:
        # the panel can explain the real permissions problem when saving.
        pass
    return True


def marquer_configuration_initiale() -> None:
    """Finalize initial setup, regardless of storage location."""
    _ecrire_marqueur_configuration(MARQUEUR_CONFIGURATION_INITIALE)
    try:
        (_dossier_controle() / MARQUEUR_CONFIGURATION_EN_ATTENTE).unlink(missing_ok=True)
    except OSError:
        # The definitive marker is the commit and is checked first. Antivirus
        # still holding the old small file must not turn success into error or
        # block startup.
        pass


def identite_processus(pid: int) -> str | None:
    """Stable identity of a live process: its real start time from the OS,
    never reused unlike pid alone.

    A lock or progress marker (AUDIT-2026-08-13, 28.82/28.84) recorded only the
    owner's pid: after reboot, Windows quickly reassigns pids to unrelated
    processes. `processus_vivant(pid)` then wrongly answered "yes" forever (no
    age guard, intentionally, to never steal a live process's lock): the lock
    stayed blocked indefinitely, held by a ghost. Comparing this identity at
    open and on each re-read separates "same process, still alive" from "pid
    recycled by someone else"."""
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            # ctypes otherwise assumes a C function returns 32-bit int;
            # a 64-bit HANDLE could be truncated on Windows x64.
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = (
                wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME))
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return None
            try:
                creation, exit_t, kernel_t, user_t = (
                    wintypes.FILETIME(), wintypes.FILETIME(),
                    wintypes.FILETIME(), wintypes.FILETIME(),
                )
                ok = kernel32.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(exit_t),
                    ctypes.byref(kernel_t), ctypes.byref(user_t))
                if not ok:
                    return None
                return f"{creation.dwLowDateTime}:{creation.dwHighDateTime}"
            finally:
                kernel32.CloseHandle(handle)
        except OSError:
            return None
    try:
        resultat = lancer(["ps", "-o", "lstart=", "-p", str(pid)],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, errors="replace",
                          check=False)
    except OSError:
        # `ps` missing (seen in practice: python:3.12-slim image does not
        # install it by default, unlike a full distro): no comparison possible,
        # not a reason to crash the whole app. `verrou()` then falls back to
        # old behavior (identite None = nothing to compare), less protected
        # against recycled pid but functional.
        return None
    valeur = (resultat.stdout or "").strip()
    return valeur or None


def processus_vivant(pid: int) -> bool:
    """True if this number refers to an existing process.

    Numbers get reused after a while: the card also stores start time, and a
    card whose process is gone is removed on first read."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # A killed process stays a "zombie" until its parent reaps it, and
        # signal zero still reaches it: without this second check, "stop" would
        # report the target survived when it is dead, as a runner showed immediately.
        try:
            etat = lancer(["ps", "-o", "state=", "-p", str(pid)],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, errors="replace",
                          check=False)
        except OSError:
            # `ps` missing (e.g. python:3.12-slim, seen via identite_processus
            # - AUDIT-2026-08-13, 28.85): signal 0 already confirmed the pid
            # exists, we just cannot tell zombie or not. Treat as alive, as before
            # this check was added.
            return True
        return not (etat.stdout or "").strip().startswith("Z")
    # Query the OS directly via Win32 (OpenProcess + GetExitCodeProcess),
    # without a tasklist subprocess: spawning costs tens to hundreds of ms,
    # which adds up during shutdown wait (arreter() checks several members each
    # second) and makes delay depend on machine load rather than real process
    # state. Same API as identite_processus() above.
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_ACCESS_DENIED = 5
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetLastError.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            erreur = kernel32.GetLastError()
            if erreur == ERROR_ACCESS_DENIED:
                # The pid exists (otherwise error would be "invalid parameter"),
                # but opening it is denied: that proves nothing about state, only
                # that the question could not be asked. Deciding "dead" here would
                # steal a still-live owner's lock (same principle as tasklist before).
                return True
            return False
        try:
            code_sortie = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code_sortie))
        finally:
            kernel32.CloseHandle(handle)
        return (code_sortie.value == STILL_ACTIVE) if ok else True
    except OSError:
        # kernel32 is the OS core: this should never happen, but if it did,
        # same principle as the POSIX branch above.
        return True


def ligne_de_commande(pid: int) -> str:
    """Process command line, or empty string if unreadable.

    Image name alone cannot confirm identity: two python.exe can coexist with
    no relation. The command line carries the launched script path."""
    if pid <= 0:
        return ""
    if os.name != "nt":
        resultat = lancer(["ps", "-o", "args=", "-p", str(pid)],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, errors="replace",
                          check=False)
        return (resultat.stdout or "").strip()
    # New cards no longer use this path: they compare native creation time
    # via GetProcessTimes. It remains only to read old-version cards. Windows 7
    # ships with PowerShell 2, without Get-CimInstance (PowerShell 3+): picking
    # Get-WmiObject dynamically on that environment avoids migration concluding
    # "process absent" for lack of cmdlet.
    resultat = lancer(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"if (Get-Command Get-CimInstance -ErrorAction SilentlyContinue) {{ "
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' "
         f"-ErrorAction SilentlyContinue).CommandLine }} else {{ "
         f"(Get-WmiObject Win32_Process -Filter 'ProcessId={pid}' "
         f"-ErrorAction SilentlyContinue).CommandLine }}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, errors="replace", check=False,
    )
    return (resultat.stdout or "").strip()


def _empreintes_attendues() -> list:
    """What must appear in the command line of a genuine process of this project
    — the entry point itself, or one of its sub-verbs (serve.py, watch.py,
    merge_daily.py...).

    Two markers, not one: the entry point carries the project name in its command
    line however the user typed it (relative, absolute, bundle), but sub-verbs
    launched from sources are different scripts (see self_command) that do not
    carry that name; only their shared folder does, reliably, since they
    relaunch themselves with an absolute path."""
    if frozen():
        return ["blink2video"]
    return ["blink2video", str(Path(__file__).resolve().parent)]


def processus_correspond(pid: int, marqueurs: list | None = None,
                         identite: str | None = None) -> bool:
    """True if this PID really refers to a process of this project.

    Pids eventually get reused by unrelated software; mistaking them for the
    instance we think we track already made "stop" kill an HP service, a
    terminal and a messenger on a user's machine because a number matched.
    Checking pid existence is not enough: verify identity before touching it,
    *a fortiori* before killing it."""
    if not processus_vivant(pid):
        return False
    if identite is not None:
        # For any new-format card, PID + OS creation time is identity. No
        # PowerShell/WMI, so same behavior on stock Windows 7 and current Windows.
        # If the OS refuses to answer, never kill on guesswork: False is safe.
        actuelle = identite_processus(pid)
        return actuelle is not None and actuelle == identite
    ligne = ligne_de_commande(pid)
    return any(m in ligne for m in (marqueurs or _empreintes_attendues()))


def _ecrire_fiche(fiche: Path, donnees: dict) -> None:
    """Write a process card (instance or workers) atomically.

    Direct write could let a concurrent read see truncated JSON mid-write;
    lire_instances() then treated it as stale and deleted it, making stop lose
    a still-live process (review 27/08, bug 8). Same pattern as _ecrire_registre()
    (blink_registre.py)."""
    _ecrire_texte_atomique(fiche, json.dumps(donnees, ensure_ascii=False))


def _verrou_fiche(fiche: Path, attente: int = 5):
    """Serialize parent/child updates to the same instance card."""
    return verrou(f"instance-{fiche.stem}", f"fiche-{os.getpid()}",
                   stale_after=120, attente=attente, racine=_dossier_controle())


def inscrire_instance(entrees: list, enfants=None) -> Path:
    """Write the current instance card and remove it on exit.

    Without it, stopping a console-less instance would mean hunting pids by hand
    then killing a tree hoping not to miss anyone. Also purges dead instance
    cards without going through "stop"."""
    import atexit
    import datetime as dt

    # Also overwrites a value inherited from an old supervisor on restart. All
    # children launched afterward know which card to update when registering ffmpeg.
    os.environ[INSTANCE_PID_ENV] = str(os.getpid())

    # Purge dead cards without "stop" (external kill, crash, power loss):
    # lire_instances() already cleans them as a read side effect, but nothing
    # called it again once a session ran for days (seen in practice, 2026-09-01:
    # multi-day-old cards still in .blink_run). Each launch sweeps leftovers.
    instances_vivantes = lire_instances(journal=print)

    # Never clear a shutdown request that is really in progress. A new instance
    # launched at the same moment would otherwise cancel the signal under old
    # loops. Only an orphan flag with no recognized instance belongs to a prior
    # session.
    if arret_demande():
        if instances_vivantes:
            raise BusyError("shutdown in progress")
        effacer_arret_demande()

    dossier = _dossier_controle() / INSTANCES
    dossier.mkdir(parents=True, exist_ok=True)
    fiche = dossier / f"{os.getpid()}.json"
    membres = [os.getpid(), *(enfants or [])]
    identites = {}
    for pid in membres:
        identite = identite_processus(int(pid))
        if identite is not None:
            identites[str(pid)] = identite

    # The second call, right after launching children, completes the same card.
    # The lock closes the race with a fast child already registering ffmpeg:
    # without it, two read-modify-replace could clobber each other.
    with _verrou_fiche(fiche):
        travailleurs = []
        try:
            precedente = json.loads(fiche.read_text(encoding="utf-8"))
            travailleurs = list(precedente.get("travailleurs") or [])
            anciennes_identites = precedente.get("identites") or {}
            if isinstance(anciennes_identites, dict):
                anciennes_identites = dict(anciennes_identites)
                anciennes_identites.update(identites)
                identites = anciennes_identites
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

        _ecrire_fiche(fiche, {
            "pid": os.getpid(),
            "depuis": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "verbes": entrees,
            # Children are recorded to verify after shutdown that none survived:
            # that is exactly what happened before.
            "enfants": list(enfants or []),
            "travailleurs": travailleurs,
            "identites": identites,
        })
    def nettoyer_fiche() -> None:
        """Remove the card only if no tracked child outlives it."""
        try:
            donnees = json.loads(fiche.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        identites_lues = donnees.get("identites") or {}
        if not isinstance(identites_lues, dict):
            identites_lues = {}
        autres = [*(donnees.get("enfants") or []),
                  *(donnees.get("travailleurs") or [])]
        for autre in autres:
            pid = int(autre or 0)
            attendue = identites_lues.get(str(pid))
            if attendue is not None:
                actuelle = identite_processus(pid)
                if actuelle is None and processus_vivant(pid):
                    return
                if actuelle == attendue:
                    return
            elif processus_correspond(pid):
                return
        fiche.unlink(missing_ok=True)

    atexit.register(nettoyer_fiche)
    return fiche


def lire_instances(journal=None) -> list:
    """Cards of instances actually running, stale ones removed.

    I-13: a dead parent does not mean the instance is done. A lingering child
    can outlive it (parent supervises but is not in the same process group as
    children on POSIX), and "stop" needs the card to find that survivor. We
    only remove cards where neither parent nor any child is alive.

    Identity (PID + native creation time, see `identite_processus`) alone
    decides who is still "us": stronger than command-line matching, which
    already wrongly stopped an HP service, terminal and messenger (see
    `processus_correspond`). Optional `journal` gets a readable line (PID and
    original verbs) for each removed stale card: to verify afterward *which*
    instance was judged dead, without basing the decision on that text."""
    dossier = _dossier_controle() / INSTANCES
    vivantes = []
    for fiche in sorted(dossier.glob("*.json")) if dossier.is_dir() else []:
        try:
            donnees = json.loads(fiche.read_text(encoding="utf-8"))
        except OSError:
            # Gone between glob() and read: another process (its own atexit, etc.)
            # already cleaned it, nothing to purge here.
            continue
        except json.JSONDecodeError:
            # Present but unreadable. _ecrire_fiche() is atomic (temp + replace),
            # so this is no longer a concurrent write window, only real corruption:
            # deleting would make stop lose a possibly still-live process (review
            # 27/08, bug 8). Leave it as-is for this read.
            continue
        membres = [donnees.get("pid"), *(donnees.get("enfants") or []),
                   *(donnees.get("travailleurs") or [])]
        identites = donnees.get("identites") or {}
        if not isinstance(identites, dict):
            identites = {}

        def encore_notre_processus(membre) -> bool:
            pid = int(membre or 0)
            attendue = identites.get(str(pid))
            if attendue is None:
                # Old card: temporary fallback to command line.
                return processus_correspond(pid)
            actuelle = identite_processus(pid)
            if actuelle is None:
                # Distinguish "dead" from "alive but impossible to query".
                # In the second case, keep the card: removing it would lose the
                # only clue without proving it stale.
                return processus_vivant(pid)
            return actuelle == attendue

        if any(encore_notre_processus(membre) for membre in membres):
            donnees["fiche"] = fiche
            vivantes.append(donnees)
        else:
            if journal:
                commande = " ".join(" ".join(g) for g in donnees.get("verbes") or [])
                journal(f"fiche perimee retiree : PID {donnees.get('pid')} "
                        f"({commande or '?'}), depuis {donnees.get('depuis', '?')}")
            fiche.unlink(missing_ok=True)
    return vivantes


def _fiche_courante() -> Path:
    # Verbs run in child processes of the supervisor that holds the card. It
    # passes its PID via environment: without that, a child merge looked for
    # "<its own pid>.json", missing, and ffmpeg was never really registered
    # despite the intended code.
    proprietaire = os.environ.get(INSTANCE_PID_ENV) or str(os.getpid())
    return _dossier_controle() / INSTANCES / f"{proprietaire}.json"


def inscrire_travailleur(pid: int) -> None:
    """Add a worker PID (ffmpeg during merge) to the current process card.

    ffmpeg launched in merge is a real child, but `arreter()` never applies
    taskkill /T to its own PID (see arreter_processus): protection against
    wrongly killing an adopted browser also left ffmpeg orphaned until finish
    after "stop". Registering here gives "stop" an entry to kill directly,
    without relying on the parent's taskkill.

    Silent if the card does not exist yet: direct merge calls outside blink_cli
    supervision (tests, script) simply have nothing to record."""
    fiche = _fiche_courante()
    try:
        with _verrou_fiche(fiche):
            try:
                donnees = json.loads(fiche.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            travailleurs = set(donnees.get("travailleurs") or [])
            travailleurs.add(pid)
            donnees["travailleurs"] = sorted(travailleurs)
            identite = identite_processus(pid)
            if identite is not None:
                identites = dict(donnees.get("identites") or {})
                identites[str(pid)] = identite
                donnees["identites"] = identites
            _ecrire_fiche(fiche, donnees)
    except BusyError:
        # Registration is best-effort: merge process kill /T remains fallback if
        # its card stayed busy for 5 seconds.
        return


def retirer_travailleur(pid: int) -> None:
    """Symmetric to inscrire_travailleur: call whether the batch succeeded,
    failed, or was killed by run_ffmpeg_batch's silence watchdog."""
    fiche = _fiche_courante()
    try:
        with _verrou_fiche(fiche):
            try:
                donnees = json.loads(fiche.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            travailleurs = set(donnees.get("travailleurs") or [])
            travailleurs.discard(pid)
            donnees["travailleurs"] = sorted(travailleurs)
            identites = dict(donnees.get("identites") or {})
            identites.pop(str(pid), None)
            donnees["identites"] = identites
            _ecrire_fiche(fiche, donnees)
    except BusyError:
        return


def arreter_processus(pid: int, avec_descendance: bool = False) -> None:
    """Stop a process, and its descendants if requested.

    The distinction matters off Windows: a side-by-side verb gets its own
    session (see flags_enfant), so we can kill its whole group, ffmpeg included.
    The parent shares the launching terminal's group: the same treatment would
    kill that terminal. That happened to the test suite on runners.

    On Windows the distinction matters equally: "taskkill /T" walks the tree
    Windows sees for the pid, including any process that became our child without
    being our worker, e.g. browser opened by webbrowser.open() when no window
    existed yet. "stop"/"restart" on the main process (avec_descendance=False)
    closed all of Chrome that way: /T must apply only to a child known to be ours
    (avec_descendance=True)."""
    if os.name == "nt":
        commande = ["taskkill", "/F", "/PID", str(pid)]
        if avec_descendance:
            commande.insert(2, "/T")
        lancer(commande, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL, check=False)
        return
    import signal

    def envoyer(signal_) -> bool:
        try:
            if avec_descendance:
                os.killpg(os.getpgid(pid), signal_)
            else:
                os.kill(pid, signal_)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    if not envoyer(signal.SIGTERM):
        return
    for _ in range(20):
        if not processus_vivant(pid):
            return
        time.sleep(0.25)
    envoyer(signal.SIGKILL)


# On Windows, any program launched from a console-less app opens one for its
# run: a black window flashing for each thumbnail, video analysis, notification.
# This flag prevents that. Elsewhere it does not exist and is zero.
SANS_FENETRE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def console_disponible() -> bool:
    """True if this process has a console to write to.

    False under pythonw.exe, when blink is launched by autostart entry."""
    if os.name != "nt":
        return True
    import ctypes

    return bool(ctypes.windll.kernel32.GetConsoleWindow())


def flags_enfant() -> int:
    """Flags for a verb launched alongside others.

    It must use the parent's console when one exists, or errors vanish: "serve"
    refusing a taken port said nothing and failure was opaque. When there is none,
    keep CREATE_NO_WINDOW or Windows opens one per verb, those flashing black
    windows."""
    return 0 if console_disponible() else SANS_FENETRE


def debloquer_sigterm() -> None:
    """Clear inherited SIGTERM blocking from the process that launched us.

    The "start" supervisor blocks SIGTERM to wait in a dedicated thread (see
    blink_cli._sortie_propre_sur_sigterm), and signal masks propagate to
    children: without this unblock, a child born from it, e.g. via tray
    "Restart", would ignore "stop"'s SIGTERM until fallback SIGKILL. No effect
    on Windows."""
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})


if frozen():
    # LD_LIBRARY_PATH restored for system programs (issue #23): see
    # nico579_commons.environnement. Imported only here: from sources, common
    # lib installs after this module (dependency bootstrap), and the function is
    # a no-op outside a bundle anyway.
    from nico579_commons import environnement
    environnement.retablir_environnement_systeme()
debloquer_sigterm()


def lancer(commande, **options):
    """subprocess.run, never flashing a window on Windows."""
    options.setdefault("creationflags", SANS_FENETRE)
    return subprocess.run(commande, **options)


def demarrer(commande, **options):
    """subprocess.Popen, same precaution."""
    options.setdefault("creationflags", SANS_FENETRE)
    return subprocess.Popen(commande, **options)


def _applescript(texte: str) -> str:
    """Encode a string for AppleScript, whose escaping is not shell-like: only
    quotes and backslashes matter."""
    return '"' + texte.replace("\\", "\\\\").replace('"', '\\"') + '"'



def _declarer_identite() -> None:
    """Declare application identity to Windows, once.

    Without it, the API accepts the notification and the system drops it with
    no error or trace. The key lives in the user hive, concerns notifications
    only, and is removed by deleting it:

        HKCU\\SOFTWARE\\Classes\\AppUserModelId\\blink2video
    """
    if sys.platform != "win32":
        return
    try:
        import winreg
    except ImportError:
        return
    chemin = 'SOFTWARE\\Classes\\AppUserModelId' + '\\' + APP_ID
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, chemin) as cle:
            if winreg.QueryValueEx(cle, "DisplayName")[0] == APP_ID:
                return
    except OSError:
        pass
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, chemin) as cle:
            winreg.SetValueEx(cle, "DisplayName", 0, winreg.REG_SZ, APP_ID)
    except OSError:
        # Read-only registry: notification may fail, not a reason to interrupt
        # work in progress.
        pass


def toast(titre: str, corps: str, url: str = "") -> None:
    """Non-blocking Windows notification without installing anything.

    Deliberately unlike the popup: outages are rare and must be seen, so they
    block until acknowledged; new clips are frequent and mundane, a modal each
    time would be unbearable.

    Uses PowerShell, which exposes the Windows 10 notification API. Avoids a
    dependency for a dozen lines and hand-writing a Win32 notification icon."""
    if sys.platform == "darwin":
        lancer(
            ["osascript", "-e",
             f"display notification {_applescript(corps)} with title {_applescript(titre)}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=20,
        )
        return
    if sys.platform.startswith("linux"):
        if shutil.which("notify-send"):
            lancer(["notify-send", titre, corps],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False, timeout=20)
            return
    if sys.platform != "win32":
        print(f"{titre} : {corps}")
        return
    _declarer_identite()

    def echappe(valeur: str) -> str:
        return (valeur.replace("&", "&amp;").replace("<", "&lt;")
                      .replace(">", "&gt;").replace("'", "&apos;"))

    lancement = (f' activationType="protocol" launch="{echappe(url)}"') if url else ""
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom,"
        " ContentType = WindowsRuntime] | Out-Null;"
        "$x = [Windows.Data.Xml.Dom.XmlDocument]::new();"
        # activationType=protocol: Windows opens the URL on click without
        # registering an activation handler. That jumps from "new clip" to the
        # page that shows it.
        f"$x.LoadXml('<toast{lancement}><visual><binding template=\"ToastGeneric\">"
        f"<text>{echappe(titre)}</text><text>{echappe(corps)}</text>"
        "</binding></visual></toast>');"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($x);"
        # A notification must be sent on behalf of an app declared to Windows.
        # Rather than register one, we borrow PowerShell's identity, already
        # declared on every install.
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        + repr(APP_ID) +
        ").Show($n)"
    )
    lancer(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=20,
    )


def notifier_nouveau_media(camera: str, chemin: Path, type_media: str) -> None:
    """Outbound webhook (GitHub issue #11): one JSON POST per ready clip or photo,
    once the file is actually written, never before.

    Synchronous like toast() above, called as-is from download (async but one clip
    at a time, never parallel) and from serve.py (already synchronous): a brief
    POST is negligible vs a multi-second download, not worth bridging to the
    event loop for this notification alone.

    Best effort, no retry, any failure absorbed here rather than raised: misconfigured
    URL, dead remote server or network blip must never fail a download that just
    succeeded, nor block the next file."""
    url = lire_reglages().get("webhook_notif_url", "")
    if not url:
        return
    try:
        charge = json.dumps(
            {"camera": camera, "chemin": str(chemin), "type": type_media}).encode("utf-8")
        requete = urllib.request.Request(
            url, data=charge, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(requete, timeout=10):
            pass
    except Exception:
        # Deliberately broad (see docstring): building the request can raise for
        # a URL accepted by settings validation (correct scheme) but malformed
        # later (missing host, forbidden character) - found in audit before any
        # real incident; first version only guarded the network call, not request
        # construction right before.
        pass


class BusyError(RuntimeError):
    """The resource is already held by another operation."""


def _lire_verrou(fichier: Path):
    """Lock file contents, or None if missing/unreadable.

    Unreadable covers corruption and the brief window where another process
    created the file but has not finished writing: in both cases we cannot assert
    ownership, so retry rather than decide wrongly."""
    try:
        return json.loads(fichier.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _supprimer_verrou(fichier: Path) -> None:
    """Delete a lock file known to be ours, absorbing transient Windows refusal.

    Antivirus or indexer can briefly reopen a file just read or written; on
    Windows that makes unlink() fail with PermissionError (WinError 32) though
    no legitimate owner holds it. A few quick retries suffice; concurrent open
    is never long."""
    for tentative in range(10):
        try:
            fichier.unlink(missing_ok=True)
            return
        except PermissionError:
            if tentative == 9:
                raise
            time.sleep(0.05)


@contextlib.contextmanager
def verrou(nom: str, owner: str, stale_after: int = 600, attente: int = 0,
           racine: Path | None = None):
    """Reserve a resource across processes for the duration of an operation.

    Two resources cannot be shared: the Sync Module, which handles one command
    at a time and answers "System is busy" to a second, and merging, which would
    write the same files twice. Verbs run in separate processes, so an in-memory
    lock is not enough: a on-disk marker is required.

    Acquisition uses exclusive file creation (``O_CREAT | O_EXCL``): two
    processes attempting it at once cannot both succeed, the OS guarantees that.
    The old protocol read the file, decided it was free, then wrote: nothing
    stopped two processes from passing the first two steps together (B-05). A
    per-call acquisition token accompanies owner/pid: it ensures we never release,
    on exit, a lock another process re-acquired under a recycled PID.

    One safeguard remains against a marker left after a crash: ignore those whose
    process no longer exists. `stale_after` is NO LONGER read at all by this
    function (found auditing this file: ghost parameter, never consulted below) —
    it never steals a live process's lock on age alone (B-05, a download pass can
    legitimately exceed ten minutes on a large clip, and age alone proves nothing);
    most radically, age no longer matters, only process identity (see below).
    Kept in the signature to avoid breaking callers, who still document a
    considered duration here (7200 s for merge, 600 s default...) even though it
    no longer changes behavior.

    PID recycled by an unrelated process while the real owner is dead — long
    documented as uncovered here, seen in practice (AUDIT-2026-08-13, 28.82/28.84:
    merge loop blocked 15h+ after Windows reboot reused the pid) — is now resolved
    via `identite_processus()`: the process's real OS start time is compared to
    the lock's. A live pid whose identity no longer matches is not the same
    process: the lock is treated as abandoned, purged like any stale marker.

    `attente` is how long we retry before giving up. A live stream lasts minutes;
    without wait, a loop hitting it would lose a whole turn while the resource
    often frees in seconds."""
    import uuid

    dossier_verrou = Path(racine) if racine is not None else app_dir()
    dossier_verrou.mkdir(parents=True, exist_ok=True)
    fichier = dossier_verrou / f".blink_{nom}.lock"
    jeton = uuid.uuid4().hex
    # monotonic, not time.time(): NTP/DST correction during wait must not shorten
    # or lengthen the delay promised by `attente` (same fix already in
    # verrou_controle() below and nettoyer_lances() in blink_cli.py — not here yet).
    # "at" below stays time.time() on purpose: real timestamp to compare with
    # other time.time() (age = time.time() - presente["at"]), not a delay to measure.
    limite = time.monotonic() + max(attente, 0)
    contenu = json.dumps(
        {"owner": owner, "pid": os.getpid(), "jeton": jeton, "at": time.time(),
         "identite": identite_processus(os.getpid())}
    ).encode("utf-8")

    while True:
        try:
            descripteur = os.open(fichier, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass
        except PermissionError as erreur:
            # On Windows, a lock just deleted by its owner stays "pending deletion"
            # while antivirus or indexer keeps it open: recreating is refused
            # (access denied), neither success nor FileExistsError. The lock is
            # freeing, just not yet: retry in the same wait window, as filelock
            # does for this case. The error reached verrou() here and background
            # exclusion died without applying (Windows CI 25–26/09/2026). Elsewhere
            # this refusal is a real permissions failure.
            if os.name != "nt":
                raise
            if time.monotonic() >= limite:
                raise BusyError(f"lock being deleted or "
                                f"inaccessible: {fichier}") from erreur
            time.sleep(0.05)
            continue
        else:
            # Creation succeeded: the lock is ours, no one else could get the same
            # file at the same instant.
            with os.fdopen(descripteur, "wb") as flux:
                flux.write(contenu)
            break

        presente = _lire_verrou(fichier)
        if presente is None:
            try:
                existe = fichier.exists()
            except PermissionError:
                # Windows, Python 3.8: a lock just deleted stays "pending deletion"
                # and stat() is refused (Win7 CI 29/09/2026, `stop` test). Like
                # creation above, not free yet: treat as present marker; `attente`
                # and limit apply.
                if os.name != "nt":
                    raise
                existe = True
            if existe:
                # Present but unreadable: corrupt, or another process's write window
                # not finished. Both look the same here (same limit as B-05), but
                # must still honor `attente` — a stuck corrupt marker (interrupted
                # write, disk issue) otherwise spun forever without BusyError or
                # giving up (code review 0eab463, bug #3).
                if time.monotonic() >= limite:
                    raise BusyError(f"lock unreadable or corrupt: {fichier}")
                time.sleep(0.05)
                continue
            # File truly absent between failed create and read (owner just freed
            # it): retry immediately, no wait.
            continue
        pid_verrou = int(presente.get("pid") or 0)
        identite_enregistree = presente.get("identite")
        identite_actuelle = identite_processus(pid_verrou)
        # Alive AND same identity: a pid recycled after the real owner died must
        # not count as them (AUDIT-2026-08-13, 28.82/28.84). A marker without
        # "identite" is old format (before this fix): nothing to compare, fall
        # back to old behavior rather than purge a legitimate lock. Same if
        # identite_processus() cannot query current pid (OpenProcess denied):
        # None here does not prove different identity, only unread, so do not
        # decide "different" on that alone (review 27/08, bug 1).
        # Exception, seen 29/09/2026: marker has identity but current pid unread.
        # Our-account owner is always readable; denied pid (access denied) is
        # protected or another account, i.e. recycled (AggregatorHost.exe): pre-
        # reboot lock blocked autostart forever.
        meme_processus = (
            identite_enregistree is None
            or (identite_actuelle is not None
                and identite_actuelle == identite_enregistree)
        )
        if not (processus_vivant(pid_verrou) and meme_processus):
            # Owner dead, or pid recycled by someone else: purge under mutex, not
            # direct unlink.
            # Old protocol (read, decide "dead", delete) left a window: a second
            # process with the same verdict could delete the lock a first process
            # just recreated instead of the stale marker — both thought they owned
            # it (code review 0eab463, bug #3). `.purge` file plays the same role
            # as O_CREAT|O_EXCL on the lock: one process at a time enters the next
            # section and rechecks the token before deleting.
            purge = fichier.with_name(fichier.name + ".purge")
            try:
                os.close(os.open(purge, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            except (FileExistsError, PermissionError) as erreur:
                # PermissionError: same Windows refusal as lock creation above,
                # purge marker also created then deleted each pass.
                if isinstance(erreur, PermissionError) and os.name != "nt":
                    raise
                # Another process is in this section (or rarely died before
                # finally): `limite` remains the only guard against infinite
                # wait, here too.
                if time.monotonic() >= limite:
                    raise BusyError(f"lock purge already in progress for "
                                    f"« {presente.get('owner')} »")
                time.sleep(0.05)
                continue
            try:
                encore = _lire_verrou(fichier)
                if encore is not None and encore.get("jeton") == presente.get("jeton"):
                    _supprimer_verrou(fichier)
            finally:
                _supprimer_verrou(purge)
            continue

        if time.monotonic() >= limite:
            age = time.time() - float(presente.get("at") or 0)
            raise BusyError(f"already held by « {presente.get('owner')} » "
                            f"(pid {presente.get('pid')}) for {int(age)} s")
        time.sleep(1)

    try:
        yield
    finally:
        courante = _lire_verrou(fichier)
        if courante is not None and courante.get("jeton") == jeton:
            _supprimer_verrou(fichier)


def verrou_controle(owner: str, attente: int = 0):
    """Global lock for stop/restart transitions, independent of storage."""
    return verrou("controle", owner, stale_after=120, attente=attente,
                   racine=_dossier_controle())


def verrou_configuration(owner: str = "reglages", attente: int = 0):
    """Serialize pointer switches and writing their settings."""
    return verrou("configuration", owner, stale_after=120, attente=attente,
                   racine=_dossier_controle())


# Application identity under which Windows notifications are sent.
# Windows silently drops, returning zero, any notification whose identity is not
# declared: borrowing PowerShell's worked on some installs and not others, never
# saying why. We declare our own, like Firefox or Acrobat.
APP_ID = "blink2video"


ARRET_DEMANDE = Path(".blink_arret")


def demander_arret() -> None:
    """Set the cooperative shutdown flag.

    repeter() (all --loop verbs) and the web server re-read it each turn/at short
    intervals: a verb finishes its current turn (partial file, page in service)
    then stops on its own, rather than being killed mid-work (review 27/08:
    external stop via tray or stop killed directly without letting work finish).
    Only ffmpeg is killed directly: third-party process, no control over its own
    cleanup (see arreter(), blink_cli.py)."""
    (_dossier_controle() / ARRET_DEMANDE).write_text(
        str(time.time()), encoding="utf-8")


def arret_demande() -> bool:
    return (_dossier_controle() / ARRET_DEMANDE).exists()


def effacer_arret_demande() -> None:
    """Call at the start of a new session (a prior session's flag must never
    block a new one) and after a successful shutdown sequence."""
    (_dossier_controle() / ARRET_DEMANDE).unlink(missing_ok=True)


PASSAGES = Path(".blink_passages.json")


def marquer(verbe: str) -> None:
    """Record when a verb just finished its work.

    The UI shows these times, making a stalled loop obvious at a glance."""
    import datetime as dt

    fichier = app_dir() / PASSAGES
    passages = {}
    try:
        passages = json.loads(fichier.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        passages = {}
    passages[verbe] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        fichier.write_text(json.dumps(passages, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def passages() -> dict:
    """Last run time of each verb."""
    try:
        return json.loads((app_dir() / PASSAGES).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


TRAVAIL = Path(".blink_travail.json")
TRAVAIL_TERMINE_VISIBLE = 10


def _fichier_travail(pid: int | None = None) -> Path:
    """Per-worker card so two progress updates do not overwrite each other."""
    pid = os.getpid() if pid is None else int(pid)
    return app_dir() / f"{TRAVAIL.stem}.{pid}{TRAVAIL.suffix}"


@contextlib.contextmanager
def _verrou_travail(cible: Path, attente: float = 0.25):
    """Brief exclusion between publish and purge, without polling processes.

    nico579_commons.atomique lock on neighbor file ".blink_travail.lock": it
    stays in place (removing after unlock would let two processes lock different
    files at the same path), and the OS releases it even if the holder crashes.
    BusyError if another holds it longer than `attente`.
    """
    from nico579_commons import atomique

    pile = contextlib.ExitStack()
    try:
        pile.enter_context(atomique.verrou_inter_processus(
            cible.parent / TRAVAIL.stem, delai_s=max(0, attente)))
    except TimeoutError as exc:
        raise BusyError("publication de progression en cours") from exc
    with pile:
        yield


def _ecrire_fiche_travail(cible: Path, etat: dict) -> bool:
    """Replace a card atomically, including under Windows antivirus."""
    from nico579_commons import atomique

    temporaire = atomique.chemin_part(cible)
    reussi = False
    try:
        temporaire.write_text(json.dumps(etat, ensure_ascii=False), encoding="utf-8")
        with _verrou_travail(cible):
            atomique.remplacer(temporaire, cible)
        reussi = True
    except (OSError, BusyError):
        pass
    finally:
        try:
            temporaire.unlink(missing_ok=True)
        except OSError:
            pass
    return reussi


def _lire_fiche_travail(cible: Path) -> dict:
    try:
        etat = json.loads(cible.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return etat if isinstance(etat, dict) else {}


def _supprimer_fiche_travail(cible: Path, attendu: dict, attente: float = 0) -> bool:
    """Remove only the publication read, cannot erase the next tick."""
    try:
        with _verrou_travail(cible, attente=attente):
            if _lire_fiche_travail(cible) == attendu:
                cible.unlink(missing_ok=True)
        return True
    except (OSError, BusyError):
        # Purge can wait for next poll. An active card must never be sacrificed
        # to clear an old completion.
        return False


def travail(quoi: str, fait: float = 0, total: int = 0, cle: str | None = None) -> None:
    """Publish in-progress work for anything that wants to show it.

    Verbs run in their own processes: when merge runs from a background loop, the
    UI sees none of its output. This file is the only place it learns a job is
    using the machine and how far along. Written per clip, cleared at end.

    `quoi` stays hard-coded French text for callers that show nothing (logs,
    terminal): do not force translation on them. `cle` is an optional stable tag
    the web page uses to find English in its own dictionary (I18N) without this
    file knowing translations — unknown text (missing key or unrecognized key)
    stays as-is, never an empty string."""
    import datetime as dt
    etat = {"quoi": quoi, "cle": cle, "fait": round(fait, 3), "total": total,
            "pid": os.getpid(),
            "depuis": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    # One card per PID: download and merge can overlap.
    # A single file hid the download bar as soon as merge published its tick, and
    # one fin_travail() could delete the other's fresh publish between read and
    # unlink.
    _ecrire_fiche_travail(_fichier_travail(), etat)


def fin_travail(conserver: float = 0) -> None:
    """Remove our card, or briefly keep its final displayable state.

    A kept completion carries ``termine``: it is never returned by
    :func:`travail_en_cours` and does not block Refresh or a new job. It only
    lets the web poll not miss a small download fully done between polls.
    """
    import datetime as dt

    cible = _fichier_travail()
    etat = _lire_fiche_travail(cible)
    if not etat or etat.get("pid") != os.getpid():
        return
    total = etat.get("total") or 0
    fait = etat.get("fait") or 0
    if conserver and total > 0 and fait >= total:
        final = dict(etat, termine=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                     visible_secondes=max(0, float(conserver)))
        if _ecrire_fiche_travail(cible, final):
            return
        # On Windows, antivirus may rarely refuse replace. Do not leave the old
        # "active" card while the worker sleeps: better lose terminal N/N than
        # block Refresh for fifteen minutes until expiry.
    _supprimer_fiche_travail(cible, etat, attente=0.25)


def _etats_travail(retirer_conclusion: str | None = None) -> tuple:
    """Return (active, completions still displayable), purging stale entries."""
    import datetime as dt

    maintenant = dt.datetime.now().astimezone()
    racine = app_dir()
    fichiers = list(racine.glob(f"{TRAVAIL.stem}.*{TRAVAIL.suffix}"))
    # Compatibility with a card left by an older version during in-place update.
    # New writes all use PID.
    ancienne = racine / TRAVAIL
    if ancienne.exists():
        fichiers.append(ancienne)

    actifs, termines = [], []
    for fichier in fichiers:
        etat = _lire_fiche_travail(fichier)
        perime = not etat
        termine = etat.get("termine") if etat else None
        a_retirer = bool(termine and retirer_conclusion is not None
                        and etat.get("cle") == retirer_conclusion)
        if termine:
            try:
                fini = dt.datetime.fromisoformat(str(termine))
                duree = float(etat.get("visible_secondes", TRAVAIL_TERMINE_VISIBLE))
                perime = (maintenant - fini).total_seconds() > duree or a_retirer
            except (TypeError, ValueError):
                perime = True
            if not perime:
                termines.append(etat)
        elif etat:
            try:
                depuis = dt.datetime.fromisoformat(str(etat.get("depuis")))
                age = (maintenant - depuis).total_seconds()
                pid = int(etat.get("pid") or 0)
            except (TypeError, ValueError):
                perime = True
            else:
                perime = age > 900 or not processus_vivant(pid)
            if not perime:
                actifs.append(etat)
        if perime:
            supprime = _supprimer_fiche_travail(fichier, etat, attente=0.25 if a_retirer else 0)
            if a_retirer and not supprime:
                termines.append(etat)
    return actifs, termines


def effacer_conclusion_travail(cle: str) -> bool:
    _, termines = _etats_travail(retirer_conclusion=cle)
    return not any(etat.get("cle") == cle for etat in termines)


def _priorite_travail(etat: dict, actif: bool) -> tuple:
    """Update completion, then download, then merge."""
    cle = str(etat.get("cle") or "")
    telechargement = cle in ("phase.inventory_clips", "phase.download_clips")
    # actif download > fin download > actif autre > fin autre
    categorie = (4 if actif else 3) if telechargement else (2 if actif else 1)
    if cle == "phase.update_noop":
        categorie = 5
    return categorie, str(etat.get("termine") or etat.get("depuis") or "")


def travail_en_cours() -> dict:
    """Work in progress, or an empty dict.

    Same safeguards as locks: a marker left by a dead or too-old process must not
    imply eternal computation."""
    actifs, _ = _etats_travail()
    return max(actifs, key=lambda etat: _priorite_travail(etat, True)) if actifs else {}


def travail_affichable() -> dict:
    """Active work, or last retained N/N completion for the next web poll."""
    actifs, termines = _etats_travail()
    candidats = [(etat, True) for etat in actifs] + [
        (etat, False) for etat in termines
    ]
    if not candidats:
        return {}
    choisi = dict(max(candidats, key=lambda item: _priorite_travail(*item))[0])
    # Shown work may be a priority terminal N/N while merge still runs behind.
    # The browser must distinguish "visible" from "active" so it does not wrongly
    # re-enable its button.
    choisi["actif"] = bool(actifs)
    return choisi


# I-11: options that take a value, so we never confuse that value with starting
# a new group even when it matches a verb name ("download --camera watch",
# "download --camera update"...). Kept here rather than inferred from each
# program's parser: those live in separate files (blink2video.py, merge_daily.py,
# serve.py, watch.py), predating this split and not queryable yet.
_OPTIONS_VALEUR_UNIQUE = frozenset((
    "--hub", "--camera", "--since", "--output", "--from", "--port",
    "--input", "--weekly-output", "--monthly-output", "--normalized-output",
    "--excluded-output", "--timezone", "--date", "--font", "--preset",
    "--crf", "--thumbs", "--usb-loop", "--cloud-loop",
    # Written by standard() in each "start": a trusted host named like a verb
    # ("watch", "list"...) would otherwise open a new group, and serve refused
    # to start for lack of value (audit 2026-09-24).
    "--trusted-host",
))
# "nargs=+" options: consume following words until one looks like an option
# ("watch --ignore serve jardin" targets two cameras, one named like a verb).
_OPTIONS_VALEURS_MULTIPLES = frozenset(("--exclude", "--include", "--ignore", "--unignore"))


def decouper_verbes(arguments) -> list:
    """Split "watch --loop download --loop merge" into groups.

    Each known verb name opens a group running until the next verb: options
    belong to the verb before them. That allows citing as many as needed, each
    configured differently.

    The same verb may appear multiple times with different options:
    "download --from usb --loop 10 download --from cloud --loop 1" follows two
    sources at two cadences. The old one-verb-once rule targeted conflicting
    settings; these are complementary.

    Raises ValueError if the first element is not a verb, since the first options
    would have nothing to attach to."""
    arguments = list(arguments)
    groupes = []
    indice, total = 0, len(arguments)
    while indice < total:
        element = arguments[indice]
        if element in VERBES:
            groupes.append([element])
            indice += 1
            continue
        if not groupes:
            raise ValueError(f"« {element} » n'est pas un verbe")
        groupes[-1].append(element)
        indice += 1
        if element in _OPTIONS_VALEUR_UNIQUE and indice < total:
            groupes[-1].append(arguments[indice])
            indice += 1
        elif element in _OPTIONS_VALEURS_MULTIPLES:
            while indice < total and not arguments[indice].startswith("-"):
                groupes[-1].append(arguments[indice])
                indice += 1
        elif (element == "--loop" and indice < total
              and arguments[indice].lstrip("-").isdigit()):
            # nargs="?": --loop alone is valid (default cadence), so its value is
            # consumed only if the next word is a number.
            groupes[-1].append(arguments[indice])
            indice += 1
    return groupes


def cadence_positive(valeur: str) -> int:
    """Convert a CLI cadence to a strictly positive integer."""
    try:
        minutes = int(valeur)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "cadence must be an integer number of minutes"
        ) from exc
    if minutes <= 0:
        raise argparse.ArgumentTypeError(
            "cadence must be strictly greater than zero"
        )
    return minutes


def jours_non_negatifs(valeur: str) -> int:
    """Convert a CLI day count and explicitly allow zero."""
    try:
        jours = int(valeur)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "day count must be an integer"
        ) from exc
    if jours < 0:
        raise argparse.ArgumentTypeError(
            "day count must be zero or positive"
        )
    return jours


def port_valide(valeur: str) -> int:
    """Convert a CLI port and reject values outside TCP/UDP range."""
    try:
        port = int(valeur)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "port must be an integer"
        ) from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            "port must be between 1 and 65535"
        )
    return port


def ajouter_boucle(parser) -> None:
    """Add --loop to an argument parser.

    Repetition is a way of working, not a job: it applies to any verb rather than
    being one. Defined once here, it behaves the same everywhere."""
    parser.add_argument(
        "--loop", type=cadence_positive, nargs="?", const=10, default=None,
        metavar="MINUTES",
        help=_msg("aide_loop"),
    )


def repeter(travail, minutes, journal=None) -> int:
    """Run `travail` once, or indefinitely if `minutes` is set.

    Two background-loop bugs are fixed here for all verbs sharing this function:

    - a transient error (network, ffmpeg, JSON) must not kill the worker for
      good: only Ctrl+C should stop it (I-17);
    - next turn deadline is computed from the current turn's start on
      time.monotonic(), rather than sleeping after work: a turn longer than the
      cadence must not shift all following turns by that much (O-05)."""
    import time

    if not minutes:
        return int(travail() or 0)
    print(_msg("repetition", minutes=minutes))
    if journal:
        journal(f"repeating every {minutes} min")
    periode = minutes * 60
    try:
        while not arret_demande():
            echeance = time.monotonic() + periode
            try:
                travail()
            except Exception as erreur:
                # Translated console; journal (diagnostic file) stays in its usual
                # technical form like other entries here.
                print(_msg("tour_interrompu", erreur=erreur))
                if journal:
                    journal(f"run interrupted by an error, retrying next time: {erreur}")
            # Sleep split into short slices: otherwise a shutdown requested between
            # turns would only be seen at full deadline (up to `minutes` wait),
            # not within a second (review 27/08: cooperative stop vs external kill).
            while not arret_demande():
                reste = echeance - time.monotonic()
                if reste <= 0:
                    break
                time.sleep(min(1.0, reste))
    except KeyboardInterrupt:
        if journal:
            journal("stopped repeating")
        print(_msg("arret"))
    else:
        if journal:
            journal("stop requested, clean exit")
    return 0


def commande_composee(arguments) -> list:
    """Full command line via the entry point, verbs included.

    Unlike self_command, which targets one verb's program: here blink itself
    must receive the rest, since it knows how to launch several verbs side by
    side."""
    if frozen():
        return [sys.executable, *arguments]
    return [sys.executable, "-u",
            str(Path(__file__).resolve().parent / "blink2video.py"), *arguments]


def self_command(verb: str, *arguments: str) -> list:
    """Command line to relaunch on another verb.

    The only place in the program that knows the difference between the two
    execution modes. Everything else calls self_command and ignores sources vs
    executable."""
    if frozen():
        return [sys.executable, verb, *arguments]
    if verb not in VERBES:
        raise ValueError(f"unknown verb: {verb}")
    module = VERBES[verb].module
    base = [sys.executable, "-u",
            str(Path(__file__).resolve().parent / f"{module}.py")]
    # The entry point expects its verb as first positional argument; other
    # programs are invoked directly, without a verb.
    return base + ([verb] if module == ENTREE else []) + list(arguments)
