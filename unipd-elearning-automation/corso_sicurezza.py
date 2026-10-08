"""
Avvio dedicato del corso sulla sicurezza (Moodle UniPD Formazione, corso id=383).

Usa lo stesso motore di unipd_videolezioni.py (che deve stare nella stessa cartella) con il
corso già impostato. Gira in parallelo al precorso senza disturbarlo: sessione, finestra di
Chrome e log sono separati.

Quando il corso chiede di confermare la presenza, lo script ti avvisa (suono, notifica,
Chrome in primo piano) e aspetta che la conferma la faccia tu nel browser.

Uso:
  python corso_sicurezza.py             # avvia le lezioni non ancora completate
  python corso_sicurezza.py --dry-run   # elenca soltanto le attività da fare
"""

import logging
import sys

import unipd_videolezioni as engine

COURSE_URL = "https://elearning.unipd.it/formazione/course/view.php?id=383"

if __name__ == "__main__":
    # Corso fisso e test saltati; le altre opzioni (--dry-run, --mute, -v, ...) restano disponibili
    sys.argv = [sys.argv[0], "--course-url", COURSE_URL, "--skip-tests", *sys.argv[1:]]
    args = engine.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [SICUREZZA] %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(engine.run(args))
