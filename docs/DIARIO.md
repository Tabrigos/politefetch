# Diario

## Da dove ripartire

Riscritto da capo a ogni fine sessione, mai accodato.

- **Stato**: T0, T1, T2, T3, T4 e T6 fatte. Esistono il Gate, le politiche,
  i controlli sul contenuto con lo storico per la fonte ferma, le richieste
  condizionali, i byte con il budget, lo store in memoria e SQLite e il kit
  di test.
- **Prossimo passo**: T5 (adattatori) e T7 (Postgres), in qualunque ordine;
  T8 per ultima. Da sapere per T5:
  - mandare `attempt.conditional_headers`; registrare con
    `attempt.response(status, headers=..., wire_bytes=..., decoded_bytes=...)`;
  - applicare `check_redirect`, `check_length`, `check_body`,
    `read_data_time`, `gate.note_data_time` con `check_advancing`, e
    `check_fresh`; un `ContentError` si registra come fallimento con la sua
    `shape` (D-030, D-031);
  - provare gli adattatori contro il simulatore; i conteggi di
    `tests/test_worst_case.py` devono restare gli stessi (D-026).
  Per T7: la suite di contratto ora copre anche byte, validatori e storico
  dei tempi del dato.
- **Dubbi aperti**:
  - il repository GitHub si chiama ancora `fetchguard` (D-010);
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
  delle cinque librerie copre gran parte di `SPEC.md` (D-004). `fetch-guard`
  esiste già su PyPI: il progetto diventa `politefetch` (D-010), licenza MIT
  (D-011). Decisi anche i processi figli sotto la barriera (D-012) e il 429
  con `Retry-After` (D-013).
- 2026-10-16 — T1. Entrati: interfaccia `Store` con i controlli duri dentro
  la transazione (D-014), `MemoryStore`, `SqliteStore`, una richiesta alla
  volta per host con il nuovo motivo `in_flight` (D-015), rifiuto con il
  vincolo che finisce per ultimo (D-016), riconciliazione (D-017), `spawn`
  per i processi figli con la guardia (D-019). Controllato che i test
  scoprano quattro difetti introdotti apposta (transazione `DEFERRED`,
  promessa accorciata da un fallimento, controllo "in corso" tolto, conteggio
  che non cresce). Resta: T2.
- 2026-10-16 — T2. Entrati: `HostPolicy` (D-020), `Gate` con `attempt`
  sincrono e asincrono e `may_fetch` (D-022), contatore dei 429 nello store
  (D-021), sblocco manuale con traccia (D-023), test di scenario con
  l'orologio finto e un test basato su proprietà su sequenze casuali di
  risposte. Controllato che i test scoprano sei difetti introdotti apposta.
  Resta: T3, T4, T6.
- 2026-10-17 — T6. Entrati: plugin pytest installabile (D-024), simulatore di
  fonte e contatore (D-025), conteggi del caso peggiore per ogni scenario
  (D-026), test di scenario di T2 rifatti con HTTP vero e un processo ucciso
  a richiesta partita. Controllato che i test scoprano cinque difetti
  introdotti apposta. Resta: T3, T4.
- 2026-10-17 — T4. Entrati: validatori per URL e intestazioni condizionali
  (D-028), byte sul filo e decodificati per host e giorno, budget di byte
  prenotato all'apertura e corretto alla chiusura (D-027), registrazione
  della risposta dalle intestazioni (D-029), scenario `Validated` nel
  simulatore. Controllato che i test scoprano sei difetti introdotti
  apposta. Resta: T3, T5.
- 2026-10-17 — T3. Entrati: controlli sul contenuto come funzioni pure
  (D-030), storico dei tempi del dato nello store e `check_advancing`
  (D-031), `shape` sugli errori di contenuto, README sistemato. Controllato
  che i test scoprano sette difetti introdotti apposta; due sopravvivevano
  al primo giro (il JSON troncato con una graffa dentro una stringa) e ora
  hanno il loro test. Resta: T5, T7, T8.
