"""
Automazione Moodle UniPD (medicina.elearning.unipd.it) con Playwright.

Flusso:
  1. apre https://medicina.elearning.unipd.it/
  2. clicca "Login" / "Accedi" e passa al Single Sign-On UniPD
  3. inserisce le credenziali (lette da variabili d'ambiente o chieste a terminale)
  4. naviga: I miei corsi -> Precorso di calcolo
     (in alternativa: categorie Laurea Magistrale a ciclo unico -> Farmacia -> corso, o ricerca)
  5. percorre le unità in ordine, fino alla fine:
     - videolezione non completata: la riproduce fino alla fine (velocità 1x, browser visibile);
     - test non completato: lo apre e aspetta che tu lo svolga, poi prosegue.
     Dopo ogni attività ricarica il corso, così compaiono le unità sbloccate dal test.

Uso:
  python unipd_videolezioni.py               # browser visibile (consigliato)
  python unipd_videolezioni.py --dry-run     # elenca soltanto le attività da fare
  python unipd_videolezioni.py --skip-tests  # solo video, salta i test
  python unipd_videolezioni.py --course-url "https://medicina.elearning.unipd.it/course/view.php?id=XXXX"

Funziona anche con altri Moodle UniPD passando l'URL del corso, per esempio:
  python unipd_videolezioni.py --course-url "https://elearning.unipd.it/formazione/course/view.php?id=383"
Se il corso chiede di confermare la presenza, lo script si ferma, ti avvisa con suono e
notifica e aspetta che la conferma la faccia tu nel browser: non la clicca mai da solo.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import subprocess
import sys
import time
from collections import Counter
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
STATE_FILE = HERE / "auth_state.json"        # sessione salvata (cookie); un file per ogni sito Moodle
SCREENSHOT_DIR = HERE / "screenshots"         # screenshot salvati in caso di errore

# Percorso nelle categorie di Moodle (regex case-insensitive, nell'ordine in cui si cliccano)
CATEGORY_PATH = [
    r"laurea\s+magistrale\s+a\s+ciclo\s+unico",
    r"farmacia",
    r"precorso\s+di\s+calcolo|me\s*2946",
]
COURSE_SEARCH_TERM = "Precorso di calcolo"
COURSE_NAME = re.compile(r"precorso.*calcolo", re.I)

# Tipi di attività Moodle (classe CSS "modtype_<nome>"). Tutte le altre attività non completate
# vengono aperte: se contengono un video lo script lo riproduce, altrimenti passa oltre.
TEST_MODTYPES = {"quiz"}
NON_VIDEO_MODTYPES = {
    "forum", "assign", "choice", "feedback", "glossary", "wiki", "chat", "workshop", "folder",
    "data", "survey", "attendance", "questionnaire", "label", "subsection", "bigbluebuttonbn", "zoom",
}

# Testi che Moodle (IT/EN) usa per lo stato di completamento
TODO_TEXT = re.compile(r"da fare|to do|non completat|not completed|segna come fatto|mark as done", re.I)
DONE_TEXT = re.compile(r"\bfatto\b|\bdone\b|completato|completed", re.I)

# Richiesta di conferma presenza: finestre/overlay con questi testi
PRESENCE_TEXT = re.compile(
    r"presenz|sei ancora|ancora (lì|li|qui)|sei qui|ci sei|conferm|still (here|watching)|are you there", re.I
)
PRESENCE_OVERLAYS = [
    "[role='dialog']", "[role='alertdialog']", ".modal.show", ".modal.in", ".ui-dialog",
    ".swal2-popup", ".vjs-modal-dialog", ".moodle-dialogue", "[class*='popup' i]", "[class*='overlay' i]",
]
PRESENCE_BUTTONS = re.compile(r"conferm|sono presente|presente|sono qui|ci sono|still here|i.?m here", re.I)

NAV_TIMEOUT_MS = 30_000
SSO_WAIT_S = 300          # tempo concesso per completare login/MFA a mano
VIDEO_START_TIMEOUT_S = 60
VIDEO_SEARCH_TIMEOUT_S = 20  # tempo per trovare un player nella pagina di un'attività
VIDEO_EXTRA_MARGIN_S = 120  # margine oltre la durata del video (buffering, pause)

log = logging.getLogger("unipd")


@dataclass
class Activity:
    name: str
    url: str
    modtype: str
    kind: str  # "video" oppure "test"


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


def fill_credentials(page: Page, username: str, password: str) -> bool:
    """Compila il form dell'SSO UniPD. Ritorna False se il form non è quello atteso."""
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        pass
    time.sleep(1)  # il form UniPD viene riscritto via JavaScript dopo il caricamento

    # SSO UniPD: campo visibile "j_username_js" con il solo nome utente + menu col dominio;
    # il campo "j_username" vero è nascosto e viene riempito dalla pagina stessa.
    js_field = page.locator("#j_username_js").first
    if js_field.count() and js_field.is_visible():
        local, _, domain = username.partition("@")
        js_field.fill(local, timeout=5_000)
        if domain:
            for select in page.locator("select").all():
                if not select.is_visible():
                    continue
                for opt in select.locator("option").all():
                    text = (opt.inner_text() + " " + (opt.get_attribute("value") or "")).lower()
                    if domain.lower() in text:
                        select.select_option(value=opt.get_attribute("value") or opt.inner_text())
                        break
    else:
        user_field = first_visible(page, [
            "input[name='j_username']", "input[name='username']", "input[type='email']",
            "input[autocomplete='username']", "input[type='text']",
        ], timeout_ms=15_000)
        if user_field is None:
            return False
        user_field.fill(username, timeout=5_000)

    submit_selectors = [
        "button[type='submit']", "input[type='submit']", "button:has-text('Accedi')",
        "button:has-text('Avanti')", "button:has-text('Login')",
    ]
    pass_field = first_visible(page, ["input[type='password']"], timeout_ms=3_000)
    if pass_field is None:
        # Login in due passaggi: prima il nome utente, poi la password
        submit = first_visible(page, submit_selectors)
        if submit is None:
            return False
        submit.click()
        pass_field = first_visible(page, ["input[type='password']"], timeout_ms=15_000)
        if pass_field is None:
            return False
    pass_field.fill(password, timeout=5_000)

    submit = first_visible(page, submit_selectors)
    if submit is not None:
        submit.click()
    else:
        pass_field.press("Enter")
    return True


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
    try:
        filled = fill_credentials(page, username, password)
    except PlaywrightError as exc:
        log.debug("Compilazione automatica fallita: %s", exc)
        filled = False
    if not filled:
        # Pagina di login diversa dal previsto: si completa l'accesso a mano nel browser
        screenshot(page, "login_campi_non_trovati")
        log.warning(">>> Non riesco a compilare il login da solo su %s", page.url)
        log.warning(">>> FAI IL LOGIN A MANO nella finestra di Chrome: lo script riparte da solo dopo.")

    # Attesa del rientro su Moodle (fuori dalla pagina di login). Se c'è l'autenticazione
    # a due fattori o un consenso attributi, l'utente può completarli nel browser.
    log.info("Attendo il rientro su Moodle (completa eventuale MFA/login nel browser, max %ds)...", SSO_WAIT_S)
    try:
        page.wait_for_url(
            lambda url: url.startswith(BASE_URL) and "/login/" not in url,
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

    log.info("Apro 'I miei corsi'...")
    for my_page in ("my/courses.php", "my/"):
        goto(page, BASE_URL + my_page)
        # Le schede dei corsi vengono caricate in modo asincrono dopo la pagina
        card = page.locator("a[href*='course/view.php']").filter(has_text=COURSE_NAME).first
        try:
            card.wait_for(state="visible", timeout=15_000)
        except PlaywrightTimeoutError:
            continue
        log.info("  clic su: %s", re.sub(r"\s+", " ", card.inner_text()).strip())
        card.click()
        page.wait_for_url(re.compile(r"course/view\.php"), timeout=NAV_TIMEOUT_MS)
        page.wait_for_load_state("domcontentloaded")
        return
    log.warning("Corso non trovato in 'I miei corsi', provo dalle categorie.")

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
    result = page.locator(".coursename a, h3.coursename a, a.aalink.coursename").filter(has_text=COURSE_NAME).first
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


def expand_sections(page: Page) -> None:
    """Espande eventuali sezioni compresse (formato "collapsed topics" / Moodle 4)."""
    for toggle in page.locator("a[data-toggle='collapse'][aria-expanded='false'], .collapsed[data-for='sectiontoggler']").all():
        try:
            toggle.click(timeout=2_000)
        except PlaywrightError:
            pass


def activities_on_page(page: Page) -> list[tuple[Activity, str]]:
    """Videolezioni e test della pagina corrente, nell'ordine del corso, con lo stato di completamento."""
    try:
        page.locator("li.activity").first.wait_for(state="attached", timeout=10_000)
    except PlaywrightTimeoutError:
        return []
    expand_sections(page)
    found: list[tuple[Activity, str]] = []
    for item in page.locator("li.activity").all():
        classes = item.get_attribute("class") or ""
        modtype = next((c.removeprefix("modtype_") for c in classes.split() if c.startswith("modtype_")), "")
        # Le attività non ancora sbloccate (restrizioni) non hanno il link: vengono saltate
        link = item.locator("a.aalink, .activityname a, .activityinstance a").first
        if not link.count():
            continue
        name = re.sub(r"\s+", " ", link.inner_text()).strip()
        url = link.evaluate("el => el.href || ''")
        if modtype in TEST_MODTYPES:
            kind = "test"
        elif modtype in NON_VIDEO_MODTYPES:
            continue
        else:
            kind = "video"  # possibile video: lo si verifica aprendo l'attività
        if not url:
            continue
        state = completion_state(item)
        log.debug("Attività '%s' [%s] stato=%s", name, modtype, state)
        found.append((Activity(name=name, url=url, modtype=modtype, kind=kind), state))
    return found


def find_pending_activities(page: Page, course_url: str, include_unknown: bool, summary: bool = False) -> list[Activity]:
    """Tutte le attività da fare del corso, unità per unità (anche se ogni unità ha una pagina propria)."""
    goto(page, course_url)
    section_urls: list[str] = []
    for a in page.locator("a[href*='course/section.php'], .sectionname a[href*='section='], "
                          ".section-title a[href*='section=']").all():
        href = a.evaluate("el => el.href")  # URL assoluto
        if href and href not in section_urls:
            section_urls.append(href)

    found = activities_on_page(page)
    for url in section_urls:
        goto(page, url)
        found += activities_on_page(page)

    if summary:
        counts = Counter((act.modtype, state) for act, state in found)
        log.info("Riepilogo del corso (tipo attività / stato -> quante):")
        for (modtype, state), n in sorted(counts.items()):
            log.info("    %-14s %-8s %d", modtype, state, n)

    pending: list[Activity] = []
    seen: set[str] = set()
    for act, state in found:
        if act.url in seen:
            continue
        seen.add(act.url)
        if state == "todo" or (state == "unknown" and include_unknown):
            pending.append(act)
    return pending


def do_test(page: Page, activity: Activity) -> None:
    """I test li svolgi tu: lo script apre la pagina e aspetta che tu abbia finito."""
    log.info("📝 TEST: %s", activity.name)
    goto(page, activity.url)
    print("\n" + "=" * 70)
    print(f"  È il momento del test: '{activity.name}'")
    print("  Svolgilo nella finestra di Chrome (consegna compresa).")
    print("  Quando hai finito, torna qui e premi INVIO per continuare.")
    print("=" * 70)
    input()


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


def notify_user(page: Page, title: str, message: str) -> None:
    """Avvisa con campanello nel Terminale, notifica e suono del Mac, e porta Chrome in primo piano."""
    print("\a\n" + "!" * 70 + f"\n  {title}: {message}\n" + "!" * 70, flush=True)
    if sys.platform == "darwin":
        script = f'display notification "{message}" with title "{title}" sound name "Glass"'
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
        subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
    try:
        page.bring_to_front()
    except PlaywrightError:
        pass


def find_presence_prompt(page: Page) -> Locator | None:
    """Cerca una richiesta di conferma presenza visibile, nella pagina o negli iframe del player."""
    for frame in page.frames:
        try:
            for sel in PRESENCE_OVERLAYS:
                for loc in frame.locator(sel).filter(has_text=PRESENCE_TEXT).all():
                    if loc.is_visible():
                        return loc
            button = frame.get_by_role("button", name=PRESENCE_BUTTONS).first
            if button.count() and button.is_visible():
                return button
        except PlaywrightError:
            continue
    return None


def wait_for_user_presence(page: Page) -> float:
    """Se c'è una richiesta di presenza avvisa e aspetta che la confermi tu. Ritorna i secondi attesi."""
    prompt = find_presence_prompt(page)
    if prompt is None:
        return 0.0
    started = time.monotonic()
    log.info("  ✋ richiesta di conferma presenza: aspetto che la confermi tu nel browser...")
    last_alert = 0.0
    while find_presence_prompt(page) is not None:
        if time.monotonic() - last_alert > 60:  # ripete l'avviso ogni minuto
            notify_user(page, "Conferma la presenza", "Il corso chiede di confermare che stai seguendo")
            last_alert = time.monotonic()
        time.sleep(2)
    log.info("  presenza confermata, proseguo.")
    return time.monotonic() - started


def handle_js_dialog(page: Page, dialog) -> None:
    """Finestre alert/confirm del browser: le accetta solo dopo che premi Invio nel Terminale."""
    if dialog.type == "beforeunload":  # "vuoi lasciare la pagina?": non riguarda la presenza
        dialog.accept()
        return
    notify_user(page, "Il corso chiede una conferma", dialog.message[:120].replace('"', "'"))
    print(f"Messaggio del corso: {dialog.message}")
    input("Se sei qui e vuoi confermare, premi INVIO nel Terminale... ")
    dialog.accept()


def mark_done_if_manual(page: Page) -> None:
    """Se l'attività ha il completamento manuale, preme "Segna come fatto" a video finito."""
    button = page.locator("button[data-toggletype='manual:mark-done']").first
    try:
        if button.count() and button.is_visible():
            button.click()
            page.locator("button[data-toggletype='manual:undo']").first.wait_for(timeout=10_000)
            log.info("  ✔ segnato come fatto")
    except PlaywrightError as exc:
        log.warning("  impossibile premere 'Segna come fatto': %s", exc)


def watch_video(page: Page, activity: Activity) -> bool | None:
    """True = video visto fino alla fine, False = problema, None = l'attività non contiene video."""
    log.info("▶ Apro: %s", activity.name)
    goto(page, activity.url)

    # Attività di tipo URL/Risorsa: il link può aprire una pagina intermedia con il vero link
    if activity.modtype in {"url", "resource"} and find_video_frame(page, 5) is None:
        inner = page.locator(".urlworkaround a, .resourceworkaround a, #region-main a[href]").first
        if inner.count():
            inner.click()
            page.wait_for_load_state("domcontentloaded")

    frame = find_video_frame(page, VIDEO_SEARCH_TIMEOUT_S)
    if frame is None:
        log.info("  nessun video in '%s' [%s], passo oltre.", activity.name, activity.modtype)
        return None

    wait_for_user_presence(page)
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
        # Prima la presenza: il video non va mai fatto ripartire sopra una richiesta di conferma
        deadline += wait_for_user_presence(page)
        if state["ended"] or (state["d"] and state["t"] >= state["d"] - 1):
            log.info("  ✔ video finito: %s", activity.name)
            mark_done_if_manual(page)
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


def configure_site(course_url: str | None) -> None:
    """Ricava il sito Moodle dall'URL del corso (es. .../formazione/course/view.php?id=383)."""
    global BASE_URL, STATE_FILE
    if course_url and "/course/" in course_url:
        BASE_URL = course_url.split("/course/")[0] + "/"
    slug = re.sub(r"[^a-z0-9]+", "_", BASE_URL.split("://", 1)[-1].lower()).strip("_")
    STATE_FILE = HERE / f"auth_state_{slug}.json"
    legacy = HERE / "auth_state.json"  # sessione salvata dalle versioni precedenti (solo medicina)
    if "medicina" in slug and legacy.exists() and not STATE_FILE.exists():
        legacy.rename(STATE_FILE)
    log.info("Sito Moodle: %s", BASE_URL)


def run(args: argparse.Namespace) -> int:
    configure_site(args.course_url)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=args.headless,
            slow_mo=100,
            args=["--autoplay-policy=no-user-gesture-required"] + (["--mute-audio"] if args.mute else []),
        )
        context = new_context(browser, use_saved_state=not args.fresh_login)
        page = context.new_page()
        page.on("dialog", lambda dialog: handle_js_dialog(page, dialog))
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

            pending = find_pending_activities(page, course_url, args.include_unknown, summary=True)
            log.info("Attività da fare: %d", len(pending))
            for i, act in enumerate(pending, 1):
                log.info("  %d. [%s] %s (%s)", i, act.kind.upper(), act.name, act.modtype)
            if args.dry_run:
                return 0

            # Una attività alla volta, ricaricando il corso dopo ognuna: completare un test
            # può sbloccare l'unità successiva, che prima non era visibile.
            handled: set[str] = set()
            completed = failures = 0
            while True:
                todo = [a for a in pending if a.url not in handled]
                if args.skip_tests:
                    todo = [a for a in todo if a.kind != "test"]
                if not todo or (args.limit and completed + failures >= args.limit):
                    break
                act = todo[0]
                handled.add(act.url)
                played = False
                try:
                    if act.kind == "test":
                        do_test(page, act)
                        completed += 1
                    else:
                        result = watch_video(page, act)
                        if result is None:
                            continue  # non era un video
                        played = True
                        if result:
                            completed += 1
                        else:
                            failures += 1
                except (PlaywrightError, RuntimeError) as exc:
                    failures += 1
                    log.error("Errore su '%s': %s", act.name, exc)
                    screenshot(page, f"errore_{act.name}")
                # Ricarico il corso così Moodle aggiorna completamenti e unità sbloccate
                pending = find_pending_activities(page, course_url, args.include_unknown)
                if not played and act.kind == "video":
                    continue
                if any(a.url == act.url for a in pending):
                    log.warning("  ⚠ Moodle non segna ancora '%s' come completata.", act.name)
                else:
                    log.info("  ✔ risulta completata su Moodle: %s", act.name)

            log.info("Fine. Completate: %d, con problemi: %d", completed, failures)
            exit_code = 1 if failures else 0
        except (PlaywrightError, RuntimeError) as exc:
            log.error("Esecuzione interrotta: %s", exc)
            screenshot(page, "errore_fatale")
            exit_code = 2
        except KeyboardInterrupt:
            log.info("Interrotto dall'utente.")
            exit_code = 130
        finally:
            # Dopo Ctrl+C il driver di Playwright può essere già chiuso: ignoro gli errori
            for close in (context.close, browser.close):
                try:
                    close()
                except Exception:
                    pass
        return exit_code


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Precorso di calcolo (Farmacia ME2946): videolezioni e test non completati")
    p.add_argument("--course-url", help="URL diretto del corso (salta la navigazione tra le categorie)")
    p.add_argument("--dry-run", action="store_true", help="elenca le attività da fare senza eseguirle")
    p.add_argument("--skip-tests", action="store_true", help="riproduce solo i video, senza fermarsi ai test")
    p.add_argument("--limit", type=int, default=0, help="numero massimo di attività da eseguire")
    p.add_argument("--include-unknown", action="store_true",
                   help="includi anche i video senza tracciamento del completamento")
    p.add_argument("--mute", action="store_true", help="video senza audio (utile se nel frattempo segui altro)")
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
