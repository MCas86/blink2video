"""Start monitoring with the login session, via the system's mechanism.

Each platform has its own, and none requires administrator rights as long
as we stick to the current session:

  Windows : a shortcut in the Startup folder. The Task Scheduler
            would also work, but its root folder needs elevation,
            whereas this shortcut never does.
  macOS   : a launch agent, loaded at login.
  Linux   : a systemd user service.

Each is undone by deleting a file, which is deliberate: a startup
mechanism you can no longer remove is a nuisance.

Installation changes your session configuration: it only happens on
explicit request, and `--dry-run` shows what would be done without doing it.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from nico579_commons import demarrage
from nico579_commons import relance as relance_commune

import runtime

LIBELLES = {
    "en": {
        "aide_desc": "Start monitoring along with the login session.",
        "aide_exemples":
            "Examples: blink2video autostart on | blink2video autostart status | "
            "blink2video autostart off --dry-run",
        "aide_etat": "on installs, off removes, status reports (default)",
        "aide_metavar_verbe": "VERB",
        "aide_quoi":
            "what to run at login, with its options. Default: « watch --loop », "
            "which monitors, alerts, fetches, assembles and serves the interface",
        "aide_dry_run": "show what would be done without changing anything",
        "verbe_inconnu": "unknown verb: {verbe}",
        "note_open_browser":
            "Note: « --open-browser » will open a browser every "
            "time you log in.",
        "en_cours_non": "Running   : no",
        "en_cours_ligne": "Running   : {commande} (PID {pid}, since {depuis})",
        "plateforme_non_prise_en_charge":
            "Autostart not supported on {plateforme}.",
        "creerait": "Would create {cible}",
        "cible_label": "  target: {executable}",
        "args_label": "  args  : {arguments}",
        "raccourci_non_cree": "shortcut not created",
        "echec_raccourci": "Failed: {detail}",
        "ecrirait": "Would write {cible}:\n{contenu}",
        "label_raccourcis_demarrage": "Startup shortcuts",
        "label_agents_lancement": "Launch agents",
        "label_services_utilisateur": "User services",
        "intitule_aucune": "{intitule}: none",
        "intitule_compte": "{intitule}: {n}",
        "lance_commande": "    runs: {commande}",
        "supprimerait": "Would delete {cible}",
        "demarrage_retire": "Autostart removed: {cible}",
        "demarrage_deja_absent": "Autostart already absent: {cible}",
        "demarrage_installe": "Autostart installed: {cible}",
        "installation_refusee":
            "Autostart failed: {commande} returned {code}. "
            "File left in place for inspection: {cible}",
        "plist_invalide":
            "Autostart failed: the generated agent is not a valid plist "
            "({erreur}). Nothing was written.",
        "commande_label": "  command: {commande}",
        "prendra_effet": "  It will take effect at the next login.",
        "session_systemd_absente":
            "  No systemd session for {utilisateur}: the service will start at "
            "their next login. To start at boot without logging in: "
            "sudo loginctl enable-linger {utilisateur}",
        "agent_migre": "Login agent updated: {cible} (takes effect at the next login)",
    },
}
LIBELLES["fr"] = dict(LIBELLES["en"])



def _(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


NOM = "blink2video"


def etiquette(quoi: tuple) -> str:
    """Entry name, derived from the verb: one per automated verb.

    Several entries coexist, that is the common need: the loop that
    monitors and fetches, and the interface that stays available. Naming them
    after their verb lets you remove one without touching the others."""
    return f"{NOM}-{(quoi or DEFAUT)[0]}"


# What we automate by default: the interface, and the full loop every
# minute. « all » itself carries both paces: cloud on every turn, for
# a tenth of a second, and the USB manifest once in ten, because it
# wakes the sync module. One loop, therefore one state, one
# assembly and one notification. Nothing requires automating
# that: « autostart on watch --loop » would only alert, without fetching anything.
# The interface does not open a browser: it waits for someone to come, or for a
# click on a notification.
DEFAUT = ("start",)


def commande(verbe_et_options: tuple = DEFAUT) -> list:
    """Line to have executed at startup, for the requested verb.

    runtime.self_command already knows how to relaunch correctly whether we run
    from sources or from a bundle: that is exactly what must be
    written into the startup mechanism.

    A substitution is still required under Windows when running from
    sources: python.exe would open a black console at every login.
    pythonw.exe runs the same thing without a window, which these
    programs can afford since they report in watch.log."""
    arguments = list(verbe_et_options or DEFAUT)
    if arguments[0] not in runtime.VERBES:
        raise ValueError(_("verbe_inconnu", verbe=arguments[0]))
    # Via the entry point, not a verb's program: only it knows how to
    # launch several verbs side by side.
    ligne = runtime.commande_composee(arguments)
    if sys.platform == "win32" and not runtime.frozen():
        sans_fenetre = Path(ligne[0]).with_name("pythonw.exe")
        if sans_fenetre.is_file():
            ligne[0] = str(sans_fenetre)
    return ligne


def appliquer_tous(etat: str, simulation: bool, quoi: tuple) -> int:
    """Schedules the cited command, as-is.

    « autostart » is a prefix: it only registers at startup what one
    would have typed without it. Since the entry point already knows how to launch several
    verbs, « autostart on serve watch --loop merge --loop 60 » places a single
    entry, which will launch all three. The entry is named after the first
    verb, which lets you keep several and remove just one."""
    if quoi and "--open-browser" in quoi:
        print(_("note_open_browser"))
    if quoi:
        # Check syntax before writing anything: a faulty startup
        # entry is only discovered at the next login,
        # when nobody is watching anymore.
        runtime.decouper_verbes(list(quoi))
    code = appliquer(etat, simulation, quoi or DEFAUT)
    if etat == "status":
        # What is installed and what is running are two things: you can have
        # an entry placed without a living instance, or the reverse after a
        # manual launch.
        instances = runtime.lire_instances()
        if not instances:
            print(_("en_cours_non"))
        for fiche in instances:
            commande = " ".join(" ".join(g) for g in fiche.get("verbes") or [])
            print(_("en_cours_ligne", commande=commande, pid=fiche['pid'],
                    depuis=fiche.get('depuis', '?')))
    return code


def appliquer(etat: str, simulation: bool = False, quoi: tuple = DEFAUT) -> int:
    """`on` installs, `off` removes, `status` reports."""
    if sys.platform == "win32":
        return _windows(etat, simulation, quoi)
    if sys.platform == "darwin":
        return _macos(etat, simulation, quoi)
    if sys.platform.startswith("linux"):
        return _linux(etat, simulation, quoi)
    print(_("plateforme_non_prise_en_charge", plateforme=sys.platform))
    return 1


def est_installe(quoi: tuple = DEFAUT) -> bool:
    """True if autostart is currently installed.

    `appliquer("status", ...)` prints and returns an exit code that never
    says whether it is active (0 in all cases, including « none »): fine
    for a CLI, unusable as-is by a programmatic caller like
    the web interface. Same condition as each `status` branch, without
    printing or modifying anything."""
    if sys.platform == "win32":
        return any(_dossier_demarrage().glob(f"{NOM}*.lnk"))
    if sys.platform == "darwin":
        return any((Path.home() / "Library/LaunchAgents").glob(f"com.nico579.{NOM}*.plist"))
    if sys.platform.startswith("linux"):
        return any((Path.home() / ".config/systemd/user").glob(f"{NOM}*.service"))
    return False


# ------------------------------------------------------------------- Windows

def _dossier_demarrage() -> Path:
    import ctypes

    tampon = ctypes.create_unicode_buffer(260)
    # CSIDL_STARTUP = 7: current user's Startup folder.
    ctypes.windll.shell32.SHGetFolderPathW(None, 7, None, 0, tampon)
    return Path(tampon.value)


def _raccourci(quoi: tuple = ()) -> Path:
    return _dossier_demarrage() / f"{etiquette(quoi)}.lnk"


def _entree(quoi: tuple = DEFAUT) -> demarrage.Entree:
    """This verb's startup entry, for nico579_commons.demarrage (the
    same code lidar2map and watch2notif use to place files): only what is
    specific to blink2video remains here — the entry name, the command and the
    working directory."""
    return demarrage.Entree(
        etiquette(quoi), tuple(commande(quoi)), runtime.app_dir(),
        "Surveillance blink2video", label_macos=_label_macos(quoi),
        anciens_labels_macos=(ANCIEN_LABEL_MACOS,))


def _entree_nommee(quoi: tuple = DEFAUT) -> demarrage.Entree:
    """For remove or list: the name is enough, the command is not evaluated (an
    unknown verb must not prevent removing an entry)."""
    return demarrage.Entree(etiquette(quoi), (), runtime.app_dir(),
                            label_macos=_label_macos(quoi))


def _message_retrait(cible: Path, existait: bool) -> int:
    print(_("demarrage_retire" if existait else "demarrage_deja_absent", cible=cible))
    return 0


def _windows(etat: str, simulation: bool, quoi: tuple = DEFAUT) -> int:
    if etat == "status":
        return _lister(demarrage.installees(NOM, plateforme="win32"),
                       _("label_raccourcis_demarrage"))
    if etat == "off":
        return _retirer(demarrage.chemin_raccourci(_entree_nommee(quoi)), simulation)

    entree = _entree(quoi)
    cible = demarrage.chemin_raccourci(entree)
    executable = entree.commande[0]
    arguments = subprocess.list2cmdline(entree.commande[1:])
    if simulation:
        print(_("creerait", cible=cible))
        print(_("cible_label", executable=executable))
        print(_("args_label", arguments=arguments))
        return 0

    # No window opens: the executable is built without a console
    # (console=False in blink2video.spec), like pythonw.exe which replaces it
    # from sources. WindowStyle 7 (minimized) is only a safeguard now.
    try:
        demarrage.activer(entree, plateforme="win32", lancer=runtime.lancer)
    except demarrage.ErreurDemarrage as erreur:
        print(_("echec_raccourci", detail=erreur.valeurs.get("detail") or _("raccourci_non_cree")))
        return 1
    return _installe(cible, quoi)


def _chaine_ps(valeur: str) -> str:
    """PowerShell literal string: only the apostrophe is doubled."""
    return "'" + valeur.replace("'", "''") + "'"


# --------------------------------------------------------------------- macOS

# Name all agents carried until 0.14: the same for each
# entry, whereas launchd identifies an agent by that name. Two entries
# therefore could not coexist, and removing one could unload the other.
# Each agent now carries its file name, per Apple's
# convention, like those of lidar2map and watch2notif.
ANCIEN_LABEL_MACOS = f"com.nico579.{NOM}"
RELANCE_MACOS = {"SuccessfulExit": False}


def _label_macos(quoi: tuple = DEFAUT) -> str:
    return f"com.nico579.{etiquette(quoi)}"


def _retirer_agent_ancien_nom() -> None:
    """Unloads the agent still loaded under the old common name, if it is.

    launchd keeps the definition read at login. Once the
    file is rewritten (see migrer_agents_macos), the loaded agent still carries
    the old name: neither « unload » of the file, nor a new « load », would
    find it, and it would relaunch a second supervisor beside the
    new one."""
    runtime.lancer(["launchctl", "remove", ANCIEN_LABEL_MACOS], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def migrer_agents_macos(dossier=None) -> list:
    """Rewrites on disk agents placed by an earlier version.

    Their name and relaunch policy changed (issue #31): without this
    rewrite, an already-installed agent would keep its defaults until
    reinstalled. launchd only rereads the file at login;
    the fix therefore takes effect at the next one, with no action. Same
    principle as the watch2notif update. Returns the rewritten files.
    An unreadable file stays as-is: never at the cost of a startup."""
    import plistlib

    if dossier is None:
        dossier = Path.home() / "Library/LaunchAgents"
    reecrits = []
    for fichier in sorted(Path(dossier).glob(f"com.nico579.{NOM}-*.plist")):
        try:
            agent = plistlib.loads(fichier.read_bytes())
        except Exception:
            continue
        attendu = {"Label": fichier.stem, "KeepAlive": dict(RELANCE_MACOS)}
        if all(agent.get(cle) == valeur for cle, valeur in attendu.items()):
            continue
        agent.update(attendu)
        temporaire = fichier.with_name(fichier.name + ".tmp")
        try:
            temporaire.write_bytes(plistlib.dumps(agent, sort_keys=False))
            os.replace(temporaire, fichier)
        except OSError:
            temporaire.unlink(missing_ok=True)
            continue
        print(_("agent_migre", cible=fichier))
        reecrits.append(fichier)
    return reecrits


def _macos(etat: str, simulation: bool, quoi: tuple = DEFAUT) -> int:
    if etat == "status":
        return _lister(demarrage.installees(NOM, plateforme="darwin",
                                            prefixe_label=f"com.nico579.{NOM}"),
                       _("label_agents_lancement"))
    entree = _entree_nommee(quoi) if etat == "off" else _entree(quoi)
    cible = demarrage.chemin_agent(entree)
    if etat == "off":
        if simulation:
            print(_("supprimerait", cible=cible))
            return 0
        existait = cible.exists()
        # Even without a file: an agent loaded under the old common name.
        _retirer_agent_ancien_nom()
        demarrage.desactiver(entree, plateforme="darwin", lancer=runtime.lancer)
        return _message_retrait(cible, existait)

    # Values escaped by plistlib: a folder like « Blink & Videos »
    # wrote a raw &, and launchd refused the plist (audit of 26/09/2026,
    # B08). Reread before writing: never announce an agent installed that
    # launchd would refuse at the next login.
    import plistlib
    try:
        contenu = demarrage.contenu_agent(entree)
        plistlib.loads(contenu.encode("utf-8"))
    except Exception as erreur:
        print(_("plist_invalide", erreur=erreur))
        return 1
    if simulation:
        print(_("ecrirait", cible=cible, contenu=contenu))
        return 0
    demarrage.activer(entree, plateforme="darwin", lancer=runtime.lancer)
    return _installe(cible, quoi)


# --------------------------------------------------------------------- Linux

# These two come from the common package (nico579_commons.demarrage), born here:
# the « systemctl --user » environment (issue #23) and an ExecStart=
# argument in quotes, with % and $ doubled (audit of 26/09/2026, B07).
env_systemctl = demarrage.env_systemctl
argument_systemd = demarrage.argument_systemd


# Under the unit placed by « autostart on », every process launched by blink2video
# stays in the unit's cgroup, whatever its parent: start_new_session
# changes session, not cgroup. When « stop » exits the main
# process, systemd closes the unit and kills what remains in that cgroup, including
# what was meant to replace update files or relaunch
# blink2video after Apply; and since exit is with code 0,
# Restart=on-failure relaunches nothing (issue #35, Linux counterpart of issue #31).
UNITE_ENV = "BLINK_UNITE_SYSTEMD"


def unite_systemd(cgroup: Path = Path("/proc/self/cgroup")) -> str:
    """blink2video unit (« blink2video-start.service ») this process
    belongs to, or "": not Linux, not in such a unit, or
    unreadable cgroup. cgroup reading is that of nico579-commons, where this
    function was born (issue #35); only the application name remains here."""
    return relance_commune.unite_systemd(NOM, cgroup)


def sortir_du_service(commande: list, environ=None) -> bool:
    """Relaunches ``commande`` outside the blink2video unit this process
    belongs to, and returns True: the caller then stops there, the rest
    runs in the relaunched process. Returns False outside such a unit,
    once already left, or if systemd-run does not respond: the caller
    then continues as before.

    systemd-run --user --scope places the command in a transient « scope »
    unit, its own cgroup, which the end of the service does not reach. An
    empty first try verifies that leaving really works: without
    it, a systemd-run that failed after our departure would leave
    nobody to finish the work. Both gestures, the try and the prefix,
    are those of nico579_commons.relance.hors_du_service; only what is
    specific to blink2video remains here: the left unit's name travels in
    BLINK_UNITE_SYSTEMD, for relancer_service()."""
    env = dict(os.environ if environ is None else environ)
    unite = unite_systemd()
    if not unite or env.get(UNITE_ENV):
        return False
    env = env_systemctl(env)
    a_lancer = relance_commune.hors_du_service(
        commande, nom=NOM, unite=unite,
        lancer=lambda cmd, **options: runtime.lancer(cmd, env=env, **options))
    if a_lancer == list(commande):
        return False
    env[UNITE_ENV] = unite
    runtime.demarrer(a_lancer, env=env, stdin=subprocess.DEVNULL)
    return True


def relancer_service(environ=None) -> str:
    """Relaunches via systemd the unit left by sortir_du_service(): start
    thus resumes under its service's guard, rather than detached in the
    transient scope, and the next login finds the unit
    in the expected state. Returns the unit name if systemctl accepted it,
    "" otherwise: the caller then relaunches as before."""
    env = dict(os.environ if environ is None else environ)
    unite = env.pop(UNITE_ENV, "")
    if not unite:
        return ""
    try:
        resultat = runtime.lancer(["systemctl", "--user", "start", unite],
                                  check=False, env=env_systemctl(env),
                                  stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return unite if resultat.returncode == 0 else ""


def _linux(etat: str, simulation: bool, quoi: tuple = DEFAUT) -> int:
    if etat == "status":
        return _lister(demarrage.installees(NOM, plateforme="linux"),
                       _("label_services_utilisateur"))
    entree = _entree_nommee(quoi) if etat == "off" else _entree(quoi)
    cible = demarrage.chemin_unite(entree)
    env = env_systemctl() if not simulation else None
    if etat == "off":
        if simulation:
            print(_("supprimerait", cible=cible))
            return 0
        existait = cible.exists()
        demarrage.desactiver(entree, plateforme="linux", lancer=runtime.lancer, env=env)
        return _message_retrait(cible, existait)

    if simulation:
        print(_("ecrirait", cible=cible, contenu=demarrage.contenu_unite(entree)))
        return 0
    try:
        notes = demarrage.activer(entree, plateforme="linux", lancer=runtime.lancer, env=env)
    except demarrage.ErreurDemarrage as erreur:
        # With a systemd session, a refusal is a real failure: no longer
        # announce it as a successful install (audit of 26/09/2026, B07).
        v = erreur.valeurs
        print(_("installation_refusee", commande=v["commande"], code=v["retour"],
                cible=v["cible"]))
        return 1
    code = _installe(cible, quoi)
    for cle, valeurs in notes:
        if cle == "session_systemd_absente":
            print(_("session_systemd_absente", utilisateur=valeurs["utilisateur"]))
    return code


# ------------------------------------------------------------------- shared

def lue(cible: Path) -> list:
    """Command actually written into the installed mechanism.

    We reread it rather than recalculate: what matters to the user
    is what will run, not what we would install today."""
    try:
        if cible.suffix == ".lnk":
            # A shortcut only exists under Windows, and so does powershell:
            # the guard is explicit rather than inferred from the extension.
            if sys.platform != "win32":
                return []
            import subprocess as sp
            script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
                      + _chaine_ps(str(cible)) + "); "
                      "Write-Output $s.TargetPath; Write-Output $s.Arguments")
            sortie = sp.run(["powershell", "-NoProfile", "-NonInteractive",
                             "-Command", script], stdout=sp.PIPE, text=True,
                            errors="replace", check=False).stdout
            return [l.strip() for l in (sortie or "").splitlines() if l.strip()]
        texte = cible.read_text(encoding="utf-8")
        if cible.suffix == ".service":
            for ligne in texte.splitlines():
                if ligne.startswith("ExecStart="):
                    return ligne.split("=", 1)[1].split()
        if cible.suffix == ".plist":
            import re
            bloc = re.search(r"<array>(.*?)</array>", texte, re.S)
            if bloc:
                return re.findall(r"<string>(.*?)</string>", bloc.group(1))
    except Exception:
        pass
    return []


def _lister(entrees: list, intitule: str) -> int:
    """All installed entries, with what each actually launches."""
    if not entrees:
        print(_("intitule_aucune", intitule=intitule))
        return 0
    print(_("intitule_compte", intitule=intitule, n=len(entrees)))
    for cible in entrees:
        print(f"  {cible.name}")
        commande_lue = lue(cible)
        if commande_lue:
            print(_("lance_commande", commande=" ".join(commande_lue)))
    return 0


def _retirer(cible: Path, simulation: bool) -> int:
    if simulation:
        print(_("supprimerait", cible=cible))
        return 0
    existait = cible.exists()
    cible.unlink(missing_ok=True)
    print(_("demarrage_retire" if existait else "demarrage_deja_absent", cible=cible))
    return 0


def _installe(cible: Path, quoi: tuple = DEFAUT) -> int:
    print(_("demarrage_installe", cible=cible))
    print(_("commande_label", commande=" ".join(commande(quoi))))
    print(_("prendra_effet"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="blink2video autostart",
        description=_("aide_desc"),
        epilog=_("aide_exemples"),
    )
    parser.add_argument("etat", choices=("on", "off", "status"), nargs="?",
                        default="status", help=_("aide_etat"))
    # nargs="*" plus parse_known_args: options unknown here, like
    # « --port 8899 », join the verb, while --dry-run stays understood
    # wherever it is placed. REMAINDER swallowed --dry-run with the rest, and a
    # simulation installed for real.
    parser.add_argument("quoi", nargs="*", metavar=_("aide_metavar_verbe"),
                        help=_("aide_quoi"))
    parser.add_argument("--dry-run", action="store_true", help=_("aide_dry_run"))
    args, restant = parser.parse_known_args()
    quoi = tuple(args.quoi) + tuple(restant)
    try:
        return appliquer_tous(args.etat, args.dry_run, quoi)
    except ValueError as erreur:
        # An unknown verb deserves a message, not a stack trace.
        parser.error(str(erreur))


if __name__ == "__main__":
    raise SystemExit(main())
