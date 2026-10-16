# Diario

## Da dove ripartire

Riscritto da capo a ogni fine sessione, mai accodato.

- **Stato**: T0 fatta. Impianto (`uv`, `hatchling`, ruff, mypy strict,
  pytest, `hypothesis`, CI su 3.12 e 3.13), moduli vuoti con docstring,
  gerarchia degli errori, `Clock` con `SystemClock` e `FakeClock`, barriera
  sul socket attiva in ogni test, `docs/DECISIONI.md` (D-001…D-009).
- **Prossimo passo**: T1 (store e registro), che crea anche la suite di
  contratto (D-002).
- **Dubbi aperti**:
  - il nome: `fetch-guard` esiste già su PyPI, quindi `fetchguard` rischia il
    rifiuto (D-005); alternative `fetchwarden`, `politefetch`, `pollwarden`.
    Meglio decidere prima di T1;
  - la licenza, non ancora scelta (D-009);
  - 429 con `Retry-After`: la lettura prudente è congelare, da scrivere in T2;
  - la barriera non raggiunge i processi figli: i test con due processi di T1
    devono attivarla anche nel figlio (D-008);
  - lo stato di httpx (vedi T5): il 2026-10-16 `httpx` è a 0.28.1 e `httpx2`
    a 2.13.1.

## Log

Una riga per sessione: data, tappa, cosa è entrato, cosa resta.

- 2026-10-15 — progetto avviato: `SPEC.md` dalle sette regole sulle fonti
  esterne a quota, `docs/TAPPE.md`. Ricognizione iniziale: retry, cache,
  rate limit e circuit breaker esistono separati o combinati in librerie
  giovani; nessuna trovata con promesse persistenti, tetti che reggono ai
  riavvii e freschezza sul contenuto.
- 2026-10-15 — revisione delle tappe prima di T0. Ambiente verificato: `uv`,
  Python 3.12/3.13, PyPI raggiungibile, PostgreSQL 16 installato ma spento.
  Tre decisioni: (1) la barriera sul socket nasce minima in T0 dentro
  `fetchguard.testing`, non con `pytest-socket`, perché è parte del prodotto e
  la regola "i test non usano la rete" vale da subito; (2) T7 dopo T3 e T4,
  con una suite di contratto su tutti gli store, invece di un'interfaccia
  completa definita in anticipo (metodi senza test) o spezzata per tappa
  (romperebbe l'atomicità: il budget di byte si consuma nella transazione di
  apertura); (3) il simulatore è un server HTTP locale, così T6 non dipende
  da T5 e gli adattatori si provano sul codice reale.
- 2026-10-16 — T0. Entrati: impianto, errori, orologio, barriera con la prova
  sulla regola 5 (1.1.1.1 bloccato, loopback e socket Unix passano, test
  basati su proprietà su tutti gli indirizzi), test che il core usi solo la
  libreria standard, CI, README, `docs/DECISIONI.md`. Ricognizione: nessuna
  delle cinque librerie copre gran parte di `SPEC.md` (D-004). Resta: nome e
  licenza da decidere.
