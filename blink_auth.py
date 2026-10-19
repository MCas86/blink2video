"""Blink authentication: saved session, credentials, 2FA.

Extracted from blink2video.py at step 8 of the remediation plan
(AUDIT-2026-08-13.md, section 20, 8.1): this file only knows the
session, never clips or the registry. It assumes runtime.bootstrap()
has already run (the caller decides when: see blink_cli.py and O-06/8.7)."""

from __future__ import annotations  # Python 3.8 (Windows 7 build): "X | None" annotations are only evaluated when writing strings, never at runtime.

import asyncio
from contextlib import asynccontextmanager
import getpass
import json
import os
import ssl
import stat as stat_module
import time
import uuid

import runtime
import blink_tls

import certifi
from aiohttp import ClientSession, TCPConnector

from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink

# I-01: merge_daily.py, serve.py and watch.py already derive their default
# paths from runtime.app_dir() (BASE_DIR); blink2video.py was the only one of
# the four using relative paths, resolved by chance of the launch
# working directory rather than the data root.
BASE_DIR = runtime.app_dir()
CONFIG = BASE_DIR / "blink_auth.json"

# I-02/4.5: whitelist of persisted fields. blink.auth.login_attributes
# returns self.data in full, which still contains username/password as
# passed to Auth(); trusting it without filtering would leak the password
# as soon as one more field appeared there someday on the blinkpy side.
AUTH_FIELDS = (
    "username", "token", "expires_in", "expiration_date", "refresh_token",
    "host", "region_id", "client_id", "account_id", "user_id", "hardware_id",
)

LIBELLES = {
    "en": {
        "session_illisible_fichier":
            "Session unreadable [file]. Signing in again is required.",
        "session_illisible_json":
            "Session unreadable [JSON data]. Signing in again is required.",
        "session_illisible_schema":
            "Session unreadable [JSON structure]. Signing in again is required.",
        "connexion_titre": "\nSigning in to the Blink account",
        "prompt_email": "Email address: ",
        "prompt_password": "Password (hidden): ",
        "prompt_code": "Blink code: ",
        "code_envoye": "\nA Blink verification code was just sent.",
        "code_refuse": "Code rejected. {remaining} attempt(s) left.",
        "session_reutilisee": "Reusing the session saved in {config}...",
        "session_invalide": "The saved session is no longer valid.",
    },
}
LIBELLES["fr"] = dict(LIBELLES["en"])



def _(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


def contexte_tls() -> ssl.SSLContext:
    """Extends the system roots with the Mozilla store shipped by certifi.

    Windows 7 no longer necessarily receives new root authorities. We
    keep its store (notably useful behind an enterprise proxy) and
    add certifi's, without ever disabling TLS validation or
    hostname checking.
    """
    return blink_tls.contexte_tls()


def session_http() -> ClientSession:
    """Creates an aiohttp session whose every request uses this store."""
    return ClientSession(connector=TCPConnector(ssl=contexte_tls()))


@asynccontextmanager
async def session_http_temporaire():
    """Also closes the SSL transport before the short loop disappears."""
    session = session_http()
    try:
        yield session
    finally:
        await session.close()
        # aiohttp recommends 250 ms to let asyncio close its SSL
        # transports; needed especially with Python 3.8's ProactorEventLoop.
        await asyncio.sleep(0.250)


def load_saved_session() -> dict | None:
    """Loads a Blink session without ever reusing a stored password."""
    if not CONFIG.exists():
        return None

    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except OSError:
        print(_("session_illisible_fichier"))
        return None
    except json.JSONDecodeError:
        print(_("session_illisible_json"))
        return None

    if not isinstance(data, dict):
        print(_("session_illisible_schema"))
        return None

    if not isinstance(data.get("refresh_token"), str) or not data["refresh_token"]:
        return None

    # Auth.startup() requires these keys, even when a refresh_token is present.
    # An empty string avoids keeping the password on disk.
    data["username"] = data.get("username", "")
    data["password"] = ""
    return data


def ask_credentials() -> dict:
    """Asks for credentials in the terminal."""
    print(_("connexion_titre"))
    username = input(_("prompt_email")).strip()
    password = getpass.getpass(_("prompt_password"))
    return {"username": username, "password": password}


def make_blink(session: ClientSession, login_data: dict) -> Blink:
    """Builds a Blink client with an explicit authentication flow.

    I-03: Auth refreshes its token in memory across requests (see
    Auth.query in blinkpy), without ever writing it to disk itself. A
    background loop can run for hours: without persistence on every
    refresh, a crash or simple restart falls back on a stale token.
    `callback` is the extension point blinkpy provides for that."""
    blink = Blink(session=session)
    blink.auth = Auth(login_data, no_prompt=True, session=session,
                      callback=lambda: save_session(blink))
    return blink


async def prompt_2fa_code(attempt: int) -> str:
    """Asks for the verification code in the terminal."""
    return input(_("prompt_code")).strip()


async def finish_login(blink: Blink, ask_code=None) -> bool:
    """Starts Blink and optionally completes two-step verification.

    `ask_code` lets the question be asked elsewhere than in the terminal:
    serve.py wires a browser form there. It is a coroutine so
    waiting for the code does not freeze the event loop while the
    Blink session stays open."""
    try:
        connected = await blink.start()
    except BlinkTwoFARequiredError:
        ask_code = ask_code or prompt_2fa_code
        print(_("code_envoye"))
        for attempt in range(3):
            code = (await ask_code(attempt) or "").strip()
            if code and await blink.send_2fa_code(code):
                return True
            remaining = 2 - attempt
            if remaining:
                print(_("code_refuse", remaining=remaining))
        return False

    return bool(connected)


async def connect_saved(session: ClientSession):
    """Opens a Blink session from the saved file, without asking anything.

    Distinct from `connect`, which falls back to the terminal when the session is
    no longer valid: behind a server there is nobody to answer, better
    to return None and let the caller propose a reconnection."""
    saved = load_saved_session()
    if not saved:
        return None
    blink = make_blink(session, saved)
    if not await finish_login(blink, ask_code=_no_code):
        return None
    save_session(blink)
    return blink


async def _no_code(attempt: int) -> str:
    """Refuses two-step verification outside an interactive session."""
    return ""


async def login(session: ClientSession, username: str, password: str, ask_code=None):
    """Opens a Blink session from credentials already collected.

    Entry point for serve.py, which collects them in the browser. The password
    is only used here: `save_session` never writes it to disk."""
    blink = make_blink(session, {"username": username, "password": password})
    if not await finish_login(blink, ask_code):
        return None
    save_session(blink)
    return blink


def save_session(blink: Blink) -> None:
    """Saves session tokens, never the Blink password.

    Two to four processes can connect in parallel at startup
    (USB download, cloud download, watch, serve), and this callback is also
    the one blinkpy's auto-refresh triggers along the way
    (see make_blink, I-03): two writes can therefore overlap without
    guaranteed order. Three precautions: a temporary unique to this process
    (I-02, the old `blink_auth.tmp` was shared by all), an internal timestamp
    that prevents a late save from overwriting a more recent one
    already on disk, and - since the 0eab463 code review, bug #7 - a
    lock (`runtime.verrou`, already used elsewhere for the Sync Module and
    assembly) that makes the read-then-decide-then-write atomic:
    without it, two processes could each read the old contents, each
    conclude « mine is more recent », then write in unguaranteed
    order - whichever finishes second overwrites the newest token,
    whatever timestamp it carried."""
    complet = blink.auth.login_attributes
    data = {champ: complet[champ] for champ in AUTH_FIELDS if champ in complet}
    data["updated_at"] = time.time()

    try:
        with runtime.verrou("session-save", f"blink_auth-{os.getpid()}", attente=5):
            precedent = None
            if CONFIG.exists():
                try:
                    precedent = json.loads(CONFIG.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    precedent = None
            if isinstance(precedent, dict):
                # A malformed updated_at (hand-tampered file, partially
                # written then recovered JSON) must not prevent the
                # save: treated as no known preference (0),
                # not as an exception that bubbles up to Blink's automatic
                # refresh callback (review of 27/08,
                # bug 7).
                try:
                    precedent_updated_at = float(precedent.get("updated_at") or 0)
                except (TypeError, ValueError):
                    precedent_updated_at = 0
                if precedent_updated_at > data["updated_at"]:
                    return

            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            temporary = CONFIG.with_name(
                f"{CONFIG.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
            )
            try:
                temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
                if os.name != "nt":
                    os.chmod(temporary, stat_module.S_IRUSR | stat_module.S_IWUSR)
                temporary.replace(CONFIG)
            finally:
                temporary.unlink(missing_ok=True)
    except runtime.BusyError:
        # Another process has held the lock for more than 5 s for a
        # simple token write: more likely it just finished its
        # own (already up to date) than a real deadlock - abandon here rather than
        # make the caller wait longer for a redundant
        # save.
        pass


async def connect(session: ClientSession) -> Blink | None:
    """Opens a Blink session, with interactive reconnection as last resort."""
    saved_session = load_saved_session()

    if saved_session:
        print(_("session_reutilisee", config=CONFIG))
        blink = make_blink(session, saved_session)
        connected = await finish_login(blink)
        if not connected:
            print(_("session_invalide"))
            blink = make_blink(session, ask_credentials())
            connected = await finish_login(blink)
    else:
        blink = make_blink(session, ask_credentials())
        connected = await finish_login(blink)

    if not connected:
        return None

    save_session(blink)
    return blink


async def preflight() -> dict:
    """Read-only check of the saved session (E-01, section 17.3).

    Strictly side-effect free: no arming, live view, USB manifest,
    download, assembly or notification. Only used to decide whether
    « start » can continue directly or must first go through
    web onboarding."""
    etat = {"authenticated": False, "networks": 0, "sync_modules": 0,
            "cameras": 0, "cloud_only": False, "error": None}
    try:
        async with session_http_temporaire() as session:
            blink = await connect_saved(session)
            if blink is None:
                return etat
            etat["authenticated"] = True
            synchronisations = getattr(blink, "sync", None) or {}
            etat["sync_modules"] = len(synchronisations)
            etat["cloud_only"] = not synchronisations
            reseaux = set()
            for sync in synchronisations.values():
                etat["cameras"] += len(getattr(sync, "cameras", None) or {})
                reseau = getattr(sync, "network_id", None)
                if reseau is not None:
                    reseaux.add(reseau)
            etat["networks"] = len(reseaux)
    except Exception as erreur:  # a network failure must not bring down « start »
        etat["error"] = f"{type(erreur).__name__}: {erreur}"
    return etat
