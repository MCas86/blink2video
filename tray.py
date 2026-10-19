"""System tray icon for the « start » instance: open, update, restart,
stop and create a shortcut without going through the terminal or the web page.

The menu shared by the four applications (blink2video, lidar2map, watch2notif,
gpxsolar), its refresh on the main thread under macOS (issue #31)
and the thread that carries Restart/Stop/Update through to completion
(race of 2026-09-03: « Restart » stopped without relaunching) come from
nico579_commons.tray, born from this file. Only the actions specific to
blink2video remain here.

pystray (and thus nico579_commons.tray) chooses win32 under Windows, AppKit
under macOS, AppIndicator/GTK under Linux. Without a graphical server (SSH,
headless machine, container) or without these packages (running from
sources, or they are not required), we continue without an icon, never as a
blocking error: degrade rather than crash, like resource_dir() or
app_dir().

Restart/Stop go through nettoyer(), provided by blink_cli.py, which
stops the launched verbs directly; Restart then relaunches via
« blink2video restart », the same mechanism as the settings page button
(serve.py, /api/redemarrer).

« Update », when a newer version exists, goes through
« blink2video update », the same mechanism as the update button on the page
(serve.py, /api/update). `maj.disponible(reseau=False)` only reads the cache
already maintained by serve.py's background thread: opening the menu
never queries GitHub itself."""

import os
import subprocess
import threading
import webbrowser

import maj
import raccourci_bureau
import runtime


def disponible() -> bool:
    """False if nico579_commons, pystray or Pillow fail to load here:
    missing packages (sources), or no system-tray backend
    (Linux without AppIndicator/GTK, session without display)."""
    try:
        from nico579_commons import tray as commun
    except Exception:
        return False
    return commun.disponible()


def _relancer(sans_relance: bool) -> None:
    arguments = ("restart", "--sans-relance") if sans_relance else ("restart",)
    runtime.demarrer(
        runtime.self_command(*arguments), cwd=str(runtime.app_dir()),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))


def _mettre_a_jour() -> None:
    # Detached: this process is part of what the update will stop.
    runtime.demarrer(
        runtime.self_command("update"), cwd=str(runtime.app_dir()),
        stdin=subprocess.DEVNULL,
        stdout=(runtime.app_dir() / "maj.log").open("ab"),
        stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))


def _version_disponible():
    return (maj.disponible(reseau=False) or {}).get("version")


def executer(port: int, arret: threading.Event, nettoyer) -> None:
    """Blocks on the icon loop; main thread required under macOS.

    `arret`: set by the caller when a watched verb dies on its
    own (crash); the icon then closes so control returns to the
    usual cleanup.

    `nettoyer`: stops directly, in this same process, the verbs that
    the caller launched (see nettoyer_lances(), blink_cli.py). Restart
    and Stop call it synchronously rather than relying on a
    detached « blink2video restart »: observed in the wild on Windows 7, the icon
    could disappear without anything stopping behind it (bug 6, review of
    27/08). executer() only returns once that work is done (see
    nico579_commons.tray.Tray.executer)."""
    from nico579_commons import tray as commun

    adresse = f"http://127.0.0.1:{port}/"

    def redemarrer():
        nettoyer()
        _relancer(sans_relance=False)

    actions = commun.Actions(
        ouvrir=lambda: webbrowser.open(adresse),
        redemarrer=redemarrer,
        arreter=nettoyer,
        version_disponible=_version_disponible,
        mettre_a_jour=_mettre_a_jour,
        creer_raccourci=raccourci_bureau.creer,
        langue=runtime.lire_langue,
    )
    icone = runtime.resource_dir() / "assets" / "blink2video.ico"
    commun.Tray("blink2video", icone, actions, arret=arret).executer()
