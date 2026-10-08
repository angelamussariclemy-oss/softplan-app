"""
Automazione Moodle UniPD (medicina.elearning.unipd.it) con Playwright.

Flusso:
  1. apre https://medicina.elearning.unipd.it/
  2. clicca "Login" / "Accedi" e passa al Single Sign-On UniPD
  3. inserisce le credenziali (lette da variabili d'ambiente o chieste a terminale)
  4. naviga: Laurea Magistrale a ciclo unico -> Farmacia (ME2946) -> Precorso di calcolo
  5. apre una alla volta le videolezioni non ancora completate e le riproduce
     fino alla fine (velocità 1x, browser visibile), poi torna al corso.

Uso:
  python unipd_videolezioni.py               # browser visibile (consigliato)
  python unipd_videolezioni.py --dry-run     # elenca soltanto le videolezioni da vedere
  python unipd_videolezioni.py --course-url "https://medicina.elearning.unipd.it/course/view.php?id=XXXX"
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Frame,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# --------------------------------------------------------------------------- #
# Configurazione
# --------------------------------------------------------------------------- #
BASE_URL = "https://medicina.elearning.unipd.it/"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "auth_state.json"        # sessione salvata (cookie) per evitare login ripetuti
SCREENSHOT_DIR = HERE / "screenshots"         # screenshot salvati in caso di errore

# Percorso nelle categorie di Moodle (regex case-insensitive, nell'ordine in cui si cliccano)
CATEGORY_PATH = [
    r"laurea\s+magistrale\s+a\s+ciclo\s+unico",
    r"farmacia",
    r"precorso\s+di\s+calcolo|me\s*2946",
]
COURSE_SEARCH_TERM = "Precorso di calcolo"

# Tipi di attività Moodle che di solito contengono video (classe CSS "modtype_<nome>")
VIDEO_MODTYPES = {"kalvidres", "kalvidpres", "videotime", "hvp", "h5pactivity", "url", "page", "resource", "lti"}
VIDEO_NAME_HINT = re.compile(r"video|lezione|lesson|registrazion|parte\s*\d", re.I)

# Testi che Moodle (IT/EN) usa per lo stato di completamento
TODO_TEXT = re.compile(r"da fare|to do|non completat|not completed|segna come fatto|mark as done", re.I)
DONE_TEXT = re.compile(r"\bfatto\b|\bdone\b|completato|completed", re.I)

NAV_TIMEOUT_MS = 30_000
SSO_WAIT_S = 300          # tempo concesso per completare login/MFA a mano
VIDEO_START_TIMEOUT_S = 60
VIDEO_EXTRA_MARGIN_S = 120  # margine oltre la durata del video (buffering, pause)

log = logging.getLogger("unipd")


@dataclass
class Activity:
    name: str
    url: str
    modtype: str


# --------------------------------------------------------------------------- #
# Utility
# --------------------------------------------------------------------------- #
def screenshot(page: Page, label: str) -> None:
    """Salva uno screenshot per il debug; non deve mai far fallire lo script."""
    try:
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        path = SCREENSHOT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{re.sub(r'[^\w-]+', '_', label)[:60]}.png"
        page.screenshot(path=str(path), full_page=True)
        log.info("Screenshot salvato: %s", path)
    except PlaywrightError as exc:
        log.warning("Impossibile salvare lo screenshot: %s", exc)


def goto(page: Page, url: str, retries: int = 3) -> None:
    """page.goto con qualche tentativo in caso di errori di rete."""
    for attempt in range(1, retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
            return
        except PlaywrightTimeoutError:
            # "networkidle" può non arrivare mai su pagine con polling: la pagina è comunque caricata
            if page.url.startswith("http"):
                return
        except PlaywrightError as exc:
            log.warning("Navigazione verso %s fallita (tentativo %d/%d): %s", url, attempt, retries, exc)
            if attempt == retries:
                raise
            time.sleep(2 * attempt)


def first_visible(page_or_frame: Page | Frame, selectors: list[str], timeout_ms: int = 5_000) -> Locator | None:
    """Ritorna il primo locator visibile tra quelli proposti (attendendo fino a timeout_ms)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for sel in selectors:
            loc = page_or_frame.locator(sel).first
            try:
                if loc.is_visible():
                    return loc
            except PlaywrightError:
                pass
        time.sleep(0.25)
    return None


def is_logged_in(page: Page) -> bool:
    # In Moodle il menu utente compare solo dopo il login; la pagina di login ha "loginform"
    return page.locator("#user-menu-toggle, .usermenu .userbutton, a[href*='logout.php']").count() > 0


# --------------------------------------------------------------------------- #
# Passi 1-3: apertura sito e login
# --------------------------------------------------------------------------- #
def login(page: Page, username: str, password: str) -> None:
    goto(page, BASE_URL)
    if is_logged_in(page):
        log.info("Sessione già attiva, login non necessario.")
        return

    log.info("Clic su 'Login'/'Accedi'...")
    login_link = first_visible(page, [
        "a:has-text('Accedi')", "a:has-text('Login')", "a:has-text('Log in')",
        "a[href*='login/index.php']", "button:has-text('Accedi')",
    ], timeout_ms=10_000)
    if login_link is None:
        raise RuntimeError("Pulsante di login non trovato nella home page.")
    login_link.click()
    page.wait_for_load_state("domcontentloaded")

    # La pagina di login di Moodle UniPD mostra i pulsanti del Single Sign-On
    sso_button = first_visible(page, [
        "a.login-identityprovider-btn", ".potentialidplist a", "a[href*='auth/shibboleth']",
        "a[href*='auth/saml']", "a[href*='auth/oauth2']",
        "a:has-text('Single Sign On')", "a:has-text('SSO')", "a:has-text('Shibboleth')",
        "a:has-text('Università di Padova')",
    ], timeout_ms=5_000)
    if sso_button is not None:
        log.info("Selezione accesso SSO UniPD: %s", sso_button.inner_text().strip()[:60])
        sso_button.click()
        try:
            page.wait_for_url(lambda url: "/login/index.php" not in url, timeout=15_000)
        except PlaywrightTimeoutError:
            log.warning("Il clic sul pulsante SSO non ha cambiato pagina.")

    log.info("Inserimento credenziali...")
    user_field = first_visible(page, [
        "input[name='j_username']", "#j_username_js", "input[name='username']",
        "input[type='email']", "input[autocomplete='username']",
    ], timeout_ms=15_000)
    pass_field = first_visible(page, [
        "input[name='j_password']", "input[name='password']", "input[type='password']",
    ], timeout_ms=5_000)

    if user_field is None or pass_field is None:
        # Pagina di login diversa dal previsto: si completa l'accesso a mano nel browser
        screenshot(page, "login_campi_non_trovati")
        log.warning(">>> Non riesco a compilare il login da solo su %s", page.url)
        log.warning(">>> FAI IL LOGIN A MANO nella finestra di Chrome: lo script riparte da solo dopo.")
    else:
        user_field.fill(username)
        # Il SSO UniPD a volte chiede il dominio (studenti.unipd.it / unipd.it) in una select
        domain_select = page.locator("select[name*='dominio' i], select[id*='domain' i], select[name*='domain' i]").first
        if domain_select.count() and "@" not in username:
            options = domain_select.locator("option").all_inner_texts()
            studenti = next((o.strip() for o in options if "studenti" in o.lower()), None)
            if studenti:
                domain_select.select_option(label=studenti)
        pass_field.fill(password)

        submit = first_visible(page, [
            "button[type='submit']", "input[type='submit']", "button:has-text('Accedi')", "button:has-text('Login')",
        ])
        if submit is not None:
            submit.click()
        else:
            pass_field.press("Enter")

    # Attesa del rientro su Moodle (fuori dalla pagina di login). Se c'è l'autenticazione
    # a due fattori o un consenso attributi, l'utente può completarli nel browser.
    log.info("Attendo il rientro su Moodle (completa eventuale MFA/login nel browser, max %ds)...", SSO_WAIT_S)
    try:
        page.wait_for_url(
            lambda url: url.startswith("https://medicina.elearning.unipd.it/") and "/login/" not in url,
            timeout=SSO_WAIT_S * 1000,
        )
        page.wait_for_load_state("domcontentloaded")
    except PlaywrightTimeoutError:
        screenshot(page, "login_timeout")
        raise RuntimeError("Login non completato: credenziali errate o MFA non confermata in tempo.")

    if not is_logged_in(page):
        screenshot(page, "login_fallito")
        raise RuntimeError("Rientrato su Moodle ma l'utente non risulta autenticato.")
    log.info("Login completato.")


# --------------------------------------------------------------------------- #
# Passo 4a: navigazione fino al corso
# --------------------------------------------------------------------------- #
def open_course(page: Page, course_url: str | None) -> None:
    if course_url:
        log.info("Apro direttamente il corso: %s", course_url)
        goto(page, course_url)
        return

    log.info("Navigazione tra le categorie: %s", " -> ".join(CATEGORY_PATH))
    try:
        goto(page, BASE_URL + "course/index.php")
        for pattern in CATEGORY_PATH:
            link = page.get_by_role("link", name=re.compile(pattern, re.I)).first
            link.wait_for(state="visible", timeout=10_000)
            log.info("  clic su: %s", link.inner_text().strip())
            link.click()
            page.wait_for_load_state("domcontentloaded")
        if "course/view.php" in page.url:
            return
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        log.warning("Navigazione per categorie non riuscita (%s). Uso la ricerca corsi.", exc.__class__.__name__)

    # Fallback: ricerca corsi di Moodle
    goto(page, f"{BASE_URL}course/search.php?search={COURSE_SEARCH_TERM.replace(' ', '+')}")
    result = page.locator(".coursename a, h3.coursename a, a.aalink.coursename").filter(
        has_text=re.compile(r"precorso.*calcolo", re.I)
    ).first
    try:
        result.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeoutError:
        screenshot(page, "corso_non_trovato")
        raise RuntimeError("Corso 'Precorso di calcolo' non trovato. Usa --course-url.")
    log.info("Corso trovato tramite ricerca: %s", result.inner_text().strip())
    result.click()
    page.wait_for_url(re.compile(r"course/view\.php"), timeout=NAV_TIMEOUT_MS)


# --------------------------------------------------------------------------- #
# Passo 4b: individuazione videolezioni non viste
# --------------------------------------------------------------------------- #
def completion_state(activity: Locator) -> str:
    """'done', 'todo' o 'unknown' in base agli indicatori di completamento di Moodle 3.x/4.x."""
    # Moodle 4.x: badge/pulsanti dentro la regione "completionrequirements"
    region = activity.locator("[data-region='completionrequirements'], .completion-info, .activity-information")
    texts = " ".join(t for t in region.all_inner_texts() if t)
    # Moodle 3.x: icone di completamento con attributo alt/title
    for img in activity.locator(".autocompletion img, .togglecompletion img, img.icon[src*='completion']").all():
        texts += " " + (img.get_attribute("alt") or "") + " " + (img.get_attribute("title") or "")
    # Pulsante manuale "Segna come fatto" => non completato
    if activity.locator("button[data-toggletype='manual:mark-done']").count():
        return "todo"
    if activity.locator("button[data-toggletype='manual:undo']").count():
        return "done"
    if TODO_TEXT.search(texts):
        return "todo"
    if DONE_TEXT.search(texts):
        return "done"
    return "unknown"


def find_unwatched_videos(page: Page, include_unknown: bool) -> list[Activity]:
    page.locator("li.activity").first.wait_for(state="attached", timeout=NAV_TIMEOUT_MS)
    # Espande eventuali sezioni compresse (formato "collapsed topics" / Moodle 4)
    for toggle in page.locator("a[data-toggle='collapse'][aria-expanded='false'], .collapsed[data-for='sectiontoggler']").all():
        try:
            toggle.click(timeout=2_000)
        except PlaywrightError:
            pass

    activities: list[Activity] = []
    for item in page.locator("li.activity").all():
        classes = item.get_attribute("class") or ""
        modtype = next((c.removeprefix("modtype_") for c in classes.split() if c.startswith("modtype_")), "")
        link = item.locator("a.aalink, .activityname a, .activityinstance a").first
        if not link.count():
            continue
        name = re.sub(r"\s+", " ", link.inner_text()).strip()
        url = link.get_attribute("href") or ""
        is_video = modtype in {"kalvidres", "kalvidpres", "videotime", "hvp", "h5pactivity"} or (
            modtype in VIDEO_MODTYPES and VIDEO_NAME_HINT.search(name)
        )
        if not is_video or not url:
            continue
        state = completion_state(item)
        log.debug("Attività '%s' [%s] stato=%s", name, modtype, state)
        if state == "todo" or (state == "unknown" and include_unknown):
            activities.append(Activity(name=name, url=url, modtype=modtype))
    return activities


# --------------------------------------------------------------------------- #
# Passo 4c: riproduzione di una videolezione
# --------------------------------------------------------------------------- #
def find_video_frame(page: Page, timeout_s: int) -> Frame | None:
    """Il player (Kaltura, H5P, YouTube...) è spesso in un iframe annidato: cerca il frame con <video>."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for frame in page.frames:
            try:
                if frame.locator("video").count() > 0:
                    return frame
            except PlaywrightError:
                continue  # frame staccato durante il caricamento
        time.sleep(1)
    return None


def start_playback(frame: Frame) -> None:
    # Prima un vero clic sul pulsante play (gesto utente), poi fallback via JS
    play_btn = first_visible(frame, [
        "button[aria-label*='play' i]", "button[title*='play' i]", ".playkit-pre-playback-play-button",
        ".largePlayBtn", ".vjs-big-play-button", ".h5p-play", ".ytp-large-play-button",
    ], timeout_ms=5_000)
    if play_btn is not None:
        try:
            play_btn.click()
        except PlaywrightError:
            pass
    frame.evaluate("""() => {
        const v = document.querySelector('video');
        if (v && v.paused) { v.play().catch(() => {}); }
    }""")


def watch_video(page: Page, activity: Activity) -> bool:
    log.info("▶ Apro: %s", activity.name)
    goto(page, activity.url)

    # Attività di tipo URL/Risorsa: il link può aprire una pagina intermedia con il vero link
    if activity.modtype in {"url", "resource"} and find_video_frame(page, 5) is None:
        inner = page.locator(".urlworkaround a, .resourceworkaround a, #region-main a[href]").first
        if inner.count():
            inner.click()
            page.wait_for_load_state("domcontentloaded")

    frame = find_video_frame(page, VIDEO_START_TIMEOUT_S)
    if frame is None:
        log.warning("Nessun player video trovato in '%s'.", activity.name)
        screenshot(page, f"no_video_{activity.name}")
        return False

    start_playback(frame)
    try:
        frame.wait_for_function(
            "() => { const v = document.querySelector('video'); return v && !v.paused && v.currentTime > 0; }",
            timeout=VIDEO_START_TIMEOUT_S * 1000,
        )
    except PlaywrightTimeoutError:
        log.warning("Il video non è partito automaticamente: premi play nel browser.")

    duration = frame.evaluate("() => document.querySelector('video')?.duration || 0") or 0
    if not duration or duration == float("inf"):
        duration = 2 * 3600  # durata ignota (stream): limite di sicurezza di 2 ore
    log.info("  durata: %d min %02d s", duration // 60, duration % 60)

    deadline = time.monotonic() + duration + VIDEO_EXTRA_MARGIN_S
    last_log = 0.0
    while time.monotonic() < deadline:
        try:
            state = frame.evaluate("""() => {
                const v = document.querySelector('video');
                return v ? {t: v.currentTime, d: v.duration, ended: v.ended, paused: v.paused} : null;
            }""")
        except PlaywrightError:
            # Il player può ricaricare l'iframe (es. cambio qualità): lo ricerco
            frame = find_video_frame(page, 30)
            if frame is None:
                break
            continue
        if state is None:
            break
        if state["ended"] or (state["d"] and state["t"] >= state["d"] - 1):
            log.info("  ✔ completato: %s", activity.name)
            return True
        if state["paused"]:
            start_playback(frame)  # riprende se il player si è fermato da solo
        if time.monotonic() - last_log > 60:
            log.info("  avanzamento: %d%%", 100 * state["t"] / (state["d"] or 1))
            last_log = time.monotonic()
        time.sleep(5)

    log.warning("Timeout durante la riproduzione di '%s'.", activity.name)
    screenshot(page, f"timeout_{activity.name}")
    return False


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def get_credentials() -> tuple[str, str]:
    username = os.environ.get("UNIPD_USER") or input("Username UniPD (nome.cognome@studenti.unipd.it): ").strip()
    password = os.environ.get("UNIPD_PASSWORD") or getpass.getpass("Password UniPD: ")
    if not username or not password:
        raise SystemExit("Credenziali mancanti.")
    return username, password


def new_context(browser, use_saved_state: bool) -> BrowserContext:
    kwargs = {"locale": "it-IT", "viewport": {"width": 1366, "height": 850}}
    if use_saved_state and STATE_FILE.exists():
        kwargs["storage_state"] = str(STATE_FILE)
    ctx = browser.new_context(**kwargs)
    ctx.set_default_timeout(NAV_TIMEOUT_MS)
    return ctx


def run(args: argparse.Namespace) -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=args.headless,
            slow_mo=100,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        context = new_context(browser, use_saved_state=not args.fresh_login)
        page = context.new_page()
        exit_code = 0
        try:
            goto(page, BASE_URL)
            if not is_logged_in(page):
                username, password = get_credentials()
                login(page, username, password)
            context.storage_state(path=str(STATE_FILE))

            open_course(page, args.course_url)
            course_url = page.url
            log.info("Pagina del corso: %s", course_url)

            pending = find_unwatched_videos(page, include_unknown=args.include_unknown)
            log.info("Videolezioni non ancora viste: %d", len(pending))
            for i, act in enumerate(pending, 1):
                log.info("  %d. %s [%s]", i, act.name, act.modtype)
            if args.dry_run or not pending:
                return 0

            failures = 0
            for act in pending[: args.limit or None]:
                try:
                    if not watch_video(page, act):
                        failures += 1
                except (PlaywrightError, RuntimeError) as exc:
                    failures += 1
                    log.error("Errore su '%s': %s", act.name, exc)
                    screenshot(page, f"errore_{act.name}")
                finally:
                    # Torno al corso così Moodle aggiorna lo stato di completamento
                    goto(page, course_url)
            log.info("Fine. Completate: %d, con problemi: %d", len(pending[: args.limit or None]) - failures, failures)
            exit_code = 1 if failures else 0
        except (PlaywrightError, RuntimeError) as exc:
            log.error("Esecuzione interrotta: %s", exc)
            screenshot(page, "errore_fatale")
            exit_code = 2
        except KeyboardInterrupt:
            log.info("Interrotto dall'utente.")
            exit_code = 130
        finally:
            context.close()
            browser.close()
        return exit_code


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Videolezioni non viste - Precorso di calcolo (Farmacia ME2946)")
    p.add_argument("--course-url", help="URL diretto del corso (salta la navigazione tra le categorie)")
    p.add_argument("--dry-run", action="store_true", help="elenca le videolezioni da vedere senza riprodurle")
    p.add_argument("--limit", type=int, default=0, help="numero massimo di videolezioni da riprodurre")
    p.add_argument("--include-unknown", action="store_true",
                   help="includi anche i video senza tracciamento del completamento")
    p.add_argument("--headless", action="store_true", help="browser invisibile (sconsigliato: MFA e play manuale)")
    p.add_argument("--fresh-login", action="store_true", help="ignora la sessione salvata in auth_state.json")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(run(args))
