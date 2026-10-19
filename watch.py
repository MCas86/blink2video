"""Monitor the Blink installation and report degradations locally.

The need came from observation: a camera can stop recording without
anything flagging it. The Portal had been offline for sixteen days, discovered
by chance. A monitoring system that stops silently is worse than no
monitoring, since you still rely on it.

This script compares current state to the previous pass and alerts only
on degradations: a camera going offline, a battery no longer ok,
detection disabled, abnormally long silence. Returns to
normal are reported too, but lightly, so you know an
incident is closed.

In continuous mode (--loop), the shared loop repeats this check. Starting
the UI, download, and merge remain the responsibility
of other verbs, brought together by the start command.

Without --loop, it runs one check and exits, suitable for
periodic launch by a scheduler.
"""

import argparse
import asyncio
import datetime as dt
import sys
from collections import Counter

# Before any dependency import: an isolated environment
# is prepared here and the program re-launched inside it if needed.
import runtime

runtime.bootstrap()

import blink_auth
import merge_daily as md


BASE_DIR = runtime.app_dir()
WATCH_STATE = BASE_DIR / ".blink_watch_state.json"
# The download registry lives with the clips, in the output folder.
CLIPS = md.DEFAULT_INPUT

# Beyond this silence, a camera that was recording is considered failed.
# Two days rather than one: a garden can stay quiet twenty-four hours.
SILENCE_DAYS = 2


def _identite_camera(etat: dict):
    """Comparison identity, independent of Blink name and order."""
    reseau = str(etat.get("network_id") or "")
    appareil = str(etat.get("device_id") or "")
    return ("device", reseau, appareil) if appareil else (
        "name", reseau, str(etat.get("name") or "").casefold())


def _libelles_cameras(etats: list) -> dict:
    """Change visible names only when they are actually ambiguous."""
    noms = Counter(etat["name"].casefold() for etat in etats)
    reserves = set(noms)
    cameras = {}
    for etat in sorted(etats, key=lambda e: (e["name"].casefold(), _identite_camera(e))):
        nom = etat["name"]
        if noms[nom.casefold()] > 1:
            suffixe = ", ".join(valeur for valeur in (
                "network " + etat["network_id"] if etat.get("network_id") else "",
                "device " + etat["device_id"] if etat.get("device_id") else "",
            ) if valeur) or "no identity"
            nom += " [" + suffixe + "]"
            while nom.casefold() in reserves:
                nom += " (2)"
        reserves.add(nom.casefold())
        cameras[nom] = etat
    return cameras


def _cameras_correspondantes(entree: dict, cameras: dict) -> list:
    """An old clip without ids never refers to two homonyms."""
    nom = str(entree.get("camera") or "camera").strip().casefold()
    reseau = str(entree.get("network_id") or "")
    appareil = str(entree.get("device_id") or "")
    candidats = []
    for libelle, etat in cameras.items():
        r = str(etat.get("network_id") or "")
        a = str(etat.get("device_id") or "")
        if (reseau and r and reseau != r) or (appareil and a and appareil != a):
            continue
        # IDs on both sides allow a rename; otherwise the name
        # is still required, especially for old USB clips without ID.
        if not (appareil and a and reseau and r):
            if nom != str(etat.get("name") or libelle).strip().casefold():
                continue
        candidats.append(libelle)
    return candidats


def regrouper_entrees_cameras(cameras: dict, entrees: dict) -> dict:
    """Assign each entry at most once, without resolving ambiguities.

    Grouping is local to the call: no cache must survive a change
    of cameras or registry, especially for delete permissions.
    """
    groupes = {nom: {} for nom in cameras}
    for cle, entree in entrees.items():
        if isinstance(entree, dict):
            correspondantes = _cameras_correspondantes(entree, cameras)
            if len(correspondantes) == 1:
                groupes[correspondantes[0]][cle] = entree
    return groupes


def camera_entries(libelle: str, cameras: dict, entrees: dict) -> dict:
    """Entries assignable unambiguously to a displayed camera."""
    return regrouper_entrees_cameras(cameras, entrees).get(libelle, {})


def normaliser_sourdines(ignores, cameras: dict, precedentes=None) -> set:
    """Migrate old names and track a device whose label changes."""
    resultat = set()
    for nom in ignores:
        ancien = (precedentes or {}).get(nom) or {}
        if ancien.get("name"):
            correspondants = [cle for cle, etat in cameras.items()
                              if _identite_camera(etat) == _identite_camera(ancien)]
        else:
            correspondants = [cle for cle, etat in cameras.items()
                              if cle == nom or etat.get("name") == nom]
        resultat.update(correspondants or [nom])
    return resultat


def _photos_cameras(blink, home: dict) -> dict:
    bruts = [item for groupe in ("cameras", "owls", "doorbells")
             for item in home.get(groupe) or [] if isinstance(item, dict)]
    objets = []
    for sync in blink.sync.values():
        for nom, camera in sync.cameras.items():
            attrs = camera.attributes or {}
            appareil = (getattr(camera, "device_id", None)
                        or getattr(camera, "camera_id", None)
                        or attrs.get("device_id") or attrs.get("camera_id") or attrs.get("id"))
            objets.append((sync, camera, {
                "name": nom.strip(),
                "network_id": str(getattr(camera, "network_id", None)
                                  or getattr(sync, "network_id", None) or ""),
                "device_id": str(appareil or ""),
            }))
    photos, utilises = [], set()
    for info in bruts:
        meta = {"name": str(info.get("name") or "camera").strip(),
                "network_id": str(info.get("network_id") or info.get("network") or ""),
                "device_id": str(info.get("id") or info.get("device_id")
                                 or info.get("camera_id") or "")}
        candidats = _cameras_correspondantes(
            {**meta, "camera": meta["name"]},
            {str(i): objet[2] for i, objet in enumerate(objets)})
        # Without ID on the blinkpy object, two devices with same name/network
        # stay distinct via homescreen, without borrowing their readings.
        if len(candidats) == 1:
            index = int(candidats[0])
            sync, camera, objet_meta = objets[index]
            homonymes = [brut for brut in bruts
                         if str(brut.get("name") or "").strip() == meta["name"]
                         and str(brut.get("network_id") or brut.get("network") or "")
                         == meta["network_id"]]
            fiable = bool(objet_meta["device_id"]) or len(homonymes) == 1
            utilises.add(index)
        else:
            sync = next((s for s in blink.sync.values()
                         if str(getattr(s, "network_id", "")) == meta["network_id"]), None)
            camera, fiable = None, False
        enabled = info.get("enabled")
        if enabled is None and fiable:
            enabled = camera.motion_enabled
        photos.append({**meta,
                       "online": str(info.get("status") or "") != "offline",
                       "armed": bool(enabled),
                       "battery": info.get("battery", camera.attributes.get("battery")
                                           if fiable else None),
                       "system_armed": bool(getattr(sync, "arm", False))})
    for index, (sync, camera, meta) in enumerate(objets):
        if index not in utilises:
            photos.append({**meta, "online": True,
                           "armed": bool(camera.motion_enabled),
                           "battery": camera.attributes.get("battery"),
                           "system_armed": bool(sync.arm)})
    return _libelles_cameras(photos)




async def read_state(timezone) -> dict:
    """Snapshot the installation: cameras, module, last known clip."""
    async with blink_auth.session_http_temporaire() as session:
        blink = await blink_auth.connect_saved(session)
        if blink is None:
            raise RuntimeError(
                "Blink session missing or expired; run « python blink2video.py login » again"
            )
        await blink.refresh(force=True)

        home = blink.homescreen or {}
        modules = [
            {"name": str(m.get("name") or "").strip(),
             "id": str(m.get("id") or ""),
             "network_id": str(m.get("network_id") or ""),
             "online": str(m.get("status") or "") != "offline"}
            for m in (home.get("sync_modules") or [])
        ]

        cameras = _photos_cameras(blink, home)

    return {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "modules": modules,
        "cameras": cameras,
        "last_clip": last_clip_per_camera(timezone, cameras),
    }


def last_clip_per_camera(timezone, cameras=None) -> dict:
    """Date of last acquired clip per camera, from the local registry.

    We read the download registry rather than querying Blink: that is
    free, and what matters is clips actually arriving locally.

    An excluded clip (code review 0eab463, bug #11) still counts
    for this date: excluded means the user does not want it
    in assembled videos, not that the camera detected nothing at
    that instant - ignoring it here regressed last known activity
    to an older clip, or removed it entirely
    if all recent clips were excluded, risking a false
    silence alert."""
    state = md.load_json(CLIPS / md.DOWNLOAD_STATE, {})
    latest: dict = {}
    for entry in (state.get("clips") or {}).values():
        if not isinstance(entry, dict):
            continue
        try:
            created = md.parse_created_at(entry["created_at"]).astimezone(timezone)
        except (KeyError, TypeError, ValueError):
            continue
        camera = str(entry.get("camera") or "camera").strip()
        if cameras is not None:
            correspondantes = _cameras_correspondantes(entry, cameras)
            if len(correspondantes) != 1:
                continue
            camera = correspondantes[0]
        if camera not in latest or created > latest[camera]:
            latest[camera] = created
    return {name: moment.isoformat() for name, moment in latest.items()}


# Follows page language (runtime.lire_langue(), see tray.py): these
# messages end up in a notification or Windows dialog,
# visible even with the page closed, so in the user's chosen language,
# not the system locale.
MESSAGES = {
    "en": {
        "aide_desc": "Monitors the Blink installation and reports anomalies.",
        "aide_dry_run": "show alerts without saving the observed state",
        "aide_test": "send a test notification and exit",
        "aide_port": "kept for compatibility; watch no longer starts the interface",
        "aide_ignore": "mute cameras: no more alerts about them",
        "aide_unignore": "unmute",
        "aide_ignore_module":
            "mute Sync Modules (id, network or name): no more alerts about them",
        "aide_unignore_module": "unmute a Sync Module",
        "module_hors_ligne": 'Module "{nom}" offline.',
        "module_retour": 'Module "{nom}" back online.',
        "camera_hors_ligne": 'Camera "{nom}" offline.',
        "camera_retour": 'Camera "{nom}" back online.',
        "camera_batterie": 'Camera "{nom}": battery "{etat}".',
        "camera_detection_coupee": 'Camera "{nom}": detection disabled.',
        "camera_detection_reactivee": 'Camera "{nom}": detection re-enabled.',
        "systeme_desarme": "System fully disarmed.",
        "camera_silence": 'Camera "{nom}": no clip for {jours} day(s).',
        "camera_jamais_enregistree":
            'Camera "{nom}": no clip recorded in {jours} day(s) of monitoring.',
        "titre_echec": "Blink: monitoring failed",
        "titre_anomalies": "Blink: {n} issue(s)",
        "titre_retour": "Blink: back to normal",
        "hint_sourdine": "To stop being notified about a camera:",
        "hint_sourdine_module": "To stop being notified about a module:",
        "format_moment": "%d/%m/%Y at %H:%M",
        "alerte_ligne": "ALERT    {ligne}",
        "retabli_ligne": "fixed    {ligne}",
        "rien_a_signaler": "Nothing to report ({moment}).",
        "cameras_en_sourdine": "Muted cameras:",
        "modules_en_sourdine": "Muted modules:",
        "module_inconnu": 'Unknown module: "{ref}". Known modules: {connus}.',
        "module_ambigu":
            '"{ref}" matches several modules ({candidats}): use the id.',
        "aucune": "none",
        "titre_test_alerte": "Blink: alert test",
        "corps_test_alerte": "This is a test. Monitoring can reach you.",
    },
}
MESSAGES["fr"] = dict(MESSAGES["en"])



def _msg(cle: str, **kw) -> str:
    return runtime.traduire(MESSAGES, cle, **kw)


def _etat_camera_precedent(nom: str, etat: dict, avant: dict, cameras: dict) -> dict:
    """Track device identity; name alone is not enough for homonyms."""
    if not etat.get("name"):
        return avant.get(nom) or {}

    ancien = next((e for e in avant.values() if e.get("name")
                   and _identite_camera(e) == _identite_camera(etat)), {})
    # Old saves indexed by name remain usable if that
    # name is unique in the full inventory, muted cameras included.
    if not ancien and sum(e.get("name") == etat["name"] for e in cameras.values()) == 1:
        candidat = avant.get(etat["name"]) or {}
        if not candidat.get("name"):
            ancien = candidat
    return ancien


def _comparer_camera(nom: str, etat: dict, ancien: dict) -> tuple:
    """List connectivity, battery, and detection transitions, in that order."""
    alertes, retours = [], []
    # The first observation of an anomaly also counts as a transition.
    if not etat["online"] and (not ancien or ancien.get("online")):
        alertes.append(_msg("camera_hors_ligne", nom=nom))
    elif etat["online"] and ancien and not ancien.get("online"):
        retours.append(_msg("camera_retour", nom=nom))

    if etat["battery"] and etat["battery"] != "ok" and (
            not ancien or ancien.get("battery") == "ok"):
        alertes.append(_msg("camera_batterie", nom=nom, etat=etat["battery"]))

    if not etat["armed"] and (not ancien or ancien.get("armed")):
        alertes.append(_msg("camera_detection_coupee", nom=nom))
    elif etat["armed"] and ancien and not ancien.get("armed"):
        retours.append(_msg("camera_detection_reactivee", nom=nom))
    return alertes, retours


def _alertes_silence(previous: dict, current: dict, cameras: dict, timezone) -> list:
    """Report crossing the silence threshold, only when online and armed."""
    alertes = []
    now = dt.datetime.now(timezone)
    for name, iso in (current.get("last_clip") or {}).items():
        etat = cameras.get(name) or {}
        if not etat.get("online") or not etat.get("armed"):
            continue
        try:
            jours = (now - dt.datetime.fromisoformat(iso)).days
        except ValueError:
            continue
        # Without a date from the previous pass, a camera already silent must
        # alert on first observation.
        deja = 0
        previous_at = previous.get("at")
        if previous_at:
            try:
                deja = (dt.datetime.fromisoformat(previous_at)
                        - dt.datetime.fromisoformat(iso)).days
            except ValueError:
                pass
        if jours >= SILENCE_DAYS > deja:
            alertes.append(_msg("camera_silence", nom=name, jours=jours))
    return alertes


def suivre_premiers_releves(previous: dict, current: dict) -> dict:
    """First observation date for each camera with no clip yet.

    `last_clip` only contains cameras that already recorded: a camera
    that never recorded never appeared there, and the silence
    check did not see it, however long. This anchor
    (since when have we watched it with no clip?) was missing. A camera that gets
    a clip, or leaves the installation, drops out of tracking on its own."""
    connues = previous.get("first_seen") or {}
    avec_clip = current.get("last_clip") or {}
    maintenant = current.get("at") or dt.datetime.now(dt.timezone.utc).isoformat()
    return {nom: connues.get(nom) or maintenant
            for nom in (current.get("cameras") or {}) if nom not in avec_clip}


def _alertes_jamais_enregistre(previous: dict, current: dict, cameras: dict,
                               timezone) -> list:
    """Same threshold and conditions as _alertes_silence: online, armed,
    and a single alert on crossing."""
    alertes = []
    now = dt.datetime.now(timezone)
    for nom, iso in sorted((current.get("first_seen") or {}).items()):
        etat = cameras.get(nom) or {}
        if not etat.get("online") or not etat.get("armed"):
            continue
        try:
            depuis = dt.datetime.fromisoformat(iso)
            jours = (now - depuis).days
            deja = 0
            if previous.get("at"):
                deja = (dt.datetime.fromisoformat(previous["at"]) - depuis).days
        except (ValueError, TypeError):
            continue
        if jours >= SILENCE_DAYS > deja:
            alertes.append(_msg("camera_jamais_enregistree", nom=nom, jours=jours))
    return alertes


def cle_module(module: dict) -> str:
    """What identifies a module in the mute list: its Blink id,
    else its name (module without id, older state)."""
    return module.get("id") or module["name"]


def resoudre_modules(references, modules: list):
    """(keys, unknown, ambiguous): resolve modules by id, network,
    name, or label name (network). A name shared by two modules is
    ambiguous: it would mute both (default names are identical)."""
    cles, inconnus, ambigus = set(), [], []
    for ref in references:
        trouves = [m for m in modules if ref in (
            m.get("id"), m.get("network_id"), m["name"], _libelle_module(m, modules))
            and ref]
        if not trouves:
            inconnus.append(ref)
        elif len({cle_module(m) for m in trouves}) > 1:
            ambigus.append((ref, trouves))
        else:
            cles.add(cle_module(trouves[0]))
    return cles, inconnus, ambigus


def _module_precedent(module: dict, anciens: list):
    """Previous entry for the same module, or None if we cannot assert it.

    By Blink id when both sides have one: two Sync Modules
    can share the same name (default My Blink Sync Module). Matched
    by name, the offline module was compared to the online one with
    the same name, and its drop was reported every pass (ten-minute
    window). Without id (older state, or module without id), name only
    works if it designates a single module."""
    cle = module.get("id")
    if cle:
        for ancien in anciens:
            if ancien.get("id") == cle:
                return ancien
    memes = [a for a in anciens
             if a["name"] == module["name"] and not (cle and a.get("id"))]
    return memes[0] if len(memes) == 1 else None


def _libelle_module(module: dict, modules: list) -> str:
    """The name, followed by network when two modules share the same name."""
    homonymes = [m for m in modules if m["name"] == module["name"]]
    if len(homonymes) > 1 and module.get("network_id"):
        return f"{module['name']} ({module['network_id']})"
    return module["name"]


def compare(previous: dict, current: dict, timezone, ignores: set) -> tuple:
    """Compare two observations without repeating already reported anomalies.

    Message order: modules, cameras, system, then prolonged silence.
    Muted cameras produce neither alert nor recovery.
    """
    alerts, recoveries = [], []
    avant = previous.get("cameras") or {}
    cameras = current.get("cameras") or {}
    ignores = normaliser_sourdines(ignores, cameras, avant)
    maintenant = {nom: etat for nom, etat in cameras.items() if nom not in ignores}

    modules = current.get("modules") or []
    muets = set(previous.get("ignored_modules") or [])
    for module in modules:
        if cle_module(module) in muets or module["name"] in muets:
            # Like a muted camera: neither alert nor recovery.
            continue
        etait = _module_precedent(module, previous.get("modules") or [])
        nom = _libelle_module(module, modules)
        if not module["online"] and (etait is None or etait.get("online")):
            alerts.append(_msg("module_hors_ligne", nom=nom))
        elif module["online"] and etait is not None and not etait.get("online"):
            recoveries.append(_msg("module_retour", nom=nom))

    for name, etat in sorted(maintenant.items()):
        ancien = _etat_camera_precedent(name, etat, avant, cameras)
        alertes_camera, retours_camera = _comparer_camera(name, etat, ancien)
        alerts.extend(alertes_camera)
        recoveries.extend(retours_camera)

    if maintenant and not any(e["system_armed"] for e in maintenant.values()):
        if not avant or any(e.get("system_armed") for e in avant.values()):
            alerts.append(_msg("systeme_desarme"))

    alerts.extend(_alertes_silence(previous, current, maintenant, timezone))
    alerts.extend(_alertes_jamais_enregistre(previous, current, maintenant, timezone))
    return alerts, recoveries


toast = runtime.toast


def popup(title: str, body: str) -> None:
    """Show a Windows dialog box with no extra dependencies.

    This is the right notification here: detection runs on the user's
    session, so they are at the screen. Email would require an
    SMTP server, app password, and setup, to reach someone already there.

    ctypes rather than a notification library: MessageBoxW has been
    part of Windows forever, nothing to install or maintain. The
    window is brought to the foreground, otherwise it would hide behind
    others and the alert would be missed."""
    if sys.platform != "win32":
        print(f"\n{title}\n{body}")
        return
    import ctypes

    ICONE_AVERTISSEMENT, PREMIER_PLAN, AU_DESSUS = 0x30, 0x10000, 0x40000
    ctypes.windll.user32.MessageBoxW(
        None, body, title, ICONE_AVERTISSEMENT | PREMIER_PLAN | AU_DESSUS
    )


def journal(ligne: str) -> None:
    """Timestamped log line: launched by scheduler, the script has no console."""
    moment = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with (BASE_DIR / "watch.log").open("a", encoding="utf-8") as fichier:
            fichier.write(f"{moment}  {ligne}\n")
    except OSError:
        pass




def un_tour(args, timezone) -> None:
    """One check: read installation state, compare, alert.

    Nothing else. Observing is this verb's job; fetching and assembling
    belong to download and merge, which run alongside on their
    own schedule."""
    _controler(args, timezone)
    runtime.marquer("watch")

def _controler(args, timezone) -> None:
    try:
        current = asyncio.run(read_state(timezone))
    except Exception as error:
        message = f"Could not query Blink: {error}"
        journal(message)
        popup(_msg("titre_echec"), message)
        return

    # Same lock as --ignore/--unignore (main()): without it, a background pass
    # that re-reads then re-writes while an --ignore command does the
    # same silently loses one of the two updates (last
    # writer wins).
    with runtime.verrou("watch", "controle", stale_after=60, attente=10):
        previous = md.load_json(WATCH_STATE, {})
        ignores = normaliser_sourdines(previous.get("ignored") or [],
                                      current.get("cameras") or {},
                                      previous.get("cameras") or {})
        current["first_seen"] = suivre_premiers_releves(previous, current)
        alerts, recoveries = compare(previous, current, timezone, ignores)
        current["ignored"] = sorted(ignores)
        current["ignored_modules"] = sorted(previous.get("ignored_modules") or [])
        # Write before notifying: the dialog waits for a click, and an
        # unrecorded anomaly would be reported twice on the next pass.
        if not args.dry_run:
            md.save_json(WATCH_STATE, current)

    moment = dt.datetime.now(timezone).strftime(_msg("format_moment"))
    for ligne in alerts:
        print(_msg("alerte_ligne", ligne=ligne))
    for ligne in recoveries:
        print(_msg("retabli_ligne", ligne=ligne))
    if not alerts and not recoveries:
        print(_msg("rien_a_signaler", moment=moment))

        journal("; ".join(alerts + recoveries) or "nothing to report")
    if alerts and not args.dry_run:
        corps = [f"- {ligne}" for ligne in alerts]
        modules = current.get("modules") or []
        a_signaler = [
            m for m in modules if not m["online"]
            and _msg("module_hors_ligne", nom=_libelle_module(m, modules)) in alerts]
        if len(a_signaler) < len(alerts):
            corps += ["", _msg("hint_sourdine"),
                      '  blink2video watch --ignore "camera name"']
        if a_signaler:
            corps += ["", _msg("hint_sourdine_module")]
            corps += [f'  blink2video watch --ignore-module "{cle_module(m)}"'
                      for m in a_signaler]
        popup(_msg("titre_anomalies", n=len(alerts)), "\n".join(corps))
    for ligne in recoveries:
        toast(_msg("titre_retour"), ligne)


def _sourdine_modules(args) -> int:
    """--ignore-module / --unignore-module: 0 if state was updated, 2 if
    a module does not exist or is ambiguous (nothing is written then)."""
    with runtime.verrou("watch", "sourdine", stale_after=60, attente=10):
        state = md.load_json(WATCH_STATE, {})
        modules = state.get("modules") or []
        muets = set(state.get("ignored_modules") or [])
        a_ajouter, inconnus, ambigus = resoudre_modules(args.ignore_module, modules)
        a_retirer, inconnus_retrait, ambigus_retrait = resoudre_modules(
            args.unignore_module, modules)
        # Unmuting a key still on record remains possible even if
        # the module left the installation.
        for ref in list(inconnus_retrait):
            if ref in muets:
                a_retirer.add(ref)
                inconnus_retrait.remove(ref)
        inconnus += inconnus_retrait
        ambigus += ambigus_retrait
        if inconnus or ambigus:
            connus = ", ".join(
                f"{_libelle_module(m, modules)} [{cle_module(m)}]" for m in modules) \
                or _msg("aucune")
            for ref in inconnus:
                print(_msg("module_inconnu", ref=ref, connus=connus))
            for ref, trouves in ambigus:
                candidats = ", ".join(cle_module(m) for m in trouves)
                print(_msg("module_ambigu", ref=ref, candidats=candidats))
            return 2
        muets = (muets | a_ajouter) - a_retirer
        state["ignored_modules"] = sorted(muets)
        md.save_json(WATCH_STATE, state)
    print(_msg("modules_en_sourdine"), ", ".join(state["ignored_modules"]) or _msg("aucune"))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blink2video watch",
        description=_msg("aide_desc"),
    )
    parser.add_argument("--timezone", default="Europe/Paris")
    runtime.ajouter_boucle(parser)
    parser.add_argument("--dry-run", action="store_true", help=_msg("aide_dry_run"))
    parser.add_argument("--test", action="store_true", help=_msg("aide_test"))
    parser.add_argument(
        "--port", type=runtime.port_valide, default=8765, help=_msg("aide_port"),
    )
    parser.add_argument(
        "--ignore", metavar="CAMERA", nargs="+", default=[], help=_msg("aide_ignore"),
    )
    parser.add_argument(
        "--unignore", metavar="CAMERA", nargs="+", default=[], help=_msg("aide_unignore"),
    )
    parser.add_argument(
        "--ignore-module", metavar="MODULE", nargs="+", default=[],
        help=_msg("aide_ignore_module"),
    )
    parser.add_argument(
        "--unignore-module", metavar="MODULE", nargs="+", default=[],
        help=_msg("aide_unignore_module"),
    )
    return parser.parse_args()


def main() -> int:
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # Python 3.8 (Windows 7 build, see build-win7.yml): no zoneinfo in stdlib.
        from backports.zoneinfo import ZoneInfo

    args = parse_args()
    timezone = ZoneInfo(args.timezone)

    # Mute options change configuration then the check continues:
    # an option should specify how the command works, not
    # divert it from its purpose. Same approach as merge --exclude, which
    # excludes a clip then merges.
    if args.ignore or args.unignore:
        # Same lock as _controler(): a background pass may be
        # re-reading/re-writing WATCH_STATE while mute is changed.
        with runtime.verrou("watch", "sourdine", stale_after=60, attente=10):
            state = md.load_json(WATCH_STATE, {})
            cameras = state.get("cameras") or {}
            ignores = normaliser_sourdines(state.get("ignored") or [], cameras)
            ignores |= normaliser_sourdines(args.ignore, cameras)
            ignores -= normaliser_sourdines(args.unignore, cameras)
            state["ignored"] = sorted(ignores)
            md.save_json(WATCH_STATE, state)
        print(_msg("cameras_en_sourdine"), ", ".join(state["ignored"]) or _msg("aucune"))

    if args.ignore_module or args.unignore_module:
        code = _sourdine_modules(args)
        if code:
            return code

    if args.test:
        popup(_msg("titre_test_alerte"), _msg("corps_test_alerte"))
        return 0

    # This program checks state, nothing more, as its name says. Repetition
    # is an option shared by all verbs, not a verb itself.
    return runtime.repeter(
        lambda: un_tour(args, timezone),
        args.loop, journal,
    )


if __name__ == "__main__":
    raise SystemExit(main())
