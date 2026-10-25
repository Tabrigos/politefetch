# Tappe

Una tappa per sessione cloud, una PR per tappa. Per lanciarne una basta il
prompt:

> Lavora sulla tappa Tn di `docs/TAPPE.md`, seguendo `AGENTS.md`.

Ordine:

```
T0 → T1 → T2 → { T3, T4, T6 } → { T5, T7 } → T8 → T9
```

T0, T1, T2 in fila (sono le fondamenta); poi T3, T4, T6 anche in parallelo;
T5 dopo T2, T4 e T6 (i suoi test usano il simulatore); T7 dopo T3 e T4
(implementa lo store completo); T8 e T9 per ultime.

Dopo la revisione del 2026-10-25 (D-045), prima della prima pubblicazione:

```
R1 → R2 → R3 → R4 → T11 → TestPyPI → tag
```

In fila: R1 e R2 toccano entrambe la chiusura del tentativo, R3 e R4
cambiano l'API che la T11 descrive.

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

- [x] Fatta

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

- [x] Fatta

Funzioni pure: troncato (Content-Length, JSON finito a metà, corpo vuoto),
HTML al posto di JSON o testo, redirect verso un altro host, freschezza con
estrattore e `max_age`, fonte ferma (tempo del dato che non avanza per N
letture, con lo storico nello store; i nuovi metodi dello store entrano nella
suite di contratto).

## T4 — Richieste condizionali e traffico

- [x] Fatta

ETag e Last-Modified per URL nello store; byte sul filo e decodificati per
host e giorno; budget di byte per host consumato come il tetto, nella stessa
transazione dell'apertura della riga (si modifica l'operazione di T1, non se
ne aggiunge una separata). I nuovi metodi dello store entrano nella suite di
contratto.

## T5 — Adattatori

- [x] Fatta

Interfaccia `Fetcher` e adattatori `urllib` (core), `httpx`, `requests`, sync e
async dove il client lo permette. Ogni adattatore si prova contro il
simulatore di T6 (server HTTP locale), sugli stessi scenari. Prima di
scegliere: a ottobre 2026 httpx risultava fermo alla 0.28.1 con una 1.0 in
sviluppo e un fork di Pydantic, `httpx2` (informazione da issue di terzi:
verificala su PyPI e scrivi la scelta in `docs/DECISIONI.md`). La forma del fallimento (timeout di
connessione, reset, rifiuto, TLS, codice HTTP) si registra in modo uniforme.

## T6 — Kit di test

- [x] Fatta

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

- [x] Fatta

Extra `postgres` (driver da scegliere e motivare in `docs/DECISIONI.md`). Le
stesse prove di T1 e la suite di contratto completa (con i metodi di T3 e
T4), eseguite contro PostgreSQL 16 dell'ambiente, compreso il caso di due
processi. Il cluster è installato ma spento all'avvio del container: va
avviato nel test o nello script di setup (`pg_ctlcluster 16 main start`).

## T8 — Stato e rifinitura

- [x] Fatta

Stato leggibile per host in JSON; README con un esempio completo eseguibile
(verificato da un test); CHANGELOG. La versione 0.1.0 passa a T9 (D-040).
Niente pubblicazione su PyPI: si decide dopo.

## T9 — La skill per gli agenti e 0.1.0

- [x] Fatta

La skill per gli assistenti AI dentro il pacchetto, secondo lo schema comune
"skill dei pacchetti" v1 (D-040; riferimento: `src/swpc_kit/skill/` di
swpc-kit):

- `src/politefetch/skill/politefetch/SKILL.md`, scritto a mano e leggibile da
  una persona: il modello mentale (promessa, un tentativo, rifiuti normali),
  le scelte (adattatore, store, politica), come leggere `Refused`,
  `FetchFailed` e `ContentError`, lo stato, il kit di test e cosa non fare
  (ritentare intorno a `fetch.get`, dormire su `Refused`, `MemoryStore` in
  produzione, `unfreeze` automatico, redirect a mano, barriera spenta);
- `references/api.md` generato dalle firme e dalle docstring, con un test che
  fallisce se è vecchio; ogni blocco Python della skill eseguito contro il
  simulatore; ogni nome citato esiste; ogni nome pubblico è citato;
  `metadata.version` uguale alla versione del pacchetto;
- l'entry point `agent_skills` e `python -m politefetch.skill install | check
  | show | path | sync`, con gli stessi comportamenti e messaggi di swpc-kit;
- README: la sezione per gli assistenti con l'hook `SessionStart`;
  `AGENTS.md`: chi cambia l'API pubblica aggiorna la skill;
- versione 0.1.0 (pacchetto, `metadata.version`, CHANGELOG). Niente
  pubblicazione su PyPI.

## Revisione prima della pubblicazione

Due revisioni scettiche del 2026-10-25 sul commit `109a674`: chi adotta la
libreria in un progetto (dipendenze, integrazione, facilità d'uso) e chi
cerca di romperla (affidabilità). Ogni difetto è stato provato con uno
script contro il simulatore o gli store veri; quelli segnati *riprodotto*
sono stati rieseguiti a parte e danno lo stesso risultato. Regole comuni
alle quattro tappe:

- ogni difetto diventa prima un test di regressione rosso sul codice di
  oggi, poi la correzione (`AGENTS.md`, "Tests first");
- ciò che cambia lo store entra nella suite di contratto dei tre store e
  nelle proprietà con crash;
- quando un punto lascia aperte più strade, vale la più cauta per la fonte,
  scritta in `docs/DECISIONI.md`.

## R1 — Esiti che non si perdono

- [x] Fatta (D-046)

Gate, politiche e store: un 403/429/451 congela sempre, e una riga in volo
non viene mai chiusa mentre la richiesta è viva.

1. **La chiusura tardiva rende l'host più prudente, mai meno.** Oggi
   `close_request` su una riga già riconciliata non fa nulla
   (`store/sqlite.py:446`, lo stesso negli altri due store): se la risposta
   arriva dopo `reconcile_after`, il 403 si perde. Riprodotto: con una fonte
   lenta, due richieste in parallelo e nessun congelamento dopo il 403. Ora
   la chiusura tardiva registra l'esito sulla riga e applica all'host solo
   ciò che stringe: congelamento, Retry-After, promessa più lontana,
   fallimento in più. Un esito buono tardivo non azzera nulla. La scadenza
   che impedisce alla richiesta di durare tanto arriva in R2: questa è la
   seconda difesa.
2. **Un header illeggibile non butta via lo stato HTTP.** `"²".isdigit()` è
   vero e `int("²")` fallisce: `parse_retry_after` (`policy.py:106`) solleva
   `ValueError` dentro `Attempt.response`/`failure`, la riga si chiude come
   `exception:ValueError` senza stato, e il 403 non congela. Riprodotto con
   i tre client, su 403 e su 429. Solo cifre ASCII. In più, un test di
   proprietà con hypothesis: su testo qualsiasi il parsing degli header non
   solleva mai, e un 403 congela qualunque header porti.
3. **La chiusura che fallisce.** Uno store occupato oltre il `busy_timeout`
   fa fallire `close_request`. Il chiamante riceve `OperationalError` al
   posto del `FetchFailed`, la riga resta in volo, viene poi riconciliata
   senza stato e il congelamento si perde (riprodotto). La chiusura si
   ritenta sullo store, che è il nostro database e non la fonte, con un
   limite. Se fallisce ancora, il chiamante riceve un errore di politefetch
   che porta l'esito HTTP e la causa, invece di vederli mascherati. La riga
   resta in volo e l'host bloccato fino alla riconciliazione, come scrive la
   documentazione.
4. **La cancellazione asincrona chiude la riga.** Su `CancelledError`,
   `Attempt.__aexit__` (`gate.py:376`) esce senza chiudere. Un `wait_for`
   scaduto, o `asyncio.timeout`, tiene così l'host `in_flight` per tutto
   `reconcile_after` e perde un esito già registrato (riprodotto: oltre 60
   minuti). La chiusura va in un thread protetto da `asyncio.shield`, poi la
   cancellazione riprende. Senza esito la riga si chiude come `cancelled`:
   un fallimento, perché la richiesta può essere arrivata.
5. **Un 3xx diverso da 304 non è una risposta buona** in `Attempt.response`
   (oggi lo è, se si usa il Gate senza adattatore). Gli adattatori lo
   rifiutano già.
6. **La chiave dell'host si normalizza**: minuscole, senza punto finale,
   IDNA. Lo stesso vale per le chiavi delle politiche, altrimenti
   `example.org.` ha un tetto suo accanto a `example.org`. `localhost` e
   `127.0.0.1` restano host diversi, e la documentazione lo dice.
7. **Un Retry-After lunghissimo si vede e si toglie.** Resta onorato anche
   su un 200, perché significa meno richieste. Oggi però blocca fino a 365
   giorni (`_LONGEST_RETRY_AFTER`) e `unfreeze` non lo toglie: `unfreeze` lo
   toglie, con la sua traccia, e `status` lo mostra.

## R2 — Adattatori: scadenza, dimensione, decodifica

- [x] Fatta (D-047)

I trasporti leggono il corpo in un colpo solo (`response.read()`,
`response.content`, `stream.read()`), con un timeout che vale per ogni
blocco letto.

1. **Una scadenza totale per richiesta.** Una fonte che manda un byte ogni
   pochi secondi tiene viva la richiesta per sempre (riprodotto oltre
   `reconcile_after`). I trasporti leggono a blocchi con una scadenza totale
   (`deadline`, default da scrivere in `docs/DECISIONI.md`). Il `Fetcher`
   rifiuta, alla costruzione, una scadenza che non sia minore di
   `gate.reconcile_after`. L'orologio della scadenza è iniettabile, come
   quello del Gate.
2. **Un limite alla dimensione del corpo.** Con `byte_budget="1kB"` la prima
   risposta ha scaricato 5 MB: il budget si consuma solo dopo. La lettura si
   ferma al limite, che per default è il byte budget rimasto quando c'è, e
   l'esito è un fallimento con i byte davvero letti.
3. **La decodifica in un posto solo, nel core.** Un gzip troncato con
   framing corretto passa per buono con `expect="text"` o `"bytes"`
   (riprodotto con httpx e requests: 145 byte su 545). I trasporti
   chiedono e restituiscono il corpo grezzo; il core lo decodifica con la
   libreria standard (`gzip`, `deflate`, `Accept-Encoding` di conseguenza)
   e controlla la fine dello stream. Uno stream che non finisce è
   `SourceNotReady`. Così i byte sul filo e quelli decodificati si contano
   allo stesso modo nei tre client, e urllib smette di chiedere il corpo
   non compresso.
4. **`Content-Length` con cifre non ASCII** (`adapters/base.py:257`): lo
   stesso difetto di R1.2.
5. **Un client con retry propri** (decisione 2 sotto). Una `Session` di
   requests con `HTTPAdapter(max_retries=…)`, o un `httpx.Client` con
   `HTTPTransport(retries=…)`, ripete la richiesta sotto il Gate: con
   `daily_cap=1` sono arrivate alla fonte 5 richieste, e un 503 diventa
   `connection` (riprodotto). `SKILL.md` invita a passare il proprio client
   senza avvertire.
6. **Un `User-Agent` solo**: con httpx un `user-agent` minuscolo passato
   dall'utente si aggiunge al nostro invece di sostituirlo. Gli header si
   uniscono senza distinguere maiuscole e minuscole.

## R3 — Contenuto: tempi, dati vecchi, HTML

- [ ] Fatta

1. **Un tempo del dato nel futuro non avvelena lo storico.** Oggi
   `apply_data_time` (`store/base.py:458`) tiene il massimo, quindi un solo
   tempo del 2099 rende `StuckSource` ogni lettura successiva, con un
   backoff che cresce (riprodotto). Un tempo oltre `now` più una tolleranza
   (default da decidere, per gli orologi un po' avanti) è
   `UnexpectedContent` e non entra nello storico.
2. **`StaleData` e `StuckSource`** (decisione 3 sotto). Oggi contano come
   fallimenti di trasporto: fanno crescere il backoff, non salvano i
   validatori e non portano con sé il contenuto. In più i 304 contano come
   letture per `stuck_after`, che si esprime in letture e non in tempo.
3. **Riconoscere l'HTML senza fermarsi al primo byte.** Oggi lo riconosce
   solo se il corpo comincia con `<!doctype html` o `<html`
   (`checks.py:46`), quindi una pagina d'errore che comincia con `<!--`,
   `<head>` o `<body>` passa per testo. Si saltano BOM, spazi e commenti e si
   guarda il primo kB.

## R4 — Integrazione e dipendenze

- [ ] Fatta

1. **Dipendenze**:
   - `requests` porta `urllib3>=2`, perché l'adattatore usa
     `NameResolutionError`;
   - un job CI con le versioni minime (`uv sync --resolution
     lowest-direct`);
   - l'extra `postgres` dipende da `psycopg` senza `[binary]`, come psycopg
     raccomanda alle librerie, e il README spiega come aggiungerlo;
   - il limite `httpx<1` si rivede a ogni versione.
2. **Più endpoint dello stesso host** (decisione 1 sotto). Promessa e
   `in_flight` valgono per host: un cron che legge `/kp.json` e poi
   `/wind.json` dello stesso host non legge mai il secondo (riprodotto).
3. **Messaggi d'errore**:
   - un 403/429 dice che l'host ora è congelato, e come lo si sblocca;
   - un host senza politica dà un errore di politefetch, non un
     `ValueError` qualsiasi;
   - `HostPolicy(min_interval=60)` fallisce alla costruzione con un
     messaggio chiaro, non con `AttributeError` alla prima richiesta.
4. **Postgres**:
   - nomi qualificati dallo schema al posto di `SET search_path` di sessione,
     che con pgbouncer in modalità transazione si perde;
   - una connessione caduta si riapre;
   - all'apertura, un controllo della distanza fra l'orologio della macchina
     e quello del server.

   Non verificati dal vivo: vanno prima riprodotti.
5. **Documentazione**:
   - con il plugin pytest disattivato (`-p no:politefetch`), il marker non è
     registrato;
   - su Windows un rifiuto su loopback si presenta come timeout di
     connessione (D-044);
   - il tetto riparte a mezzanotte UTC;
   - il `User-Agent` con un contatto è responsabilità di chi usa la
     libreria;
   - lo snippet `gate.attempt` del README diventa eseguibile e verificato da
     un test.
6. **Può aspettare la 0.2**: la pulizia del registro delle richieste
   (`prune`), che oggi cresce senza limiti.

### Da decidere prima di R2–R4

1. **Endpoint dello stesso host.** *Deciso da Paolo il 2026-10-25, insieme
   alla sessione swpc-kit:* un solo intervallo per host, documentato, con un
   esempio di cron che legge più endpoint. È la scelta più cauta per la
   fonte. Il trasporto educato di swpc-kit aspetterà l'intervallo dell'host
   fra un prodotto e l'altro. Scartati i gruppi o le promesse per URL.
2. **Client con retry.** *Deciso da Paolo il 2026-10-25:* `ValueError` alla
   costruzione del `Fetcher` quando i retry del client sono riconoscibili
   (anche i soli retry di connessione di httpx: una regola senza eccezioni),
   e un avvertimento nella documentazione per i trasporti scritti a mano.
   Scartati il solo avviso (in produzione non si vede) e l'azzeramento dei
   retry sul client dell'utente (effetto nascosto su un oggetto suo).
3. **Dati vecchi o fermi.** *Deciso da Paolo il 2026-10-25:*
   - contano come risposte arrivate: vale `min_interval`, il backoff non
     cresce, i validatori si salvano;
   - l'eccezione porta il `FetchResult`, così chi vuole può usare il dato
     vecchio;
   - `stuck_after` accetta anche una durata (`"6h"`).

   Scartati il backoff di oggi (meno richieste, ma una fonte ferma per un
   giorno si rilegge sempre più di rado anche quando riparte, e senza
   validatori ogni lettura riscarica il corpo) e un intervallo dedicato ai
   dati vecchi (un parametro in più per poco: con le richieste condizionali
   le letture di una fonte ferma sono 304). Non è la scelta che manda meno
   richieste: il dubbio riguarda la freschezza del dato, non il carico della
   fonte, e il ritmo resta quello che la policy considera educato.

## T11 — La skill v2

- [ ] Fatta

La skill secondo lo schema comune aggiornato, lo stesso della T11 di
swpc-kit: toglie `sync` e l'entry point `agent_skills` prima che qualcuno li
adotti. Dopo R1–R4, perché la skill descrive l'API che quelle tappe
cambiano. Il modulo `skill` si cambia d'accordo con swpc-kit (D-040).
