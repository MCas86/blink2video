// Anti-CSRF token (see Handler.jeton_valide on the server, 28.60): set on
// every request that modifies something. Done once here, before any
// other script, so no fetch() below has to worry about it.
const BLINK_TOKEN = "__TOKEN__";
const BLINK_PID = "__PID__";
const _fetchNatif = window.fetch;
window.fetch = (entree, options) => {
  options = options || {};
  const headers = new Headers(options.headers || {});
  headers.set("X-Blink-Token", BLINK_TOKEN);
  options = { ...options, headers };
  return _fetchNatif(entree, options);
};

// The browser may close its connection before aiortc notices.
// Explicitly signal this tab's sessions, including an offer still in
// progress: a page with no live view must never cut another tab's.
// pagehide also covers reloads and bfcache.
window.addEventListener("pagehide", () => {
  for (const name of nomsDirectsActifs()) stopWatch(name, true);
});

// Any value from Blink or a filename is data, never markup. The few
// views built from HTML templates all go through this function; actions
// use data-* and event delegation, so no name is reinjected into JS.
const h = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
const avecJeton = (url) => `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(BLINK_TOKEN)}`;

// If the server restarted outside the three transitions tracked below
// (manual reinstall while the tab stayed open, crash...), the token
// set by this page is stale: any protected route falls back to
// http.server's default send_error(), which replies with HTML
// (DOCTYPE), never JSON. .json() then fails with a specific SyntaxError,
// never raised by a real JSON error (send_json() always returns valid
// JSON, even at 500). The only sensible recovery is a full reload,
// which gets a fresh page and token (reported by cutthin on Reddit,
// 2026-08-31).
// Exception: 502/503/504. A proxy (Caddy, nginx...) answers in place of
// the server while it restarts, with an empty or HTML body: that is not
// a stale token, the server is simply gone. Reloading then brought the
// browser's error page, with no script to recover (issue #35, Caddy);
// sonderRelance() reloads when it comes back.
async function lireJSON(reponse) {
  try {
    return await reponse.json();
  } catch (erreur) {
    const absent = reponse.status >= 502 && reponse.status <= 504;
    if (erreur instanceof SyntaxError && !absent) location.reload();
    throw erreur;
  }
}

let data = { clips: [], cameras: [], days: [] };
let videos = { daily: [], weekly: [], monthly: [] };
let snapshots = [];
const $ = (id) => document.getElementById(id);

// ── i18n ─────────────────────────────────────────────────────────────────
// Same pattern as lidar2map's gui/app.js: inline dict per locale + data-i18n
// on static nodes, t()/tf() called directly in JS-generated text. Zero
// deps. This fork is English-only: I18N.fr is overwritten from I18N.en
// after both catalogs are defined. Detection / FR-EN buttons remain for
// compatibility; both show English strings.
const I18N = {
  fr: {},
  en: {
    "view.live": "Live Views", "view.direct": "Live Recordings",
    "view.alllive": "All Live Views",
    "view.rotation": "Live View Rotation",
    "reglages.sharedAccess": "Shared camera access",
    "reglages.sharedAccess.hint": "Friends authenticate with Cloudflare Access (their email). You keep the Blink login. Assign cameras below. Leave AUD empty for local-only admin on this machine.",
    "reglages.cfAud": "Access application AUD",
    "reglages.accessAdmins": "Admin emails (comma-separated)",
    "reglages.accessAdd": "Add friend",
    "reglages.accessSave": "Save access",
    "reglages.accessSaved": "Access settings saved.",
    "reglages.accessSaveFailed": "Could not save access settings.",
    "reglages.accessRemove": "Remove",
    "reglages.accessNoCams": "No cameras loaded yet. Open Live Views once, then reopen Settings.",
    "reglages.accessGuestHint": "View-only: live, clips and pictures for the cameras you check.",
    "access.denied": "Cloudflare Access login required.",
    "access.pending": "Signed in via Cloudflare Access as {email}. No cameras assigned yet — ask the admin to grant cameras in Settings → Shared camera access.",
    "access.whoami": "Signed in as {email}",
    "view.clips": "Detection Recordings", "view.daily": "Detection Daily",
    "view.weekly": "Detection Weekly", "view.monthly": "Detection Monthly",
    "view.pictures": "Pictures",
    "filter.allcameras": "all cameras",
    "btn.refresh": "↻ Refresh", "btn.reglages": "⚙ Settings…", "btn.reglages.title": "Settings",
    "entete.pid": "Server PID {pid}",
    "update.installing": "Install {version}",
    "update.title": "Version {version} published. Download, stop and restart are automatic.",
    "update.updating": "Updating…",
    "update.progress": "Updating to {version}: downloading, then restarting…",
    "update.failed": "The update could not be started: {erreur}",
    "passages.updated": "updated {heure}",
    "passages.new.one": " · {n} new clip, click Refresh",
    "passages.new.many": " · {n} new clips, click Refresh",
    "auth.title": "Blink login", "auth.title.2fa": "Two-step verification",
    "auth.hint": "The password is only used to open the session; only the tokens are stored, never the password.",
    "auth.hint.2fa": "Blink just sent a code. Enter it to finish logging in.",
    "auth.email": "Email address", "auth.password": "Password",
    "auth.show": "Show", "auth.hide": "Hide",
    "auth.show.aria": "Show password", "auth.hide.aria": "Hide password",
    "auth.code": "Code received by SMS or email",
    "auth.cancel": "Cancel", "auth.ok": "Log in", "auth.validate": "Confirm",
    "auth.connecting": "Signing in…", "auth.failed": "Login failed.",
    "reglages.title": "Settings",
    "reglages.tab.general": "General", "reglages.tab.video": "Video",
    "reglages.tab.acces": "Access & automation", "reglages.tab.alertes": "Alerts",
    "reglages.initial.hint": "Check the data folder and time zone in particular. No clip will be downloaded until you apply these settings.",
    "reglages.autostart": "Start monitoring automatically",
    "reglages.autostart.title": "Starts the web server and clip processing in the background when your session opens (at boot for a Linux service with lingering); does not open this page by itself",
    "reglages.auto": "Automatic page refresh",
    "reglages.auto.title": "Reload the list as soon as clips arrive",
    "reglages.showOut": "Show discarded clips",
    "reglages.serveur": "Server port",
    "reglages.storageDir": "Videos folder",
    "reglages.storageDir.placeholder": "C:/path/to/the/folder",
    "reglages.storageDir.hint": "Clips, assembled videos, lives and snapshots. Does not move those already saved at the old location: do it yourself if you change this path. Empty = default location, Documents/blink2video.",
    "reglages.storageDir.browse": "Browse…",
    "reglages.storageDir.browse.unavailable": "Folder picker unavailable on this machine: type the path directly.",
    "reglages.accesDistant": "Remote access over a mesh VPN",
    "reglages.accesDistant.hint": "To reach this instance directly from a mesh VPN (Tailscale, WireGuard), no reverse proxy in front.",
    "reglages.accesDistant.hint.text": "Use together with the BLINK_BIND environment variable set to the listening address (see the README, \"Reaching it remotely\" section): this setting alone, without it, changes nothing about who can reach the interface. Several values allowed, comma-separated: hostnames, IP addresses or subnets (192.168.1.0/24). Only exact names and addresses also allow embedding the page in an iframe.",
    "reglages.trustedHost": "Trusted host",
    "reglages.cadence": "Camera polling interval",
    "reglages.usb": "Local storage (minutes)", "reglages.cloud": "Cloud (minutes)",
    "reglages.video": "Video", "reglages.timestamp": "Burn the date and time into the image",
    "reglages.timestamp.style.hint": "Empty = size automatically scaled to the video's height. Color: a name (white, yellow…) or hex code, optionally with transparency (e.g. white@0.8).",
    "reglages.fontSize": "Font size", "reglages.fontColor": "Color",
    "reglages.boxOpacity": "Box opacity",
    "reglages.timezone": "Time zone",
    "reglages.liveProtocol": "Live view protocol",
    "reglages.liveProtocol.webrtc": "WebRTC (fast)",
    "reglages.liveProtocol.mse": "MSE (compatible)",
    "reglages.liveAutoStop": "Auto-stop live view (s)",
    "reglages.liveAutoStop.hint": "Stops the live view on its own after this delay, up to 420 s, even in continuous live. Empty or 0: no auto-stop; Blink closes every live view by itself after about 6 minutes, and a camera's \"Continuous live\" box starts it again.",
    "reglages.archivage": "Per-camera time-based video creation",
    "reglages.downloadAuto": "Download clips automatically",
    "reglages.downloadAuto.hint": "Unchecked, no clip is fetched or stored anymore: useful to keep only the live view. The cadences below then have no effect.",
    "reglages.mergeJour": "Daily",
    "reglages.mergeSemaine": "Weekly", "reglages.mergeMois": "Monthly",
    "reglages.archivage.hint": "Weekly and Monthly are assembled from the Daily: unchecking \u201cDaily\u201d also disables the other two.",
    "reglages.alertes": "Mute alerts",
    "reglages.suppressionAuto": "Automatic deletion after download",
    "reglages.webhook": "Webhook picture",
    "reglages.webhook.hint": "Triggers a picture remotely (home automation) without opening this page.",
    "reglages.webhook.hint.text": "Call this URL with GET, replacing NOM_CAMERA with a camera's exact name, to trigger a picture remotely. The secret is part of the URL: keep it private.",
    "reglages.webhook.url": "URL",
    "reglages.webhook.regenerer": "Regenerate the secret",
    "reglages.webhook.regenerer.confirm": "Regenerating will invalidate the current URL: any existing automation will need the new one. Continue?",
    "reglages.webhookNotif": "Webhook notification",
    "reglages.webhookNotif.hint": "Calls this URL with POST on every new clip or picture, once the file is actually done writing to disk.",
    "reglages.webhookNotif.hint.text": "Sends a JSON POST (camera, path, type) to the URL below every time a clip or picture is ready. Best-effort, no retry: a missed notification never holds up downloading.",
    "reglages.webhookNotifUrl": "Notification URL",
    "reglages.hint": "Settings only take effect on restart: \u201cApply\u201d saves and restarts. Changing the port redirects this page to the new address.",
    "reglages.apply": "Apply", "reglages.restarting": "Restarting…",
    "reglages.restart": "Restart",
    "reglages.restart.hint": "Restarts without changing any setting, for example to pick up an update already in place.",
    "reglages.checkUpdates": "Check for updates",
    "reglages.checkUpdates.hint": "Asks GitHub right away whether a newer version is out. Without this button, blink2video checks by itself every hour.",
    "reglages.checkUpdates.running": "Checking…",
    "reglages.checkUpdates.none": "blink2video {version} is up to date.",
    "reglages.checkUpdates.found": "Version {version} is out: the \"Install {version}\" button is at the top of the page.",
    "reglages.checkUpdates.offline": "GitHub did not answer. Try again in a moment.",
    "reglages.restarting.settings": "Restarting with the new settings…",
    "reglages.portchange": "Port changed: redirecting to {url} once the shutdown is confirmed…",
    "reglages.stop": "Stop camera monitoring", "reglages.close": "Close",
    "reglages.error.cadence": "Intervals must be at least 1 minute.",
    "reglages.error.port": "The port must be between 1 and 65535.",
    "reglages.error.timezone": "The time zone cannot be empty.",
    "stop.stopping": "Stopping…",
    "stop.stopped": "Shutdown requested. This page will become unavailable.",
    "stop.failed": "Shutdown did not complete; blink2video is still responding.",
    "reglages.restartFailed": "Restart did not begin. Check the logs and try again.",
    "sourdine.loading": "Loading…", "sourdine.unavailable": "Camera list unavailable.",
    "sourdine.none": "No known camera yet.",
    "suppressionAuto.loading": "Loading…",
    "suppressionAuto.unavailable": "Camera list unavailable.",
    "suppressionAuto.none": "No known camera yet.",
    "suppressionAuto.hint": "Once a clip is successfully downloaded, it is deleted from its source (local USB/microSD storage or subscription cloud, depending on the camera).",
    "phase.inventory_clips": "Finding clips to download",
    "phase.download_clips": "Downloading clips",
    "phase.prepare_clips": "Preparing clips",
    "phase.assemble_videos": "Assembling videos",
    "phase.update_download": "Downloading the update ({mo} MB)",
    "phase.update_install": "Installing the update",
    "phase.step_download": "Downloading", "phase.step_merge": "Merging",
    "phase.cloud_section": "Subscription cloud",
    "phase.usb_section": "Local storage: {hub}",
    "live.querying": "Querying the Blink system…",
    "live.count": "{n} camera(s) · {m} with detection on",
    "system.armed": "System armed", "system.disarmed": "System disarmed",
    "camera.offline": "OFFLINE", "camera.noeffect": "no effect, system disarmed",
    "camera.detection.on": "Detection on", "camera.detection.off": "Detection off",
    "camera.wake": "Wake", "camera.waking": "Waking…",
    "camera.wake.title": "Wakes the camera now (takes a photo). Uses a bit of battery, up to 2 minutes.",
    "camera.snapshot": "Snapshot", "camera.snapshotting": "Snapshot…",
    "camera.snapshot.saved": "Snapshot saved ✓",
    "camera.snapshot.title": "Takes a picture now and keeps it under Pictures. Uses a bit of battery, up to 2 minutes.",
    "camera.continuous": "Continuous live",
    "camera.continuous.title": "Starts the live view again each time Blink closes it, after about 6 minutes, until you click Stop. A pause of about ten seconds marks each restart.",
    "camera.continuous.title.battery": "Starts the live view again each time Blink closes it, after about 6 minutes, until you click Stop. A pause of about ten seconds marks each restart. On this battery camera, an endless live view drains it quickly.",
    "camera.battery": "battery {v}", "camera.wifi": "Wi-Fi {v} dBm",
    "camera.lfr": "module link {v}", "camera.measured.at": "measured at {v}",
    "camera.measured.on": "measured on {d} at {h}", "camera.firmware": "firmware {v}",
    "camera.noclips": "no clip retrieved", "camera.clipssource": "clips: {v}",
    "camera.none": "—",
    "watch.live": "View live", "watch.retry": "Retry", "watch.stop": "Stop",
    "watch.noh264": "This browser does not offer H.264 for WebRTC. Check OpenH264 in Firefox, or select MSE in settings.",
    "watch.waking": "Waking the camera…", "watch.waking.seconds": "Waking the camera… {s} s",
    "watch.waking.slow": "Waking the camera… {s} s (a battery camera is slower)",
    "watch.reconnecting": "Reconnecting…",
    "rotation.waiting": "Waiting for connection",
    "live.fullscreen.title": "Expand fullscreen",
    "live.fullscreen.title.exit": "Exit fullscreen",
    "live.record.start": "Record", "live.record.stop": "Stop recording",
    "live.record.title": "Records this live view to disk (Blink_Direct folder)",
    "watch.noimage": "No image received. The camera did not respond.",
    "watch.interrupted": "The live view was interrupted. You can restart it.",
    "watch.refused": "The stream was refused by the server.",
    "watch.refused.code": "The stream was refused by the server ({code}).",
    "watch.refused.retry": "Stream refused. A previous live view may still be closing: try again in a few seconds.",
    "watch.codec.unsupported": "Codec not supported by this browser: {codec}",
    "command.sending": "Sending command…",
    "clips.none.filtered": "No clip matches this filter.",
    "clips.none.ever": "No clip retrieved yet.<br>Download is already running in the background (USB every 10 min, cloud every minute): clips will appear here on their own. Check that a USB drive is plugged into the module: without it, recordings only go to the Blink subscription cloud, which this tool does not read.",
    "direct.none.ever": "No direct recording yet.<br>Open Direct, click Record while a camera is playing: it will appear here.",
    "clips.window": "{m}/{total} clips",
    "clips.previous": "Previous", "clips.next": "Next",
    "clips.first": "First", "clips.last": "Last",
    "clips.go.page": "Go to page", "clips.go": "Go",
    "clips.per.page": "Clips per page", "clips.all": "All",
    "clips.page": "Page {page}/{pages} · {total} clips",
    "filtre.showHidden": "Show hidden cameras",
    "reglages.tailleCartes": "Thumbnail size",
    "reglages.tailleCartes.petites": "Small", "reglages.tailleCartes.moyennes": "Medium",
    "reglages.tailleCartes.grandes": "Large", "reglages.tailleCartes.tres_grandes": "Extra large",
    "reglages.barrePagination": "Show the pagination bar at the top of the list",
    "reglages.barrePagination.title": "When hidden, the bar at the bottom of the list stays available to change page. Specific to this browser.",
    "reglages.masquees": "Hidden cameras",
    "reglages.masquees.hint": "A hidden camera disappears from the lists on this page (clips, videos, pictures, live) but keeps recording and downloading. This setting is specific to this browser.",
    "masquees.none": "No known camera yet.",
    "live.toutesMasquees": "All cameras of this system are hidden.",
    "range.title": "Period",
    "range.today": "Today (24h)", "range.week": "This week (7d)",
    "range.month": "This month", "range.2months": "Last 2 months",
    "range.all": "All history", "range.custom": "Custom period",
    "range.custom.depuis": "from {v}", "range.custom.jusqua": "until {v}",
    "range.custom.hint": "Or a precise range, down to the hour:",
    "filtre.button": "🔍 Filter", "filtre.button.title": "Filter",
    "filtre.title": "Filter", "filtre.camera": "Camera",
    "filtre.groupBy": "Group by", "filtre.groupBy.camera": "Camera",
    "filtre.groupBy.day": "Day",
    "range.from": "From", "range.to": "to", "range.apply": "Filter",
    "videos.count": "{n} video(s) · {duree} total",
    "videos.none": "No assembled video. Run a refresh.",
    "videos.download": "Download",
    "pictures.count": "{n} picture(s)",
    "pictures.none": "No picture yet. Take one from Live, or via the webhook.",
    "pictures.delete": "Delete",
    "pictures.delete.title": "Delete this picture from disk.",
    "pictures.delete.confirm": "Delete this picture?",
    "clip.resume": "Resume", "clip.discard": "Discard",
    "clip.discard.title": "Remove this clip from the assembled videos (daily, weekly, monthly). The downloaded copy stays on disk.",
    "clip.resume.title": "Include this clip in future assemblies again.",
    "clip.deleteSource": "Delete",
    "clip.deleteSource.pending": "Deleting…",
    "clip.deleteSource.title": "Delete this clip from its source (local storage or subscription cloud). The copy already downloaded here is not affected. Can take up to a minute for local storage.",
    "selection.apply": "✓ Apply ({n})",
    "selection.confirm.suppression": "{n} clip(s) will be deleted from their source (local USB/microSD storage or subscription cloud). Copies already downloaded here are not affected. Continue?",
    "selection.partial": "{n} deletion(s) failed or found nothing to delete (already removed elsewhere). The rest of the selection was applied.",
    "refresh.starting": "Starting…", "refresh.errors": "Finished with errors",
    "refresh.disconnected": "\\nConnection lost.\\n",
  },
};
// English-only UI for this fork: French catalog mirrors English.
I18N.fr = Object.assign({}, I18N.en);
let _lang = "en";
function t(k) { return (I18N[_lang] && I18N[_lang][k]) || I18N.en[k] || k; }
function tf(k, v) {
  let s = t(k);
  for (const p in (v || {})) s = s.split("{" + p + "}").join(v[p]);
  return s;
}
function detectLang() {
  return (navigator.language || "en").toLowerCase().startsWith("fr") ? "fr" : "en";
}
function applyI18n() {
  document.documentElement.lang = _lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const v = t(el.dataset.i18n); if (v) el.textContent = v;
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    const v = t(el.dataset.i18nPlaceholder); if (v) el.placeholder = v;
  });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    const v = t(el.dataset.i18nTitle); if (v) el.title = v;
  });
  document.querySelectorAll("[data-lang-btn]").forEach((b) =>
    b.classList.toggle("active", b.dataset.langBtn === _lang));
}
function setLang(code, persist) {
  _lang = code === "en" ? "en" : "fr";
  applyI18n();
  // Dynamic value (process PID), so rendered here rather than via
  // data-i18n, which cannot interpolate any parameter in its translation.
  $("pidServeur").textContent = tf("entete.pid", { pid: BLINK_PID });
  // The show/hide password button tracks its own state (masked or not),
  // which applyI18n does not know: re-applied here rather than via
  // data-i18n, which would overwrite "Hide" with "Show" if the password
  // was already visible when the language changed.
  const pass = $("pass");
  if (pass) {
    const masque = pass.type === "password";
    $("passToggle").textContent = t(masque ? "auth.show" : "auth.hide");
    $("passToggle").setAttribute("aria-label", t(masque ? "auth.show.aria" : "auth.hide.aria"));
  }
  // Rendered by JS rather than data-i18n: rebuild so the language applies
  // immediately, without waiting for the next event that would normally
  // trigger this render. fill() handles an empty array fine (the "all"
  // option stays), so no guard here.
  if (typeof fill === "function") {
    fill($("camera"), camerasConnues(), t("filter.allcameras"));
  }
  if (typeof render === "function" && data.clips) render();
  if (typeof renderLive === "function" && system) renderLive();
  // An active live view freezes the grid (see renderLive()): the fullscreen
  // button already placed therefore survives the language change without
  // being rebuilt, and must be retranslated here rather than stay in the
  // old language.
  if (typeof syncExpandButtons === "function") syncExpandButtons();
  // #sourdineListe carries data-i18n="sourdine.loading" as HTML fallback:
  // applyI18n() just overwrote its real checkboxes with that loading text
  // if the panel is open during the language switch. Rebuild immediately
  // rather than leave the list frozen until the next panel open.
  if (typeof chargerSourdine === "function" && $("reglages")?.open) chargerSourdine();
  if (typeof chargerCamerasMasquees === "function" && $("reglages")?.open) chargerCamerasMasquees();
  if (typeof chargerSuppressionAuto === "function" && $("reglages")?.open) chargerSuppressionAuto();
  if (persist) localStorage.setItem("lang", _lang);
  // Sent on every call, not only an explicit choice (persist): the systray
  // menu (tray.py) reads this value to show the same language as the page,
  // including when it comes from detectLang() and was never chosen by hand.
  // Best-effort, never blocking: an offline page or background tab must
  // not break the display.
  fetch("/api/lang", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lang: _lang }) }).catch(() => {});
}

// ── Display preferences local to this browser (issue #40) ─────────────────
// Like page size and auto-refresh: stored in localStorage, so local to each
// device (a phone and a PC may want different things), with no server setting
// or restart. Hiding a camera does not touch it: it keeps recording and
// downloading.
// Per-user access (/api/me). Local mode without Cloudflare → admin.
let ACCES = { email: "", role: "admin", cameras: null, permissions: ["*"] };
function estAdmin() { return ACCES.role === "admin"; }
// Cloudflare Access login (or local admin): Settings must stay reachable so
// camera allowlists can be edited. Guests still cannot call admin APIs.
function estConnecteAcces() {
  return ACCES.role === "admin" || ACCES.role === "guest";
}

async function chargerAcces() {
  try {
    ACCES = await lireJSON(await fetch("/api/me"));
  } catch (erreur) {
    ACCES = { email: "", role: "admin", cameras: null, permissions: ["*"] };
  }
  appliquerRoleAcces();
}

function appliquerRoleAcces() {
  document.body.classList.toggle("role-guest", !estAdmin());
  const reglages = $("reglagesButton");
  // Anyone who got through Cloudflare Access (or local mode) sees Settings.
  if (reglages) reglages.hidden = !estConnecteAcces();
  const refresh = $("refresh");
  if (refresh) refresh.hidden = !estAdmin();
  const apply = $("applyButton");
  if (apply && !estAdmin()) apply.hidden = true;
  let who = $("accessWhoami");
  if (!who) {
    who = document.createElement("span");
    who.id = "accessWhoami";
    who.className = "sub tiny";
    const header = document.querySelector("header");
    if (header) header.append(who);
  }
  if (ACCES.email) {
    who.textContent = tf("access.whoami", {
      email: ACCES.email + (estAdmin() ? "" : ` (${ACCES.role || "guest"})`),
    });
    who.hidden = false;
  } else {
    who.hidden = true;
  }
  if (ACCES.role === "unauthenticated") {
    $("list").innerHTML = `<p class="empty">${h(ACCES.error || t("access.denied"))}</p>`;
    $("count").textContent = "";
    return;
  }
  if (ACCES.role === "guest" && ACCES.pending_allowlist) {
    // Still load the app; empty camera lists come from the API. Show a hint.
    if (!$("accessPendingHint")) {
      const hint = document.createElement("p");
      hint.id = "accessPendingHint";
      hint.className = "empty";
      hint.textContent = tf("access.pending", { email: ACCES.email || "" });
      const list = $("list");
      if (list && list.parentNode) list.parentNode.insertBefore(hint, list);
    } else {
      $("accessPendingHint").textContent = tf("access.pending", { email: ACCES.email || "" });
      $("accessPendingHint").hidden = false;
    }
  } else {
    const hint = $("accessPendingHint");
    if (hint) hint.hidden = true;
  }
}

const CLE_CAMERAS_MASQUEES = "blink2video.camerasMasquees";
const CLE_TAILLE_CARTES = "blink2video.tailleCartes";
const TAILLES_CARTES = { petites: 220, moyennes: 320, grandes: 480, tres_grandes: 720 };
const TAILLE_CARTES_DEFAUT = "moyennes";
let camerasMasquees = restaurerCamerasMasquees();
// Temporary reveal (filter window checkbox): never persisted; the list
// goes back to hidden on the next load.
let montrerMasquees = false;
let tailleCartes = restaurerTailleCartes();

function restaurerCamerasMasquees() {
  try {
    const liste = JSON.parse(localStorage.getItem(CLE_CAMERAS_MASQUEES) || "[]");
    if (Array.isArray(liste)) return new Set(liste.filter((nom) => typeof nom === "string"));
  } catch (erreur) {}
  return new Set();
}

function memoriserCamerasMasquees() {
  try {
    localStorage.setItem(CLE_CAMERAS_MASQUEES, JSON.stringify([...camerasMasquees].sort()));
  } catch (erreur) {}
}

function estMasquee(nom) {
  return camerasMasquees.has(nom) && !montrerMasquees;
}

function restaurerTailleCartes() {
  try {
    const nom = localStorage.getItem(CLE_TAILLE_CARTES);
    if (Object.prototype.hasOwnProperty.call(TAILLES_CARTES, nom)) return nom;
  } catch (erreur) {}
  return TAILLE_CARTES_DEFAUT;
}

function appliquerTailleCartes() {
  document.documentElement.style.setProperty("--carte-min", `${TAILLES_CARTES[tailleCartes]}px`);
}
appliquerTailleCartes();

// Natural order of camera names ("2-Stairs" before "10-Basement"), in the
// page language. One collator per language: building one per comparison
// would be costly on a long list.
const _collateurs = {};
function comparerNoms(a, b) {
  if (!_collateurs[_lang]) {
    _collateurs[_lang] = new Intl.Collator(_lang, { numeric: true, sensitivity: "base" });
  }
  return _collateurs[_lang].compare(a, b);
}

function majCaseMasquees() {
  $("hiddenLabel").hidden = camerasMasquees.size === 0;
  if (!camerasMasquees.size) {
    montrerMasquees = false;
    $("showHidden").checked = false;
  }
}

function chargerCamerasMasquees() {
  const conteneur = $("camerasMasqueesListe");
  const noms = camerasPourMasquage();
  if (!noms.length) {
    conteneur.textContent = t("masquees.none");
    return;
  }
  conteneur.replaceChildren();
  for (const nom of noms) {
    const label = document.createElement("label");
    const case_ = document.createElement("input");
    case_.type = "checkbox";
    case_.checked = camerasMasquees.has(nom);
    case_.onchange = () => {
      if (case_.checked) camerasMasquees.add(nom);
      else camerasMasquees.delete(nom);
      memoriserCamerasMasquees();
      majCaseMasquees();
      fill($("camera"), camerasConnues(), t("filter.allcameras"));
      render();
    };
    label.appendChild(case_);
    label.append(` ${nom}`);
    conteneur.appendChild(label);
  }
}

// data.cameras (from /api/clips) only knows a camera that has at least one
// downloaded detection clip. A camera only seen live (Direct Recording
// button, never a clip) was therefore missing: its videos still grouped
// under its name, but it could not be chosen in the filter (seen in
// practice, 2026-09-04). Union with cameras actually present in each
// videos category (daily/weekly/monthly/direct) covers that case and the
// same categories for the same reason, nothing specific to direct.
function toutesLesCameras() {
  const ensemble = new Set(data.cameras || []);
  for (const liste of Object.values(videos)) {
    for (const v of liste) ensemble.add(v.camera);
  }
  // A camera with no clip or assembled video (webhook only, never armed)
  // must still appear here: otherwise nothing can filter on it in Photos.
  for (const s of snapshots) ensemble.add(s.camera);
  return [...ensemble];
}

// The filter omits hidden cameras (except temporary reveal), sorted in
// natural order: "2-Stairs" before "10-Basement".
function camerasConnues() {
  return toutesLesCameras().filter((nom) => !estMasquee(nom)).sort(comparerNoms);
}

// Settings window list: all cameras, hidden or not, including those only
// seen live and those hidden then gone.
function camerasPourMasquage() {
  const ensemble = new Set([...toutesLesCameras(), ...camerasMasquees]);
  for (const s of (system && system.systems) || []) {
    for (const c of s.cameras) ensemble.add(c.name);
  }
  return [...ensemble].sort(comparerNoms);
}

// c.battery (cameraCard, below) is set only if Blink publishes one for
// this camera: a permanently wired Blink Mini has none - seen in practice
// (2026-09-04): the long wake text ("a battery camera is slower") still
// showed on it, false for that model. Unknown (system not loaded yet) =>
// true: keep the old text rather than wrongly claim the opposite.
function cameraSurBatterie(name) {
  for (const s of (system && system.systems) || []) {
    const c = s.cameras.find((cam) => cam.key === name);
    if (c) return Boolean(c.battery);
  }
  return true;
}

function fill(select, values, all) {
  const kept = select.value;
  select.replaceChildren();
  const optionToutes = document.createElement("option");
  optionToutes.value = "";
  optionToutes.textContent = all;
  select.append(optionToutes);
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.append(option);
  }
  if (values.includes(kept)) select.value = kept;
}

// "clips" and "direct" share data.clips (see collect(), serve.py): each
// entry's "kind" field decides which of the two views shows it.
function visible() {
  const kindAttendu = $("view").value === "direct" ? "direct" : "clip";
  return data.clips.filter((c) =>
    c.kind === kindAttendu &&
    (!$("camera").value || c.camera === $("camera").value) &&
    !estMasquee(c.camera) &&
    ($("showOut").checked || !c.excluded));
}

function duration(seconds) {
  const s = Math.round(seconds);
  const parts = [Math.floor(s / 3600), Math.floor(s / 60) % 60, s % 60];
  return (parts[0] ? parts : parts.slice(1))
    .map((v, i) => (i ? String(v).padStart(2, "0") : v)).join(":");
}

function vueRotation() {
  const kind = $("view").value;
  return kind === "rotation" || kind === "alllive";
}

function vueDirecte() {
  const kind = $("view").value;
  return kind === "live" || kind === "alllive" || kind === "rotation";
}

function render() {
  heuresDePassage();
  const kind = $("view").value;
  // Clips and Direct Clips share data.clips and therefore renderClips()
  // (2026-09-04, same presentation requested for both); Daily/Weekly/
  // Monthly stay on renderVideos(kind), Live on renderLive().
  if (kind === "rotation" || kind === "alllive") {
    $("outLabel").hidden = true;
    $("filtreButton").hidden = true;
    $("periodeSection").hidden = true;
    $("filtreResume").textContent = "";
    $("filtreCompte").textContent = "";
    if (!system) return loadSystem(false);
    if (system.error) {
      $("list").innerHTML = `<p class="empty">${h(system.error)}</p>`;
      return;
    }
    // Already cycling: do not rebuild / restart on incidental render().
    if (rotationActive) return;
    startRotation();
    return;
  }
  if (kind === "live") return renderLive();
  const carteClips = kind === "clips" || kind === "direct";
  $("outLabel").hidden = !carteClips;
  // Same scope as the camera: Clips, Direct Clips, Daily, Weekly, Monthly
  // all filter there; only Live has no use for it. The period applies to
  // Clips and Direct Clips (/api/clips, both from data.clips):
  // daily/weekly/monthly still do not read it.
  $("filtreButton").hidden = kind === "live";
  $("periodeSection").hidden = !carteClips;
  // Group by day only makes sense for dailies: weeklies and monthlies have
  // one file per week/month per camera, so grouping by day would change
  // nothing (issue #14: "week and month makes no sense" for this mode,
  // per the reporter). Nor with a chosen camera (issue #37, see
  // regroupable()).
  majGroupBy();
  $("filtreResume").textContent = carteClips ? resumeFiltre() : "";
  // The count only matters for clips/direct; renderClips() resets it each
  // render, but leaving this view must clear it, not leave it stale behind
  // another view.
  if (!carteClips) $("filtreCompte").textContent = "";
  if (!carteClips) nettoyerLecteursClips();
  if (kind === "live") return renderLive();
  if (kind === "pictures") return renderPictures();
  return carteClips ? renderClips() : renderVideos(kind);
}

// --- live view and arming --------------------------------------------------
let system = null;
let rafraichirVignettes = true;

async function loadSystem(force) {
  if (system && !force) {
    return vueRotation() ? render() : renderLive();
  }
  // The view may have changed between this call firing and resolving
  // (quick switch to Clips, or early fire on load before the default view
  // is set): only touch the DOM if Live/Rotation is still shown, else the
  // late response would overwrite a clip list already on screen without
  // the dropdown hinting at it.
  if (vueDirecte() && !nomsDirectsActifs().length) {
    $("list").innerHTML = `<p class="empty">${t("live.querying")}</p>`;
    $("count").textContent = "";
  }
  try {
    system = await lireJSON(await fetch("/api/system"));
  } catch (error) {
    system = { error: String(error) };
  }
  if ($("view").value === "live") renderLive();
  else if (vueRotation()) render();
}

// Work started by background loops, outside this page: download and
// assembly publish progress in a file, the only way for the page to learn
// the machine is busy. While it runs, the button stays disabled: a second
// job would wait on the same lock and make no progress.
let travailEnCours = false;
let travailVisible = false;
let actualisationLocale = false;
// Wait loop for a restart after clicking "Update" (see its onclick below).
// montrerTravail() stops it from outside when the update finishes without
// restarting (phase.update_noop): without that, it would wait for a server
// return that never comes.
let miseAJourAttente = null;
let generationMaj = 0;

// The server never knows the displayed language (per-tab choice in
// localStorage): a phase label therefore always arrives in French, with a
// stable key when a translation exists. Missing or unknown key: show the
// received text as-is rather than an empty string, which would hide real
// work in progress (e.g. real bug: "Downloading clips" stuck in French
// regardless of chosen language).
function libellePhase(cle, texteBrut, valeurs) {
  if (!cle || !((I18N[_lang] && I18N[_lang][cle]) || I18N.fr[cle])) return texteBrut;
  return valeurs ? tf(cle, valeurs) : t(cle);
}

function montrerTravail(travail) {
  if (actualisationLocale) return;    // our own bar already speaks
  const visible = !!(travail && travail.quoi);
  if (!visible) {
    if (travailVisible) {
      $("work").classList.remove("on");
      rechargerEnArrierePlan();
    }
    travailEnCours = false;
    travailVisible = false;
    $("refresh").disabled = false;
    return;
  }
  if (travail.cle === "phase.update_noop") {
    // The update finished without stopping or restarting the server
    // (already up to date, missing archive, git pull refused...). The
    // button's onclick only waits for disappear-then-return: without this
    // explicit unlock, it would stay stuck on "Updating…" forever while
    // nothing is running.
    if (miseAJourAttente !== null) {
      clearInterval(miseAJourAttente);
      miseAJourAttente = null;
    }
    const bouton = $("update");
    delete bouton.dataset.encours;
    bouton.disabled = false;
    montrerMaj({ version: bouton.dataset.version });
    gelerPendantMaj(false);
  }
  const termine = !!travail.termine;
  const actif = travail.actif === undefined ? !termine : !!travail.actif;
  const etaitActif = travailEnCours;
  travailEnCours = actif;
  travailVisible = true;
  $("refresh").disabled = actif;
  $("work").classList.add("on");
  const total = travail.total || 0;
  const fait = travail.fait || 0;
  const quoi = travail.cle === "phase.update_download"
    ? libellePhase(travail.cle, travail.quoi, { mo: Math.round(total) })
    : libellePhase(travail.cle, travail.quoi);
  // total === 1: a single step, not a progress measure (e.g. "already up
  // to date", see phase.update_noop) - a "1/1 (100 %)" fraction would add
  // nothing.
  if (total > 1) {
    $("bar").max = total;
    $("bar").value = fait;
    const courant = fait >= total ? total : fait + 1;
    $("phase").textContent =
      `${quoi} ${courant}/${total} (${Math.round((fait / total) * 100)} %)`;
  } else {
    $("bar").removeAttribute("value");
    $("phase").textContent = quoi;
  }
  // The N/N held for ten seconds is visible, but is no longer a lock: a
  // new click remains possible. If the page had seen active work, its end
  // triggers the same refresh as a direct disappearance.
  if (termine && etaitActif && !actif) rechargerEnArrierePlan();
}

// A published version newer than the one running: the button appears and
// does everything, from download to relaunch. During the operation the
// server stops and comes back: the page waits for its return, then reloads.
function montrerMaj(neuve) {
  const bouton = $("update");
  bouton.hidden = !(neuve && neuve.version);
  if (bouton.hidden || bouton.dataset.encours) return;
  bouton.dataset.version = neuve.version;
  bouton.textContent = tf("update.installing", { version: neuve.version });
  bouton.title = tf("update.title", { version: neuve.version });
}

// Controls that can send a request or open a new state (settings, filter,
// selection, live view) while an update is in flight: frozen for the
// operation, otherwise a click elsewhere can hit a server that is stopping
// then coming back under another version. "refresh" already has its own
// rule (tied to any active work in montrerTravail) and need not be here.
const CONTROLES_GELES_PENDANT_MAJ = ["reglagesButton", "filtreButton", "applyButton", "view"];

function gelerPendantMaj(gele) {
  for (const id of CONTROLES_GELES_PENDANT_MAJ) {
    const element = $(id);
    if (element) element.disabled = gele;
  }
}

// Server state as seen from the page, to wait for a relaunch (update,
// Apply, Restart):
//   "absent"  connection refused, or 502/503/504: a proxy (nginx...) in
//             front of the server answers in its place while it is down,
//             without fetch() throwing;
//   "autre"   403: another process answers, with its own token. That is
//             the relaunched server, faster than the poll could see it
//             absent;
//   "present" any other response.
async function etatServeur() {
  try {
    const reponse = await fetch("/api/status", { cache: "no-store" });
    if (reponse.status === 403) return "autre";
    if (reponse.status >= 502 && reponse.status <= 504) return "absent";
    return "present";
  } catch (erreur) {
    return "absent";
  }
}

// One poll step: relaunch is done and the page reloads when the server
// returns after being seen absent, or as soon as another process answers
// (issue #35: the page stayed on the old server and had to be reloaded by
// hand). suivi.parti says whether absence was seen.
async function sonderRelance(suivi) {
  const etat = await etatServeur();
  if (etat === "absent") suivi.parti = true;
  else if (etat === "autre" || suivi.parti) location.reload();
}

$("update").onclick = async () => {
  const generation = ++generationMaj;
  const bouton = $("update");
  bouton.dataset.encours = "1";
  bouton.disabled = true;
  bouton.textContent = t("update.updating");
  // The button stays usable until the server confirms launch: a rejected
  // request (server unreachable) or unreadable response left it greyed
  // until page reload (audit 2026-10-02, B13).
  let resultat;
  try {
    const reponse = await fetch("/api/update", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    resultat = await lireJSON(reponse);
  } catch (erreur) {
    resultat = { error: tf("update.failed", { erreur: String(erreur) }) };
  }
  if (resultat.error) {
    alert(resultat.error);
    bouton.disabled = false;
    delete bouton.dataset.encours;
    montrerMaj({ version: bouton.dataset.version });
    return;
  }
  gelerPendantMaj(true);
  $("phase").textContent = tf("update.progress", { version: resultat.version });
  $("bar").removeAttribute("value");
  $("work").classList.add("on");
  $("refresh").disabled = true;
  // The server will disappear then return under its new version. We probe
  // its presence, and its return is the finish line. A conclusion that
  // restarts nothing (already up to date, failure...) never triggers this:
  // phase.update_noop, seen by montrerTravail(), then stops this loop and
  // unlocks the button from outside.
  const suivi = { parti: false };
  miseAJourAttente = setInterval(() => sonderRelance(suivi), 2000);
  // Last safety net if even phase.update_noop never arrived (e.g. child
  // process died before writing anything): better an unexplained unlock
  // than a permanently dead button.
  setTimeout(() => {
    if (generation !== generationMaj || miseAJourAttente === null) return;
    clearInterval(miseAJourAttente);
    miseAJourAttente = null;
    bouton.disabled = false;
    delete bouton.dataset.encours;
    gelerPendantMaj(false);
  }, 900000);
};

let dernierRechargementAuto = 0;

// Reload triggered by the background (new clip detected, or end of a job)
// rather than an explicit click: never more than once every 60 seconds,
// the normal watch rhythm of veiller(). During a large batch in progress,
// each new clip or travailEnCours flip could otherwise trigger a full grid
// reload (all video thumbnails destroyed and rebuilt), perceived as
// flickering (seen in practice, 2026-08-27).
function rechargerEnArrierePlan() {
  const maintenant = Date.now();
  if (maintenant - dernierRechargementAuto < 60000) return;
  // load() clears any Exclude/Delete selection not yet applied (see its
  // comment): correct for a user-requested reload, but here the caller is
  // a background event (tracked work ending, new clips) unrelated to what
  // they are doing. Defer instead of silently wiping; the next
  // heuresDePassage()/montrerTravail() pass will retry.
  const { exclure, inclure, supprimer } = calculerSelection();
  if (exclure.length + inclure.length + supprimer.length > 0) return;
  // Same reason as freezing renderLive() for an active live view (see its
  // comment): rebuilding #list destroys any clip <video> being played,
  // perceived as an untimely refresh mid-viewing (reported by Nico,
  // 2026-09-12). Defer, as for a pending selection above.
  if ([...$("list").querySelectorAll("video")].some((v) => !v.paused)) return;
  dernierRechargementAuto = maintenant;
  load();
}

async function heuresDePassage() {
  let etat = {};
  try {
    etat = await lireJSON(await fetch("/api/passages"));
  } catch (erreur) { return; }
  montrerMaj(etat.maj);
  const vus = etat.passages || {};
  // Clips arrived since the page was loaded: we say so, and you click
  // Refresh. The list does not reshuffle under someone reading it.
  //
  // Compare against total_known.clip (the real registry total, never
  // bounded by the active filter), never data.clips.length: that only
  // counts what the current filter shows (one camera, a narrow period…),
  // not everything known. Comparing the real total to a filtered subset
  // announced hundreds of "new" clips that were not new (seen in practice,
  // 2026-08-27). total_known has been a per-kind object since 2026-09-04:
  // subtracting it whole gave NaN, and new clips were never announced
  // again (audit 26/09, B03). Before the first gallery load, nothing to
  // compare.
  const connus = data && data.total_known ? data.total_known.clip : undefined;
  const arrives = typeof connus === "number"
    ? Math.max(0, (etat.clips || 0) - connus) : 0;
  // A single time, the most recent of the three. Detailing the most overdue
  // verb weighed down the line for a rare case.
  const dates = ["watch", "download", "merge"].filter((cle) => vus[cle]);
  if (!dates.length) return;

  const instant = (cle) => new Date(vus[cle]).getTime();
  const plusRecent = dates.reduce((a, b) => (instant(a) > instant(b) ? a : b));
  // Choice remembered across displays: a preference you must re-check every
  // open would not be one. During a job, do not reload: the list would
  // change underfoot with each assembled video.
  if (arrives && $("auto").checked && !travailEnCours) {
    rechargerEnArrierePlan();
    return;
  }
  const nouveaux = arrives
    ? tf(arrives > 1 ? "passages.new.many" : "passages.new.one", { n: arrives })
    : "";
  $("passages").textContent =
    tf("passages.updated", { heure: vus[plusRecent].slice(11, 16) }) + nouveaux;
}

async function etatDuTravail() {
  if ($("update").dataset.encours && miseAJourAttente === null) return;
  const generation = generationMaj;
  try {
    const etat = await lireJSON(await fetch("/api/travail", { cache: "no-store" }));
    if (generation === generationMaj) montrerTravail(etat.travail);
  } catch (erreur) { /* the next pass will retry */ }
}

function nomsDirectsActifs() {
  return [...new Set([
    ...Object.keys(LIVE_PENDING), ...Object.keys(MSE_ABORT),
    ...Object.keys(WEBRTC_ABORT), ...Object.keys(WEBRTC_PC),
    ...Object.keys(WEBRTC_SESSION),
  ])];
}

function peindreGrilleLive() {
  if (!system || system.error) return;
  // Hidden cameras excluded; the rest in natural name order.
  const camerasVisibles = (s) => s.cameras
    .filter((c) => !estMasquee(c.name))
    .sort((a, b) => comparerNoms(a.name, b.name));
  const cameras = system.systems.reduce((n, s) => n + camerasVisibles(s).length, 0);
  const armed = system.systems.reduce(
    (n, s) => n + camerasVisibles(s).filter((c) => c.armed).length, 0);
  $("count").textContent = tf("live.count", { n: cameras, m: armed });

  $("list").innerHTML = system.systems.map((s) => `
    <h2>
      ${h(s.name)}
      <span class="sub tiny">${h([s.module,
        s.module_firmware ? tf("camera.firmware", { v: s.module_firmware }) : null,
        s.module_serial].filter(Boolean).join(" · "))}</span>
      ${estAdmin() ? `<button class="act admin-only ${s.armed ? "in" : "out"}"
              data-action="arm" data-scope="system" data-name="${h(s.key)}"
              data-armed="${!s.armed}">
        ${h(s.armed ? t("system.armed") : t("system.disarmed"))}
      </button>` : ""}
    </h2>
    ${camerasVisibles(s).length
      ? `<div class="grid wide">${camerasVisibles(s).map((c) => cameraCard(c, s.armed)).join("")}</div>`
      : `<p class="sub tiny">${h(t("live.toutesMasquees"))}</p>`}
  `).join("");
  if (rafraichirVignettes) actualiserVignettes();
  rafraichirVignettes = false;
}

function renderLive() {
  // Rebuilding the grid replaces all its HTML, including an in-progress
  // live view: the <video> tag and its AbortController would survive as
  // orphans under a fresh DOM that no longer references them (seen in
  // practice: a background clip arrival is enough to trigger this refresh
  // while a live view runs, which then seems to stop and never resume).
  // An active live view therefore freezes the grid until it stops.
  if (nomsDirectsActifs().length) return;
  if (!system) return loadSystem(false);
  if (system.error) {
    $("list").innerHTML = `<p class="empty">${h(system.error)}</p>`;
    return;
  }
  peindreGrilleLive();
}

function actualiserVignettes() {
  for (const cadre of $("list").querySelectorAll(".live")) {
    const still = cadre.querySelector(".still");
    const name = cadre.querySelector('[data-action="watch-live"]')?.dataset.name;
    if (!still || !name) continue;
    // Keep the last image visible during the Blink request.
    fetch(urlVignette(name, true)).then((response) => {
      if (response.ok && still.isConnected) still.src = urlVignette(name);
    }).catch(() => {});
  }
}

// A camera reading arrives dated (ISO, to the minute, server time), with
// "is it today?": the page composes the text in its language and the
// browser's date format, like dateLocale() and dateSnapshot(). The server
// used to send "28/09 à 14:30", hard-coded French (issue #37), and the page
// guessed which phrase to use by searching for that "à" inside.
function dateReleve(c, locale) {
  const m = (c.measured_at || "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!m) return null;
  const heure = `${m[4]}:${m[5]}`;
  if (c.measured_today) return tf("camera.measured.at", { v: heure });
  const jour = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])).toLocaleDateString(
    locale, { day: "2-digit", month: "2-digit", timeZone: "UTC" });
  return tf("camera.measured.on", { d: jour, h: heure });
}

// Temperature: browser decimal separator, not a hard-coded comma.
function degres(valeur, locale) {
  return valeur.toLocaleString(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

function cameraCard(c, systemArmed) {
  // A reading older than one hour is dated, and an offline camera is
  // flagged as such: its last known temperature may be weeks old.
  const vieille = c.age_seconds !== null && c.age_seconds > 3600;
  const num = (v) => v !== null && v !== undefined;
  const releve = [
    c.battery ? tf("camera.battery", { v: c.battery }) + (num(c.battery_signal) ? ` (${c.battery_signal})` : "") : null,
    num(c.temperature) ? `${degres(c.temperature)} °C` : null,
    num(c.wifi) ? tf("camera.wifi", { v: c.wifi }) : null,
    num(c.lfr) ? tf("camera.lfr", { v: c.lfr }) : null,
  ].filter(Boolean).join(" · ");
  const date = dateReleve(c);
  const details = [
    c.offline ? t("camera.offline") : null,
    releve || null,
    date,
    c.armed && !systemArmed ? t("camera.noeffect") : null,
  ].filter(Boolean).join(" · ");
  return `<div class="card ${c.offline ? "out" : ""}">
    <div class="live" id="live-${cssId(c.key)}">${repos(c.key, t("watch.live"))}</div>
    <div class="meta">
      <div>
        <div class="time">${h(c.name)} <strong class="rotation-countdown" data-for="${h(c.key)}" hidden></strong></div>
        <div class="sub">${h(details || t("camera.none"))}</div>
        <div class="sub tiny">${h([c.model,
          c.firmware ? tf("camera.firmware", { v: c.firmware }) : null, c.serial,
          c.clips_source ? tf("camera.clipssource", { v: c.clips_source }) : t("camera.noclips"),
        ].filter(Boolean).join(" · "))}</div>
      </div>
      ${estAdmin() ? `<button class="act admin-only ${c.armed ? "in" : "out"}"
              data-action="arm" data-scope="camera" data-name="${h(c.key)}"
              data-armed="${!c.armed}">
        ${h(c.armed ? t("camera.detection.on") : t("camera.detection.off"))}
      </button>
      <button class="act grouped admin-only" title="${h(t("camera.wake.title"))}"
              data-action="wake" data-name="${h(c.key)}">${h(t("camera.wake"))}</button>
      <button class="act grouped admin-only" title="${h(t("camera.snapshot.title"))}"
              data-action="snapshot" data-name="${h(c.key)}">${h(t("camera.snapshot"))}</button>
      <label class="act grouped continu admin-only"
             title="${h(t(c.battery ? "camera.continuous.title.battery" : "camera.continuous.title"))}">
        <input type="checkbox" data-action="continu" data-name="${h(c.key)}"${directContinu(c.key) ? " checked" : ""}>
        ${h(t("camera.continuous"))}
      </label>` : ""}
    </div>
  </div>`;
}

const cssId = (name) => name.replace(/[^\w-]/g, "_");

// Only link in the whole chain the server cannot observe itself: a
// successful SDP negotiation or connectionState "connected" does not prove
// an image actually reached the screen (decode, <video> render — all
// happen here). Best-effort by design: a lost signal must never disturb
// the displayed live view; only direct.log suffers.
function signalerDirect(name, evenement, detail, sessionId = WEBRTC_SESSION[name]) {
  fetch(`/api/direct-signal/${encodeURIComponent(name)}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ evenement, detail: detail || "", session_id: sessionId }),
  }).catch(() => {});
}

// A failed live view must restore its original button: leaving "Stop"
// would imply a stream is running, with no way to restart. Removing the
// <img> also closes the connection left open on the server.
function failWatch(name, message, sessionId) {
  signalerDirect(name, "abandon", message, sessionId);
  const box = $("live-" + cssId(name));
  // Rotation / All Live drive themselves: show the error briefly, then
  // skip to the next camera — never leave a "View live" button that needs
  // a click.
  if (rotationActive && vueRotation()) {
    if (box) box.innerHTML = `<p class="hint overlay">${h(message)}</p>`;
    if (rotationTimer) clearTimeout(rotationTimer);
    rotationTimer = setTimeout(() => {
      stopWatch(name);
      cycleNext();
    }, 2500);
    return;
  }
  if (box) box.innerHTML = repos(name, t("watch.retry")) + `<p class="hint overlay">${h(message)}</p>`;
}

// Neither <img> (MJPEG) nor the MSE live <video> carries the controls
// attribute (a scrubber makes no sense on an endless stream): fullscreen
// therefore cannot come for free from the browser as with recorded clips.
// Dedicated button rather than a click on the whole card: tried first,
// judged unclear in use.
function toggleFullscreen(name) {
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    return;
  }
  const box = $("live-" + cssId(name));
  (box.requestFullscreen || box.webkitRequestFullscreen).call(box);
}

// Shared by both protocols since explicit MSE stop was added (2026-09-06):
// /api/arreter-direct already finds the matching session in
// DIRECT_WEBRTC_SESSION or DIRECT_MSE_SESSION alike; the request body was
// never WebRTC-specific.
function arreterSessionDirect(sessionId, beacon = false) {
  if (!sessionId) return;
  const body = JSON.stringify({ session_id: sessionId });
  if (beacon) {
    try {
      if (navigator.sendBeacon(
        `/api/arreter-direct?token=${encodeURIComponent(BLINK_TOKEN)}`,
        new Blob([body], { type: "application/json" }),
      )) return;
    } catch (erreur) { /* fetch keepalive is the fallback if Beacon is unavailable */ }
  }
  fetch("/api/arreter-direct", { method: "POST", keepalive: true,
    headers: { "Content-Type": "application/json" }, body }).catch(() => {});
}

function stopWatch(name, beacon = false) {
  // The card may have vanished underfoot (view refresh during live):
  // resetting to idle is cosmetic, but cutting the streams below must never
  // depend on it.
  const box = $("live-" + cssId(name));
  // In solo rotation the page advances itself: do not restore a "View live"
  // button that would require a click between cameras. All Live keeps the
  // dashboard tiles, so idle cameras get their button back.
  if (box && !(rotationActive && $("view").value === "rotation")) {
    box.innerHTML = repos(name, t("watch.live"));
  }
  const autoStopTimer = LIVE_AUTO_STOP_TIMER[name];
  if (autoStopTimer) { clearTimeout(autoStopTimer); delete LIVE_AUTO_STOP_TIMER[name]; }
  const pending = LIVE_PENDING[name];
  if (pending) { pending.abort(); delete LIVE_PENDING[name]; }
  const controller = MSE_ABORT[name];
  if (controller) { controller.abort(); delete MSE_ABORT[name]; }
  // Capture the id before aborting: the attempt's finally may clear its
  // state, and a new attempt may already have started. Without this
  // explicit signal, a camera silent after the abort() above only freed
  // the module after LIVE_MAX_SECONDS (300 s): send_live_mse() detects a
  // closed connection only when writing the next block, which might never
  // arrive (real bug).
  const mseSessionId = MSE_SESSION[name];
  if (mseSessionId) {
    delete MSE_SESSION[name];
    arreterSessionDirect(mseSessionId, beacon);
  }
  // webrtcController cuts a retry in progress (between attempts, no active
  // pc yet); pc closes the current attempt if a negotiation is already
  // underway. Both may briefly coexist; neither is guaranteed present at
  // a given moment.
  const webrtcController = WEBRTC_ABORT[name];
  const sessionId = WEBRTC_SESSION[name];
  if (sessionId) {
    delete WEBRTC_SESSION[name];
    arreterSessionDirect(sessionId, beacon);
  }
  if (webrtcController) { webrtcController.abort(); delete WEBRTC_ABORT[name]; }
  const pc = WEBRTC_PC[name];
  if (pc) {
    pc.close();
    delete WEBRTC_PC[name];
  }
}

// --- MSE/fMP4: remux without re-encode, <video> decoded by the browser ---
// Unlike <img>, a fetch() does not stop on its own when the tag is dropped:
// it needs its own AbortController, kept here per camera so stopWatch()
// can cut it.
const MSE_ABORT = {};

// "Continuous live", per camera (cameraCard checkbox): each session Blink
// closes by itself (after about six minutes of video, measured 2026-09-28)
// is immediately followed by a new one, without the ten-minute budget of
// ordinary retries, until Stop is clicked. Unchecked by default: on a
// battery camera, endless live drains it fast. Kept across visits, like
// the filter; without storage (private browsing), the choice lasts for
// the open page.
const CLE_DIRECT_CONTINU = "blink2video.directContinu";
const DIRECT_CONTINU_REVERIFICATION_MS = 60 * 1000;
const _directsContinus = new Set((() => {
  try {
    const liste = JSON.parse(localStorage.getItem(CLE_DIRECT_CONTINU) || "[]");
    return Array.isArray(liste) ? liste.filter((nom) => typeof nom === "string") : [];
  } catch (erreur) {
    return [];
  }
})());

function directContinu(name) {
  // Rotation owns the dwell time: never auto-reconnect a camera mid-cycle.
  if (rotationActive) return false;
  return _directsContinus.has(name);
}

function choisirDirectContinu(name, actif) {
  if (actif) _directsContinus.add(name);
  else _directsContinus.delete(name);
  try {
    localStorage.setItem(CLE_DIRECT_CONTINU, JSON.stringify([..._directsContinus]));
  } catch (erreur) { /* storage unavailable: choice lasts for this page */ }
}

// Total live budget timer (MSE_BUDGET_TOTAL_MS, WEBRTC_BUDGET_TOTAL_MS): on
// expiry, `surEcoulement`, except in continuous live where it rechecks each
// minute; unchecking mid-stream thus still applies the already-elapsed
// budget. Returns an object whose `id` is the current timer, for
// clearTimeout.
function armerBudget(name, delaiMs, surEcoulement) {
  const minuterie = { id: null };
  const verifier = () => {
    if (directContinu(name)) {
      minuterie.id = setTimeout(verifier, DIRECT_CONTINU_REVERIFICATION_MS);
      return;
    }
    surEcoulement();
  };
  minuterie.id = setTimeout(verifier, delaiMs);
  return minuterie;
}

// One id per watchMse() (not per reconnect attempt, like WEBRTC_SESSION):
// sent to the server (send_live_mse) so it can interrupt its send loop on
// explicit request rather than wait up to 300 s for a camera block to
// detect the fetch was cancelled (real bug: tab closed during camera
// silence, module stayed busy for minutes).
const MSE_SESSION = {};
// Blink sometimes closes the session mid-stream, unrelated to this project
// (seen in practice: between a few frames and ~1 MB sent, then the relay
// connection drops mid-packet - cause identified in blinkpy, see
// blink_engine.py). A manual retry almost always works: automate it. The
// failure counter only rises on a retry that delivered no frame; once a
// frame arrives, it resets to zero, so a live view that works in fits and
// starts is not abandoned. MSE_BUDGET_TOTAL_MS still caps total duration: a
// forgotten open tab must not restart the camera forever. The delay between
// attempts is not cosmetic: Blink allows only one live session per account
// at a time and takes time to free the previous one server-side; a too-fast
// retry hits that unreleased session, not a real problem.
const MSE_MAX_ECHECS_A_VIDE = 5;
const MSE_DELAI_RECONNEXION_MS = 3000;
const MSE_DELAI_MODULE_OCCUPE_MS = 10000;
const MSE_BUDGET_TOTAL_MS = 10 * 60 * 1000;
// sourceopen is purely local and should be near-instant. The HTTP response
// covers up to 45 s to open Blink then 40 s for the moov; its bound is
// therefore deliberately wider. Once headers are received, the browser
// still has one minute to decode its first real frame.
const MSE_DELAI_SOURCEOPEN_MS = 10 * 1000;
const MSE_DELAI_REPONSE_MS = 110 * 1000;
const MSE_DELAI_PREMIERE_IMAGE_MS = 60 * 1000;
// The server itself cuts a session after 420 s (LIVE_MAX_SECONDS); Blink
// usually around six minutes. This margin also interrupts read() or
// updateend if the browser does not propagate its close.
const MSE_DUREE_APRES_LECTURE_MS = 450 * 1000;
const MSE_DELAI_DECODAGE_FINAL_MS = 1000;

function erreurAnnulationMse() {
  // Error + name stays understood by browsers that do not yet accept the
  // two-argument DOMException constructor (notably older engines still
  // seen on Windows 7).
  const error = new Error("Aborted");
  error.name = "AbortError";
  return error;
}

function attendreOuAbandon(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(erreurAnnulationMse()); return; }
    let id = null;
    const nettoyer = () => {
      if (id !== null) clearTimeout(id);
      signal.removeEventListener("abort", abandonner);
    };
    const abandonner = () => {
      nettoyer();
      reject(erreurAnnulationMse());
    };
    signal.addEventListener("abort", abandonner, { once: true });
    id = setTimeout(() => {
      nettoyer();
      resolve();
    }, ms);
  });
}

// Unlike fetch(), an arbitrary Promise (including reader.read() depending
// on the engine) need not reject when its signal is aborted. This wrapper
// ensures each wait yields; the caller can then explicitly cancel the
// reader in its finally.
function operationOuAbandon(operation, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(erreurAnnulationMse()); return; }
    let terminee = false;
    const finir = (suite, valeur) => {
      if (terminee) return;
      terminee = true;
      signal.removeEventListener("abort", abandonner);
      suite(valeur);
    };
    const abandonner = () => finir(reject, erreurAnnulationMse());
    signal.addEventListener("abort", abandonner, { once: true });
    try {
      Promise.resolve(operation()).then(
        (valeur) => finir(resolve, valeur),
        (error) => finir(reject, error)
      );
    } catch (error) {
      finir(reject, error);
    }
  });
}

function creerTentativeMse(signalParent) {
  const controller = new AbortController();
  let expiree = false;
  let id = null;
  const expirer = () => {
    expiree = true;
    controller.abort();
  };
  const propagerArret = () => controller.abort();
  const armer = (ms) => {
    if (id !== null) clearTimeout(id);
    id = setTimeout(expirer, ms);
  };

  if (signalParent.aborted) controller.abort();
  else {
    signalParent.addEventListener("abort", propagerArret, { once: true });
    armer(MSE_DELAI_SOURCEOPEN_MS);
  }

  return {
    signal: controller.signal,
    abandonner: () => controller.abort(),
    attendreServeur: () => {
      if (!controller.signal.aborted) armer(MSE_DELAI_REPONSE_MS);
    },
    attendrePremiereImage: () => {
      if (!controller.signal.aborted) armer(MSE_DELAI_PREMIERE_IMAGE_MS);
    },
    confirmerLecture: () => {
      if (!controller.signal.aborted) armer(MSE_DUREE_APRES_LECTURE_MS);
    },
    aExpire: () => expiree,
    nettoyer: () => {
      clearTimeout(id);
      signalParent.removeEventListener("abort", propagerArret);
    },
  };
}

function attendreOuvertureMse(mediaSource, signal) {
  if (mediaSource.readyState === "open") return Promise.resolve();
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(erreurAnnulationMse()); return; }
    let terminee = false;
    const nettoyer = () => {
      mediaSource.removeEventListener("sourceopen", ouverte);
      mediaSource.removeEventListener("sourceclose", fermee);
      mediaSource.removeEventListener("error", fermee);
      signal.removeEventListener("abort", abandonner);
    };
    const finir = (suite, valeur) => {
      if (terminee) return;
      terminee = true;
      nettoyer();
      suite(valeur);
    };
    const ouverte = () => finir(resolve);
    const fermee = () => finir(reject, new Error(t("watch.noimage")));
    const abandonner = () => finir(reject, erreurAnnulationMse());
    mediaSource.addEventListener("sourceopen", ouverte, { once: true });
    mediaSource.addEventListener("sourceclose", fermee, { once: true });
    mediaSource.addEventListener("error", fermee, { once: true });
    signal.addEventListener("abort", abandonner, { once: true });
  });
}

function ajouterSegmentMse(sourceBuffer, value, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(erreurAnnulationMse()); return; }
    let terminee = false;
    const nettoyer = () => {
      sourceBuffer.removeEventListener("updateend", ajoute);
      sourceBuffer.removeEventListener("error", echoue);
      signal.removeEventListener("abort", abandonner);
    };
    const finir = (suite, valeur) => {
      if (terminee) return;
      terminee = true;
      nettoyer();
      suite(valeur);
    };
    const ajoute = () => finir(resolve);
    const echoue = () => finir(reject, new Error(t("watch.noimage")));
    const abandonner = () => finir(reject, erreurAnnulationMse());
    sourceBuffer.addEventListener("updateend", ajoute, { once: true });
    sourceBuffer.addEventListener("error", echoue, { once: true });
    signal.addEventListener("abort", abandonner, { once: true });
    try {
      sourceBuffer.appendBuffer(value);
    } catch (error) {
      finir(reject, error);
    }
  });
}

// One connection -> stream -> end cycle. Returns whether at least one
// frame arrived (used by watchMse to retry or abandon).
async function connecterMse(name, video, signalGlobal, texteAttente, t0, reveilInitial, sessionId) {
  const box = $("live-" + cssId(name));
  let hint = $("hint-" + cssId(name));
  if (box && !hint) {
    box.insertAdjacentHTML(
      "beforeend",
      `<p class="hint overlay" id="hint-${cssId(name)}">${h(texteAttente)}</p>`
    );
    hint = $("hint-" + cssId(name));
  }
  if (hint) hint.textContent = texteAttente;

  // Counter during the very first wake only (not reconnects after failure):
  // without it, the 15-25 s wake of a battery camera looks like a hang.
  // Threshold and text switch taken as-is from the old MJPEG live (watch(),
  // removed by 7339f85; see AUDIT-2026-08-13.md 28.15).
  let minuteur = null;
  if (hint && reveilInitial) {
    minuteur = setInterval(() => {
      const s = Math.round((performance.now() - t0) / 1000);
      const lent = s > 20 && cameraSurBatterie(name);
      hint.textContent = tf(lent ? "watch.waking.slow" : "watch.waking.seconds", { s });
    }, 500);
  }

  const tentative = creerTentativeMse(signalGlobal);
  const signal = tentative.signal;
  let mediaSource = null;
  let url = null;
  let response = null;
  let reader = null;
  let sourceBuffer = null;
  let lecture = false;
  let erreurMedia = null;

  const confirmerLecture = () => {
    if (lecture) return;
    lecture = true;
    signalerDirect(name, "image affichee (mse)");
    tentative.confirmerLecture();
    if (hint) hint.remove();
    if (window.__mseMetric == null) window.__mseMetric = performance.now() - t0;
    signalerVideoRotation(name);
  };
  const signalerErreurMedia = () => {
    if (signal.aborted) return;
    erreurMedia = new Error(t("watch.noimage"));
    tentative.abandonner();
  };
  try {
    mediaSource = new MediaSource();
    url = URL.createObjectURL(mediaSource);
    video.addEventListener("loadeddata", confirmerLecture);
    video.addEventListener("playing", confirmerLecture);
    video.addEventListener("error", signalerErreurMedia);
    const ouverture = attendreOuvertureMse(mediaSource, signal);
    video.src = url;
    await ouverture;
    tentative.attendreServeur();
    mediaSource.addEventListener("error", signalerErreurMedia);

    response = await operationOuAbandon(
      () => fetch(
        `/live-mse/${encodeURIComponent(name)}?session_id=${encodeURIComponent(sessionId)}`,
        { signal },
      ), signal
    );
    if (!response.ok) {
      let message = response.status === 409
        ? t("watch.refused.retry")
        : tf("watch.refused.code", { code: response.status });
      // A 409 describes the current lock, not LAST_LIVE_ERROR. Hitting that
      // endpoint here often re-showed the camera's previous 503.
      if (response.status !== 409) {
        try {
          const reponseErreur = await operationOuAbandon(
            () => fetch("/api/live-error", { cache: "no-store", signal }), signal
          );
          const info = await operationOuAbandon(() => lireJSON(reponseErreur), signal);
          if (info.camera === name && info.message) message = info.message;
        } catch (error) { /* keep the generic message */ }
      }
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }

    const codec = response.headers.get("X-Codec") || "avc1.42E01E";
    const mimeType = `video/mp4; codecs="${codec}"`;
    if (!MediaSource.isTypeSupported(mimeType)) {
      throw new Error(tf("watch.codec.unsupported", { codec }));
    }
    if (!response.body || typeof response.body.getReader !== "function") {
      throw new Error(t("watch.noimage"));
    }
    tentative.attendrePremiereImage();
    sourceBuffer = mediaSource.addSourceBuffer(mimeType);
    sourceBuffer.mode = "sequence";
    reader = response.body.getReader();

    for (;;) {
      const morceau = await operationOuAbandon(() => reader.read(), signal);
      if (morceau.done) break;
      if (!morceau.value || morceau.value.byteLength === 0) continue;
      await ajouterSegmentMse(sourceBuffer, morceau.value, signal);
      if (!lecture) {
        // The first chunk is often only ftyp+moov. play() helps autoplay,
        // but only loadeddata/playing above proves a real frame was decoded.
        const lancement = video.play();
        if (lancement && typeof lancement.catch === "function") {
          lancement.catch(() => {});
        }
      }
    }
    if (mediaSource.readyState === "open" && !sourceBuffer.updating) {
      mediaSource.endOfStream();
    }
    // Give the engine one turn to emit loadeddata after the last append;
    // an EOF with only the moov then remains a real failure.
    if (!lecture) await attendreOuAbandon(MSE_DELAI_DECODAGE_FINAL_MS, signal);
    if (!lecture) throw new Error(t("watch.noimage"));
    return true;
  } catch (error) {
    let finalError = erreurMedia || error;
    if (signalGlobal.aborted) finalError = erreurAnnulationMse();
    else if (tentative.aExpire()) finalError = new Error(t("watch.noimage"));
    else if (!(finalError instanceof Error)) finalError = new Error(String(finalError));
    finalError.mseLecture = lecture;
    throw finalError;
  } finally {
    if (minuteur) clearInterval(minuteur);
    video.removeEventListener("loadeddata", confirmerLecture);
    video.removeEventListener("playing", confirmerLecture);
    video.removeEventListener("error", signalerErreurMedia);
    if (mediaSource) mediaSource.removeEventListener("error", signalerErreurMedia);
    // Even after EOF or a codec error (so without clicking Stop), this
    // attempt's fetch must get its abort: cancel() alone is not uniformly
    // propagated to the HTTP connection by browsers.
    tentative.abandonner();
    // cancel() is deliberately not awaited: some engines can abandon the
    // read but only resolve the Promise after the network close. Cleanup
    // itself must never become the new hang.
    try {
      const annulation = reader
        ? reader.cancel()
        : response && response.body && typeof response.body.cancel === "function"
          ? response.body.cancel() : null;
      if (annulation && typeof annulation.catch === "function") {
        annulation.catch(() => {});
      }
    } catch (error) { /* stream already closed */ }
    if (sourceBuffer && mediaSource && mediaSource.readyState === "open") {
      try { sourceBuffer.abort(); } catch (error) { /* already detached */ }
    }
    if (mediaSource && mediaSource.readyState === "open") {
      try { mediaSource.endOfStream(); } catch (error) { /* append interrupted */ }
    }
    if (url !== null) {
      try { URL.revokeObjectURL(url); } catch (error) { /* URL already revoked */ }
    }
    tentative.nettoyer();
  }
}

// --- WebRTC: relay without re-encode (blink_webrtc.py on the server) ---
// Web page setting since 2026-09-03 (BACKLOG.md): active only if
// system.webrtc is true ("live_protocol" = "webrtc", and aiortc installed
// on the server). No automatic fallback to MSE if negotiation fails
// (removed 2026-09-03, at the user's explicit request): protocol choice
// belongs to the setting, not a silent substitution - otherwise the
// setting becomes misleading, you think you have WebRTC while sometimes
// getting MSE unnoticed. Retry loop for the initial negotiation
// (watchWebRTC()/tenterWebRTC() below), symmetric to connecterMse(): a
// single try left "it's stuck" for the user whenever a battery camera
// took longer than PREMIERE_IMAGE_MAX_SECONDS (blink_webrtc.py) to wake,
// especially right after switching from another camera (2026-09-03,
// BACKLOG.md). A cut AFTER the first frame (connection dropped mid-
// stream, once negotiation succeeded) is still not auto-retried: different
// scope, not the symptom seen here; a new click is enough for that case.
const WEBRTC_PC = {};
const WEBRTC_ABORT = {};
const WEBRTC_SESSION = {};
const LIVE_PENDING = {};
const LIVE_AUTO_STOP_TIMER = {};
const WEBRTC_MAX_ECHECS = 5;
const WEBRTC_DELAI_RECONNEXION_MS = 3000;
const WEBRTC_DELAI_MODULE_OCCUPE_MS = 10000;
const WEBRTC_BUDGET_TOTAL_MS = 10 * 60 * 1000;
// Absolute budget: module acquisition, Blink wake, H.264 parameters and
// SDP. After the response, the first frame also has its own bound.
const WEBRTC_DELAI_DEMARRAGE_MS = 130 * 1000;
const WEBRTC_DELAI_PREMIERE_IMAGE_MS = 30 * 1000;
const WEBRTC_DELAI_DECONNEXION_MS = 10 * 1000;
// Same margin as MSE above the server's 420 s (SESSION_MAX_SECONDS).
const WEBRTC_DUREE_APRES_LECTURE_MS = 450 * 1000;

function nouvelIdentifiantDirect() {
  const octets = new Uint8Array(16);
  crypto.getRandomValues(octets);
  return Array.from(octets, (octet) => octet.toString(16).padStart(2, "0")).join("");
}

// MODULE_SLOT (serve.py) is global: the sync module relays only one live
// at a time, whatever the camera. /api/attente-module waits until that
// token is really free before trying anything - replaced on 2026-09-03 a
// guessed fixed delay (10s, then 1s for this precise case): measured in
// practice on two successive switches, the real free time (blinkpy waits
// for the real end of the Blink relay connection before sending "done")
// varied from 1.65s to 8.8s - far too long for a fixed delay, not enough
// for another. Without this, switching cameras without clicking Stop left
// the first camera holding the lock, the second falling to 409 -
// "it doesn't work", reported by the user (2026-09-03).
async function attendreModuleLibre(signal) {
  try {
    const reponse = await operationOuAbandon(
      () => fetch("/api/attente-module", { signal }), signal);
    const info = await operationOuAbandon(() => lireJSON(reponse), signal);
    return !!info.libre;
  } catch (erreur) {
    if (signal.aborted) throw erreurAnnulationMse();
    return true;  // fail open: the next attempt will say if it still blocks
  }
}

async function watchLive(name) {
  // Record the choice before any wait. A more recent click invalidates
  // requests whose /attente-module has not answered yet as well.
  for (const autre of nomsDirectsActifs()) stopWatch(autre);
  const controller = new AbortController();
  LIVE_PENDING[name] = controller;
  // Rotation has its own dwell timer; auto-stop would put the tile back
  // to "View live" mid-rotation and look like the user must click again.
  const autoStopSeconds = Number(system && system.live_auto_stop_seconds) || 0;
  if (autoStopSeconds > 0 && !rotationActive) {
    LIVE_AUTO_STOP_TIMER[name] = setTimeout(() => stopWatch(name), autoStopSeconds * 1000);
  }
  const t0 = performance.now();
  let budgetEcoule = false;
  const idBudget = setTimeout(() => {
    budgetEcoule = true;
    controller.abort();
  }, WEBRTC_BUDGET_TOTAL_MS);
  const box = $("live-" + cssId(name));
  if (box) {
    box.innerHTML = `<p class="hint overlay">${h(t("watch.waking"))}</p>
      <button class="watch stop" data-action="stop-live"
              data-name="${h(name)}">${h(t("watch.stop"))}</button>`;
  }
  try {
    await attendreModuleLibre(controller.signal);
    if (controller.signal.aborted || LIVE_PENDING[name] !== controller) return;
    // These functions install their controller before their first wait:
    // the grid stays protected when LIVE_PENDING is removed in finally.
    return system && system.webrtc
      ? watchWebRTC(name, controller, t0) : watchMse(name);
  } catch (error) {
    if (budgetEcoule && LIVE_PENDING[name] === controller) failWatch(name, t("watch.noimage"));
    else if (error.name !== "AbortError") failWatch(name, String(error.message || error));
  } finally {
    clearTimeout(idBudget);
    if (LIVE_PENDING[name] === controller) delete LIVE_PENDING[name];
  }
}

const CLE_ROTATION = "blink2video.rotation";
let rotationTimer = null;
let rotationCountdown = null;
let rotationIndex = 0;
let rotationActive = false;
let rotationCameraCourante = null;
let rotationSecondesDwell = 30;

function cameraParCle(cle) {
  if (!system || !system.systems) return null;
  for (const s of system.systems) {
    const trouvee = (s.cameras || []).find(
      (c) => c.key === cle || c.name === cle);
    if (trouvee) return trouvee;
  }
  return null;
}

function camerasPourRotation() {
  if (!system || !system.systems) return [];
  const prefs = JSON.parse(localStorage.getItem(CLE_ROTATION) || "{}");
  // Empty array is truthy but matches nothing: treat as "all cameras".
  const selected = (prefs.cameras && prefs.cameras.length) ? prefs.cameras : null;
  const now = new Date();
  const hour = now.getHours();
  if (prefs.timeWindow && (hour < prefs.startHour || hour >= prefs.endHour)) return [];

  const all = [];
  for (const s of system.systems) {
    for (const c of (s.cameras || []).slice().sort(
      (a, b) => comparerNoms(a.name, b.name))) {
      if (estMasquee(c.name)) continue;
      if (c.offline) continue;  // only online cameras
      if (!selected || selected.includes(c.name) || selected.includes(c.key)) {
        all.push(c.key || c.name);
      }
    }
  }
  return all;
}

function peindreSceneRotation(name, cams, pos) {
  const cam = cameraParCle(name);
  const label = cam ? cam.name : name;
  $("count").textContent = `Rotation · ${pos}/${cams.length}`;
  $("list").innerHTML =
    `<div class="rotation-stage">
      <div class="rotation-banner">
        <span class="rotation-title">
          <strong>${h(label)}</strong>
          <strong class="rotation-countdown waiting">${h(t("rotation.waiting"))}</strong>
        </span>
        <span class="sub">${h(`${pos} / ${cams.length}`)}</span>
      </div>
      <div class="live" id="live-${cssId(name)}">
        <p class="hint overlay">${h(t("watch.waking"))}</p>
      </div>
    </div>`;
}

function elCountdownRotation(name) {
  if ($("view").value === "alllive" && name) {
    const esc = (typeof CSS !== "undefined" && CSS.escape)
      ? CSS.escape(name) : name.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
    return document.querySelector(`.rotation-countdown[data-for="${esc}"]`);
  }
  return document.querySelector(".rotation-countdown");
}

function afficherAttenteRotation(name) {
  for (const el of document.querySelectorAll(".rotation-countdown")) {
    if (el.dataset.for && el.dataset.for !== name) {
      el.hidden = true;
      el.classList.remove("waiting");
      el.textContent = "";
    }
  }
  const el = elCountdownRotation(name);
  if (!el) return;
  el.hidden = false;
  el.classList.add("waiting");
  el.textContent = t("rotation.waiting");
}

function arreterCountdownRotation() {
  if (rotationCountdown) clearInterval(rotationCountdown);
  rotationCountdown = null;
}

function demarrerCountdownRotation(secondesTotales) {
  arreterCountdownRotation();
  let restant = Math.max(0, Math.ceil(secondesTotales));
  const afficher = () => {
    const el = elCountdownRotation(rotationCameraCourante);
    if (!el) return;
    el.classList.remove("waiting");
    el.hidden = false;
    el.textContent = `(${restant})`;
  };
  afficher();
  rotationCountdown = setInterval(() => {
    restant -= 1;
    if (restant < 0) {
      arreterCountdownRotation();
      return;
    }
    afficher();
  }, 1000);
}

// Called when the first video frame is on screen. Starts the dwell
// countdown and the advance timer — not during wake/connect.
function signalerVideoRotation(name) {
  if (!rotationActive || !vueRotation()) return;
  if (name !== rotationCameraCourante) return;
  if (rotationTimer) return;  // already armed for this camera
  demarrerCountdownRotation(rotationSecondesDwell);
  rotationTimer = setTimeout(() => {
    if (!rotationActive || !vueRotation()) return;
    if (name !== rotationCameraCourante) return;
    stopWatch(name);
    cycleNext();
  }, rotationSecondesDwell * 1000);
}

function startRotation() {
  stopRotation();
  const cams = camerasPourRotation();
  if (!cams.length) {
    $("count").textContent = "";
    const label = $("view").value === "alllive" ? "All Live Views" : "rotation";
    $("list").innerHTML =
      `<p class="empty">No online cameras available for ${label}.</p>`;
    return;
  }
  if ($("view").value === "alllive") peindreGrilleLive();
  rotationActive = true;
  rotationIndex = 0;
  cycleNext();
}

async function cycleNext() {
  if (!rotationActive || !vueRotation()) return;
  const cams = camerasPourRotation();
  if (!cams.length) {
    stopRotation();
    $("count").textContent = "";
    const label = $("view").value === "alllive" ? "All Live Views" : "rotation";
    $("list").innerHTML =
      `<p class="empty">No online cameras available for ${label}.</p>`;
    return;
  }
  const pos = (rotationIndex % cams.length) + 1;
  const name = cams[rotationIndex % cams.length];
  rotationIndex++;
  const prefs = JSON.parse(localStorage.getItem(CLE_ROTATION) || "{}");
  rotationSecondesDwell = Number(prefs.duration) || 30;
  rotationCameraCourante = name;
  if (rotationTimer) clearTimeout(rotationTimer);
  rotationTimer = null;
  arreterCountdownRotation();
  if ($("view").value === "alllive") {
    // Keep the Live Views dashboard; only wake the next tile.
    if (!$("list").querySelector(".grid.wide")) peindreGrilleLive();
    $("count").textContent = `All Live · ${pos}/${cams.length}`;
    afficherAttenteRotation(name);
    const box = $("live-" + cssId(name));
    if (box) {
      box.innerHTML = `<p class="hint overlay">${h(t("watch.waking"))}</p>
        <button class="watch stop" data-action="stop-live"
                data-name="${h(name)}">${h(t("watch.stop"))}</button>`;
    }
  } else {
    peindreSceneRotation(name, cams, pos);
  }
  // Dwell countdown + advance timer start in signalerVideoRotation()
  // once the first frame is on screen — not during wake.

  try {
    await watchLive(name);
  } catch (erreur) { /* failWatch may advance; dwell timer otherwise will */ }
}

function stopRotation() {
  if (rotationTimer) clearTimeout(rotationTimer);
  rotationTimer = null;
  arreterCountdownRotation();
  rotationCameraCourante = null;
  // Keep rotationActive true while cutting streams so stopWatch does
  // not paint a "View live" button on a tile we are about to replace.
  for (const name of nomsDirectsActifs()) stopWatch(name);
  rotationActive = false;
}

async function watchWebRTC(name, controller = new AbortController(), t0 = performance.now()) {
  if (controller.signal.aborted) return;
  const box = $("live-" + cssId(name));
  if (!box) return;
  WEBRTC_ABORT[name] = controller;
  box.innerHTML =
    `<video autoplay muted playsinline></video>
     <button class="watch stop" data-i18n="watch.stop"
             data-action="stop-live" data-name="${h(name)}">${h(t("watch.stop"))}</button>
     ${recordBtn(name)}
     ${expandBtn(name)}`;
  const video = box.querySelector("video");
  let budgetEcoule = false;
  let lecture = false;
  const budget = armerBudget(name,
    Math.max(0, WEBRTC_BUDGET_TOTAL_MS - (performance.now() - t0)), () => {
      budgetEcoule = true;
      controller.abort();
    });

  let hint = null;
  let minuteur = null;
  const afficherIndice = (texte) => {
    if (!hint) {
      box.insertAdjacentHTML(
        "beforeend",
        `<p class="hint overlay" id="hint-${cssId(name)}">${h(texte)}</p>`
      );
      hint = $("hint-" + cssId(name));
    } else if (hint.isConnected) {
      hint.textContent = texte;
    }
  };
  const confirmerLecture = () => {
    lecture = true;
    derniereErreur = null;
    if (minuteur !== null) { clearInterval(minuteur); minuteur = null; }
    if (hint) { hint.remove(); hint = null; }
    signalerVideoRotation(name);
  };

  // An SDP answer does not prove playback: retry failures until
  // the first frame. After that, an end returns control to the user,
  // except in continuous live, where Blink having closed the session (or the stream
  // having stopped after playback) opens a new one immediately.
  let echecs = 0;
  let essais = 0;
  let derniereErreur = null;
  try {
    while (echecs < WEBRTC_MAX_ECHECS
           && (directContinu(name) || performance.now() - t0 < WEBRTC_BUDGET_TOTAL_MS)) {
      if (controller.signal.aborted) return;
      const reveilInitial = essais === 0;
      afficherIndice(reveilInitial ? t("watch.waking") : t("watch.reconnecting"));
      minuteur = reveilInitial ? setInterval(() => {
        const s = Math.round((performance.now() - t0) / 1000);
        const lent = s > 20 && cameraSurBatterie(name);
        afficherIndice(tf(lent ? "watch.waking.slow" : "watch.waking.seconds", { s }));
      }, 500) : null;
      try {
        await tenterWebRTC(name, video, controller.signal, ++essais, confirmerLecture);
        if (!directContinu(name)) break;
        echecs = 0;
        derniereErreur = null;
      } catch (error) {
        if (error.name === "AbortError" && controller.signal.aborted) {
          if (!budgetEcoule) return;
          break;
        }
        derniereErreur = error;
        if (error.webrtcIncompatible || (error.webrtcLecture && !directContinu(name))) break;
        // Another tab or a download may occupy the module.
        // Only the global budget bounds this case, without exhausting camera retries.
        if (error.webrtcLecture) echecs = 0;
        else if (error.status !== 409) echecs++;
      } finally {
        if (minuteur !== null) { clearInterval(minuteur); minuteur = null; }
      }
      if (controller.signal.aborted || echecs >= WEBRTC_MAX_ECHECS) break;
      try {
        await attendreOuAbandon(derniereErreur && derniereErreur.status === 409
          ? WEBRTC_DELAI_MODULE_OCCUPE_MS : WEBRTC_DELAI_RECONNEXION_MS, controller.signal);
      } catch (error) {
        if (!budgetEcoule) return;
        break;
      }
    }
  } finally {
    clearTimeout(budget.id);
    if (WEBRTC_ABORT[name] === controller) delete WEBRTC_ABORT[name];
  }
  if (controller.signal.aborted && !budgetEcoule) return;
  if (hint) hint.remove();
  if (lecture && (!derniereErreur || budgetEcoule)) {
    if (box.isConnected) box.innerHTML = repos(name, t("watch.live"));
  } else {
    failWatch(name, String((derniereErreur && derniereErreur.message) || derniereErreur
      || t("watch.noimage")), derniereErreur && derniereErreur.sessionId);
  }
}

// Keeping the attempt until the stream ends allows identical cleanup
// after ICE error, missing frame, cancel during offer, or playback.
async function tenterWebRTC(name, video, signal, essai, surLecture = () => {}) {
  if (signal.aborted) throw erreurAnnulationMse();
  const sessionId = nouvelIdentifiantDirect();
  const controller = new AbortController();
  // Browser and server on the same LAN (that is all
  // blink2video usage): no NAT to traverse; a STUN server would only
  // add a useless network round-trip.
  const pc = new RTCPeerConnection({ iceServers: [] });
  WEBRTC_PC[name] = pc;
  WEBRTC_SESSION[name] = sessionId;
  let lecture = false;
  let finNormale = false;
  let erreurEvenement = null;
  let stream = null;
  let track = null;
  let idImage = null;
  let idPremiereImage = null;
  let idDuree = null;
  let idDeconnexion = null;
  const abandonner = () => controller.abort();
  const finir = (error = null) => {
    if (controller.signal.aborted) return;
    erreurEvenement = error || (!lecture ? new Error(t("watch.noimage")) : null);
    finNormale = !erreurEvenement;
    controller.abort();
  };
  const idDemarrage = setTimeout(() => finir(new Error(t("watch.noimage"))),
    WEBRTC_DELAI_DEMARRAGE_MS);
  const confirmerImage = () => {
    if (lecture || controller.signal.aborted || !stream || video.srcObject !== stream) return;
    lecture = true;
    clearTimeout(idDemarrage);
    clearTimeout(idPremiereImage);
    idDuree = setTimeout(() => finir(), WEBRTC_DUREE_APRES_LECTURE_MS);
    signalerDirect(name, "image affichee (webrtc)", "", sessionId);
    surLecture();
  };
  const verifierImage = () => {
    // playing alone may precede any decode. Recent engines offer
    // presentation proof via requestVideoFrameCallback; on
    // others, also check for a decoded frame and its dimensions.
    if (!video.requestVideoFrameCallback && video.readyState >= 2 && video.videoWidth > 0) {
      confirmerImage();
    }
  };
  const interrompu = () => finir(new Error(t(lecture ? "watch.interrupted" : "watch.noimage")));
  const finPiste = () => finir();
  const etatConnexion = () => {
    const etats = [pc.connectionState, pc.iceConnectionState];
    if (etats.includes("failed")) interrompu();
    else if (etats.includes("closed")) finir();
    else if (etats.includes("disconnected")) {
      if (idDeconnexion === null) idDeconnexion = setTimeout(interrompu, WEBRTC_DELAI_DECONNEXION_MS);
    } else if (idDeconnexion !== null) {
      clearTimeout(idDeconnexion);
      idDeconnexion = null;
    }
  };
  pc.ontrack = (evenement) => {
    if (controller.signal.aborted) return;
    track = evenement.track;
    stream = evenement.streams[0] || new MediaStream([track]);
    track.addEventListener("ended", finPiste);
    video.srcObject = stream;
    if (video.requestVideoFrameCallback) {
      idImage = video.requestVideoFrameCallback(() => {
        idImage = null;
        confirmerImage();
      });
    }
    try {
      const lancement = video.play();
      if (lancement && typeof lancement.catch === "function") lancement.catch(interrompu);
    } catch (error) { interrompu(); }
  };
  pc.addEventListener("connectionstatechange", etatConnexion);
  pc.addEventListener("iceconnectionstatechange", etatConnexion);
  video.addEventListener("loadeddata", verifierImage);
  video.addEventListener("playing", verifierImage);
  video.addEventListener("error", interrompu);
  video.addEventListener("ended", finPiste);
  signal.addEventListener("abort", abandonner, { once: true });

  try {
    pc.addTransceiver("video", { direction: "recvonly" });
    // Data channel used only for its close: when the server
    // closes the session (Blink cut, ceiling, silent stream), the
    // browser sees it close at once, whereas the connection only goes
    // "disconnected" after several seconds, followed by
    // WEBRTC_DELAI_DECONNEXION_MS. The end is noticed immediately, and
    // "Continuous live" restarts without waiting (twenty seconds saved,
    // measured 2026-09-28).
    pc.createDataChannel("fin").addEventListener("close", () => finir());
    const offre = await operationOuAbandon(() => pc.createOffer(), controller.signal);
    // Check the real offer before waking the camera. The presence
    // of RTCPeerConnection alone does not guarantee the H.264 decoder.
    if (!/^a=rtpmap:\d+ H264\/90000\s*$/im.test(offre.sdp || "")) {
      const error = new Error(t("watch.noh264"));
      error.webrtcIncompatible = true;
      throw error;
    }
    await operationOuAbandon(() => pc.setLocalDescription(offre), controller.signal);
    const reponse = await operationOuAbandon(() => fetch(`/live-webrtc/${encodeURIComponent(name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sdp: pc.localDescription.sdp, type: pc.localDescription.type, essai, session_id: sessionId,
      }),
      signal: controller.signal,
    }), controller.signal);
    const info = await operationOuAbandon(() => lireJSON(reponse), controller.signal);
    if (!reponse.ok) {
      const error = new Error(info.error || `HTTP ${reponse.status}`);
      error.status = reponse.status;
      throw error;
    }
    if (info.session_id !== sessionId) throw new Error(t("watch.refused"));
    await operationOuAbandon(() => pc.setRemoteDescription({ sdp: info.sdp, type: info.type }),
      controller.signal);
    if (!lecture) idPremiereImage = setTimeout(() => finir(new Error(t("watch.noimage"))),
      WEBRTC_DELAI_PREMIERE_IMAGE_MS);
    await operationOuAbandon(() => new Promise(() => {}), controller.signal);
  } catch (error) {
    if (signal.aborted) throw erreurAnnulationMse();
    if (finNormale) return true;
    const cause = erreurEvenement || error;
    cause.webrtcLecture = lecture;
    cause.sessionId = sessionId;
    throw cause;
  } finally {
    controller.abort();
    clearTimeout(idDemarrage);
    clearTimeout(idPremiereImage);
    clearTimeout(idDuree);
    clearTimeout(idDeconnexion);
    if (idImage !== null) video.cancelVideoFrameCallback(idImage);
    signal.removeEventListener("abort", abandonner);
    video.removeEventListener("loadeddata", verifierImage);
    video.removeEventListener("playing", verifierImage);
    video.removeEventListener("error", interrompu);
    video.removeEventListener("ended", finPiste);
    if (track) track.removeEventListener("ended", finPiste);
    pc.removeEventListener("connectionstatechange", etatConnexion);
    pc.removeEventListener("iceconnectionstatechange", etatConnexion);
    pc.ontrack = null;
    pc.close();
    if (stream && video.srcObject === stream) video.srcObject = null;
    if (WEBRTC_PC[name] === pc) delete WEBRTC_PC[name];
    if (WEBRTC_SESSION[name] === sessionId) delete WEBRTC_SESSION[name];
    arreterSessionDirect(sessionId);
  }
}

async function watchMse(name) {
  const box = $("live-" + cssId(name));
  box.innerHTML =
    `<video autoplay muted playsinline></video>
     <button class="watch stop" data-i18n="watch.stop"
             data-action="stop-live" data-name="${h(name)}">${h(t("watch.stop"))}</button>
     ${recordBtn(name)}
     ${expandBtn(name)}`;
  const video = box.querySelector("video");
  const t0 = performance.now();
  window.__mseMetric = null;

  const controller = new AbortController();
  MSE_ABORT[name] = controller;
  // One id for the whole watchMse() duration, not per reconnect attempt:
  // send_live_mse() records it on each new try, and /api/arreter-direct must
  // be able to cut WHICHEVER attempt is current when the user clicks Stop.
  const sessionId = nouvelIdentifiantDirect();
  MSE_SESSION[name] = sessionId;
  let budgetEcoule = false;
  const budget = armerBudget(name, MSE_BUDGET_TOTAL_MS, () => {
    budgetEcoule = true;
    controller.abort();
  });

  let echecsAVide = 0;
  let derniereErreur = null;
  let lecturePendantBudget = false;
  try {
    // In continuous live, only Stop or repeated failures with no frame
    // mettent fin aux reprises (voir directContinu).
    while (echecsAVide < MSE_MAX_ECHECS_A_VIDE
           && (directContinu(name) || performance.now() - t0 < MSE_BUDGET_TOTAL_MS)) {
      const reveilInitial = echecsAVide === 0 && derniereErreur === null;
      const texte = reveilInitial ? t("watch.waking") : t("watch.reconnecting");
      try {
        const aLu = await connecterMse(
          name, video, controller.signal, texte, t0, reveilInitial, sessionId
        );
        lecturePendantBudget = lecturePendantBudget || aLu;
        derniereErreur = null;
        echecsAVide = aLu ? 0 : echecsAVide + 1;
      } catch (error) {
        if (error.name === "AbortError" && controller.signal.aborted) {
          if (!budgetEcoule) return;  // Stop button
          break;
        }
        derniereErreur = error;
        if (error.mseLecture) {
          lecturePendantBudget = true;
          echecsAVide = 0;
        } else if (error.status !== 409) {
          // 409 = another tab or a refresh still holds the module.
          // Do not burn the five tries in a few seconds; the global
          // ten-minute budget remains the hard bound.
          echecsAVide++;
        }
      }
      if (controller.signal.aborted || echecsAVide >= MSE_MAX_ECHECS_A_VIDE) break;
      const delai = derniereErreur && derniereErreur.status === 409
        ? MSE_DELAI_MODULE_OCCUPE_MS : MSE_DELAI_RECONNEXION_MS;
      try {
        await attendreOuAbandon(delai, controller.signal);
      } catch (error) {
        if (!budgetEcoule) return;  // stop requested during the wait
        break;
      }
    }
  } finally {
    clearTimeout(budget.id);
    if (MSE_ABORT[name] === controller) delete MSE_ABORT[name];
    if (MSE_SESSION[name] === sessionId) delete MSE_SESSION[name];
  }
  if (controller.signal.aborted && !budgetEcoule) return;
  const finParBudget = budgetEcoule
    || performance.now() - t0 >= MSE_BUDGET_TOTAL_MS;
  if (finParBudget && lecturePendantBudget) {
    // Total budget elapsed after real playback: silent return to
    // idle, as before, rather than a fake end-of-stream error.
    stopWatch(name);
  } else if (derniereErreur) {
    failWatch(name, String(derniereErreur.message || derniereErreur));
  } else {
    failWatch(name, t("watch.noimage"));
  }
}

// Idle state of a frame: the camera's last known image, and the button on
// top. Stopping a live view returns here, so the thumbnail comes back
// instead of leaving a black rectangle until page reload.
function repos(name, libelle) {
  return `<img class="still" src="${h(urlVignette(name))}" alt="">
     <button class="watch" data-action="watch-live" data-name="${h(name)}">${h(libelle)}</button>
     ${expandBtn(name)}`;
}

function urlVignette(name, refresh = false) {
  const url = `/camthumb/${encodeURIComponent(name)}?v=${Date.now()}`;
  return avecJeton(refresh ? `${url}&refresh=1` : url);
}

// Factored: placed both here (idle, including the failure state that
// reuses repos()) and in watchMse() once the stream starts, to stay
// visible in all states rather than appear only during
// lecture.
function expandBtn(name) {
  // Icon only, never text: a new button is never yet
  // the current fullscreen element, so the "enter" state is always right
  // at creation. syncExpandButtons() fixes the icon/label later,
  // on language change as on entering/leaving fullscreen.
  return `<button class="watch expand" data-action="fullscreen" data-name="${h(name)}"
                   title="${h(t("live.fullscreen.title"))}"
                   aria-label="${h(t("live.fullscreen.title"))}">⛶</button>`;
}

function syncExpandButtons() {
  const actif = document.fullscreenElement || document.webkitFullscreenElement || null;
  document.querySelectorAll(".watch.expand").forEach((b) => {
    const estActif = b.closest(".live") === actif;
    b.textContent = estActif ? "×" : "⛶";
    const libelle = t(estActif ? "live.fullscreen.title.exit" : "live.fullscreen.title");
    b.title = libelle;
    b.setAttribute("aria-label", libelle);
  });
}
document.addEventListener("fullscreenchange", syncExpandButtons);
document.addEventListener("webkitfullscreenchange", syncExpandButtons);

// Like expandBtn: placed in repos() AND in watchWebRTC()/watchMse(), but
// only a live view actually started has a stream to record - repos()
// therefore omits it. Always "stopped" at creation: the server clears
// ENREGISTREMENT_DIRECT_ACTIF at the start of each new live (serve.py),
// so a new button can never start already active.
function recordBtn(name) {
  if (!estAdmin()) return "";
  return `<button class="watch record admin-only" data-action="toggle-record" data-name="${h(name)}"
                   title="${h(t("live.record.title"))}">${h(t("live.record.start"))}</button>`;
}

function appliquerEtatEnregistrement(bouton, actif) {
  bouton.classList.toggle("active", actif);
  bouton.textContent = t(actif ? "live.record.stop" : "live.record.start");
  bouton.title = actif ? t("live.record.stop") : t("live.record.title");
}

// One global server-side flag (ENREGISTREMENT_DIRECT_ACTIF,
// serve.py): the response's "actif" field is the source of truth, not
// the button's assumed state before the click - two tabs on the same live
// thus stay consistent with each other.
async function toggleRecord(name, bouton) {
  const sessionId = WEBRTC_SESSION[name];
  if (!sessionId && !MSE_ABORT[name]) return;
  const actif = !bouton.classList.contains("active");
  bouton.disabled = true;
  try {
    const reponse = await fetch("/api/direct-enregistrement", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actif, session_id: sessionId }),
    });
    const info = await lireJSON(reponse);
    if (!reponse.ok || info.error || WEBRTC_SESSION[name] !== sessionId) return;
    appliquerEtatEnregistrement(bouton, Boolean(info.actif));
  } catch (erreur) {
    // Nothing more useful to show than a button left in its
    // previous state: the user sees the click did not take and can
    // recliquer.
  } finally {
    bouton.disabled = false;
  }
}

async function setArmed(scope, name, armed) {
  $("count").textContent = t("command.sending");
  const answer = await fetch("/api/arm", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, name, armed }),
  });
  const result = await lireJSON(answer);
  if (result.error) { alert(result.error); return loadSystem(true); }
  system = result;
  renderLive();
}

// Unlike setArmed(), may take up to 2 minutes (see reveiller_camera on the
// server): the button disables during the wait rather than implying a
// second click would speed anything up. try/finally, not just the happy
// path, also covers when renderLive() is frozen (live active elsewhere,
// see 28.20) and therefore never replaces this button with a new one.
async function reveillerCamera(name, bouton) {
  const libelle = bouton.textContent;
  bouton.disabled = true;
  bouton.textContent = t("camera.waking");
  try {
    const answer = await fetch("/api/reveiller", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const result = await lireJSON(answer);
    if (result.error) { alert(result.error); return loadSystem(true); }
    system = result;
    renderLive();
  } finally {
    bouton.disabled = false;
    bouton.textContent = libelle;
  }
}

async function prendreSnapshot(name, bouton) {
  const libelle = bouton.textContent;
  bouton.disabled = true;
  bouton.textContent = t("camera.snapshotting");
  try {
    const answer = await fetch("/api/snapshot", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const result = await lireJSON(answer);
    if (result.error) { alert(result.error); bouton.textContent = libelle; return; }
    const still = document.querySelector(`#live-${cssId(name)} .still`);
    if (still) still.src = urlVignette(name);
    bouton.textContent = t("camera.snapshot.saved");
    setTimeout(() => {
      if (bouton.isConnected && bouton.textContent === t("camera.snapshot.saved")) {
        bouton.textContent = libelle;
      }
    }, 3000);
    if ($("view").value === "pictures") chargerSnapshots();
  } catch (error) {
    alert(String(error));
    bouton.textContent = libelle;
  } finally {
    bouton.disabled = false;
  }
}

// Dedicated function rather than going through load(): a call after a
// manual shot (prendreSnapshot) or on each view switch (load()) must not
// re-fetch clips/videos or reset their pending checkboxes
// (excludedStaged/supprimerStaged) for nothing.
async function chargerSnapshots() {
  const answer = await fetch("/api/snapshots");
  const result = await lireJSON(answer);
  snapshots = result.snapshots || [];
  if ($("view").value === "pictures") render();
}

async function supprimerSnapshot(fichier, bouton) {
  if (!confirm(t("pictures.delete.confirm"))) return;
  bouton.disabled = true;
  try {
    const answer = await fetch("/api/snapshot-supprimer", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fichier }),
    });
    const result = await lireJSON(answer);
    if (result.error) { alert(result.error); return; }
    await chargerSnapshots();
  } finally {
    bouton.disabled = false;
  }
}

const CLIPS_PAR_PAGE = 50;
const TAILLES_PAGE_CLIPS = [25, 50, 100, 0];
const CLE_TAILLE_PAGE_CLIPS = "blink2video.clipsParPage";
let pageClips = 0;
// The top bar (pinned under the header) takes space and not everyone wants
// it: a Settings panel checkbox hides it, local to this browser like
// thumbnail size. The one at the bottom of the list stays, to change page.
const CLE_BARRE_PAGINATION = "blink2video.barrePagination";
let barrePaginationHaute = (() => {
  try { return localStorage.getItem(CLE_BARRE_PAGINATION) !== "0"; } catch (erreur) { return true; }
})();
let clipsParPage = restaurerTaillePageClips();
let nettoyerLecteursClips = () => {};

// The header is pinned at the top, and the "Clips per page" bar pins just under it:
// it needs the real height, which changes when the header wraps (narrow
// window). Published as CSS variable (--entete-h), watched when the header changes size.
(function suivreHauteurEntete() {
  const entete = document.querySelector("header");
  if (!entete) return;
  const publier = () => document.documentElement.style.setProperty("--entete-h", `${entete.offsetHeight}px`);
  publier();
  if (window.ResizeObserver) new ResizeObserver(publier).observe(entete);
  else window.addEventListener("resize", publier);
})();

function restaurerTaillePageClips() {
  try {
    const taille = Number(localStorage.getItem(CLE_TAILLE_PAGE_CLIPS) ?? CLIPS_PAR_PAGE);
    if (TAILLES_PAGE_CLIPS.includes(taille)) return taille;
  } catch (erreur) {}
  return CLIPS_PAR_PAGE;
}

function navigationClips(total, pages) {
  const selecteur = `<label>${h(t("clips.per.page"))}
    <select data-action="clip-page-size" data-direction="size">${TAILLES_PAGE_CLIPS.map((taille) => `<option value="${taille}" ${taille === clipsParPage ? "selected" : ""}>${taille || h(t("clips.all"))}</option>`).join("")}</select>
  </label>`;
  if (pages === 1) return total > CLIPS_PAR_PAGE ? `<div class="pagination">${selecteur}</div>` : "";
  return `<nav class="pagination" aria-label="${h(tf("clips.page", { page: pageClips + 1, pages, total }))}">
    ${selecteur}
    <button data-action="clip-page" data-page="0" data-direction="first" ${pageClips === 0 ? "disabled" : ""}>${h(t("clips.first"))}</button>
    <button data-action="clip-page" data-page="${pageClips - 1}" data-direction="previous" ${pageClips === 0 ? "disabled" : ""}>${h(t("clips.previous"))}</button>
    <span>${h(tf("clips.page", { page: pageClips + 1, pages, total }))}</span>
    <button data-action="clip-page" data-page="${pageClips + 1}" data-direction="next" ${pageClips === pages - 1 ? "disabled" : ""}>${h(t("clips.next"))}</button>
    <button data-action="clip-page" data-page="${pages - 1}" data-direction="last" ${pageClips === pages - 1 ? "disabled" : ""}>${h(t("clips.last"))}</button>
    <form data-action="clip-page-jump">
      <label>${h(t("clips.go.page"))} <input name="page" type="number" min="1" max="${pages}" step="1" value="${pageClips + 1}" inputmode="numeric" required></label>
      <button type="submit">${h(t("clips.go"))}</button>
    </form>
  </nav>`;
}

function changerPageClips(page, direction, taille = clipsParPage) {
  if (page === pageClips && taille === clipsParPage) return;
  pageClips = page;
  if (taille !== clipsParPage) {
    try { localStorage.setItem(CLE_TAILLE_PAGE_CLIPS, String(taille)); } catch (erreur) {}
  }
  clipsParPage = taille;
  renderClips();
  $("list").scrollIntoView({ block: "start" });
  const navigation = $("list").querySelector(".pagination");
  const controle = navigation?.querySelector(`[data-direction="${direction}"]:not(:disabled)`)
    || navigation?.querySelector('input[name="page"]');
  controle?.focus({ preventScroll: true });
}

function renderClips() {
  nettoyerLecteursClips();
  // Specific to the displayed kind (clip or direct): data.clips mixes both
  // since 2026-09-04; this summary must not count the other view.
  const kindAttendu = $("view").value === "direct" ? "direct" : "clip";
  const genre = data.clips.filter((c) => c.kind === kindAttendu);
  const clips = visible();
  const taille = clipsParPage || Math.max(1, clips.length);
  const pages = Math.max(1, Math.ceil(clips.length / taille));
  pageClips = Math.max(0, Math.min(pageClips, pages - 1));
  // No count here: the clips are in plain sight, and three numbers
  // higher on the page would say nothing one was looking for.
  $("count").textContent = "";

  // Beside the filter summary rather than in the list: text that describes
  // what is shown stays with the rest of what describes the filter, not
  // mixed into the scrolling results themselves.
  $("filtreCompte").textContent = data.filtered
    ? tf("clips.window", { m: genre.length, total: data.total_known[kindAttendu] })
    : "";

  if (!clips.length) {
    // Distinguish "nothing matches the filter" from "nothing was ever
    // fetched": in the second case, the most common cause is no
    // USB key on the module, with recordings then going to the cloud
    // of the Blink subscription, which this tool does not read. "Run blink2video
    // download" no longer belongs here: with the default composition
    // (start), download already runs in the background, and saying so
    // on top only confused a first-time user right when
    // the page is still empty (seen in practice, cold start).
    // "clips.none.ever" talks about download/USB key, specific to detection
    // clips - irrelevant for a live recording, which has its own message
    // ("direct.none.ever").
    $("list").innerHTML = genre.length
      ? `<p class="empty">${t("clips.none.filtered")}</p>`
      : `<p class="empty">${t(kindAttendu === "direct" ? "direct.none.ever" : "clips.none.ever")}</p>`;
    return;
  }
  const tranche = clips.slice(pageClips * taille, (pageClips + 1) * taille);
  const navigation = navigationClips(clips.length, pages);
  const days = [...new Set(tranche.map((c) => c.day))];
  $("list").innerHTML = (barrePaginationHaute ? navigation : "") + days.map((day) => `
    <h2>${h(day)}</h2>
    <div class="grid">${tranche.filter((c) => c.day === day).map(card).join("")}</div>
  `).join("") + navigation;
  preparerLecteursClips();
}

// "Group by": the choice only applies for dailies, and while no camera
// is chosen. With a camera, each day would have only one card, alone on
// its row under its own title (issue #37): it then falls back to a single
// group, where days follow in a grid, each dated on its card. The choice
// stays saved and returns when the camera is unchecked.
function regroupable(kind) {
  return kind === "daily" && !$("camera").value;
}

// The "Group by" choice follows the camera while the filter panel is
// open: decided only at render, it only appeared after "Filter"
// then reopening the panel (issue #36, follow-up).
function majGroupBy() {
  $("groupBySection").hidden = !regroupable($("view").value);
}

function renderVideos(kind) {
  const items = (videos[kind] || [])
    .filter((v) => !$("camera").value || v.camera === $("camera").value)
    .filter((v) => !estMasquee(v.camera));
  const total = items.reduce((sum, v) => sum + v.duration, 0);
  $("count").textContent = items.length
    ? tf("videos.count", { n: items.length, duree: duration(total) })
    : "";
  if (!items.length) {
    $("list").innerHTML = `<p class="empty">${t("videos.none")}</p>`;
    return;
  }
  // Group by day (issue #14): all cameras of the same daily together,
  // without reopening each camera to compare the same day. A daily's label
  // is already YYYY-MM-DD (nom_fichier = f"{day}_{camera}.mp4",
  // merge_daily.py): alphabetical sort IS chronological; no need to reparse.
  // Only makes sense for "daily" (render() hides the selector otherwise):
  // weeklies/monthlies have one file per period per camera; grouping by
  // "day" would change nothing.
  const parJour = regroupable(kind) && $("groupBy").value === "day";
  const cles = parJour
    ? [...new Set(items.map((v) => v.label))].sort().reverse()
    : [...new Set(items.map((v) => v.camera))].sort(comparerNoms);
  const correspond = parJour
    ? (v, cle) => v.label === cle
    : (v, cle) => v.camera === cle;
  const titre = parJour ? (cle) => dateLocale(cle) : (cle) => cle;
  $("list").innerHTML = cles.map((cle) => `
    <h2>${h(titre(cle))}</h2>
    <div class="grid wide">
      ${items.filter((v) => correspond(v, cle))
        .sort((a, b) => parJour ? comparerNoms(a.camera, b.camera) : 0)
        .map((v) => videoCard(v, parJour)).join("")}
    </div>
  `).join("");
}

// The filename carries the UTC timestamp (declencherSnapshot, serve.py):
// no separate field to ask for, just reformat for display.
function dateSnapshot(horodatage) {
  const m = horodatage.match(/^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})Z_/);
  if (!m) return horodatage;
  return new Date(`${m[1]}T${m[2]}:${m[3]}:${m[4]}Z`).toLocaleString();
}

// A daily's label is YYYY-MM-DD (see renderVideos): with no time, interpret
// as UTC midnight then reformat to local date so a west-of-UTC timezone
// does not shift the display to the previous day.
function dateLocale(jour) {
  const m = jour.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return jour;
  return new Date(`${jour}T00:00:00Z`).toLocaleDateString(undefined, { timeZone: "UTC" });
}

function renderPictures() {
  const items = snapshots.filter((s) => (!$("camera").value || s.camera === $("camera").value)
    && !estMasquee(s.camera));
  $("count").textContent = items.length ? tf("pictures.count", { n: items.length }) : "";
  if (!items.length) {
    $("list").innerHTML = `<p class="empty">${t("pictures.none")}</p>`;
    return;
  }
  const cameras = [...new Set(items.map((s) => s.camera))].sort(comparerNoms);
  $("list").innerHTML = cameras.map((camera) => `
    <h2>${h(camera)}</h2>
    <div class="grid">
      ${items.filter((s) => s.camera === camera).map(pictureCard).join("")}
    </div>
  `).join("");
}

function pictureCard(s) {
  const media = avecJeton(`/snapshot/${encodeURI(s.fichier)}`);
  return `<div class="card">
    <img class="snapshot" loading="lazy" src="${h(media)}" alt="">
    <div class="meta">
      <div class="time">${h(dateSnapshot(s.horodatage))}</div>
      <a class="act" href="${h(media)}" download>${h(t("videos.download"))}</a>
      ${estAdmin() ? `<button class="act grouped admin-only" title="${h(t("pictures.delete.title"))}"
              data-action="delete-snapshot" data-fichier="${h(s.fichier)}">${h(t("pictures.delete"))}</button>` : ""}
    </div>
  </div>`;
}

function videoCard(v, parJour = false) {
  const url = `${v.kind}/${encodeURI(v.path)}`;
  const media = avecJeton(`/media/${url}`);
  const poster = avecJeton(`/thumb/${url}`);
  // Group by day (issue #14): the h2 already carries the date; repeating
  // the same label on each card would teach nothing - the camera name no
  // longer appears anywhere else in this mode.
  return `<div class="card">
    <video preload="none" controls playsinline
           poster="${h(poster)}" src="${h(media)}"></video>
    <div class="meta">
      <div>
        <div class="time">${h(parJour ? v.camera : v.label)}</div>
        <div class="sub">${h(duration(v.duration))}</div>
      </div>
      <a class="act" href="${h(media)}" download>${h(t("videos.download"))}</a>
    </div>
  </div>`;
}

function preparerLecteursClips() {
  const liste = $("list");
  const disponibles = [];
  const etats = new Map([...liste.querySelectorAll(".clip-player")].map((zone) =>
    [zone, { zone, proche: false, video: null, lecture: null }]));

  function liberer(etat, forcer = false) {
    const video = etat.video;
    if (!video || (!forcer && (etat.proche || !video.paused
        || etat.zone.contains(document.activeElement)
        || document.pictureInPictureElement === video
        || etat.zone.contains(document.fullscreenElement)))) return;
    etat.lecture = {
      temps: video.readyState ? video.currentTime : (etat.lecture?.temps || 0),
      volume: video.volume, muet: video.muted, vitesse: video.playbackRate, boucle: video.loop,
    };
    etat.video = null;
    etat.evenements.abort();
    video.pause();
    video.removeAttribute("src");
    video.removeAttribute("poster");
    video.load();
    video.remove();
    if (!forcer) disponibles.push(video);
    etat.zone.tabIndex = 0;
  }

  function creer(etat) {
    if (etat.video) return etat.video;
    const video = disponibles.pop() || document.createElement("video");
    etat.video = video;
    etat.evenements = new AbortController();
    const { signal } = etat.evenements;
    video.preload = "none";
    video.controls = true;
    video.playsInline = true;
    video.poster = etat.zone.dataset.poster;
    video.src = etat.zone.dataset.src;
    video.setAttribute("aria-label", etat.zone.getAttribute("aria-label"));
    const lecture = etat.lecture;
    video.volume = lecture?.volume ?? 1;
    video.muted = lecture?.muet ?? false;
    video.playbackRate = lecture?.vitesse ?? 1;
    video.loop = lecture?.boucle ?? false;
    if (lecture) {
      video.addEventListener("loadedmetadata", () => { video.currentTime = lecture.temps; }, { once: true, signal });
    }
    video.addEventListener("pause", () => liberer(etat), { signal });
    video.addEventListener("leavepictureinpicture", () => liberer(etat), { signal });
    video.addEventListener("fullscreenchange", () => liberer(etat), { signal });
    etat.zone.append(video);
    etat.zone.tabIndex = -1;
    return video;
  }

  const observation = new IntersectionObserver((entrees) => {
    for (const entree of entrees) {
      const etat = etats.get(entree.target);
      etat.proche = entree.isIntersecting;
      if (etat.proche) creer(etat);
      else liberer(etat);
    }
  }, { rootMargin: "600px 0px" });
  const focus = (event) => {
    const etat = etats.get(event.target.closest(".clip-player"));
    if (etat && event.target === etat.zone) creer(etat).focus({ preventScroll: true });
  };
  const quitterFocus = (event) => {
    const etat = etats.get(event.target.closest(".clip-player"));
    if (etat) queueMicrotask(() => liberer(etat));
  };
  liste.addEventListener("focusin", focus);
  liste.addEventListener("focusout", quitterFocus);
  for (const zone of etats.keys()) observation.observe(zone);
  nettoyerLecteursClips = () => {
    observation.disconnect();
    observation.takeRecords();
    liste.removeEventListener("focusin", focus);
    liste.removeEventListener("focusout", quitterFocus);
    for (const etat of etats.values()) liberer(etat, true);
    disponibles.length = 0;
    nettoyerLecteursClips = () => {};
  };
}

function card(c) {
  const [an, mois, jour] = c.day.split("-");
  const ligne = [c.camera, duration(c.duration), `${jour}/${mois}/${an}`, c.time,
                 c.model].filter(Boolean).join(" · ");
  // c.kind ("clip" or "direct") picks the URL prefix: resolve_media()
  // (serve.py) routes each to its own disk root, same routes
  // /media|thumb/<kind>/<identity> for both (2026-09-04, same
  // presentation requested for live recordings).
  return `<div class="card ${c.excluded ? "out" : ""}">
    <div class="clip-player" tabindex="0" aria-label="${h(ligne)}"
         data-poster="${h(avecJeton(`/thumb/${c.kind}/${encodeURI(c.identity)}`))}"
         data-src="${h(avecJeton(`/media/${c.kind}/${encodeURI(c.identity)}`))}"></div>
    <div class="meta">
      <div class="time line">${h(ligne)}</div>
      ${estAdmin() ? `<label class="act admin-only" title="${h(t("clip.discard.title"))}">
        <input type="checkbox" ${c.excludedStaged ? "checked" : ""}
               data-action="stage-exclusion" data-identity="${h(c.identity)}">
        ${h(t("clip.discard"))}
      </label>
      ${c.sourceDeleted
        || (c.kind === "clip" && (data.suppressionAuto || []).includes(c.cameraKey))
        ? "" : `
      <label class="act admin-only" title="${h(t("clip.deleteSource.title"))}">
        <input type="checkbox" ${c.supprimerStaged ? "checked" : ""}
               data-action="stage-suppression" data-identity="${h(c.identity)}">
        ${h(t("clip.deleteSource"))}
      </label>`}` : ""}
    </div>
  </div>`;
}

// The filter (camera + period + "Group by") survives from visit to visit:
// someone who only wants this week's clips need not redo that choice on
// each page open. Default "all history" until something has been chosen
// (seen in practice, 2026-08-27). "Group by" reset to its default on each
// reload, hence on each blink2video restart (issue #36).
const CLE_FILTRE = "blink2video.filtre";
const GROUPEMENTS = ["camera", "day"];

function sauvegarderFiltre() {
  try {
    localStorage.setItem(CLE_FILTRE, JSON.stringify(
      { camera: $("camera").value, plage: plageClips, groupBy: $("groupBy").value }));
  } catch (erreur) { /* storage unavailable (private browsing…): never mind */ }
}

// The "Group by" <select> has its options from the HTML (unlike the
// camera one, filled later): the kept choice is set right away, before
// the first render.
function restaurerGroupement(filtre) {
  if (filtre && GROUPEMENTS.includes(filtre.groupBy)) $("groupBy").value = filtre.groupBy;
}

function restaurerFiltre() {
  try {
    const brut = localStorage.getItem(CLE_FILTRE);
    return brut ? JSON.parse(brut) : null;
  } catch (erreur) {
    return null;
  }
}

const _filtrePersiste = restaurerFiltre();
let plageClips = _filtrePersiste?.plage || { preset: "all" };
restaurerGroupement(_filtrePersiste);
// In-progress choice in the panel, applied only on Filter click - until
// no preset or custom range has been touched in this panel opening,
// Filter only reuses plageClips.
let plageEnAttente = null;
// The restored camera can only be set once the real list is known
// (first fill(), see load()): an empty <select> ignores any value given
// before it has its <option>s.
let _filtreCameraAppliquee = false;

function paramsPourPlage() {
  const params = new URLSearchParams();
  if (plageClips.preset === "all") params.set("all", "1");
  else if (plageClips.preset) params.set("preset", plageClips.preset);
  else {
    if (plageClips.depuis) params.set("depuis", plageClips.depuis);
    if (plageClips.jusqua) params.set("jusqua", plageClips.jusqua);
  }
  const s = params.toString();
  return s ? `?${s}` : "";
}

// Two load() calls may overlap (close clicks between presets, or a
// background load() while another still runs): without cancelling the
// previous one, a slower request started earlier (often the initial load
// for "this month") could answer after a faster one and re-apply stale
// data on top - the selection then seemed stuck on this month, or the
// grid rendered twice in a row (flicker). AbortController is the standard
// mechanism for that, not a local invention (seen in practice, 2026-08-27).
let __chargementEnCours = null;

async function load() {
  __chargementEnCours?.abort();
  const controleur = new AbortController();
  __chargementEnCours = controleur;
  let nouvellesDonnees, nouvellesVideos;
  try {
    const [answer, videoAnswer] = await Promise.all([
      fetch(`/api/clips${paramsPourPlage()}`, { signal: controleur.signal }),
      fetch("/api/videos", { signal: controleur.signal }),
    ]);
    // Body reads too: cancel may land after the headers, during the
    // read, and AbortError then escaped as an unhandled rejection (two
    // close view changes, seen by the page test in a real browser,
    // 2026-10-12).
    nouvellesDonnees = await lireJSON(answer);
    nouvellesVideos = await lireJSON(videoAnswer);
  } catch (erreur) {
    if (erreur.name === "AbortError") return; // a more recent request took over
    throw erreur;
  }
  data = nouvellesDonnees;
  videos = nouvellesVideos;
  if (data.error) { $("log").style.display = "block"; $("log").textContent = data.error; return; }
  // Selection state: zeroed on each reload, it reflects the
  // real state just re-read, not a still-pending intent.
  for (const c of data.clips || []) {
    c.excludedStaged = c.excluded;
    c.supprimerStaged = false;
  }
  // Name alone: the model only appeared here for cameras already seen
  // live, and under its internal code (hawk, sedona, lotus) when Blink
  // does not give the name (issue #37). It stays on the camera card.
  fill($("camera"), camerasConnues(), t("filter.allcameras"));
  // The restored camera can only be set once; later times, fill() already
  // has enough to preserve the current selection alone (see its own note
  // on `kept`).
  if (!_filtreCameraAppliquee) {
    _filtreCameraAppliquee = true;
    if (_filtrePersiste?.camera && data.cameras.includes(_filtrePersiste.camera)) {
      $("camera").value = _filtrePersiste.camera;
    }
  }
  render();
  majBoutonAppliquer();
  chargerSnapshots();
}

// A preset is enough to find a recent incident (today, this
// week…); the custom range aims at a precise period to the
// hour, so you need not scroll a always-armed camera's whole
// history (reported on Reddit, 2026-08-27). Neither applies
// until Filter has been clicked: picking a preset then
// changing your mind for a camera must not already have reloaded the grid
// once for nothing.
function choisirPreset(nom) {
  plageEnAttente = { preset: nom };
  $("rangeFrom").value = ""; $("rangeTo").value = "";
  for (const bouton of document.querySelectorAll("#filtre .presets button")) {
    bouton.classList.toggle("primary", bouton.dataset.preset === nom);
  }
}

function choisirPlagePersonnalisee() {
  const depuis = $("rangeFrom").value, jusqua = $("rangeTo").value;
  plageEnAttente = (depuis || jusqua) ? { depuis, jusqua } : null;
  for (const bouton of document.querySelectorAll("#filtre .presets button")) {
    bouton.classList.remove("primary");
  }
}

function ouvrirFiltre() {
  plageEnAttente = null;
  for (const bouton of document.querySelectorAll("#filtre .presets button")) {
    bouton.classList.toggle("primary", bouton.dataset.preset === plageClips.preset);
  }
  $("rangeFrom").value = plageClips.depuis || "";
  $("rangeTo").value = plageClips.jusqua || "";
  majGroupBy();
  $("filtre").showModal();
}

async function appliquerFiltre() {
  pageClips = 0;
  if (plageEnAttente) plageClips = plageEnAttente;
  sauvegarderFiltre();
  $("filtre").close();
  // No intermediate loading message: /api/clips answers in a few
  // milliseconds locally, too fast to read as loading - only as a
  // flicker of the whole grid on each filter change (seen in practice,
  // 2026-08-27).
  await load();
}

// A custom range shows the dates actually chosen rather than a generic
// "Custom period": that is exactly to target a precise period, so show
// it without reopening the panel.
function libellePlagePersonnalisee() {
  const formater = (v) => {
    if (!v) return null;
    const [date, heure] = v.split("T");
    const [, mois, jour] = date.split("-");
    return `${jour}/${mois} ${heure || "00:00"}`;
  };
  const depuis = formater(plageClips.depuis);
  const jusqua = formater(plageClips.jusqua);
  if (depuis && jusqua) return `${depuis} → ${jusqua}`;
  if (depuis) return tf("range.custom.depuis", { v: depuis });
  if (jusqua) return tf("range.custom.jusqua", { v: jusqua });
  return t("range.custom");
}

// Summary shown in the header beside the Filter button, to know what
// is active without reopening the panel. The camera always appears there,
// including the silent default "all cameras" - same logic as the period,
// already shown even on its own default ("all history"): both tell the
// truth about what is shown, never only what restricts something.
function resumeFiltre() {
  const morceaux = [];
  const option = $("camera").selectedOptions[0];
  morceaux.push(option ? option.textContent : t("filter.allcameras"));
  if ($("view").value === "clips" || $("view").value === "direct") {
    morceaux.push(plageClips.preset ? t(`range.${plageClips.preset}`) : libellePlagePersonnalisee());
  }
  return morceaux.join(" · ");
}

// Exclude and Delete are checkboxes, not immediate actions: one by one,
// USB delete would pay on each click the Sync Module manifest regeneration
// delay (up to one minute, see AUDIT 28.73/75). The Apply button handles
// the whole batch in one call, one manifest read per affected Sync Module
// rather than one per clip.
function stagerExclusion(identity, coche) {
  const clip = data.clips.find((c) => c.identity === identity);
  if (clip) clip.excludedStaged = coche;
  majBoutonAppliquer();
}

function stagerSuppression(identity, coche) {
  const clip = data.clips.find((c) => c.identity === identity);
  if (clip) clip.supprimerStaged = coche;
  majBoutonAppliquer();
}

function calculerSelection() {
  const exclure = [], inclure = [], supprimer = [];
  for (const c of data.clips || []) {
    if (!!c.excludedStaged !== !!c.excluded) (c.excludedStaged ? exclure : inclure).push(c.identity);
    if (c.supprimerStaged && !c.sourceDeleted) supprimer.push(c.identity);
  }
  return { exclure, inclure, supprimer };
}

function majBoutonAppliquer() {
  const bouton = $("applyButton");
  if (!bouton) return;
  const { exclure, inclure, supprimer } = calculerSelection();
  const n = exclure.length + inclure.length + supprimer.length;
  bouton.hidden = n === 0;
  bouton.textContent = tf("selection.apply", { n });
}

async function appliquerSelection() {
  const { exclure, inclure, supprimer } = calculerSelection();
  if (!exclure.length && !inclure.length && !supprimer.length) return;
  if (supprimer.length && !confirm(tf("selection.confirm.suppression", { n: supprimer.length }))) return;
  const bouton = $("applyButton");
  bouton.disabled = true;
  bouton.textContent = t("clip.deleteSource.pending");
  try {
    const reponse = await fetch("/api/appliquer-selection", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exclure, inclure, supprimer }) });
    const resultat = await lireJSON(reponse);
    if (resultat.error) { alert(resultat.error); return; }
    // Exclude/Reinclude: optimistic update, like the old toggle()
    // (AUDIT 28.33/28.75). The registry writes in the background on the
    // server so the response is never blocked; re-reading it right away
    // here sometimes races, before the write finishes (reported by
    // Nico: "excluded stay present" until a manual F5).
    for (const identity of exclure) {
      const clip = data.clips.find((c) => c.identity === identity);
      if (clip) { clip.excluded = true; clip.excludedStaged = true; }
    }
    for (const identity of inclure) {
      const clip = data.clips.find((c) => c.identity === identity);
      if (clip) { clip.excluded = false; clip.excludedStaged = false; }
    }
    // Delete: the call was synchronous on the server; the result is
    // therefore reliable at once, not a guess.
    let echecs = 0;
    for (const [identity, statut] of Object.entries(resultat.resultats || {})) {
      const clip = data.clips.find((c) => c.identity === identity);
      if (statut === "supprime" || statut === "deja_absent") {
        if (clip) { clip.sourceDeleted = true; clip.supprimerStaged = false; }
      } else {
        echecs++;
      }
    }
    if (echecs) alert(tf("selection.partial", { n: echecs }));
    render();
  } catch (erreur) {
    alert(String(erreur));
  } finally {
    bouton.disabled = false;
    majBoutonAppliquer();
  }
}

// --- Blink login -----------------------------------------------------------
// The server keeps the Blink session open between the two requests: the page
// only has to ask questions in the order it demands.
let authResolve = null;

function showAuth(stage, message) {
  $("authError").textContent = message || "";
  const code = stage === "2fa";
  $("authCreds").hidden = code;
  $("authCode").hidden = !code;
  $("authTitle").textContent = code ? t("auth.title.2fa") : t("auth.title");
  $("authHint").textContent = code ? t("auth.hint.2fa") : t("auth.hint");
  $("authOk").textContent = code ? t("auth.validate") : t("auth.ok");
  if (!$("auth").open) $("auth").showModal();
  (code ? $("code") : $("user")).focus();
}

function authenticate() {
  return new Promise((resolve) => {
    authResolve = resolve;
    showAuth("creds", "");
  });
}

$("passToggle").onclick = () => {
  const masque = $("pass").type === "password";
  $("pass").type = masque ? "text" : "password";
  $("passToggle").textContent = t(masque ? "auth.hide" : "auth.show");
  $("passToggle").setAttribute("aria-label", t(masque ? "auth.hide.aria" : "auth.show.aria"));
};

$("authCancel").onclick = () => {
  $("auth").close();
  if (authResolve) { authResolve(false); authResolve = null; }
};

$("authOk").onclick = async () => {
  const code = !$("authCode").hidden;
  $("authOk").disabled = true;
  $("authError").textContent = t("auth.connecting");
  const body = code
    ? { code: $("code").value }
    : { username: $("user").value, password: $("pass").value };
  let result;
  try {
    const answer = await fetch(code ? "/api/2fa" : "/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    result = await lireJSON(answer);
  } catch (error) {
    result = { status: "error", message: String(error) };
  }
  $("authOk").disabled = false;
  $("pass").value = "";
  if (result.status === "ok") {
    $("auth").close();
    $("code").value = "";
    // ?login=1 only served to open this dialog on first launch; leaving
    // it in the address would show the login again on every refresh or
    // from a bookmark, even though the session is valid.
    const parametres = new URLSearchParams(location.search);
    if (parametres.get("login") === "1") {
      parametres.delete("login");
      const suite = parametres.toString();
      history.replaceState(null, "", location.pathname + (suite ? `?${suite}` : ""));
    }
    if (authResolve) { authResolve(true); authResolve = null; }
  } else if (result.status === "2fa") {
    $("code").value = "";
    showAuth("2fa", "");
  } else {
    showAuth(code ? "2fa" : "creds", result.message || t("auth.failed"));
  }
};

$("refresh").onclick = async () => {
  const status = await lireJSON(await fetch("/api/status"));
  if (!status.authenticated && !(await authenticate())) return;
  if (status.initial_setup) {
    await ouvrirReglages(true);
    return;
  }

  const button = $("refresh");
  button.disabled = true;
  actualisationLocale = true;
  $("log").style.display = "block";
  $("log").textContent = "";
  $("work").classList.add("on");
  let label = t("refresh.starting");
  $("phase").textContent = label;
  $("bar").removeAttribute("value");   // indeterminate bar until we know

  const source = new EventSource(avecJeton("/api/refresh"));
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.phase) {
      label = event.phase_key === "phase.usb_section"
        ? libellePhase(event.phase_key, event.phase, { hub: event.phase_hub })
        : libellePhase(event.phase_key, event.phase);
      $("phase").textContent = label;
      $("bar").removeAttribute("value");
    }
    if (event.progress) {
      // done is fractional: the integer part counts finished clips, the
      // decimal the progress within the current clip. The bar therefore
      // advances continuously instead of jumping one step per clip.
      const p = event.progress;
      $("bar").max = p.total;
      $("bar").value = p.done;
      const current = Math.min(Math.floor(p.done) + 1, p.total);
      $("phase").textContent =
        `${label} ${current}/${p.total} (${Math.round((p.done / p.total) * 100)} %)`;
    }
    if (event.line !== undefined) {
      $("log").textContent += event.line + "\n";
      $("log").scrollTop = $("log").scrollHeight;
    }
    if (event.done) {
      // Sans close(), EventSource se reconnecte tout seul et relancerait
      // l'actualisation en boucle.
      source.close();
      $("work").classList.remove("on");
      button.disabled = false;
      actualisationLocale = false;
      if (!event.ok) $("phase").textContent = t("refresh.errors");
      load();
      // "Refresh" only brought back clips: battery,
      // temperature and signal of each camera stayed on their
      // last reading, sometimes days old, until you
      // opened the Live tab yourself (real bug: a
      // camera showed an old reading until a manual
      // refresh from the official app). loadSystem(true) forces the same
      // pass that app does on its side (blink.refresh(force=True),
      // which truly re-reads each camera, not only the account
      // summary - see system_state() on the server).
      rafraichirVignettes = true;
      loadSystem(true);
    }
  };
  source.onerror = () => {
    source.close();
    $("work").classList.remove("on");
    button.disabled = false;
    actualisationLocale = false;
    $("log").textContent += t("refresh.disconnected");
  };
};

$("showOut").onchange = () => { pageClips = 0; render(); };
$("showHidden").onchange = () => {
  montrerMasquees = $("showHidden").checked;
  pageClips = 0;
  fill($("camera"), camerasConnues(), t("filter.allcameras"));
  render();
};
$("barrePagination").onchange = () => {
  barrePaginationHaute = $("barrePagination").checked;
  try { localStorage.setItem(CLE_BARRE_PAGINATION, barrePaginationHaute ? "1" : "0"); } catch (erreur) {}
  render();
};
$("tailleCartes").onchange = () => {
  tailleCartes = $("tailleCartes").value;
  try { localStorage.setItem(CLE_TAILLE_CARTES, tailleCartes); } catch (erreur) {}
  appliquerTailleCartes();
};
majCaseMasquees();
// "view" needs fresh data, not just a new render of what is already in
// memory: Clips, Direct Clips, Daily, Weekly, Monthly may all have
// changed while the tab stayed open (record button, background
// download...) without /api/clips or /api/videos ever being re-fetched
// since (seen in practice, 2026-09-04: recorded live, invisible in Direct
// Clips until a full page reload). "live" does not need that; it reads
// `system` (loadSystem), never `data`/`videos`.
$("view").onchange = () => {
  pageClips = 0;
  // Always cut an active cycle when the dropdown changes (including
  // rotation ↔ All Live), then restart if the new view needs one.
  stopRotation();
  if (vueDirecte()) {
    rafraichirVignettes = true;
    render();
    return;
  }
  // Leaving Live removes its videos from the DOM: also close sessions
  // and pending wakes before rebuilding another view.
  for (const name of nomsDirectsActifs()) stopWatch(name);
  load();
};
// Only this text line updates by itself: it is precisely for
// spotting a stopped loop, which you would not see by looking at clips
// qui, eux, ne changent plus.
// An automatic worker may start at any time. The dedicated
// probe only reads .blink_travail.json: three seconds continuously stay
// light, even under Windows 7. The costlier summary (registry + passages +
// update) keeps its one-minute cadence.
etatDuTravail();
(function veillerTravail() {
  setTimeout(async () => {
    await etatDuTravail();
    veillerTravail();
  }, 3000);
})();
(function veillerPassages() {
  setTimeout(async () => {
    await heuresDePassage();
    veillerPassages();
  }, 60000);
})();
$("auto").checked = localStorage.getItem("auto") === "1";
$("auto").onchange = () => {
  localStorage.setItem("auto", $("auto").checked ? "1" : "0");
  heuresDePassage();
};
// Reflects the real system state (startup file present or not), not
// a page-side remembered preference: two installs of the same browser
// profile must not believe each other's state.
fetch("/api/autostart").then(lireJSON).then((etat) => {
  $("autostart").checked = !!etat.actif;
}).catch(() => {});
$("autostart").onchange = async () => {
  const voulu = $("autostart").checked;
  $("autostart").disabled = true;
  try {
    const reponse = await fetch("/api/autostart", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actif: voulu }),
    });
    const etat = await lireJSON(reponse);
    if (etat.error) alert(etat.error);
    $("autostart").checked = !!etat.actif;
  } catch (error) {
    alert(String(error));
    $("autostart").checked = !voulu;
  } finally {
    $("autostart").disabled = false;
  }
};

let portActuel = null;   // re-read on each open, compared to the submit

function afficherFormulaireReglages(reglages) {
  $("usbMinutes").value = reglages.usb_minutes;
  $("cloudMinutes").value = reglages.cloud_minutes;
  $("port").value = reglages.port;
  portActuel = reglages.port;
  $("storageDir").value = reglages.storage_dir;
  $("trustedHost").value = reglages.trusted_host || "";
  $("webhookNotifUrl").value = reglages.webhook_notif_url || "";
  $("liveAutoStopSeconds").value = reglages.live_auto_stop_seconds || "";
  $("timestamp").checked = reglages.timestamp;
  $("fontSize").value = reglages.font_size ?? "";
  $("fontColor").value = reglages.font_color;
  $("boxOpacity").value = reglages.box_opacity;
  appliquerDependanceTimestamp();
  $("timezone").value = reglages.timezone;
  $("liveProtocol").value = reglages.live_protocol;
  $("mergeJour").checked = reglages.merge_jour;
  $("mergeSemaine").checked = reglages.merge_semaine;
  $("mergeMois").checked = reglages.merge_mois;
  appliquerDependanceMergeJour();
  $("downloadAuto").checked = reglages.download_auto;
  appliquerDependanceDownloadAuto();
  afficherUrlWebhook(reglages.webhook_token);
}

// NOM_CAMERA stays a literal placeholder: the user replaces
// it themselves in their automation config; this page does
// not know ahead which camera the external call will target.
function afficherUrlWebhook(jeton) {
  $("webhookUrl").value = `${location.origin}/webhook/snapshot?camera=NOM_CAMERA&token=${jeton}`;
}
$("webhookUrl").onclick = () => $("webhookUrl").select();
$("webhookRegenerer").onclick = async () => {
  if (!confirm(t("reglages.webhook.regenerer.confirm"))) return;
  const reponse = await fetch("/api/webhook-regenerer", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: "{}" });
  const resultat = await lireJSON(reponse);
  afficherUrlWebhook(resultat.token);
};

// One tab per group rather than a long page to scroll: the name
// list both populates tabXxx/panelXxx (see the HTML template) and
// avoids repeating the same four lines for switch/open.
const ONGLETS_REGLAGES = ["General", "Video", "Acces", "Alertes"];

function basculerOngletReglages(actif) {
  for (const nom of ONGLETS_REGLAGES) {
    const estActif = nom === actif;
    $(`tab${nom}`).classList.toggle("active", estActif);
    $(`panel${nom}`).hidden = !estActif;
  }
}

for (const nom of ONGLETS_REGLAGES) {
  $(`tab${nom}`).onclick = () => basculerOngletReglages(nom);
}

function configurerDialogueReglages(configurationInitiale) {
  $("initialSetupHint").hidden = !configurationInitiale;
  $("reglagesClose").hidden = configurationInitiale;
  $("stopButton").hidden = configurationInitiale;
  $("reglages").dataset.initialSetup = configurationInitiale ? "1" : "0";
  // Always the first tab on open: easier to find than a
  // last remembered tab, for a dialog that is never left displayed
  // longtemps entre deux ouvertures.
  basculerOngletReglages("General");
}

async function ouvrirReglages(configurationInitiale = false) {
  if (!estConnecteAcces()) return;
  if (estAdmin()) {
    try {
      const reglages = await lireJSON(await fetch("/api/reglages"));
      configurationInitiale = configurationInitiale || !!reglages.initial_setup;
      afficherFormulaireReglages(reglages);
    } catch (erreur) { /* fields keep their last displayed value */ }
    configurerDialogueReglages(configurationInitiale);
    chargerCamerasMasquees();
    $("tailleCartes").value = tailleCartes;
    $("barrePagination").checked = barrePaginationHaute;
    chargerSourdine();
    chargerSuppressionAuto();
    await chargerFormulaireAcces();
  } else {
    // Guest signed in via Cloudflare: still open Settings so Shared access
    // is reachable if they are promoted to admin; otherwise show the Access tab.
    configurerDialogueReglages(false);
    $("tailleCartes").value = tailleCartes;
    $("barrePagination").checked = barrePaginationHaute;
    await chargerFormulaireAcces();
    basculerOngletReglages("Acces");
  }
  $("reglages").showModal();
}

let _accesEdition = null;

function camerasConnuesPourAcces() {
  const noms = new Map();
  if (system && system.systems) {
    for (const s of system.systems) {
      for (const c of s.cameras || []) {
        const label = c.name || c.key;
        const cle = c.key || c.name;
        if (cle) noms.set(cle, label);
      }
    }
  }
  return [...noms.entries()].sort((a, b) => comparerNoms(a[1], b[1]));
}

async function chargerFormulaireAcces() {
  const liste = $("accessUsersListe");
  if (!liste) return;
  try {
    const reponse = await fetch("/api/access");
    _accesEdition = await lireJSON(reponse);
    if (_accesEdition.error || reponse.status >= 400) {
      throw new Error(_accesEdition.error || "forbidden");
    }
  } catch (erreur) {
    liste.textContent = estAdmin()
      ? t("reglages.accessSaveFailed")
      : tf("access.pending", { email: ACCES.email || "" });
    return;
  }
  // Team domain is kept in blink_access.json only (not edited in the UI).
  if ($("cfAud")) $("cfAud").value = (_accesEdition.cloudflare || {}).aud || "";
  $("accessAdmins").value = (_accesEdition.admin_emails || []).join(", ");
  peindreListeAccesUsers();
}

function peindreListeAccesUsers() {
  const liste = $("accessUsersListe");
  if (!liste || !_accesEdition) return;
  const cameras = camerasConnuesPourAcces();
  liste.replaceChildren();
  if (!(_accesEdition.users || []).length) {
    const p = document.createElement("p");
    p.className = "sub tiny";
    p.textContent = t("reglages.accessGuestHint");
    liste.append(p);
  }
  for (const user of _accesEdition.users || []) {
    const bloc = document.createElement("div");
    bloc.className = "accessUser";
    const head = document.createElement("header");
    const titre = document.createElement("strong");
    titre.textContent = user.email;
    const retirer = document.createElement("button");
    retirer.type = "button";
    retirer.textContent = t("reglages.accessRemove");
    retirer.onclick = () => {
      _accesEdition.users = _accesEdition.users.filter((u) => u.email !== user.email);
      peindreListeAccesUsers();
    };
    head.append(titre, retirer);
    const cams = document.createElement("div");
    cams.className = "cams";
    if (!cameras.length) {
      cams.textContent = t("reglages.accessNoCams");
    } else {
      for (const [cle, label] of cameras) {
        const lab = document.createElement("label");
        const case_ = document.createElement("input");
        case_.type = "checkbox";
        const allowed = new Set(user.cameras || []);
        const labelLc = String(label).toLowerCase();
        case_.checked = allowed.has(cle) || allowed.has(label)
          || [...allowed].some((v) => String(v).toLowerCase() === labelLc
            || String(v) === cle);
        case_.onchange = () => {
          const set = new Set(user.cameras || []);
          if (case_.checked) {
            set.add(cle);
            set.add(label);
          } else {
            set.delete(cle);
            set.delete(label);
          }
          user.cameras = [...set];
        };
        lab.append(case_, document.createTextNode(" " + label));
        cams.append(lab);
      }
    }
    bloc.append(head, cams);
    liste.append(bloc);
  }
}

async function sauverFormulaireAcces() {
  if (!_accesEdition) return;
  const status = $("accessSaveStatus");
  const admins = ($("accessAdmins").value || "").split(",")
    .map((e) => e.trim()).filter(Boolean);
  const payload = {
    admin_emails: admins.length ? admins : [],
    cloudflare: {
      // Preserve team domain from disk; AUD can be edited in the UI.
      team_domain: (_accesEdition.cloudflare || {}).team_domain || "",
      aud: ($("cfAud") && $("cfAud").value || "").trim()
        || (_accesEdition.cloudflare || {}).aud || "",
    },
    users: (_accesEdition.users || []).map((u) => ({
      email: u.email,
      cameras: u.cameras || [],
      permissions: u.permissions || ["view_live", "view_clips", "view_pictures"],
    })),
  };
  try {
    _accesEdition = await lireJSON(await fetch("/api/access", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }));
    if (_accesEdition.error) throw new Error(_accesEdition.error);
    peindreListeAccesUsers();
    if ($("cfAud")) $("cfAud").value = (_accesEdition.cloudflare || {}).aud || "";
    $("accessAdmins").value = (_accesEdition.admin_emails || []).join(", ");
    if (status) {
      status.hidden = false;
      status.textContent = t("reglages.accessSaved");
    }
  } catch (erreur) {
    if (status) {
      status.hidden = false;
      status.textContent = t("reglages.accessSaveFailed");
    }
  }
}
$("reglagesButton").onclick = () => ouvrirReglages(false);
$("reglagesClose").onclick = () => $("reglages").close();
if ($("accessAddUser")) {
  $("accessAddUser").onclick = () => {
    if (!_accesEdition) return;
    const email = ($("accessNewEmail").value || "").trim().toLowerCase();
    if (!email) return;
    if ((_accesEdition.users || []).some((u) => u.email === email)) {
      $("accessNewEmail").value = "";
      return;
    }
    if ((_accesEdition.admin_emails || []).map((e) => e.toLowerCase()).includes(email)) {
      $("accessNewEmail").value = "";
      return;
    }
    _accesEdition.users = _accesEdition.users || [];
    _accesEdition.users.push({
      email,
      cameras: [],
      permissions: ["view_live", "view_clips", "view_pictures"],
    });
    $("accessNewEmail").value = "";
    peindreListeAccesUsers();
  };
}
if ($("accessSave")) $("accessSave").onclick = () => sauverFormulaireAcces();
$("reglages").addEventListener("cancel", (evenement) => {
  // Escape must not turn a fresh install into a blank page: the parent
  // awaits validation and no download will start. The dialog stays
  // dismissible on all ordinary openings.
  if ($("reglages").dataset.initialSetup === "1") evenement.preventDefault();
});

$("filtreButton").onclick = ouvrirFiltre;
$("camera").addEventListener("change", majGroupBy);
$("filtreClose").onclick = () => $("filtre").close();
$("filtreApply").onclick = appliquerFiltre;
for (const bouton of document.querySelectorAll("#filtre .presets button")) {
  bouton.onclick = () => choisirPreset(bouton.dataset.preset);
}
$("rangeFrom").oninput = choisirPlagePersonnalisee;
$("rangeTo").oninput = choisirPlagePersonnalisee;

// Opens the native server-side picker (tkinter: see /api/choisir-dossier)
// rather than an <input type="file" webkitdirectory> - that only yields a
// browser-relative folder name, never an absolute path usable by the
// server (web privacy restriction, not a limit of this code).
$("storageDirBrowse").onclick = async () => {
  const bouton = $("storageDirBrowse");
  bouton.disabled = true;
  try {
    const reponse = await fetch("/api/choisir-dossier");
    const resultat = await lireJSON(reponse);
    if (resultat.error) {
      alert(t("reglages.storageDir.browse.unavailable"));
    } else if (resultat.path) {
      $("storageDir").value = resultat.path;
    }
  } catch (erreur) {
    alert(t("reglages.storageDir.browse.unavailable"));
  } finally {
    bouton.disabled = false;
  }
};

// Week and month only make sense if daily runs: unchecking
// "day" greys and unchecks them, rather than letting you hope for an
// aggregate that will never be built without a base.
function appliquerDependanceMergeJour() {
  const actif = $("mergeJour").checked;
  $("mergeSemaine").disabled = !actif;
  $("mergeMois").disabled = !actif;
  if (!actif) {
    $("mergeSemaine").checked = false;
    $("mergeMois").checked = false;
  }
}
$("mergeJour").onchange = appliquerDependanceMergeJour;

// Cadences no longer make sense if nothing is downloaded: greyed rather
// than removed, so the last value returns when rechecked.
function appliquerDependanceDownloadAuto() {
  const actif = $("downloadAuto").checked;
  $("usbMinutes").disabled = !actif;
  $("cloudMinutes").disabled = !actif;
}
$("downloadAuto").onchange = appliquerDependanceDownloadAuto;

// Size/color/opacity only take effect if the timestamp is burned in:
// greyed rather than removed, same principle as cadences above.
function appliquerDependanceTimestamp() {
  const actif = $("timestamp").checked;
  $("fontSize").disabled = !actif;
  $("fontColor").disabled = !actif;
  $("boxOpacity").disabled = !actif;
}
$("timestamp").onchange = appliquerDependanceTimestamp;

// Separate from the rest of the panel: unlike cadences, port or
// timezone, mute needs no restart (watch re-reads its state on each
// pass), so each checkbox applies at once, like the Exclude button of a
// clip.
async function chargerListeReglageCameras({ liste, url, traduction, selection, champ, nomCamera }) {
  const conteneur = $(liste);
  conteneur.textContent = t(`${traduction}.loading`);
  let etat;
  try {
    etat = await lireJSON(await fetch(url));
  } catch (erreur) {
    conteneur.textContent = t(`${traduction}.unavailable`);
    return;
  }
  if (!etat.cameras.length) {
    conteneur.textContent = t(`${traduction}.none`);
    return;
  }
  conteneur.replaceChildren();
  for (const entree of etat.cameras) {
    const camera = nomCamera(entree);
    const label = document.createElement("label");
    const case_ = document.createElement("input");
    case_.type = "checkbox";
    case_.checked = etat[selection].includes(camera);
    case_.onchange = async () => {
      case_.disabled = true;
      try {
        const reponse = await fetch(url, { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ camera, [champ]: case_.checked }) });
        const resultat = await lireJSON(reponse);
        if (resultat.error) {
          alert(resultat.error);
          case_.checked = !case_.checked;
        }
      } catch (erreur) {
        alert(String(erreur));
        case_.checked = !case_.checked;
      } finally {
        case_.disabled = false;
      }
    };
    label.appendChild(case_);
    label.append(` ${camera}`);
    conteneur.appendChild(label);
  }
}

async function chargerSourdine() {
  return chargerListeReglageCameras({
    liste: "sourdineListe", url: "/api/sourdine", traduction: "sourdine",
    selection: "ignored", champ: "ignored", nomCamera: (camera) => camera,
  });
}

// Same pattern as chargerSourdine(): each checkbox applies at once,
// no restart. A camera may carry several internal keys over time
// (Sync Module replaced, old USB clips without a stable id - GitHub
// issue #1): the page only shows its name; name -> key(s) resolution is
// handled on the server (suppression_auto_choices() in serve.py), never
// here.
async function chargerSuppressionAuto() {
  return chargerListeReglageCameras({
    liste: "suppressionAutoListe", url: "/api/suppression-auto", traduction: "suppressionAuto",
    selection: "actives", champ: "actif", nomCamera: (camera) => camera.name,
  });
}

// "Check for updates": GitHub right away, without waiting for the server
// background thread (at most one hour). A found version shows
// the "Install" button at the top of the page, as usual (issue #35).
$("verifierMajButton").onclick = async () => {
  const bouton = $("verifierMajButton");
  bouton.disabled = true;
  bouton.textContent = t("reglages.checkUpdates.running");
  try {
    const resultat = await lireJSON(await fetch("/api/maj/verifier", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" }));
    if (resultat.error) {
      alert(resultat.error);
      return;
    }
    montrerMaj(resultat.maj);
    if (!resultat.ok) alert(t("reglages.checkUpdates.offline"));
    else if (resultat.maj && resultat.maj.version) {
      alert(tf("reglages.checkUpdates.found", { version: resultat.maj.version }));
    } else alert(tf("reglages.checkUpdates.none", { version: resultat.version }));
  } catch (erreur) {
    alert(t("reglages.checkUpdates.offline"));
  } finally {
    bouton.disabled = false;
    bouton.textContent = t("reglages.checkUpdates");
  }
};

// Same flow as the update button: save, wait for the
// server to restart, then get the page back. The three wait paths
// stay distinct: new port, first configuration, ordinary relaunch.
function lireParametresReglages() {
  const usb = parseInt($("usbMinutes").value, 10);
  const cloud = parseInt($("cloudMinutes").value, 10);
  const port = parseInt($("port").value, 10);
  if (!(usb >= 1) || !(cloud >= 1)) {
    alert(t("reglages.error.cadence"));
    return null;
  }
  if (!(port >= 1 && port <= 65535)) {
    alert(t("reglages.error.port"));
    return null;
  }
  const timezone = $("timezone").value.trim();
  if (!timezone) {
    alert(t("reglages.error.timezone"));
    return null;
  }
  return { usb, cloud, port, timezone };
}

async function envoyerFormulaireReglages({ usb, cloud, port, timezone }) {
  const reponse = await fetch("/api/reglages", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      usb_minutes: usb, cloud_minutes: cloud, port,
      storage_dir: $("storageDir").value.trim(),
      trusted_host: $("trustedHost").value.trim(),
      webhook_notif_url: $("webhookNotifUrl").value.trim(),
      live_auto_stop_seconds: Number($("liveAutoStopSeconds").value) || 0,
      timestamp: $("timestamp").checked, timezone,
      live_protocol: $("liveProtocol").value,
      merge_jour: $("mergeJour").checked,
      merge_semaine: $("mergeSemaine").checked,
      merge_mois: $("mergeMois").checked,
      download_auto: $("downloadAuto").checked,
      font_size: $("fontSize").value.trim() ? parseInt($("fontSize").value, 10) : null,
      font_color: $("fontColor").value.trim(),
      box_opacity: parseFloat($("boxOpacity").value),
    }) });
  return lireJSON(reponse);
}

$("reglagesApply").onclick = async () => {
  const parametres = lireParametresReglages();
  if (!parametres) return;

  const bouton = $("reglagesApply");
  let configurationInitiale = false;
  bouton.disabled = true;
  bouton.textContent = t("reglages.restarting");
  try {
    const resultat = await envoyerFormulaireReglages(parametres);
    if (resultat.error) {
      alert(resultat.error);
      return;
    }
    configurationInitiale = !!resultat.initial_setup;
  } catch (erreur) {
    // Restart may cut the response. Keep polling in that
    // case: its guard delay will signal if the stop never started.
  } finally {
    bouton.disabled = false;
    bouton.textContent = t("reglages.apply");
  }
  $("reglages").close();

  if (parametres.port !== portActuel) {
    attendreChangementPortReglages(parametres.port);
  } else if (configurationInitiale) {
    attendreFinConfigurationInitiale();
  } else {
    attendreRedemarrageReglages();
  }
};

function afficherAttenteReglages(message) {
  $("phase").textContent = message;
  $("bar").removeAttribute("value");
  $("work").classList.add("on");
  $("refresh").disabled = true;
}

function signalerEchecRedemarrageReglages() {
  $("phase").textContent = t("reglages.restartFailed");
  $("bar").value = 0;
  $("refresh").disabled = false;
}

function attendreChangementPortReglages(port) {
  // Wait for the old server to disappear before leaving its origin.
  const nouvelleAdresse = `http://${location.hostname}:${port}/`;
  afficherAttenteReglages(tf("reglages.portchange", { url: nouvelleAdresse }));
  let parti = false;
  const attentePort = setInterval(async () => {
    try {
      await fetch("/api/status", { cache: "no-store" });
    } catch (erreur) {
      parti = true;
    }
    if (parti) {
      clearInterval(attentePort);
      // Give the new server time to bind the port.
      setTimeout(() => { location.href = nouvelleAdresse; }, 3000);
    }
  }, 1000);
  setTimeout(() => {
    if (!parti) {
      clearInterval(attentePort);
      signalerEchecRedemarrageReglages();
    }
  }, 45000);
}

function attendreFinConfigurationInitiale() {
  // The start parent replaces the temporary server after validation. The
  // explicit status avoids missing a very brief cut between the two.
  afficherAttenteReglages(t("reglages.restarting.settings"));
  let delaiInitial = null;
  const attenteInitiale = setInterval(async () => {
    try {
      const reponse = await fetch("/api/status", { cache: "no-store" });
      if (reponse.status === 403) {
        // The new server is there, with its own token: this page can
        // no longer query it, and .json() failed here silently until the
        // failure delay (audit 26/09/2026, B04). A fresh page gets the
        // new token; lancerParcoursInitial() only reopens settings there
        // if the server still requires them.
        clearInterval(attenteInitiale);
        clearTimeout(delaiInitial);
        location.reload();
        return;
      }
      const etat = await reponse.json();
      if (etat.initial_setup === false) {
        clearInterval(attenteInitiale);
        clearTimeout(delaiInitial);
        // Remove ?setup=1 so already-validated settings are not reopened.
        location.href = location.pathname;
      }
    } catch (erreur) { /* coupure attendue entre les deux serveurs */ }
  }, 1000);
  delaiInitial = setTimeout(() => {
    clearInterval(attenteInitiale);
    signalerEchecRedemarrageReglages();
  }, 60000);
}

function attendreRedemarrageReglages() {
  afficherAttenteReglages(t("reglages.restarting.settings"));
  const suivi = { parti: false };
  const attente = setInterval(() => sonderRelance(suivi), 2000);
  setTimeout(() => {
    if (!suivi.parti) {
      clearInterval(attente);
      signalerEchecRedemarrageReglages();
    }
  }, 45000);
}

$("redemarrerButton").onclick = async () => {
  const bouton = $("redemarrerButton");
  bouton.disabled = true;
  bouton.textContent = t("reglages.restarting");
  try {
    const reponse = await fetch("/api/redemarrer", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    const resultat = await lireJSON(reponse);
    if (resultat.error) {
      alert(resultat.error);
      return;
    }
  } catch (erreur) {
    // As with Apply: restart may cut the response before it arrives;
    // the poll below takes over.
  } finally {
    bouton.disabled = false;
    bouton.textContent = t("reglages.restart");
  }
  $("reglages").close();
  attendreRedemarrageReglages();
};

$("stopButton").onclick = async () => {
  const bouton = $("stopButton");
  bouton.disabled = true;
  bouton.textContent = t("stop.stopping");
  try {
    const reponse = await fetch("/api/stop", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    const resultat = await reponse.json();
    if (resultat.error) {
      alert(resultat.error);
      bouton.disabled = false;
      bouton.textContent = t("reglages.stop");
      return;
    }
  } catch (erreur) { /* response may not arrive; stop is already launched */ }
  document.body.innerHTML = `<p class="empty">${t("stop.stopped")}</p>`;
  // "accepted" is not "done". If the server still answers after the
  // max grace + kill delay, make the failure visible instead of claiming
  // forever that the app is stopped.
  setTimeout(async () => {
    try {
      const reponse = await fetch("/api/status", { cache: "no-store" });
      if (reponse.ok) {
        document.body.innerHTML = `<p class="empty">${t("stop.failed")}</p>`;
      }
    } catch (erreur) { /* expected disappearance: stop confirmed */ }
  }, 30000);
};

// Cards are rebuilt on each refresh: delegation on
// their container keeps handlers out of HTML strings and avoids any
// interpretation of a camera or file name as JavaScript code.
$("list").addEventListener("click", (event) => {
  const cible = event.target.closest("[data-action]");
  if (!cible || !$("list").contains(cible)) return;
  const name = cible.dataset.name || "";
  switch (cible.dataset.action) {
    case "clip-page":
      changerPageClips(Number(cible.dataset.page), cible.dataset.direction);
      break;
    case "arm":
      setArmed(cible.dataset.scope, name, cible.dataset.armed === "true");
      break;
    case "wake":
      reveillerCamera(name, cible);
      break;
    case "continu":
      // The checkbox toggles by itself (no preventDefault here): we
      // keep its new state, read by the live retry loops.
      choisirDirectContinu(name, cible.checked);
      break;
    case "snapshot":
      prendreSnapshot(name, cible);
      break;
    case "watch-live":
      if (rotationActive && $("view").value === "alllive") {
        if (rotationTimer) clearTimeout(rotationTimer);
        rotationTimer = null;
        arreterCountdownRotation();
        rotationCameraCourante = name;
        afficherAttenteRotation(name);
      }
      watchLive(name);
      break;
    case "stop-live":
      stopWatch(name);
      break;
    case "fullscreen":
      toggleFullscreen(name);
      break;
    case "toggle-record":
      toggleRecord(name, cible);
      break;
    case "delete-snapshot":
      supprimerSnapshot(cible.dataset.fichier, cible);
      break;
  }
});
$("list").addEventListener("submit", (event) => {
  if (event.target.dataset.action !== "clip-page-jump") return;
  event.preventDefault();
  changerPageClips(event.target.elements.page.valueAsNumber - 1);
});
$("list").addEventListener("change", (event) => {
  const cible = event.target.closest("[data-action]");
  if (!cible || !$("list").contains(cible)) return;
  if (cible.dataset.action === "clip-page-size") {
    changerPageClips(0, "size", Number(cible.value));
  } else if (cible.dataset.action === "stage-exclusion") {
    stagerExclusion(cible.dataset.identity, cible.checked);
  } else if (cible.dataset.action === "stage-suppression") {
    stagerSuppression(cible.dataset.identity, cible.checked);
  }
});
$("applyButton").onclick = appliquerSelection;
document.querySelectorAll("[data-lang-btn]").forEach((button) => {
  button.onclick = () => setLang(button.dataset.langBtn, true);
});

// Ensure All Live Views exists even when the embedded HTML is from an
// older build (js updated without rebuilding the exe).
(function assurerOptionAllLive() {
  const sel = $("view");
  if (!sel || sel.querySelector('option[value="alllive"]')) return;
  const opt = document.createElement("option");
  opt.value = "alllive";
  opt.dataset.i18n = "view.alllive";
  opt.textContent = t("view.alllive");
  const rotation = sel.querySelector('option[value="rotation"]');
  if (rotation) sel.insertBefore(opt, rotation);
  else sel.append(opt);
})();

// Default view set BEFORE setLang(): that calls render(), which reads
// $("view").value to decide what to paint. Live Views is the default.
$("view").value = "live";
// Remembered manual override first; else the browser language, as
// on lidar2map's first launch. Placed here, at end of script: setLang()
// calls render()/renderLive(), which reference `let`s declared above
// (MSE_ABORT, actualisationLocale...) — called too early, before those
// declarations run, raises ReferenceError (temporal dead zone), as seen
// when testing for real in the browser.
(async function lancerParcoursInitial() {
  await chargerAcces();
  if (ACCES.role === "unauthenticated") {
    setLang(localStorage.getItem("lang") || detectLang(), false);
    appliquerRoleAcces();
    return;
  }
  setLang(localStorage.getItem("lang") || detectLang(), false);
  load();
  // E-01: login then settings chain in the same tab. The second
  // dialog opens only after successful auth; cancel
  // leaves the parent waiting and ensures no worker starts.
  const parametres = new URLSearchParams(location.search);
  let continuer = true;
  if (estAdmin() && parametres.get("login") === "1") {
    continuer = await authenticate();
  }
  if (estAdmin() && continuer && parametres.get("setup") === "1") {
    // A page reloaded with ?setup=1 after validation (stale token on
    // server replacement, see lireJSON()) only reopens the dialog as
    // required, with neither Close nor Escape, if the server still
    // requires it (audit 26/09/2026, B04). When in doubt, keep it
    // mandatory.
    let exige = true;
    try {
      const etat = await lireJSON(await fetch("/api/status", { cache: "no-store" }));
      exige = etat.initial_setup !== false;
    } catch (erreur) { /* unreadable status: keep the dialog mandatory */ }
    if (exige) {
      await ouvrirReglages(true);
    } else {
      history.replaceState(null, "", location.pathname);
    }
  }
})();
