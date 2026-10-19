"""CLI grammar and supervision: argument parsing, delegated verbs,
chaining multiple verbs, stop, browser open, onboarding.

Extracted from blink2video.py at step 8 (AUDIT-2026-08-13.md, section 20, 8.5).

O-06/8.7/8.8: importing this file must not require aiohttp or blinkpy —
neither `stop`, `open`, `--help` nor `update` needs either. Functions that
actually talk to Blink (`main`, the « start » branch of
`executer`, `accueillir`) therefore import `blink_auth`/`blink_models`/
`blink_engine` locally, just before `runtime.bootstrap()`, instead of a
top-level import paying that cost for everyone. Same principle as
`deleguer()` already applies for merge_daily/serve/watch/maj/autostart."""

from __future__ import annotations  # Python 3.8 (Windows 7 build): "X | None" annotations are only evaluated when strings are written, never at runtime.

import argparse
import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

import runtime

import blink_registre


def parse_args() -> argparse.Namespace:
    programme = Path(sys.argv[0]).stem or "blink2video"
    version = runtime.version_affichee()
    # Verbe.fr/.en already existed but only .fr was read here: main help
    # stayed French regardless of language (issue #16).
    langue = runtime.lire_langue()
    parser = argparse.ArgumentParser(
        prog=programme,
        # Verbs go in the description, not in an argument group:
        # declaring them to argparse would create false positionals that
        # pollute the usage line and break parsing.
        description=(
            f"blink2video {version}\n\n"
            + msg("aide_description") + "\n\n" + msg("aide_verbes_titre") + "\n"
            + "".join(f"  {nom:11} {getattr(verbe, langue)}\n"
                      for nom, verbe in runtime.VERBES.items())
            + "\n  " + msg("aide_verbe_help")
        ),
        # Examples follow the order verbs are encountered:
        # sign in, see what's there, fetch, assemble, view,
        # then automate. A journey, not a catalog.
        epilog=msg("aide_premiers_pas") + "\n" + "\n".join(
            f"  {programme} {commande:<20} {msg(cle)}"
            for commande, cle in (
                ("login", "aide_pas_login"),
                ("list", "aide_pas_list"),
                ("download", "aide_pas_download"),
                ("merge", "aide_pas_merge"),
                ("serve", "aide_pas_serve"),
                ("autostart on", "aide_pas_autostart"),
            )
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"blink2video {version}")
    parser.add_argument(
        "command",
        nargs="?",
        choices=tuple(runtime.VERBES),
        # No default command: with no argument, show help rather than
        # opening a connection to the Blink account. A command run with
        # nothing must not hit the network without the user's knowledge.
        default=None,
        # Each verb's detailed help is printed under standard help,
        # in one list: separating verbs handled here from delegated ones
        # teaches the user nothing and suggests the former do not exist.
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--hub", help=msg("aide_hub"))
    parser.add_argument("--camera", help=msg("aide_camera"))
    parser.add_argument(
        "--since",
        type=runtime.jours_non_negatifs,
        metavar=msg("aide_metavar_jours"),
        help=msg("aide_since"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=blink_registre.OUTPUT,
        help=msg("aide_output", defaut=blink_registre.OUTPUT),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=msg("aide_overwrite"),
    )
    parser.add_argument(
        "--from", dest="source", choices=("usb", "cloud", "all"), default="all",
        help=msg("aide_from"),
    )
    # Per-verb loop: cloud is polled every minute without waking anything,
    # whereas the local manifest wakes the module and settles for ten
    # minutes. Two cadences beat one compromise.
    runtime.ajouter_boucle(parser)
    # Internal scheduler used by « start »: one process does the first
    # USB+cloud inventory (single total), then respects the different
    # cadences of both sources. These options stay hidden: --loop is
    # the ordinary interface for a hand-typed command.
    parser.add_argument("--usb-loop", type=runtime.cadence_positive,
                        default=None, help=argparse.SUPPRESS)
    parser.add_argument("--cloud-loop", type=runtime.cadence_positive,
                        default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.command is None:
        # No command: help rather than account connection: a command
        # typed with no argument must not hit the network.
        parser.print_help()
        raise SystemExit(0)
    cadences_sources = (args.usb_loop is not None, args.cloud_loop is not None)
    if any(cadences_sources) and not all(cadences_sources):
        parser.error(msg("erreur_cadences_ensemble"))
    if all(cadences_sources) and (
            args.command != "download" or args.source != "all" or args.loop is not None):
        parser.error(msg("erreur_cadences_exigent"))
    return args


LIBELLES = {
    "en": {
        "aide_description":
            "Manage Blink cameras from a computer: live view, arming, "
            "timestamped archive.",
        "aide_verbes_titre": "Verbs:",
        "aide_verbe_help": "<verb> --help shows the options of each.",
        "aide_premiers_pas": "Getting started:",
        "aide_pas_login": "sign in once",
        "aide_pas_list": "see what the Sync Module holds",
        "aide_pas_download": "fetch the clips",
        "aide_pas_merge": "assemble the videos",
        "aide_pas_serve": "open the interface",
        "aide_pas_autostart": "monitor at every login",
        "aide_hub": "name of the Sync Module to use",
        "aide_camera": "keep only this camera",
        "aide_since": "keep only clips from the last N days",
        "aide_metavar_jours": "DAYS",
        "aide_output": "destination folder (default: {defaut})",
        "aide_overwrite": "force re-downloading visible clips, even ones already fetched",
        "aide_from":
            "where to look for clips: « usb » the Sync Module's USB drive, "
            "« cloud » the subscription's cloud, « all » both (default)",
        "erreur_cadences_ensemble": "--usb-loop and --cloud-loop go together",
        "erreur_cadences_exigent":
            "USB/cloud intervals require « download --from all » without --loop",
        "aide_desc_open": "Opens the interface in the browser, and says if nothing is listening.",
        "aide_port_open": "interface port (default: the configured port)",
        "aide_desc_stop": "Stops the running instances, including the autostart one.",
        "aide_desc_restart": "Stops the running instance, then relaunches « start » fresh.",
        "aide_sans_relance": "stop without relaunching afterwards",
        "echec_connexion": "\nBlink sign-in failed.",
        "connexion_reussie": "\nSigned in to Blink.",
        "session_sauvegardee": "Session saved to: {chemin}",
        "sync_modules_titre": "\n=== SYNC MODULES ===",
        "aucun_sync_module": "No Sync Module found on this account.",
        "sync_module_ligne": "- {nom} (ID {sync_id}, network {network_id})",
        "usb_exige_sync_module": "\nLocal storage needs a Sync Module; use --from cloud.",
        "erreur_hub": "\nError: {erreur}",
        "personne_ecoute": "Nobody is listening on {adresse}.",
        "lancez_serve":
            "Run « blink2video serve », or « blink2video autostart on » "
            "for the interface to start with the session.",
        "ouverture_adresse": "Opening {adresse}",
        "rien_ne_tourne": "Nothing is running.",
        "arret_instance": "Stopping « {commande} » (PID {pid}, since {depuis})",
        "pid_reattribue":
            "  PID {pid} no longer matches this instance "
            "(number reassigned to another program): skipped.",
        "toujours_en_vie": "Still alive: {liste}",
        "arrete": "Stopped.",
        "arret_redemarrage_en_cours": "Launch, stop or restart in progress ({erreur}). Try again in a moment.",
        "session_invalide_injoignable":
            "\nSaved session invalid or unreachable: {erreur}",
        "aucune_session_ouverture_connexion":
            "\nNo valid Blink session. Opening the sign-in page...",
        "premiere_utilisation":
            "\nFirst use: check the settings before downloading...",
        "etape_configuration": "the end of setup",
        "etape_connexion": "sign-in",
        "interface_arretee_avant":
            "The interface stopped before {etape} (code {code}). Aborting.",
        "interface_ne_repond_pas": "The interface is not responding on {adresse}. Aborting.",
        "page_configuration": "setup",
        "page_connexion": "sign-in",
        "page_prete": "{page} page ready on {adresse} (browser not opened).",
        "navigateur_indisponible":
            "Browser unavailable: open {adresse} yourself, "
            "or use « blink2video login » in a terminal.",
        "compte_accessible": "account reachable, {n} camera(s)",
        "cloud_uniquement": " (account with no Sync Module, cloud only)",
        "connexion_reussie_resume": "\nSigned in: {resume}.",
        "reglages_initiaux_enregistres":
            "\nInitial settings saved. Starting downloads...",
        "delai_configuration_depasse":
            "\nSetup timed out. Aborting, no download started.",
        "delai_connexion_depasse":
            "\nSign-in timed out. Aborting, nothing left running.",
        "start_aide_usage": "usage: blink2video start [serve options]",
        "start_aide_intro": "Launches the recommended setup:",
        "start_aide_options": "Options given here go to the interface, --port for example.",
        "start_aide_stop": "« blink2video stop » stops everything.",
        "demarrage_deja_en_cours": "Already starting elsewhere, opening the interface...",
        "stop_seul": "« stop » is used alone: it stops what's already running.",
        "impossible_demarrer_pendant_arret": "Cannot start while stopping ({erreur}).",
        "lance": "Started: {commande}",
        "etape_verbe": "Step: {commande}",
        "arrete_verbe_code": "Stopped: {verbe} (code {code})",
        "arrete_verbe_normal": "Stopped: {verbe} (normal exit)",
        "interruption_clavier": "\nStopping.",
        "option_avant_verbe":
            "« {option} » comes before the first verb: options "
            "follow the verb they apply to.",
        "liste_verbes": "Verbs: {liste}",
        "connexion_annulee": "\nLogin cancelled.",
    },
}
LIBELLES["fr"] = dict(LIBELLES["en"])



def msg(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


async def main(args: argparse.Namespace) -> int:
    # Lazy: only login/list/download/start (via this path) pay the cost
    # of aiohttp and blinkpy (O-06/8.7/8.8).
    runtime.bootstrap()
    import blink_auth
    import blink_engine
    import blink_models

    async with blink_auth.session_http_temporaire() as session:
        blink = await blink_auth.connect(session)
        if blink is None:
            print(msg("echec_connexion"))
            return 1

        print(msg("connexion_reussie"))
        print(msg("session_sauvegardee", chemin=blink_auth.CONFIG.resolve()))
        print(msg("sync_modules_titre"))
        # The modern homescreen may advertise a real storage module that
        # legacy endpoints still used by blinkpy 0.25.9 did not place in
        # blink.sync (notably with an XR). The same discovery as the downloader
        # must therefore precede display and the USB guard.
        modules_disponibles = blink_models.select_sync_modules(blink, None)
        if not modules_disponibles:
            print(msg("aucun_sync_module"))
        for name, sync in modules_disponibles:
            print(msg("sync_module_ligne", nom=name, sync_id=sync.sync_id,
                      network_id=sync.network_id))

        if args.command == "login":
            return 0

        if args.source == "usb" and not modules_disponibles:
            print(msg("usb_exige_sync_module"))
            return 1

        try:
            # --hub filter resolves regardless of source: the cloud plan
            # derives allowed networks from it (blink_engine._inventorier_cloud),
            # and an unknown module name must be reported even without USB.
            # Without --hub, cloud-only source needs no module.
            selectionnes = (modules_disponibles if not args.hub
                            else blink_models.select_sync_modules(blink, args.hub))
            modules = [] if args.source == "cloud" and not args.hub else selectionnes
        except ValueError as error:
            print(msg("erreur_hub", erreur=error))
            return 2

        return await blink_engine.boucler(blink, args, modules)


# Single entry point. Other programs keep their own file and
# argument parsing: we only call them, without moving anything.
# Merging all four into one file would yield a four-thousand-line script,
# less readable and impossible to test in pieces.
#
# Verb-style commands, like git or docker: one name to remember,
# one verb for the action. Each verb receives the arguments that follow
# unchanged, so « blink2video.py review --port 8899 » is exactly
# « blink2video serve --port 8899 ».
# Verbs, their program and description live in runtime.VERBES:
# one table, read here for help and delegation, by self_command for
# relaunch, and by docs.py for READMEs.
DELEGUES = runtime.DELEGUES


def deleguer(verbe: str, arguments: list) -> int:
    """Hands off to a verb's program, in the same process.

    Import happens here, not at file top: those modules import
    blink2video.py themselves, and pull ffmpeg or aiohttp behind them.
    A simple manifest request should not pay that load."""
    import importlib

    module = importlib.import_module(DELEGUES[verbe])
    sys.argv = [f"{DELEGUES[verbe]}.py", *arguments]
    return int(module.main() or 0)


def ouvrir(arguments: list = ()) -> int:
    """Opens the interface in the browser, and says if nobody is listening.

    The address is obvious if you know it; not for someone installing the tool.
    A verb shows up in « --help »; a port is hard to remember."""
    import socket
    import webbrowser

    parseur = argparse.ArgumentParser(
        prog="blink2video open",
        description=msg("aide_desc_open"),
    )
    # Configured port, not hard-coded 8765: without this, « open » with no argument
    # would always open 8765 even after a port change in
    # settings (review 27/08, bug 5).
    parseur.add_argument("--port", type=runtime.port_valide,
                         default=runtime.lire_reglages()["port"],
                         help=msg("aide_port_open"))
    options = parseur.parse_args(list(arguments))
    adresse = f"http://127.0.0.1:{options.port}/"

    with socket.socket() as prise:
        prise.settimeout(2)
        if prise.connect_ex(("127.0.0.1", options.port)) != 0:
            print(msg("personne_ecoute", adresse=adresse))
            print(msg("lancez_serve"))
            return 1

    print(msg("ouverture_adresse", adresse=adresse))
    webbrowser.open(adresse)
    return 0


def _arreter_instances() -> int:
    """Stop body, called under the global control lock."""
    instances = runtime.lire_instances()
    if not instances:
        runtime.effacer_arret_demande()
        print(msg("rien_ne_tourne"))
        return 0

    # Cooperative flag set before any kill (review 27/08): serve,
    # watch, download and merge re-read it (repeter()/shutdown()) and
    # exit on their own between rounds rather than being killed mid-run.
    # The kill below remains the fallback for what did not exit alone
    # within the grace period.
    runtime.demander_arret()

    restants = []
    for fiche in instances:
        commande = " ".join(" ".join(groupe) for groupe in fiche.get("verbes") or [])
        print(msg("arret_instance", commande=commande or "blink2video",
                  pid=fiche['pid'], depuis=fiche.get('depuis', '?')))
        # A process number eventually gets reassigned to unrelated software;
        # confusing it with the instance we think we track has already caused
        # « stop » to kill a third-party service and messaging on a user's
        # machine. Verify identity before killing anything,
        # never PID existence alone.
        identites = fiche.get("identites") or {}
        if not isinstance(identites, dict):
            identites = {}

        def etat_processus(pid: int, marqueurs=None):
            """True=same process, False=terminated/recycled."""
            pid = int(pid)
            attendue = identites.get(str(pid))
            if not runtime.processus_vivant(pid):
                return False
            if attendue is None:
                # Legacy record: temporary fallback to command line
                # (PowerShell 2 compatible in runtime.py).
                return runtime.processus_correspond(pid, marqueurs)
            actuelle = runtime.identite_processus(pid)
            # Stored identity but unreadable PID: our account's process is
            # always readable, so this is a PID recycled by a protected
            # service (access denied). Seen 29/09/2026: the pre-reboot record
            # cited a PID taken by AggregatorHost.exe,
            # stop counted it as alive and update, after already
            # killing the rest, gave up without relaunching anything.
            return actuelle is not None and actuelle == attendue

        membres = [int(pid) for pid in
                   [fiche["pid"], *(fiche.get("enfants") or [])]]
        travailleurs = [int(pid) for pid in fiche.get("travailleurs") or []]
        # A merge ffmpeg (merge_daily.run_ffmpeg_batch/concat_copy) does not
        # understand the flag (third-party process, no control) :
        # killed directly, no grace period. Never taskkill /T
        # on its own PID (browser protection, see
        # arreter_processus): otherwise it would stay orphaned, running
        # until done after « stop ». Dedicated fingerprint: its command line
        # never contains « blink2video ».
        for pid_ffmpeg in travailleurs:
            etat = etat_processus(pid_ffmpeg, ["ffmpeg"])
            if etat:
                runtime.arreter_processus(pid_ffmpeg, avec_descendance=True)

        # Grace period for Python members (serve/watch/download/
        # merge): the flag set above lets them exit on their own before
        # we force. processus_vivant() only here, not
        # processus_correspond(): seen in production (review
        # 27/08), full identity check (tasklist + PowerShell
        # per member) was costly enough, repeated every second, to push
        # past 15 s by well over half with only 2 members -
        # much worse with 5 verbs in a real composition. Full
        # identity check still runs once,
        # right after, before the actual kill; a PID reassigned during this
        # grace period (few-second window, very rare) at worst waits the
        # full period for nothing, never kills wrongly.
        # monotonic, not time.time(): NTP/DST correction during
        # wait must not shorten or lengthen this 15 s grace period
        # (same fix as verrou() in runtime.py, found in the
        # same audit).
        limite = time.monotonic() + 15
        pids_python = list(membres)
        while time.monotonic() < limite and any(
                runtime.processus_vivant(int(m)) for m in pids_python):
            time.sleep(1)

        for membre in membres:
            if not runtime.processus_vivant(int(membre)):
                continue
            etat = etat_processus(membre)
            if not etat:
                print(msg("pid_reattribue", pid=membre))
                continue
            runtime.arreter_processus(int(membre),
                                       avec_descendance=(membre != fiche["pid"]))

        # I-14: record is removed only if everyone is truly dead.
        # It used to be erased unconditionally here, before even knowing
        # if stop succeeded; a survivor was then untraceable on the
        # next « stop », its only trail gone with the record. A
        # member whose PID exists but no longer matches this instance
        # (above) does not count as a survivor: our
        # process is dead, only its number was reused.
        survivants = []
        for membre in [*membres, *travailleurs]:
            etat = etat_processus(membre, ["ffmpeg"] if membre in travailleurs else None)
            if etat:
                survivants.append(str(membre))
        if survivants:
            restants.extend(survivants)
            continue
        Path(fiche["fiche"]).unlink(missing_ok=True)
    if restants:
        # Keep the stop request: a slow or momentarily
        # unkillable process may still see it and exit cleanly. Clearing
        # it here made things resume as if nothing happened.
        print(msg("toujours_en_vie", liste=", ".join(restants)))
        return 1
    runtime.effacer_arret_demande()
    print(msg("arrete"))
    return 0


def arreter(arguments: list = ()) -> int:
    """Stops running instances, including the autostart one.

    An instance launched without a console cannot receive Ctrl+C, and killing
    it by number alone left its verbs behind: « watch » kept
    running, orphaned, holding the sync module. The record
    written at startup gives the process to interrupt, and the system gives
    its descendants."""
    # Options go through argparse like other verbs, even when there are
    # none: without this « stop --help » stopped the instance instead of
    # explaining, which the test suite did every run on
    # the machine's real instance.
    argparse.ArgumentParser(
        prog="blink2video stop",
        description=msg("aide_desc_stop"),
    ).parse_args(list(arguments))
    try:
        # Patient like launch (attente=10), unlike restart:
        # stop typed right after start hit while a child
        # was registering its record under the « launch » lock, and was refused.
        # Queued behind a launch, it stops what was just
        # launched, which is exactly what is asked of it.
        with runtime.verrou_controle("stop", attente=10):
            return _arreter_instances()
    except runtime.BusyError as erreur:
        print(msg("arret_redemarrage_en_cours", erreur=erreur))
        return 1


def redemarrer(arguments: list = ()) -> int:
    """Stops the running instance, then relaunches « start » fresh.

    Unlike « update », which restores exactly the previous composition
    (same --loop, read back from the record), this verb relaunches « start »
    plainly: USB/cloud cadences and everything changed in the
    settings file since are picked up, not replayed. Serves the
    web page settings panel: --sans-relance for its Stop button,
    no option for its Apply button.

    --finaliser is an implementation detail, never typed by hand:
    called from « serve », this verb is the child of the process « stop »
    will kill, entire branch included on Windows (taskkill /T). Like
    maj.installer/finaliser, a first stage only launches the
    second then returns immediately: by the time « stop » starts
    searching its tree, the first is already gone, and the second, already
    detached, escapes it."""
    parser = argparse.ArgumentParser(
        prog="blink2video restart",
        description=msg("aide_desc_restart"),
    )
    parser.add_argument("--finaliser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--delai", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--sans-relance", action="store_true",
                        help=msg("aide_sans_relance"))
    args = parser.parse_args(list(arguments))
    installe = runtime.app_dir()

    if not args.finaliser:
        suite = ["--finaliser"] + (["--sans-relance"] if args.sans_relance else [])
        if args.delai > 0:
            suite.extend(["--delai", str(args.delai)])
        runtime.demarrer(runtime.self_command("restart", *suite),
                         cwd=str(installe), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=(sys.platform != "win32"))
        return 0

    # Under systemd service, detaching is not enough: this process stays
    # in the unit's cgroup, which the main process exit brings down
    # entirely, before any relaunch (issue #35). The rest continues outside
    # the unit, and relaunch goes through systemd.
    import autostart
    if autostart.sortir_du_service(runtime.self_command("restart", *arguments)):
        return 0

    try:
        # One finalizer at a time. attente=0 is intentional: two quick
        # clicks must not queue the second then make it
        # stop the brand-new composition launched by the first.
        with runtime.verrou_controle("restart"):
            if args.delai > 0:
                time.sleep(args.delai)
            code = _arreter_instances()
            if code:
                # Never stack a new composition on an instance
                # that stop failed to finish terminating.
                return code
            # Files stay held briefly after process death,
            # while Windows closes its handles.
            instances = []
            for _ in range(20):
                instances = runtime.lire_instances()
                if not instances:
                    break
                time.sleep(1)
            if instances:
                return 1
            if not args.sans_relance and not autostart.relancer_service():
                runtime.demarrer(runtime.self_command("start"), cwd=str(installe),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 start_new_session=(sys.platform != "win32"))
            return 0
    except runtime.BusyError:
        return 1


def _port_demande(arguments: list) -> int | None:
    """Port explicitly requested in these arguments, or None if absent.

    Minimal extraction tolerant of other options (--timezone, etc.):
    lets start's « already running » shortcut look at the same
    port as will actually be used for serve relaunched later,
    rather than always trusting the saved port even when an
    explicit --port overrides it (review 27/08, bug 5)."""
    mini = argparse.ArgumentParser(add_help=False)
    mini.add_argument("--port", type=runtime.port_valide, default=None)
    options, _ = mini.parse_known_args(list(arguments))
    return options.port


def _port_ouvert(port: int) -> bool:
    """Standalone function to stay mockable without touching the global
    socket module, which asyncio also uses for its own loop."""
    import socket

    with socket.socket() as prise:
        prise.settimeout(1)
        return prise.connect_ex(("127.0.0.1", port)) == 0


MARQUEUR_RACCOURCI = ".blink_raccourci_cree"


def _proposer_raccourci_bureau() -> None:
    """Creates the desktop shortcut on the very first successful « start », never
    again: the marker prevents recreating it if the user
    deleted it on purpose later. Best-effort: failure here (no
    desktop environment, permission denied...) must never make
    startup itself fail."""
    marqueur = runtime.app_dir() / MARQUEUR_RACCOURCI
    if marqueur.exists():
        return
    try:
        marqueur.touch()
    except OSError:
        return
    try:
        import raccourci_bureau
        raccourci_bureau.creer()
    except Exception:
        pass


def accueillir(etat: dict, supplement: list, delai: float = 600.0,
               configuration_initiale: bool = False) -> int:
    """Onboarding: Blink sign-in then, on a fresh install,
    settings validation before letting « start » continue.

    The server opened here is temporary: once sign-in succeeds, it
    is stopped and « start » relaunches the full composition, serve included,
    so we need not attach an already-running process to
    normal supervision. Cost is a brief interface restart,
    invisible in use except for a tab already open at that moment.

    Two environment variables, read nowhere else except in
    tests and never documented for users: BLINK_ONBOARDING_TIMEOUT
    shortens the wait (real human time to read a 2FA code email
    has no place in an automated suite), BLINK_NO_BROWSER
    skips actually opening a browser, which the test suite must
    never trigger (section 12.5).

    In initial-setup mode, only ``serve --initial-setup`` runs:
    no download/watch/merge worker exists yet. The parent waits for the
    marker written by /api/reglages, stops this temporary server, then launches
    the full composition from the now-chosen storage root."""
    import os
    import webbrowser

    delai = float(os.environ.get("BLINK_ONBOARDING_TIMEOUT", delai))
    # Same fixed block as full composition (runtime.standard(): port,
    # timezone and trusted host saved), followed by hand-typed supplement,
    # which wins (argparse keeps the last occurrence). Without it,
    # the temporary server started on serve.py's hard-coded 8765 while
    # the user had chosen another port in settings - often
    # precisely because 8765 was taken: expired session + custom
    # port = impossible sign-in (audit 2026-09-24).
    options_serveur = [*runtime.standard()[1:runtime.LONGUEUR_BLOC_SERVE],
                       *supplement]
    # Shared minimal parser understands both argparse forms,
    # ``--port 9000`` and ``--port=9000``. A second manual extraction
    # used to live here: the ``=`` form did launch serve on the
    # requested port, but onboarding still probed and opened 8765.
    port = _port_demande(options_serveur)

    connexion_requise = not bool(etat.get("authenticated"))
    if etat.get("error"):
        print(msg("session_invalide_injoignable", erreur=etat['error']))
    if connexion_requise:
        print(msg("aucune_session_ouverture_connexion"))
    elif configuration_initiale:
        print(msg("premiere_utilisation"))

    if configuration_initiale:
        options_serveur.append("--initial-setup")
    processus = runtime.demarrer(
        runtime.self_command("serve", *options_serveur),
        cwd=str(runtime.app_dir()), creationflags=runtime.flags_enfant(),
        start_new_session=(os.name != "nt"),
    )
    parametres = []
    if connexion_requise:
        parametres.append("login=1")
    if configuration_initiale:
        parametres.append("setup=1")
    adresse = f"http://127.0.0.1:{port}/"
    if parametres:
        adresse += "?" + "&".join(parametres)

    def echec_processus() -> int:
        etape = msg("etape_configuration") if configuration_initiale else msg("etape_connexion")
        print(msg("interface_arretee_avant", etape=etape, code=processus.returncode))
        return processus.returncode or 1

    # Owner to the end (5.16): on any exit, this temporary server
    # is stopped before returning. On success, « start »
    # relaunches right after the full composition, serve included, rather than
    # attaching this already-running process to normal supervision.
    try:
        limite = time.monotonic() + delai
        pret = False
        while time.monotonic() < limite:
            if processus.poll() is not None:
                return echec_processus()
            if _port_ouvert(port):
                pret = True
                break
            time.sleep(0.5)
        if not pret:
            print(msg("interface_ne_repond_pas", adresse=adresse))
            return 1

        if os.environ.get("BLINK_NO_BROWSER") == "1":
            page = msg("page_configuration") if configuration_initiale else msg("page_connexion")
            print(msg("page_prete", page=page, adresse=adresse))
        else:
            print(msg("ouverture_adresse", adresse=adresse))
            if not webbrowser.open(adresse):
                print(msg("navigateur_indisponible", adresse=adresse))

        runtime.bootstrap()
        import blink_auth

        authentifie = not connexion_requise
        connexion_annoncee = authentifie
        while time.monotonic() < limite:
            if processus.poll() is not None:
                return echec_processus()
            if not authentifie:
                resultat = asyncio.run(blink_auth.preflight())
                authentifie = bool(resultat["authenticated"])
            if authentifie and not connexion_annoncee:
                resume = msg("compte_accessible", n=resultat['cameras'])
                if resultat["cloud_only"]:
                    resume += msg("cloud_uniquement")
                print(msg("connexion_reussie_resume", resume=resume))
                connexion_annoncee = True
                if configuration_initiale:
                    # Entering the 2FA code must not consume the time
                    # left to read and validate the panel that follows.
                    limite = time.monotonic() + delai
            if authentifie and not configuration_initiale:
                return 0
            if authentifie and runtime.configuration_initiale_effectuee():
                # /api/reglages sets the marker just before sending its
                # response. This margin lets it be delivered and
                # read by a slow browser (especially on Windows 7) before
                # the finally below shuts down the temporary server.
                time.sleep(0.75)
                print(msg("reglages_initiaux_enregistres"))
                return 0
            time.sleep(2)

        if authentifie and configuration_initiale:
            print(msg("delai_configuration_depasse"))
        else:
            print(msg("delai_connexion_depasse"))
        return 1
    finally:
        if processus.poll() is None:
            runtime.arreter_processus(processus.pid, avec_descendance=True)


def _groupe_persistant(groupe: list) -> bool:
    """True if this group stays active until an explicit stop.

    ``argparse`` accepts both ``--loop 10`` and ``--loop=10``. Keeping
    this recognition in one place avoids a command being launched
    in a loop while classified as one-shot by the supervisor.
    """
    if not groupe:
        return False
    if groupe[0] == "serve":
        return True
    options = ("--loop", "--usb-loop", "--cloud-loop")
    return any(
        argument == option or argument.startswith(option + "=")
        for argument in groupe[1:]
        for option in options
    )


def _sortie_propre_sur_sigterm() -> None:
    """Turns SIGTERM into exit code 0 for the supervisor.

    Under launchd, only exit code 0 counts as intentional: killed by
    « stop »'s SIGTERM, the supervisor looked crashed, and the autostart
    agent relaunched it immediately, undoing stop, update
    and « Apply » (issue #31, verified on a macOS runner). A simple
    signal handler would not suffice: the menu bar icon
    holds the main thread in AppKit, where Python would not run it. The
    signal is therefore blocked, then waited on by a dedicated thread (sigwait).

    Called after launching children and before creating any thread:
    those inherit the block, already-launched children do not. Any
    blink2video process launched later clears it at startup
    (runtime.debloquer_sigterm). « stop » stops children itself, from
    their records: exiting without waiting is enough, and waiting longer than
    its grace period would end in SIGKILL, which launchd would relaunch."""
    import os
    import signal
    if not hasattr(signal, "pthread_sigmask"):
        return  # Windows: taskkill does not send a signal.
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})

    def attendre() -> None:
        signal.sigwait({signal.SIGTERM})
        os._exit(0)

    threading.Thread(target=attendre, name="sigterm", daemon=True).start()


def _migrer_agents_macos() -> None:
    """Updates a launchd agent installed by an older version (see
    autostart.migrer_agents_macos). launchd launches the supervisor: so
    on first start of a new version, it fixes the agent that
    launched it. Never at the cost of startup itself."""
    if sys.platform != "darwin":
        return
    try:
        import autostart
        autostart.migrer_agents_macos()
    except Exception:
        pass


def executer(groupes: list) -> int:
    """Runs the cited verbs together.

    A single verb is handled in this process, keeping output and
    return code direct. Several are launched side by side and awaited: they
    stop together, otherwise Ctrl+C would leave
    programs with nobody to stop them."""
    if len(groupes) == 1 and groupes[0][0] == "start":
        # Help must display, not trigger setup: without this
        # handling, « start --help » launched loops and never
        # returned, which the test suite showed by hanging on it.
        if {"-h", "--help"} & set(groupes[0][1:]):
            print(msg("start_aide_usage"))
            print()
            print(msg("start_aide_intro"))
            print()
            print("  blink2video " + " ".join(runtime.standard()))
            print()
            print(msg("start_aide_options"))
            print(msg("start_aide_stop"))
            return 0
        # « start » is not another job: it is the name of the
        # recommended composition, options included. Options given after
        # it append to the first verb, « serve », hence working --port.
        supplement = groupes[0][1:]
        # Computed before the lock: the "already running" shortcut below
        # and the BusyError fallback further down must both look at the
        # explicitly requested port, not only the saved one; and the
        # fallback needs it even when lock acquisition fails before
        # it could compute it itself (review 27/08, bug 5).
        port_demande = _port_demande(supplement)
        # E-01: saved session is actually tested before launching
        # anything permanent, not only checked present on
        # disk (5.3, 5.4). If missing or invalid, the interface
        # opens alone first, with the sign-in page ready, and background
        # loops appear only after confirmed success (5.12,
        # 5.15, 5.16).
        #
        # Lock covers this whole block, until processes actually launch:
        # a desktop shortcut clicked twice in a row (or Explorer double-click
        # firing twice) otherwise launches two « start »
        # in parallel, both passing port check before
        # the first finishes starting. The second yields rather
        # than attempting concurrent startup.
        try:
            with runtime.verrou("start", "start", attente=0):
                # An instance is already listening: « start » then behaves
                # like « open », without relaunching. The same shortcut
                # (desktop or autostart) thus both starts
                # and reopens the interface already running, without
                # composing two separate commands or opening a second
                # console window to check.
                port = port_demande or runtime.lire_reglages()["port"]
                if _port_ouvert(port):
                    return ouvrir(["--port", str(port)])

                # Decided BEFORE sign-in: on a fresh install,
                # sign-in creates blink_auth.json. If we then evaluated
                # traces from an old install, this brand-new
                # session would wrongly skip the initial panel. runtime's
                # « pending » marker also keeps this decision after
                # closing the browser mid-flow.
                configuration_initiale = runtime.configuration_initiale_requise()
                runtime.bootstrap()
                import blink_auth
                etat = asyncio.run(blink_auth.preflight())
                if not etat["authenticated"] or configuration_initiale:
                    code = accueillir(
                        etat, supplement,
                        configuration_initiale=configuration_initiale)
                    if code != 0:
                        return code
                composition = runtime.standard()
                # Fixed block (serve, --port, value, --timezone, value,
                # --trusted-host, value) always precedes supplement: a
                # hand-typed « --port »/« --timezone »/« --trusted-host »
                # therefore comes after the one already in saved
                # configuration, and wins (argparse keeps the last
                # occurrence of an option).
                n = runtime.LONGUEUR_BLOC_SERVE
                code = executer(runtime.decouper_verbes(
                    [*composition[:n], *supplement, *composition[n:]]))
                if code == 0:
                    _proposer_raccourci_bureau()
                return code
        except runtime.BusyError:
            print(msg("demarrage_deja_en_cours"))
            port = port_demande or runtime.lire_reglages()["port"]
            return ouvrir(["--port", str(port)])

    if len(groupes) == 1 and groupes[0][0] == "open":
        return ouvrir(groupes[0][1:])

    if len(groupes) == 1 and groupes[0][0] == "restart":
        return redemarrer(groupes[0][1:])

    if any(groupe[0] == "stop" for groupe in groupes):
        if len(groupes) > 1:
            print(msg("stop_seul"))
            return 2
        return arreter(groupes[0][1:])

    if len(groupes) == 1:
        verbe, *arguments = groupes[0]
        if verbe in DELEGUES:
            # « update » does not register: a record finds what to
            # stop, and update is precisely what stops everything
            # else. Registered, it found itself in the list and
            # killed itself on the first « stop », silently and mid-way.
            if verbe != "update":
                try:
                    with runtime.verrou_controle("launch", attente=10):
                        runtime.inscrire_instance(groupes)
                except runtime.BusyError as erreur:
                    print(msg("impossible_demarrer_pendant_arret", erreur=erreur))
                    return 1
            return deleguer(verbe, arguments)
        if _groupe_persistant(groupes[0]):
            # A download launched alone does not go through the multi-verb
            # branch supervisor. Without a record, ``stop`` thus concluded « nothing
            # running » while the loop stayed active. Publishing the current
            # parent is enough, like a delegated verb launched alone.
            try:
                with runtime.verrou_controle("launch", attente=10):
                    runtime.inscrire_instance(groupes)
            except runtime.BusyError as erreur:
                print(msg("impossible_demarrer_pendant_arret", erreur=erreur))
                return 1
        sys.argv = ["blink2video", verbe, *arguments]
        return asyncio.run(main(parse_args()))

    # What finishes chains; what does not runs alongside.
    # « serve » carries an implicit --loop: it never returns, like
    # any verb asked to repeat. Others run once and
    # stop, so running them together would make no sense:
    # merge would start while download is still writing.
    persistant = [g for g in groupes if _groupe_persistant(g)]
    ponctuels = [g for g in groupes if g not in persistant]

    lances = []
    try:
        # Short section only: publish the record and its children as one
        # launch for stop/restart. Lock is released before
        # monitoring, which may last days.
        with runtime.verrou_controle("launch", attente=10):
            runtime.inscrire_instance(groupes)
            try:
                for verbe, *arguments in persistant:
                    lances.append((verbe, runtime.demarrer(
                        runtime.self_command(verbe, *arguments), cwd=str(runtime.app_dir()),
                        creationflags=runtime.flags_enfant(),
                        # Its own session off Windows: « stop » can then kill its
                        # group, ffmpeg included, without taking the terminal that launched
                        # the whole set.
                        start_new_session=(sys.platform != "win32"))))
                    print(msg("lance", commande=f"{verbe} {' '.join(arguments)}".rstrip()))
            except Exception:
                # A composition verb failed to start (e.g. OSError):
                # those already launched before it must never become
                # orphans invisible to stop. inscrire_instance(groupes,
                # [...]) is only reached after the full loop - without this
                # rollback, the record already written just above (no children)
                # only references this supervisor process, which will itself
                # exit soon because of the same exception, leaving
                # the first child running indefinitely with stop not
                # seeing it (found auditing this file, verified by real
                # execution: mock 2nd Popen failure).
                for _, p in lances:
                    runtime.arreter_processus(p.pid, avec_descendance=True)
                raise
            runtime.inscrire_instance(groupes, [p.pid for _, p in lances])
    except runtime.BusyError as erreur:
        print(msg("impossible_demarrer_pendant_arret", erreur=erreur))
        return 1
    _sortie_propre_sur_sigterm()
    _migrer_agents_macos()

    # Monitored together rather than awaited one after another: a verb that
    # dies in the first second must be seen immediately, not at the end
    # of a loop that will run for days. Others continue; the interface
    # going down is not a reason to stop monitoring.
    pire = 0
    annonces = set()

    def relever_arrets() -> None:
        nonlocal pire
        for rang, (verbe, processus) in enumerate(lances):
            code = processus.poll()
            if code is None or rang in annonces:
                continue
            annonces.add(rang)
            pire = max(pire, abs(code))
            print(msg("arrete_verbe_code", verbe=verbe, code=code) if code
                  else msg("arrete_verbe_normal", verbe=verbe))

    def surveiller(sur_fin=None) -> None:
        # Purge dead instance records without going through « stop » (external
        # kill, crash, power loss): lire_instances() already
        # cleans them as a side effect of reading, but nothing
        # called it while the session runs here without restarting,
        # sometimes for days (seen in production, 2026-09-01: records days old
        # still in .blink_run). This loop, not
        # relever_arrets() nor a verb's un_tour(), lives as long
        # as the session: responsibility stays here rather than spread
        # into a verb whose documented role is something else.
        MENAGE_INSTANCES_SECONDES = 600
        prochain_menage = time.monotonic() + MENAGE_INSTANCES_SECONDES
        while any(processus.poll() is None for _, processus in lances):
            relever_arrets()
            if time.monotonic() >= prochain_menage:
                runtime.lire_instances(journal=print)
                prochain_menage = time.monotonic() + MENAGE_INSTANCES_SECONDES
            time.sleep(1)
        # The last process (or all, if they finished before our first
        # pass) makes the while condition false. An explicit final
        # collection is therefore needed, otherwise its exit code vanishes and an
        # isolated failure is reported as success.
        relever_arrets()
        if sur_fin:
            sur_fin()

    # Fallback to configured port, not hard-coded 8765 (same inconsistent default
    # as bug 5, review 27/08): only applies if serve runs without
    # explicit --port in its arguments, which no longer happens via start
    # (composition always provided) but remains possible for « serve »
    # typed alone.
    port = runtime.lire_reglages()["port"]
    for verbe, *arguments in persistant:
        # Same reading as the server: last --port occurrence
        # wins, and --port=9555 form counts too. start places
        # saved options before explicit ones: the first occurrence
        # sent the icon to the old port (audit 26/09/2026, B05).
        demande = _port_demande(arguments) if verbe == "serve" else None
        if demande is not None:
            port = demande

    import tray

    def nettoyer_lances() -> None:
        # Cooperative stop first (review 27/08): the flag lets
        # each verb finish its current round then exit on its own -
        # between repeter() rounds (watch/download/merge) or via
        # shutdown() between requests (serve) - rather than being killed
        # mid-run. arreter_processus(avec_descendance=True) is only
        # fallback, past the grace period, for what did not have
        # time to exit alone. ffmpeg (which merge does not drive
        # cooperatively, third-party process) almost always goes through this
        # path, killed with its parent via /T - .terminate() alone, before this
        # fix, only touched the immediate process, never killed
        # its children, and did not wait for real exit (bug 6).
        #
        # Called directly from the notification area icon Stop/Restart menu
        # (tray.executer, below), not only
        # from this finally: going through a detached « restart » alone
        # left live processes on Windows 7 with no clear
        # reason (icon gone, nothing behind) - direct and
        # synchronous, it no longer depends on that second process. Idempotent
        # (poll() already non-None does nothing): calling twice (here and
        # from finally) only costs a useless round trip if the
        # first already cleaned everything.
        runtime.demander_arret()
        limite = time.monotonic() + 15
        while time.monotonic() < limite and any(p.poll() is None for _, p in lances):
            time.sleep(0.2)
        for _, processus in lances:
            if processus.poll() is None:
                runtime.arreter_processus(processus.pid, avec_descendance=True)
        # taskkill is normally synchronous, but verify the result rather
        # than hoping. If a process resists, keeping the flag and record
        # lets the next stop find it instead of orphaning it.
        limite_forcee = time.monotonic() + 5
        while (time.monotonic() < limite_forcee
               and any(p.poll() is None for _, p in lances)):
            time.sleep(0.2)
        if not any(p.poll() is None for _, p in lances):
            runtime.effacer_arret_demande()

    # Entire lifetime of persistent processes is covered, one-shot steps
    # included: interruption (Ctrl+C) or launch failure during
    # one of them left persistents running, with nobody left to
    # stop them (audit 26/09/2026, B06). On POSIX, they have their own
    # session: the terminal signal does not reach them.
    pire_ponctuel = 0
    try:
        # One-shot passes, one after another, in the order they are
        # cited.
        for verbe, *arguments in ponctuels:
            print(msg("etape_verbe", commande=f"{verbe} {' '.join(arguments)}".rstrip()))
            resultat = runtime.lancer(
                runtime.self_command(verbe, *arguments), cwd=str(runtime.app_dir()),
                stdin=subprocess.DEVNULL, check=False,
            )
            pire_ponctuel = max(pire_ponctuel, abs(resultat.returncode))
        if not lances:
            return pire_ponctuel

        # Notification area icon (Open/Restart/Stop) requires
        # the main thread on macOS: verb monitoring moves
        # to a separate thread, which closes the icon if one of them
        # dies on its own (crash), to avoid leaving a dead icon.
        if tray.disponible():
            fin = threading.Event()
            veilleur = threading.Thread(target=surveiller,
                                        kwargs={"sur_fin": fin.set}, daemon=True)
            veilleur.start()
            try:
                tray.executer(port, fin, nettoyer_lances)
            except Exception:
                # Icon failed mid-run (Linux backend vanishing,
                # for example): the monitoring thread
                # continues; we just wait for it.
                pass
            veilleur.join()
        else:
            surveiller()
    except KeyboardInterrupt:
        # With no persistent process, nothing to clean here: interruption
        # propagates as before.
        if not lances:
            raise
        print(msg("interruption_clavier"))
    finally:
        # Never without children: nettoyer_lances() sets the shared stop
        # flag, which other blink2video processes would read.
        if lances:
            nettoyer_lances()
    return max(pire, pire_ponctuel)


def route(argv: list) -> int:
    """Pure dispatch from sys.argv[1:]: an ordinary function rather
    than code stuck in « if __name__ », to stay testable without a
    subprocess.

    E-01/5.1: an empty list takes exactly the same path as
    « start » (preflight included), so a fresh install no longer
    has to discover « login » or « start ». --help and --version keep their
    meaning: only a truly empty list triggers this shortcut,
    never a lone option."""
    # --bootstrap= is declared by no verb parser: removed here,
    # before any downstream parse_args(), otherwise argparse rejects it as
    # unknown argument before bootstrap() could read it (review
    # 27/08, bug 4 - reproducible with « download --bootstrap=none »).
    argv = runtime.extraire_mode_bootstrap(argv)
    if not argv:
        return executer(runtime.decouper_verbes(["start"]))
    # « autostart » must come first: it executes nothing, it
    # schedules what follows. Other verbs may appear in any
    # order, each followed by its options.
    if argv[0] == "autostart":
        return deleguer("autostart", argv[1:])
    if argv[0] in runtime.VERBES:
        return executer(runtime.decouper_verbes(argv))
    # An option before the first verb belongs to nobody:
    # « blink2video --loop 5 merge » used to read as a root command
    # looping on nothing, and ran indefinitely doing nothing.
    if argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        print(msg("option_avant_verbe", option=argv[0]))
        print(msg("liste_verbes", liste=", ".join(runtime.VERBES)))
        return 2
    sys.argv = ["blink2video", *argv]
    return asyncio.run(main(parse_args()))
