# Decisioni

Le scelte di progetto con il loro perché. Una voce per decisione, numerata,
mai riscritta: se una scelta cambia, si aggiunge una voce nuova che rimanda
alla vecchia e la vecchia prende la riga "Superata da".

## Formato

```
## D-NNN — Titolo breve

- **Data e tappa**: AAAA-MM-GG, Tn
- **Contesto**: il problema, in poche righe.
- **Decisione**: cosa si fa.
- **Alternative scartate**: cosa e perché no.
- **Conseguenze**: cosa cambia per le tappe successive.
```

---

## D-001 — La barriera sul socket nasce in T0

- **Data e tappa**: 2026-10-15, revisione prima di T0
- **Contesto**: `AGENTS.md` vuole la barriera attiva in ogni test, ma la
  barriera era prevista in T6; le tappe T0–T5 sarebbero rimaste senza.
- **Decisione**: T0 scrive la barriera vera, minima, in
  `fetchguard.testing.barrier`, attiva in ogni test con una fixture automatica
  in `tests/conftest.py`. Passano solo il loopback e i socket Unix. T6 la
  completa e la rende un plugin pytest installabile.
- **Alternative scartate**: `pytest-socket` (dipendenza da togliere in T6,
  semantica diversa); una barriera solo nel `conftest.py` (diventerebbe una
  seconda barriera parallela a quella pubblica).
- **Conseguenze**: la regola 5 di `SPEC.md` è provata fin dalla prima tappa.

## D-002 — T7 dopo T3 e T4, con una suite di contratto dello store

- **Data e tappa**: 2026-10-15, revisione prima di T0
- **Contesto**: T3 e T4 aggiungono metodi allo store mentre T7 implementa
  Postgres; in parallelo lo store Postgres nascerebbe incompleto.
- **Decisione**: T7 parte dopo T3 e T4. T1 crea una suite di contratto
  parametrizzata su tutti gli store, e ogni tappa che aggiunge un metodo
  aggiunge lì i suoi test. Fino al primo rilascio niente migrazioni numerate:
  ogni modulo crea le sue tabelle con `CREATE TABLE IF NOT EXISTS`.
- **Alternative scartate**: interfaccia completa definita da T1 in anticipo
  (metodi senza test, dalla forma probabilmente sbagliata); interfacce
  separate per tappa (romperebbe l'atomicità: il budget di byte di T4 si
  consuma nella stessa transazione dell'apertura della riga di T1).
- **Conseguenze**: ordine `T0 → T1 → T2 → { T3, T4, T6 } → { T5, T7 } → T8`.

## D-003 — Il simulatore è un server HTTP locale

- **Data e tappa**: 2026-10-15, revisione prima di T0
- **Contesto**: il simulatore di T6 doveva agganciarsi all'interfaccia
  `Fetcher`, che nasce in T5.
- **Decisione**: il simulatore è un vero server HTTP su 127.0.0.1 avviato in
  un thread dal test; il contatore conta le richieste arrivate al server. I
  trasporti finti in memoria restano per i test unitari di `Gate`.
- **Alternative scartate**: un `Transport` astratto definito in T2
  (sostituirebbe nei test proprio il codice degli adattatori che va provato).
- **Conseguenze**: T5 viene dopo T6; i timeout veri vanno tenuti brevi e
  confinati agli scenari "non risponde".

## D-004 — Ricognizione: non ci si appoggia a nessuna libreria esistente

- **Data e tappa**: 2026-10-16, T0
- **Contesto**: T0 chiede di leggere `pyresilience` 0.4.0, `retryhop` 0.1.1,
  `hishel` 1.4.0, `pybreaker` 1.4.1 e `pyrate-limiter` 4.5.0 (codice delle
  wheel, non solo i README) e di fermarsi se una copre già gran parte di
  `SPEC.md`.
- **Decisione**: nessuna copre gran parte di `SPEC.md`; si va avanti senza
  dipendere da nessuna. Quattro su cinque tengono lo stato solo in memoria;
  le due che persistono (hishel, pyrate-limiter) non sono atomiche fra
  processi su SQLite senza dipendenze esterne.

  | | pyresilience | retryhop | hishel | pybreaker | pyrate-limiter |
  |---|---|---|---|---|---|
  | R1 promessa prima della chiamata | no | no | no | no (scrive dopo il fallimento) | parziale (solo intervallo minimo) |
  | R2 un solo tentativo | parziale (preset a 3 tentativi) | no (ritenta per scopo) | sì | sì | sì (ma attende bloccando) |
  | R3 pausa crescente, `Retry-After` minimo, congelamento | parziale (`Retry-After` come massimo) | parziale (`Retry-After` come massimo) | no | parziale (pausa fissa, si riapre da solo) | no |
  | R4 tetto giornaliero persistente | no | no | no | no | parziale (SQLite non atomico senza `filelock`) |
  | R5 barriera sul socket | no | no | no | no | no |
  | R6 simulatore e contatore | no | no | no | no | no |
  | R7 righe aperte e riconciliazione | parziale (contatore in memoria) | no | parziale (solo voci di cache) | no | no |
  | Controlli sul contenuto | no | no | no | no | no |
  | Richieste condizionali | no | no | sì (ma solo con il corpo in cache) | no | no |
  | Byte per host e giorno | no | no | no | no | no |

- **Alternative scartate**:
  - *pyrate-limiter per il tetto*: il tetto deve stare nella stessa
    transazione della promessa e della riga aperta, e un bucket esterno non
    può parteciparvi. Inoltre: finestra mobile di 24 ore invece del giorno
    UTC, `try_acquire` che blocca per default, orologio non iniettabile,
    sicurezza fra processi solo con `filelock` e un solo bucket.
  - *hishel per i validatori*: i validatori stanno solo insieme al corpo in
    cache, che `SPEC.md` esclude; dipendenza obbligatoria da `msgpack`; la
    freschezza euristica da `Last-Modified` serve dati vecchi senza fare
    richieste, l'opposto della freschezza sul contenuto. Una tabella
    `(url, etag, last_modified)` costa meno. hishel resta compatibile sopra
    l'adattatore httpx, se qualcuno vuole anche la cache dei corpi.
- **Conseguenze**: idee da riprendere, riscritte con la libreria standard:
  - la scrittura "solo se più grande" di pybreaker (`set_if_greater`) per una
    promessa che non si accorcia senza una risposta buona;
  - il `LOCK TABLE ... NOWAIT` di pyrate-limiter su Postgres (rifiutare in
    caso di contesa: prudente per la fonte), per T7;
  - `make_conditional_request` e il confronto degli ETag deboli di hishel
    come riferimento RFC 9110, per T4;
  - la pulizia delle voci incomplete di hishel come modello di
    riconciliazione, per T1;
  - il parsing di `Retry-After` con `email.utils`, ma usato come **minimo**
    (tutte e due le librerie che lo leggono lo usano come tetto), per T2.

## D-005 — Il nome `fetchguard` rischia di non essere registrabile

- **Superata da**: D-010
- **Data e tappa**: 2026-10-16, T0
- **Contesto**: `fetchguard` non esiste su PyPI (404), ma `fetch-guard`
  esiste: un server MCP che scarica URL e li restituisce in markdown, ultima
  versione 1.4.1 del 2026-10-15. PyPI rifiuta i nomi nuovi che, tolti i
  separatori, coincidono con uno esistente (non provato con una
  registrazione vera); e il rischio di confusione c'è comunque, perché i due
  progetti fanno cose vicine.
- **Decisione**: non si cambia il nome ora (il pacchetto non si pubblica
  prima di T8); la scelta spetta alla persona che guida il progetto. Tre
  alternative libere su PyPI il 2026-10-16, anche con il trattino:
  `fetchwarden`, `politefetch`, `pollwarden`.
- **Alternative scartate**: rinominare subito (tocca import, documenti e
  repo prima che il nome sia scelto).
- **Conseguenze**: se il nome cambia, conviene farlo prima di T1, quando il
  codice da rinominare è ancora poco.

## D-006 — La gerarchia degli errori

- **Data e tappa**: 2026-10-16, T0
- **Contesto**: `SPEC.md` elenca gli errori senza dire come si organizzano.
- **Decisione**:
  - `Refused` (nessuna richiesta è partita) e `ContentError` (una richiesta è
    partita, ma la risposta non si può usare) sono due rami distinti sotto
    `FetchGuardError`; `SourceNotReady`, `UnexpectedContent`, `StaleData` e
    `StuckSource` stanno sotto `ContentError`.
  - `Refused` porta `host`, `reason` (enum `RefusalReason`: `min_interval`,
    `backoff`, `frozen`, `daily_cap`, `byte_budget`) e `retry_after`.
    `retry_after` può mancare solo per `frozen`, cioè un congelamento senza
    scadenza che aspetta una persona: in tutti gli altri casi chi chiama deve
    sapere quando riprovare.
  - I valori di `RefusalReason` sono stringhe stabili: finiranno nello store e
    nello stato JSON.
  - `StuckSource` richiede almeno due letture: una sola non mostra un tempo
    fermo.
  - Ogni errore si può serializzare con pickle (attraversa processi).
- **Alternative scartate**: gerarchia piatta (chi chiama non distingue "non
  ho chiesto" da "ho chiesto e la risposta è da buttare"); `retry_after`
  sempre facoltativo (un rifiuto senza orario invita a riprovare subito).
- **Conseguenze**: T2 può aggiungere motivi, toccando il file il minimo.

## D-007 — L'orologio rifiuta i tempi non UTC

- **Data e tappa**: 2026-10-16, T0
- **Decisione**: `as_utc` rifiuta i `datetime` senza fuso e quelli con un
  fuso diverso da zero, invece di convertirli: un tempo non UTC che arriva
  alla libreria è un errore di chi chiama, e convertirlo in silenzio lo
  nasconderebbe. `FakeClock.advance` va solo avanti; `FakeClock.set` salta
  dove si vuole, anche indietro, come fa un orologio vero (NTP, ripresa di
  una VM). `FakeClock` sta nel core: è libreria standard e serve anche a chi
  usa fetchguard nei propri test.
- **Conseguenze**: ruff vieta `time.time`, `datetime.now` e simili fuori da
  `SystemClock` (regole `TID251` e `DTZ`), così la regola "tempo
  iniettabile" di `AGENTS.md` si controlla da sola.

## D-008 — Come si comporta la barriera

- **Superata da**: D-012, solo per la parte sui processi figli
- **Data e tappa**: 2026-10-16, T0
- **Decisione**:
  - `NetworkBlockedError` deriva da `RuntimeError`, non da `OSError`: il
    codice che cattura `OSError` per registrare "connessione fallita" non
    deve scambiare la barriera per una fonte giù.
  - Sono bloccati `connect`, `connect_ex`, `sendto` e `sendmsg` verso
    indirizzi non loopback, e le risoluzioni DNS di nomi diversi da
    `localhost`, compresa quella inversa (che interroga un DNS anche per un
    indirizzo letterale). Le famiglie diverse da IPv4, IPv6 e Unix sono
    bloccate.
  - Su un `connect` bloccato la barriera chiude il socket:
    `socket.create_connection` lo chiude solo davanti a un `OSError`, e
    altrimenti resterebbe aperto.
  - La prova usa 1.1.1.1, un indirizzo pubblico vero; un test verifica che lo
    sia e che 192.0.2.1 (TEST-NET) non lo sia.
- **Conseguenze**: la barriera non raggiunge i processi figli. I test con
  due processi (T1) e il processo ucciso del simulatore (T6) dovranno
  attivarla anche nel figlio; T6 decide come (variabile d'ambiente letta dal
  plugin, o attivazione esplicita nel figlio).

## D-009 — Strumenti e CI

- **Data e tappa**: 2026-10-16, T0
- **Decisione**:
  - `.python-version` a 3.12: si sviluppa sulla versione minima supportata; la
    CI prova 3.12 e 3.13.
  - `uv.lock` sta nel repo e la CI usa `uv sync --all-extras --locked`, così
    un lockfile non aggiornato fa fallire la CI invece di cambiare in
    silenzio.
  - `astral-sh/setup-uv` è fissata a `v10.3.0`, perché non pubblica un tag
    mobile `v10`; `actions/checkout` a `v7`.
  - ruff non tocca i file Markdown (questa versione formatta anche i blocchi
    di codice dentro i `.md`, e avrebbe riscritto `SPEC.md`).
  - Le dipendenze di sviluppo stanno in `[dependency-groups]`, che
    `uv sync` installa da solo.
- **Conseguenze**: nessuna licenza è dichiarata in `pyproject.toml`: va
  scelta prima della pubblicazione (T8).

## D-010 — Il nome è `politefetch`

- **Data e tappa**: 2026-10-16, T0
- **Contesto**: D-005; la persona che guida il progetto ha scelto fra le tre
  alternative.
- **Decisione**: pacchetto, modulo e distribuzione si chiamano
  `politefetch`; l'errore base diventa `PoliteFetchError`. `politefetch` e
  `polite-fetch` erano liberi su PyPI il 2026-10-16.
- **Alternative scartate**: `fetchwarden`, `pollwarden`.
- **Conseguenze**: le voci precedenti e il Log di `docs/DIARIO.md` citano
  ancora `fetchguard` e `fetchguard.testing`: sono storia e non si
  riscrivono. Il repository GitHub si chiama ancora `fetchguard`; il nome si
  cambia dalle impostazioni del repo (GitHub reindirizza i vecchi URL).

## D-011 — Licenza MIT

- **Data e tappa**: 2026-10-16, T0
- **Decisione**: licenza MIT, file `LICENSE` nella radice, dichiarata in
  `pyproject.toml` con `license = "MIT"` e `license-files` (PEP 639, già
  supportata da hatchling ≥ 1.27). Titolare del copyright: Sidereus Team, il nome
  usato nel primo commit.
- **Conseguenze**: chiude il punto aperto di D-009.

## D-012 — La barriera nei processi figli si attiva in modo esplicito

- **Data e tappa**: 2026-10-16, T0 (per T1 e T6)
- **Contesto**: la barriera agisce solo nel processo del test (D-008). I
  test con due processi di T1 e il client ucciso di T6 lanciano processi
  figli che partirebbero senza barriera.
- **Decisione**: una sola funzione in `politefetch.testing` avvia un processo
  figlio e vi attiva la barriera prima di eseguire il codice. Un test di
  guardia, con un controllo sull'AST come `tests/test_layout.py`, fallisce se
  un test usa `subprocess` o `multiprocessing` senza passare da quella
  funzione. La scrive T1; T6 la estende per poter uccidere il figlio con
  `SIGKILL`.
- **Alternative scartate**: attivazione automatica in ogni interprete con un
  file `.pth` o `sitecustomize` (copre tutto, ma è un meccanismo nascosto
  che gira all'avvio di ogni Python e, nel pacchetto pubblicato, toccherebbe
  anche gli utenti); attivazione esplicita senza guardia (chi se ne dimentica
  resta scoperto senza saperlo).
- **Conseguenze**: anche gli utenti della libreria possono usare la stessa
  funzione nei loro test.

## D-013 — 429 con `Retry-After`: per default si congela

- **Data e tappa**: 2026-10-16, T0 (per T2)
- **Contesto**: la regola 3 di `SPEC.md` dice sia che `Retry-After` è un
  minimo sia che 429 congela l'host fino allo sblocco manuale; su un 429 con
  `Retry-After` le due frasi si contraddicono.
- **Decisione**:
  - per default un 429 congela l'host anche se porta `Retry-After`; se c'è
    una scadenza configurata, non è mai più corta di `Retry-After`;
  - un'opzione per host, spenta per default, fa rispettare `Retry-After` al
    primo 429: pausa pari al massimo fra `Retry-After` e la pausa crescente,
    senza congelare. Un secondo 429 consecutivo, senza una risposta buona in
    mezzo, congela comunque;
  - 403 e 451 congelano sempre: per loro `Retry-After` non ha un significato
    utile.
- **Alternative scartate**: congelare sempre senza opzione (un'API con 429
  brevi e regolari fermerebbe il flusso finché una persona non interviene);
  rispettare sempre `Retry-After` (alcune fonti bannano chi torna puntuale
  allo scadere, o rispondono 429 in ciclo: un falso "puoi").
- **Conseguenze**: T2 implementa l'opzione in `HostPolicy` e i test di
  scenario della scalata al secondo 429.

## D-014 — I controlli duri stanno nello store; il Gate passa funzioni pure

- **Data e tappa**: 2026-10-16, T1
- **Contesto**: la decisione "può partire?" e la scrittura della promessa
  prudente devono essere un'unica operazione atomica, anche fra processi; e
  il tetto giornaliero deve reggere da solo, senza dipendere da altra logica
  (regola 4).
- **Decisione**:
  - `open_request` controlla dentro la propria transazione congelamento,
    richiesta già in corso, promessa non scaduta e tetto, e rifiuta con
    `Refused` senza scrivere nulla, qualunque cosa abbia calcolato chi chiama;
  - il Gate fornisce solo i tempi di cui è responsabile, come funzioni pure
    dello stato dell'host: `cautious(state)` per la promessa prudente,
    `update(state)` per la promessa dopo l'esito. Lo store le chiama dentro
    la transazione, così vedono sempre lo stato corrente;
  - le regole sono in `politefetch.store.base` (`check_admission`,
    `apply_close`), condivise da tutti gli store: T7 le riusa per Postgres.
- **Alternative scartate**:
  - valori calcolati prima dal Gate su uno stato letto a parte: fra lettura e
    scrittura un processo fermo (GC, VM sospesa) userebbe uno stato vecchio e
    scriverebbe una promessa prudente più corta del dovuto;
  - numeri di versione con conflitto e nuovo tentativo: corretto, ma sposta
    un ciclo di ritentativi nel Gate per ottenere la stessa cosa;
  - una sola funzione del Gate che decide tutto: il tetto dipenderebbe dalla
    logica del Gate, contro la regola 4.
- **Conseguenze**: le funzioni girano con lo store bloccato, quindi devono
  essere pure e veloci e non devono chiamare lo store. Se sollevano
  un'eccezione, l'operazione non scrive nulla.

## D-015 — Una richiesta alla volta per host

- **Data e tappa**: 2026-10-16, T1
- **Contesto**: la promessa prudente può scadere mentre la richiesta è
  ancora aperta (fonte che non risponde, processo appeso). Con la sola
  promessa, a quel punto partirebbe una seconda richiesta verso un host che
  già non risponde.
- **Decisione**: finché un host ha una riga in corso, ogni nuova richiesta è
  rifiutata con il nuovo motivo `RefusalReason.IN_FLIGHT`, fino alla
  chiusura o alla riconciliazione della riga. `retry_after` è la promessa
  prudente, se è nel futuro, altrimenti adesso. La promessa prudente deve
  essere successiva ad adesso: un fallimento implica sempre una pausa.
- **Alternative scartate**: affidarsi alla sola promessa prudente (permette
  richieste concorrenti verso un host appeso).
- **Conseguenze**: una richiesta appesa blocca l'host fino alla soglia di
  riconciliazione. Per questo T2 deve far girare la riconciliazione, con una
  soglia larga ma non infinita.

## D-016 — Un rifiuto riporta il vincolo che finisce per ultimo

- **Data e tappa**: 2026-10-16, T1
- **Decisione**: se più vincoli bloccano insieme (per esempio tetto esaurito
  fino a mezzanotte e promessa fino alle 8 del giorno dopo), `Refused` riporta
  quello che finisce più tardi. Un congelamento senza scadenza vince su
  tutto; a parità vale l'ordine congelamento, in corso, promessa, tetto.
- **Alternative scartate**: un ordine fisso di priorità (chi aspetta
  `retry_after` andrebbe a sbattere contro il vincolo successivo, con un
  altro rifiuto e un altro giro).

## D-017 — Chiusura e riconciliazione

- **Data e tappa**: 2026-10-16, T1
- **Decisione**:
  - una chiusura con esito buono può accorciare la promessa e azzera i
    fallimenti consecutivi; un fallimento la allunga soltanto (si tiene la
    più lontana) e incrementa i fallimenti;
  - un esito buono non può congelare l'host;
  - `update(state)` riceve lo stato prima della chiusura: il Gate calcola la
    pausa con `state.failures + 1`;
  - la riconciliazione chiude come fallite le righe aperte da più della
    soglia (stato `reconciled`, forma del fallimento `abandoned`), incrementa
    i fallimenti e lascia la promessa prudente scritta all'apertura;
  - una chiusura arrivata dopo la riconciliazione non cambia nulla: il
    verdetto prudente resta, e la riga resta contata come fallita;
  - un congelamento con scadenza smette di bloccare da solo alla scadenza;
    il record resta nello store finché T2 non introduce lo sblocco.
- **Alternative scartate**: lasciare che una chiusura tardiva con esito
  buono accorci la promessa (la riconciliazione ha già detto "fallita", e
  cambiare idea dopo sarebbe un falso "puoi").

## D-018 — Lo store SQLite e lo store in memoria

- **Data e tappa**: 2026-10-16, T1
- **Decisione**:
  - SQLite: ogni operazione critica è una transazione `BEGIN IMMEDIATE`, che
    prende il blocco di scrittura prima di leggere. Giornale WAL,
    `synchronous=FULL` (una promessa scritta sopravvive anche a un'interruzione
    di corrente, non solo a un crash), attesa sul blocco di 30 secondi
    configurabile; se l'attesa scade l'operazione solleva
    `sqlite3.OperationalError` e nessuna richiesta parte. Tempi salvati come
    testo UTC a larghezza fissa, che si ordina come i tempi. Tre tabelle:
    `hosts`, `daily_counts`, `requests`;
  - memoria: lo stato sta in un oggetto `MemoryData`. Passarlo a un nuovo
    `MemoryStore` simula un riavvio; è atomico fra thread, non fra processi.
- **Alternative scartate**: transazioni `DEFERRED` (due processi leggono
  insieme "resta una richiesta" e la spendono entrambi: un test lo prova);
  `synchronous=NORMAL` (più veloce, ma a questi volumi l'`fsync` non costa
  nulla e la prudenza sì).
- **Conseguenze**: T2 decide se avvolgere `sqlite3.OperationalError` in un
  errore di politefetch.

## D-019 — I processi figli dei test

- **Data e tappa**: 2026-10-16, T1
- **Decisione**: `politefetch.testing.processes.spawn(target, *args)` avvia
  il figlio con il metodo `spawn` (un interprete nuovo, senza eredità
  accidentali dal padre), vi attiva la barriera e restituisce il risultato o
  rilancia l'eccezione del figlio. `join(timeout)` uccide il figlio se è
  ancora vivo allo scadere; un figlio che muore senza risultato solleva
  `ChildFailed` con il codice di uscita; `kill()` lo uccide con `SIGKILL`. Le
  funzioni eseguite nei figli stanno in `tests/children.py`, e `tests/` è
  ora un pacchetto. La guardia in `tests/test_layout.py` vieta nei test
  `subprocess`, `multiprocessing`, `os.fork`, `os.exec*`, `os.spawn*`,
  `os.system`, `os.popen`, `os.posix_spawn*` e `ProcessPoolExecutor`.
- **Alternative scartate**: il metodo `fork` (copia anche la barriera del
  padre, ma con i thread è fragile e Python lo sta abbandonando come
  default).
