# Automazione videolezioni – Precorso di calcolo (Farmacia ME2946, UniPD)

Script Python + Playwright che, su https://medicina.elearning.unipd.it/:

1. apre la home page di Moodle;
2. clicca **Login/Accedi** e passa al Single Sign-On UniPD;
3. inserisce le credenziali (con attesa per eventuale MFA da completare a mano);
4. apre *I miei corsi → Precorso di calcolo* (in alternativa le categorie o la ricerca corsi);
5. percorre le unità in ordine fino alla fine:
   - **videolezioni non completate**: le riproduce fino alla fine e, se il corso usa il
     completamento manuale, preme "Segna come fatto";
   - **test**: li apre e aspetta che tu li svolga, poi premi Invio nel Terminale e prosegue;
   - dopo ogni attività ricarica il corso, così compaiono le unità sbloccate dal test, e
     controlla che Moodle segni l'attività come completata.

## Installazione

Serve Python 3.10 o superiore.

```bash
cd unipd-elearning-automation
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium        # scarica il browser usato da Playwright
```

## Credenziali

Lo script legge `UNIPD_USER` e `UNIPD_PASSWORD` dalle variabili d'ambiente; se mancano le chiede
a terminale (la password non viene mostrata).

```bash
export UNIPD_USER="nome.cognome@studenti.unipd.it"
export UNIPD_PASSWORD="..."        # Windows PowerShell: $env:UNIPD_PASSWORD="..."
```

Dopo il primo accesso la sessione viene salvata in `auth_state.json` (escluso da git): ai lanci
successivi il login viene saltato finché la sessione è valida. Trattalo come una password.

## Uso

```bash
python unipd_videolezioni.py --dry-run          # elenca solo le videolezioni da vedere
python unipd_videolezioni.py                    # le riproduce tutte, una dopo l'altra
python unipd_videolezioni.py --limit 2          # solo le prime 2
python unipd_videolezioni.py --course-url "https://medicina.elearning.unipd.it/course/view.php?id=XXXX"
```

| Opzione | Effetto |
|---|---|
| `--course-url` | apre direttamente il corso (consigliato dopo il primo lancio: è il più robusto) |
| `--dry-run` | mostra l'elenco senza riprodurre |
| `--skip-tests` | solo videolezioni, non si ferma ai test |
| `--limit N` | esegue al massimo N attività |
| `--include-unknown` | include anche i video senza tracciamento del completamento |
| `--headless` | browser invisibile (sconsigliato: MFA e play manuale non sono possibili) |
| `--fresh-login` | ignora la sessione salvata |
| `-v` | log dettagliato (stato di ogni attività) |

## Come gestisce attese ed errori

- **Attese**: `wait_for_load_state`, `wait_for_url` sul rientro dall'SSO, `locator.wait_for`
  sugli elementi e `wait_for_function` per verificare che il video sia partito. Il player
  (Kaltura, H5P, ecc.) viene cercato anche dentro gli iframe annidati.
- **Selettori multipli**: per login, pulsanti e player vengono provati più selettori, perché
  il markup può variare tra versioni di Moodle e del player.
- **Errori**: retry sulla navigazione, timeout espliciti, screenshot automatici in
  `screenshots/` a ogni problema, gestione di Ctrl+C. Un errore su una videolezione non
  blocca le successive. Codice d'uscita: 0 ok, 1 alcune lezioni con problemi, 2 errore fatale.

## Note

- Il video viene riprodotto a velocità normale nel browser visibile, così Moodle registra la
  visione reale. Lascia la finestra aperta finché non finisce. I test non vengono compilati
  dallo script.
- Lo script non è stato provato sul sito reale (non raggiungibile dall'ambiente di sviluppo):
  se un passaggio fallisce, guarda lo screenshot in `screenshots/` e adatta il selettore
  corrispondente all'inizio di `unipd_videolezioni.py` (`CATEGORY_PATH`, `VIDEO_MODTYPES`, ...).
- Usa lo script solo con il tuo account e nel rispetto del regolamento della piattaforma.
