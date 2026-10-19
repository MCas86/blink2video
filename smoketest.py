"""Checks that an installation actually works, on the user's machine.

Distinct from tests.py, which exercises the code against an integration service.
This one answers a different question: « does it work on *this* machine? ».
It is aimed at someone who just installed the tool and wants to know before
relying on it.

It therefore produces a real timestamped video that can be opened and watched,
shows a real notification, and reports on the Blink session and
autostart. It touches neither your clips nor your videos: everything
happens in a temporary folder, except the demo video, left at
the indicated location so you can watch it.

    blink2video smoketest
    blink2video smoketest --keep    keep the working folder
    blink2video smoketest --webrtc --report report.json
        test H.264/ICE/DTLS/SRTP locally, without account or camera
"""

import argparse
import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import runtime

LIBELLES = {
    "en": {
        "aide_desc": "Checks that an installation actually works, on the user's machine.",
        "aide_keep": "keep the working folder instead of deleting it",
        "aide_webrtc": "local WebRTC diagnostic only, no camera or notification",
        "aide_report": "write the WebRTC result to this JSON file",
        "erreur_report_sans_webrtc": "--report requires --webrtc",
        "marque_ok": "ok  ",
        "marque_echec": "FAIL ",
        "controle_installation_titre": "Installation check\n",
        "video_titre": "Video",
        "ffmpeg_trouve": "ffmpeg found",
        "ffmpeg_incrustation": "ffmpeg can burn in text",
        "ffmpeg_incrustation_detail": "without it, videos would come out with no timestamp",
        "police_trouvee": "font found",
        "clip_essai_fabrique": "test clip made, entirely black",
        "video_horodatee_produite": "timestamped video produced",
        "heure_dessinee": "the time is actually drawn in the image",
        "pixels_allumes_detail": "{allumes} lit pixels in the caption area",
        "a_regarder": "        to watch: {chemin}",
        "dossier_travail_conserve": "        working folder kept: {chemin}",
        "notification_titre": "\nNotification",
        "toast_corps": "Installation check: this is a test.",
        "notification_envoyee": "notification sent",
        "notification_envoyee_detail":
            "it should appear on screen; if not, see the README's limits",
        "compte_blink_titre": "\nBlink account",
        "session_enregistree": "session saved",
        "session_absente_detail":
            "run « blink2video login »; without it, neither download nor live view work",
        "clips_deja_recuperes": "clips already retrieved",
        "clips_compte_detail": "{n} clip(s), {m} excluded",
        "clips_aucun_detail": "none; run « blink2video download »",
        "demarrage_auto_titre": "\nAutostart",
        "indetermine": "  undetermined: {erreur}",
        "points_a_regarder": "{n} point(s) to look into.",
        "installation_operationnelle": "Installation working.",
    },
}
LIBELLES["fr"] = dict(LIBELLES["en"])



def _(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


runtime.bootstrap()

import merge_daily as md


CONSTATS = []


def constat(ok: bool, intitule: str, detail: str = "") -> bool:
    marque = _("marque_ok") if ok else _("marque_echec")
    print(f"  {marque} {intitule}" + (f"\n        {detail}" if detail else ""))
    CONSTATS.append(ok)
    return ok


def pixels_allumes(ffmpeg: str, video: Path) -> int:
    """Counts the light pixels at the bottom of the image, where the time is written."""
    resultat = runtime.lancer(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "1",
         "-i", str(video), "-frames:v", "1",
         "-vf", "crop=iw:ih/6:0:ih*5/6,format=gray", "-f", "rawvideo", "-"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=False,
    )
    return sum(1 for octet in resultat.stdout if octet > 60)


def main() -> int:
    parser = argparse.ArgumentParser(description=_("aide_desc"))
    parser.add_argument("--keep", action="store_true", help=_("aide_keep"))
    parser.add_argument("--timezone", default="Europe/Paris")
    parser.add_argument("--webrtc", action="store_true", help=_("aide_webrtc"))
    parser.add_argument("--report", type=Path, help=_("aide_report"))
    args = parser.parse_args()
    if args.report and not args.webrtc:
        parser.error(_("erreur_report_sans_webrtc"))
    if args.webrtc:
        return diagnostic_webrtc(args.report)

    print(_("controle_installation_titre"))

    print(_("video_titre"))
    try:
        ffmpeg = md.find_ffmpeg()
        constat(True, _("ffmpeg_trouve"), ffmpeg)
    except RuntimeError as erreur:
        constat(False, _("ffmpeg_trouve"), str(erreur))
        return bilan()

    constat(md.has_drawtext(ffmpeg), _("ffmpeg_incrustation"),
            "" if md.has_drawtext(ffmpeg)
            else _("ffmpeg_incrustation_detail"))

    try:
        police = md.find_font(None)
        constat(True, _("police_trouvee"), str(police))
    except RuntimeError as erreur:
        constat(False, _("police_trouvee"), str(erreur))
        return bilan()

    travail = Path(tempfile.mkdtemp(prefix="blink_smoketest_"))
    demonstration = runtime.dossier_sorties() / "smoketest.mp4"
    try:
        # A black clip: any lit pixel at the bottom of the image can only
        # come from the timestamp, which makes the proof indisputable.
        source = travail / "source.mp4"
        runtime.lancer(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "color=c=black:s=1280x720:d=4:r=30", "-c:v", "libx264",
             "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, check=True,
        )
        constat(pixels_allumes(ffmpeg, source) == 0,
                _("clip_essai_fabrique"))

        import datetime as dt
        try:
            from zoneinfo import ZoneInfo
        except ImportError:  # Python 3.8 (Windows 7 build, see build-win7.yml): no zoneinfo in stdlib.
            from backports.zoneinfo import ZoneInfo

        fuseau = ZoneInfo(args.timezone)
        maintenant = dt.datetime.now(dt.timezone.utc)
        clip = md.ClipInfo(created=maintenant, source=source, duration=4.0,
                           width=1280, height=720, fps=30.0, has_audio=False)
        ok, erreur = md.run_ffmpeg_batch(
            ffmpeg, [clip], 1280, 720, 30.0, fuseau,
            md.quote_filter_path(police), "veryfast", 23, demonstration,
        )
        constat(ok, _("video_horodatee_produite"), erreur)
        if ok:
            allumes = pixels_allumes(ffmpeg, demonstration)
            constat(allumes > 200, _("heure_dessinee"),
                    _("pixels_allumes_detail", allumes=allumes))
            print(_("a_regarder", chemin=demonstration))
    finally:
        if args.keep:
            print(_("dossier_travail_conserve", chemin=travail))
        else:
            shutil.rmtree(travail, ignore_errors=True)

    print(_("notification_titre"))
    try:
        import watch

        watch.toast("blink2video", _("toast_corps"),
                    url="http://127.0.0.1:8765/")
        constat(True, _("notification_envoyee"), _("notification_envoyee_detail"))
    except Exception as erreur:
        constat(False, _("notification_envoyee"), f"{type(erreur).__name__}: {erreur}")

    print(_("compte_blink_titre"))
    session = runtime.app_dir() / "blink_auth.json"
    if not session.is_file():
        constat(False, _("session_enregistree"), _("session_absente_detail"))
    else:
        constat(True, _("session_enregistree"), str(session))
        registre = md.load_json(md.DEFAULT_INPUT / md.DOWNLOAD_STATE, {})
        clips = registre.get("clips") or {}
        ecartes = sum(1 for c in clips.values() if isinstance(c, dict) and c.get("excluded"))
        constat(bool(clips), _("clips_deja_recuperes"),
                _("clips_compte_detail", n=len(clips), m=ecartes) if clips
                else _("clips_aucun_detail"))

    print(_("demarrage_auto_titre"))
    try:
        import autostart

        autostart.appliquer("status")
    except Exception as erreur:
        print(_("indetermine", erreur=erreur))

    return bilan()


def diagnostic_webrtc(rapport=None) -> int:
    """Usable in a console-less bundle without Python installed."""
    from webrtc_probe import verifier_webrtc

    try:
        resultat = asyncio.run(verifier_webrtc(md.find_ffmpeg()))
        resultat.update(ok=True, version=runtime.version_affichee())
    except Exception as erreur:
        resultat = {"ok": False, "version": runtime.version_affichee(),
                    "error": f"{type(erreur).__name__}: {erreur}"}
    texte = json.dumps(resultat, ensure_ascii=False, indent=2)
    if rapport is not None:
        rapport.write_text(texte + "\n", encoding="utf-8")
    print(texte)
    return 0 if resultat["ok"] else 1


def bilan() -> int:
    echecs = CONSTATS.count(False)
    print()
    if echecs:
        print(_("points_a_regarder", n=echecs))
        return 1
    print(_("installation_operationnelle"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
