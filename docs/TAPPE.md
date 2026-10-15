# Tappe

Una tappa per sessione cloud, una PR per tappa. Per lanciarne una basta il
prompt:

> Lavora sulla tappa Tn di `docs/TAPPE.md`, seguendo `AGENTS.md`.

Ordine: T0, T1, T2 in fila (sono le fondamenta); poi T3, T4, T6, T7 anche in
parallelo; T5 dopo T2 e T4; T8 per ultima.

## T0 — Impianto e ricognizione

- [ ] Fatta

- `pyproject.toml` con `uv` e `hatchling`, Python ≥ 3.12, layout
  `src/fetchguard/`, extra vuoti `httpx`, `requests`, `postgres`; ruff, mypy
  strict (su `src` e `tests`), pytest, `hypothesis` fra le dipendenze di
  sviluppo.
- Moduli vuoti con docstring secondo l'architettura di `SPEC.md`; gerarchia
  degli errori (`FetchGuardError`, `Refused` con motivo e "riprova dopo",
  `SourceNotReady`, `UnexpectedContent`, `StaleData`, `StuckSource`) completa
  e testata; interfaccia `Clock` con un orologio vero e uno finto.
- GitHub Actions con i cinque controlli di `AGENTS.md`.
- `README.md` iniziale in inglese; `docs/DECISIONI.md` vuoto con il formato.
- **Ricognizione**: scarica da PyPI e leggi `pyresilience`, `retryhop`,
  `hishel`, `pybreaker`, `pyrate-limiter`. Scrivi in `docs/DECISIONI.md`
  cosa coprono delle sette regole e dei controlli di `SPEC.md`, e se conviene
  appoggiarsi a qualcuno (per esempio hishel per i validatori). Controlla se
  il nome `fetchguard` è libero su PyPI; se non lo è, proponi tre alternative
  nella PR. Se una libreria copre già gran parte di `SPEC.md`, fermati e
  scrivilo: si decide prima di andare avanti.

## T1 — Store e registro

- [ ] Fatta

- Interfaccia `Store` e due implementazioni: memoria e SQLite (WAL, una
  transazione per operazione critica, sicura fra processi).
- Registro delle richieste: apertura (`in-flight`) con la promessa prudente,
  chiusura con l'esito, riconciliazione delle righe aperte oltre una soglia
  (contano come fallite).
- Conteggio giornaliero per host consumato all'apertura, giorno in UTC.
- Test basati su proprietà: con crash simulati fra apertura e chiusura e
  riavvii dello store, il conteggio non perde richieste e nessuna riga resta
  aperta dopo la riconciliazione. Test con due processi sullo stesso file.

## T2 — Gate e politiche

- [ ] Fatta

- `HostPolicy`: tetto giornaliero, intervallo minimo, pausa dopo errore
  (esponenziale con tetto), comportamento per 403/429/451 (congelamento fino
  a sblocco manuale o a una scadenza), `Retry-After` come minimo.
- `Gate`: `may_fetch(url)` → consentito, o `Refused` con motivo e "dopo";
  contesto `with gate.attempt(url) as a:` (e `async with`) che apre la riga,
  e la chiude nel `finally` con l'esito che chi chiama registra.
- Sblocco manuale di un host congelato, con traccia nello store.
- Test di scenario con l'orologio finto: la pausa cresce, una risposta buona
  accorcia la promessa, un crash la lascia prudente.

## T3 — Controlli sul contenuto

- [ ] Fatta

Funzioni pure: troncato (Content-Length, JSON finito a metà, corpo vuoto),
HTML al posto di JSON o testo, redirect verso un altro host, freschezza con
estrattore e `max_age`, fonte ferma (tempo del dato che non avanza per N
letture, con lo storico nello store).

## T4 — Richieste condizionali e traffico

- [ ] Fatta

ETag e Last-Modified per URL nello store; byte sul filo e decodificati per
host e giorno; budget di byte per host consumato come il tetto.

## T5 — Adattatori

- [ ] Fatta

Interfaccia `Fetcher` e adattatori `urllib` (core), `httpx`, `requests`, sync e
async dove il client lo permette. Prima di scegliere: a ottobre 2026 httpx
risultava fermo alla 0.28.1 con una 1.0 in sviluppo e un fork di Pydantic,
`httpx2` (informazione da issue di terzi: verificala su PyPI e scrivi la
scelta in `docs/DECISIONI.md`). La forma del fallimento (timeout di
connessione, reset, rifiuto, TLS, codice HTTP) si registra in modo uniforme.

## T6 — Kit di test

- [ ] Fatta

`fetchguard.testing`: plugin pytest con la barriera sul socket e la sua prova
(un IP pubblico vero), simulatore di fonte con gli scenari di `SPEC.md`,
contatore delle richieste partite per scenario. Usalo per riscrivere i test di
scenario di T2 come esempio.

## T7 — Store Postgres

- [ ] Fatta

Extra `postgres` (driver da scegliere e motivare in `docs/DECISIONI.md`). Le
stesse prove di T1, eseguite contro PostgreSQL 16 dell'ambiente (avviato nel
test o nello script di setup), compreso il caso di due processi.

## T8 — Stato e rifinitura

- [ ] Fatta

Stato leggibile per host in JSON; README con un esempio completo eseguibile
(verificato da un test); CHANGELOG; versione 0.1.0. Niente pubblicazione su
PyPI: si decide dopo.
