"""Desktop shortcut: open or launch blink2video with a double-click.

Installs nothing permanent, unlike autostart.py: a single file
placed on the Desktop, which the user removes themselves (drag to the
trash) when they no longer want it. No « off » counterpart.

Placing the file (each system's Desktop, .lnk, .app, .desktop, GNOME
trust mark) is in nico579_commons.raccourci, the same for the
four applications. Only what is specific to blink2video remains here: the
command, the working directory and the icon.

The command placed in the shortcut is simply « start »: blink_cli
(see the « start » branch of executer()) already behaves like « open »
when an instance is already listening on the configured port, without relaunching anything.
The same shortcut therefore serves both to start and to reopen the interface
in place. « --open-browser » is added here: absent from the standard
composition (it never opens a browser on its own, see DEFAUT in
autostart.py), it must be added explicitly for this shortcut, which only
makes sense if the interface ends up being displayed.
"""

import subprocess
import sys
from pathlib import Path

from nico579_commons import raccourci

import autostart
import runtime


def _icone() -> Path:
    """Windows takes the .ico, macOS ignores the icon. Linux desktops
    display a PNG: at the root of the bundle (blink2video.spec places it there), or
    in assets/ from sources."""
    racine = runtime.resource_dir()
    ico = racine / "assets" / "blink2video.ico"
    if not sys.platform.startswith("linux"):
        return ico
    for candidat in (racine / "blink2video.png", racine / "assets" / "blink2video.png"):
        if candidat.is_file():
            return candidat
    return ico


def _lancer(commande):
    return runtime.lancer(
        list(commande), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, errors="replace", check=False)


def creer(simulation: bool = False) -> int:
    """Places the shortcut on the Desktop; 0 if done (or simulated)."""
    langue = runtime.lire_langue()
    return raccourci.creer(
        "blink2video",
        # autostart.commande() does exactly what is needed: self_command, then
        # substituting pythonw.exe for python.exe from sources (the only
        # way to get a launch with no console at all under Windows).
        autostart.commande(("start", "--open-browser")),
        runtime.app_dir(),
        _icone(),
        "Ouvrir blink2video" if langue == "fr" else "Open blink2video",
        # Console-mode executable (console=True in the .spec): it cannot
        # launch without a window, only minimized from the start under Windows,
        # or in a terminal under Linux.
        terminal=True, reduit=True, simulation=simulation, langue=langue,
        lancer=_lancer,
    )
