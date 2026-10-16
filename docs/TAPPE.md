# Tappe

Una tappa per sessione cloud, una PR per tappa. Per lanciarne una basta il
prompt:

> Lavora sulla tappa Tn di `docs/TAPPE.md`, seguendo `AGENTS.md`.

Ordine:

```
T0 → T1 → T2 → { T3, T4, T6 } → { T5, T7 } → T8
```

T0, T1, T2 in fila (sono le fondamenta); poi T3, T4, T6 anche in parallelo;
T5 dopo T2, T4 e T6 (i suoi test usano il simulatore); T7 dopo T3 e T4
(implementa lo store completo); T8 per ultima.

## T0 — Impianto e ricognizione

- [x] Fatta

- `pyproject.toml` con `uv` e `hatchling`, Python ≥ 3.12, layout
  `src/politefetch/`, extra vuoti `httpx`, `requests`, `postgres`; ruff, mypy
  strict (su `src` e `tests`), pytest, `hypothesis` fra le dipendenze di
  sviluppo.
- Moduli vuoti con docstring secondo l'architettura di `SPEC.md`; gerarchia
  degli errori (`PoliteFetchError`, `Refused` con motivo e "riprova dopo",
  `SourceNotReady`, `UnexpectedContent`, `StaleData`, `StuckSource`) completa
  e testata; interfaccia `Clock` con un orologio vero e uno finto.
- **Barriera sul socket, versione minima**, in `politefetch.testing`: intercetta
  `socket.connect`, `connect_ex` e `getaddrinfo` e lascia passare solo il
  loopback (127.0.0.1, ::1) e i socket Unix. Attiva in ogni test con una
  fixture automatica in `tests/conftest.py`. Con la sua prova: una connessione
  verso un IP pubblico vero fallisce (la barriera la ferma prima che parta un
  pacchetto), una verso il loopback passa. Niente `pytest-socket`: la barriera
  è parte del prodotto (regola 5) e la usiamo noi per primi; T6 la completa.
- GitHub Actions con i cinque controlli di `AGENTS.md`.
- `README.md` iniziale in inglese; `docs/DECISIONI.md` con il formato e le tre
  decisioni già prese prima di T0 (barriera in T0, ordine T7 dopo T3/T4 con
  suite di contratto, simulatore come server locale: vedi il Log di
  `docs/DIARIO.md` del 2026-10-15).
- **Ricognizione**: scarica da PyPI e leggi `pyresilience`, `retryhop`,
  `hishel`, `pybreaker`, `pyrate-limiter`. Scrivi in `docs/DECISIONI.md`
  cosa coprono delle sette regole e dei controlli di `SPEC.md`, e se conviene
  appoggiarsi a qualcuno (per esempio hishel per i validatori). Controlla se
  il nome `fetchguard` è libero su PyPI; se non lo è, proponi tre alternative
  nella PR. Se una libreria copre già gran parte di `SPEC.md`, fermati e
  scrivilo: si decide prima di andare avanti.

## T1 — Store e registro

- [x] Fatta

- Interfaccia `Store` e due implementazioni: memoria e SQLite (WAL, una
  transazione per operazione critica, sicura fra processi).
- Registro delle richieste: apertura (`in-flight`) con la promessa prudente,
  chiusura con l'esito, riconciliazione delle righe aperte oltre una soglia
  (contano come fallite).
- Conteggio giornaliero per host consumato all'apertura, giorno in UTC.
- **Suite di contratto** dello store: una sola batteria di test parametrizzata
  su tutte le implementazioni (memoria, SQLite, poi Postgres). Ogni tappa che
  aggiunge un metodo allo store aggiunge qui i suoi test, così
  un'implementazione rimasta indietro fa diventare rossa la suite.
- **Schema senza migrazioni numerate** fino al primo rilascio: ogni modulo crea
  le sue tabelle con `CREATE TABLE IF NOT EXISTS`. Evita che due PR parallele
  scelgano lo stesso numero di migrazione.
- Test basati su proprietà: con crash simulati fra apertura e chiusura e
  riavvii dello store, il conteggio non perde richieste e nessuna riga resta
  aperta dopo la riconciliazione. Test con due processi sullo stesso file.
- **Processi figli sotto la barriera** (D-012): una sola funzione in
  `politefetch.testing` avvia un processo figlio e vi attiva la barriera prima
  di eseguire il codice; i test con due processi la usano. Un test di guardia
  (controllo sull'AST, come `tests/test_layout.py`) fallisce se un test usa
  `subprocess` o `multiprocessing` senza passare da quella funzione.

## T2 — Gate e politiche

- [ ] Fatta

- `HostPolicy`: tetto giornaliero, intervallo minimo, pausa dopo errore
  (esponenziale con tetto), comportamento per 403/429/451 (congelamento fino
  a sblocco manuale o a una scadenza), `Retry-After` come minimo.
- **403, 429 e 451** (D-013):
  - per default congelano l'host, anche un 429 con `Retry-After`; se c'è una
    scadenza configurata, non è mai più corta di `Retry-After`;
  - un'opzione per host, spenta per default, fa rispettare `Retry-After` al
    primo 429: pausa pari al massimo fra `Retry-After` e la pausa crescente,
    senza congelare. Un secondo 429 consecutivo (cioè senza una risposta
    buona in mezzo) congela comunque;
  - 403 e 451 congelano sempre.
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
letture, con lo storico nello store; i nuovi metodi dello store entrano nella
suite di contratto).

## T4 — Richieste condizionali e traffico

- [ ] Fatta

ETag e Last-Modified per URL nello store; byte sul filo e decodificati per
host e giorno; budget di byte per host consumato come il tetto, nella stessa
transazione dell'apertura della riga (si modifica l'operazione di T1, non se
ne aggiunge una separata). I nuovi metodi dello store entrano nella suite di
contratto.

## T5 — Adattatori

- [ ] Fatta

Interfaccia `Fetcher` e adattatori `urllib` (core), `httpx`, `requests`, sync e
async dove il client lo permette. Ogni adattatore si prova contro il
simulatore di T6 (server HTTP locale), sugli stessi scenari. Prima di
scegliere: a ottobre 2026 httpx risultava fermo alla 0.28.1 con una 1.0 in
sviluppo e un fork di Pydantic, `httpx2` (informazione da issue di terzi:
verificala su PyPI e scrivi la scelta in `docs/DECISIONI.md`). La forma del fallimento (timeout di
connessione, reset, rifiuto, TLS, codice HTTP) si registra in modo uniforme.

## T6 — Kit di test

- [ ] Fatta

`politefetch.testing`: completa la barriera di T0 e la rende un plugin pytest
installabile; simulatore di fonte con gli scenari di `SPEC.md`; contatore
delle richieste partite per scenario.

- **Il simulatore è un vero server HTTP su 127.0.0.1**, avviato in un thread
  dal test: rifiuto (porta chiusa), nessuna risposta, reset, 429, corpo più
  corto di `Content-Length`, HTML, dato fermo. Così non dipende
  dall'interfaccia `Fetcher` di T5, e gli adattatori si provano sul loro
  codice reale.
- **Il contatore conta dal lato della fonte**: le richieste arrivate al
  server, cioè quello che vedrebbe il firewall. Lo scenario "processo ucciso a
  richiesta partita" esegue il client in un sottoprocesso e lo uccide con
  `SIGKILL` mentre il server tiene aperta la richiesta. Il sottoprocesso parte
  dalla funzione di T1 che attiva la barriera nel figlio (D-012), estesa
  quanto serve per poterlo uccidere.
- I timeout dei socket usano il tempo vero: tenerli brevi (decimi di secondo)
  e confinati agli scenari "non risponde". L'orologio di `Gate` resta finto.
- I trasporti finti in memoria restano per i test unitari di `Gate`.
- Riscrivi i test di scenario di T2 come esempio, usando `urllib` direttamente
  dentro `gate.attempt` (l'adattatore arriva in T5).

## T7 — Store Postgres

- [ ] Fatta

Extra `postgres` (driver da scegliere e motivare in `docs/DECISIONI.md`). Le
stesse prove di T1 e la suite di contratto completa (con i metodi di T3 e
T4), eseguite contro PostgreSQL 16 dell'ambiente, compreso il caso di due
processi. Il cluster è installato ma spento all'avvio del container: va
avviato nel test o nello script di setup (`pg_ctlcluster 16 main start`).

## T8 — Stato e rifinitura

- [ ] Fatta

Stato leggibile per host in JSON; README con un esempio completo eseguibile
(verificato da un test); CHANGELOG; versione 0.1.0. Niente pubblicazione su
PyPI: si decide dopo.
