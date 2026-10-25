# Diario

## Da dove ripartire

Riscritto da capo a ogni fine sessione, mai accodato.

- **Stato**: tutte le tappe fatte; versione 0.1.0, non pubblicata. Esistono
  tre store (memoria, SQLite, Postgres) con la stessa suite di contratto, il
  Gate, le politiche, i controlli sul contenuto, le richieste condizionali, i
  byte, quattro adattatori, il kit di test, lo stato per host in JSON e la
  skill per gli agenti dentro il pacchetto (`python -m politefetch.skill`,
  schema comune con swpc-kit, D-040 e D-041).
- **Prossimo passo**: nessuna tappa aperta in `docs/TAPPE.md`. Repository
  pronto per diventare pubblico come `politefetch` (D-042). Il plugin e la
  barriera funzionano su Windows, con un job in CI (D-044). Pubblicazione
  decisa (D-043): prima la skill v2, poi la prova su TestPyPI e il tag.
  Chi cambia l'API pubblica rigenera la skill (`AGENTS.md`). Prima dei test:
  `eval "$(scripts/postgres-for-tests.sh)"`, altrimenti i test Postgres
  vengono saltati (su Windows lo script non c'è: lì i test Postgres si
  saltano, girano nel job Linux).
- **Dubbi aperti**:
  - `httpx2` non è supportato (D-032);
  - una modifica al modulo `skill` va concordata con swpc-kit.

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
- 2026-10-17 — T5. Entrati: nucleo comune degli adattatori (D-033),
  `UrllibFetcher`, `HttpxFetcher`, `AsyncHttpxFetcher`, `RequestsFetcher`,
  forme del fallimento uniformi, errore `FetchFailed` (D-034), scelta di
  httpx 0.28 (D-032). I conteggi del caso peggiore girano con tutti gli
  adattatori e restano uguali. Controllato che i test scoprano sei difetti
  introdotti apposta. Resta: T7, T8.
- 2026-10-17 — T7. Entrati: `PostgresStore` con psycopg 3 (D-035), uno
  scrittore alla volta per schema con lock consultivo e `lock_timeout`
  (D-036), test contro Postgres in locale e in CI, ognuno nel suo schema
  (D-037). Tutta la suite di contratto e i test con più processi passano
  anche su Postgres. Controllato che i test scoprano quattro difetti
  introdotti apposta, compreso il lock tolto. Resta: T8.
- 2026-10-18 — T8. Entrati: `gate.status(host_or_url)` e `gate.statuses()`
  con `HostStatus` e il suo JSON (D-038), con un test basato su proprietà
  che lo confronta con la decisione del gate; `rows(latest=N)` nei tre store
  con indice `(host, id)`; README riscritto con un esempio completo (job da
  cron per il Kp di SWPC) eseguito da `tests/test_readme.py`; CHANGELOG;
  `FetchFailed` esportato da `politefetch`; tolto il modulo vuoto
  `politefetch.traffic` (D-039). Decisa con l'utente e con la sessione di
  swpc-kit la skill per gli agenti dentro il pacchetto (D-040): nuova tappa
  T9, e 0.1.0 passa a T9. 897 test (1 saltato, IPv6), difetti iniettati
  presi tutti dopo un test in più su `reserved_bytes`. Resta: T9.
- 2026-10-18 — T9. Entrati: la skill in `src/politefetch/skill/politefetch/`
  (`SKILL.md` a mano, `references/api.md` generato da
  `tools/build_skill.py`), il modulo `politefetch.skill` dello schema comune
  (uguale a quello di swpc-kit, costanti a parte), l'entry point
  `agent_skills`, la sezione del README con l'hook `SessionStart`, la regola
  in `AGENTS.md`, `__version__` e la versione 0.1.0 (D-041). Provato da capo
  a fondo con Claude Code e con le wheel dei due pacchetti. 945 test (1
  saltato, IPv6); difetti iniettati presi tutti dopo un test in più sui
  parametri delle chiamate. Resta: decidere la pubblicazione.
- 2026-10-25 — preparazione all'open source, in locale (D-042): repository
  `politefetch`, autore Sidereus Team in `LICENSE` e `pyproject.toml`,
  `AGENTS.md` e `SPEC.md` in inglese, storia riscritta senza identità
  personali. Resta: renderlo pubblico, poi la pubblicazione su PyPI.
- 2026-10-25 — Windows (D-044): il plugin si importa (niente `sendmsg`), la
  barriera sorveglia anche il proactor di asyncio, che la scavalcava;
  `Child.join` non resta appeso su un figlio ucciso subito; test e generatore
  della skill adattati; job `windows` in CI. Su Windows 817 test passati, 136
  saltati (Postgres); mutazioni sui pezzi nuovi prese tutte.
