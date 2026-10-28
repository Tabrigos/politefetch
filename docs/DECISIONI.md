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

- **Superata da**: D-052, solo per la parte sulle migrazioni
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

- **Superata da**: D-046, solo per la chiusura tardiva
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

## D-020 — Le politiche per host

- **Superata da**: D-046, solo per i 3xx (buoni solo i 2xx e il 304) e per un
  `Retry-After` più lungo della politica (diventa un congelamento)
- **Data e tappa**: 2026-10-16, T2
- **Decisione**:
  - le durate si scrivono con l'unità (`"90s"`, `"15m"`, `"2h"`, `"1d"`,
    `"1h30m"`, `"500ms"`) o come `timedelta`. I numeri nudi sono rifiutati:
    `"60"` potrebbe voler dire secondi o minuti, e sbagliare può voler dire
    sessanta volte più richieste;
  - pausa dopo l'n-esimo fallimento consecutivo: `base * fattore**(n-1)`, al
    massimo `backoff_max`. Default 1 minuto, fattore 2, tetto 6 ore;
  - l'intervallo minimo si conta dalla **fine** della richiesta, non
    dall'inizio;
  - la promessa prudente è `adesso + max(intervallo minimo, pausa del
    prossimo fallimento)`;
  - `Retry-After` è un minimo su qualunque risposta, anche buona. Si leggono
    entrambe le forme (secondi e data HTTP); un valore illeggibile si ignora;
    oltre un anno si tratta come un anno, perché un valore assurdo non deve
    far traboccare i calcoli sulle date;
  - 2xx e 3xx (compreso 304) sono risposte buone; tutto il resto è un
    fallimento.
- **Alternative scartate**: intervallo minimo contato dall'inizio della
  richiesta (con richieste lente, più richieste); durate in secondi nudi.

## D-021 — Lo store conta i 429 dall'ultima risposta buona

- **Data e tappa**: 2026-10-16, T2
- **Contesto**: D-013 congela al secondo 429 "senza una risposta buona in
  mezzo"; con il solo numero di fallimenti consecutivi non si distingue un
  429 da un timeout.
- **Decisione**: `HostState.throttled` conta i 429 dall'ultima risposta
  buona; lo aggiorna lo store alla chiusura (si azzera con una risposta
  buona; la riconciliazione non lo tocca). Le colonne aggiunte a una tabella
  già esistente si creano all'apertura se mancano (`_ADDED_COLUMNS` in
  `store/sqlite.py`): un file creato da una tappa precedente continua a
  funzionare, senza migrazioni numerate (D-002).
- **Alternative scartate**: ricavarlo dalle righe del registro (una lettura
  che cresce senza limite, fuori dalla transazione); tenere "l'ultimo stato
  HTTP" (non basta: 429, timeout, 429 sono due 429 senza una risposta buona
  in mezzo).

## D-022 — Il Gate

- **Superata da**: D-046, per la cancellazione asincrona e per i nomi degli
  host; D-049, per l'host senza politica (`NoPolicy`)
- **Data e tappa**: 2026-10-16, T2
- **Decisione**:
  - un host senza politica è rifiutato con `ValueError`, a meno di una
    politica `default` esplicita;
  - l'host si ricava dall'URL, minuscolo e senza porta: porte diverse dello
    stesso host condividono promessa e tetto;
  - `may_fetch(url)` è un consiglio: restituisce un `Verdict` (vero se si può
    partire, altrimenti con il `Refused`), non apre righe, ma chiude le righe
    abbandonate. È `attempt` che decide davvero, in modo atomico;
  - prima di ogni decisione il Gate riconcilia le righe aperte da più di
    `reconcile_after` (default 1 ora);
  - un tentativo senza esito registrato conta come fallito (`unrecorded`); un
    tentativo finito con un'eccezione conta come fallito
    (`exception:<Classe>`) e l'eccezione risale; l'esito si registra una sola
    volta. Se il corpo solleva dopo aver registrato una risposta buona, conta
    la risposta della fonte;
  - `async with` chiama lo store in un thread; se il task viene cancellato
    la riga resta aperta e la chiude la riconciliazione, come dice `SPEC.md`;
  - gli errori dello store, per esempio `sqlite3.OperationalError` quando
    l'attesa sul blocco scade, non vengono avvolti: risalgono dall'ingresso
    nel tentativo e nessuna richiesta parte. Chiude il punto aperto di D-018.
- **Alternative scartate**: una politica di default implicita (un host
  dimenticato nella configurazione partirebbe con limiti scelti da nessuno);
  chiudere la riga anche durante una cancellazione asincrona (bloccherebbe il
  loop degli eventi, o fingerebbe un'attesa sicura che non c'è).

## D-023 — Lo sblocco manuale

- **Data e tappa**: 2026-10-16, T2
- **Decisione**: `unfreeze(host, by=..., note=...)` toglie il congelamento,
  attivo o scaduto, e scrive una traccia (`unfreezes`) con chi, quando,
  perché e il congelamento tolto. `by` è obbligatorio. Fallimenti consecutivi,
  conteggio dei 429 e promessa restano: dopo lo sblocco la pausa riprende da
  dove era, e con l'opzione di D-013 un nuovo 429 congela subito. Su un host
  senza congelamento non fa nulla e non lascia tracce.
- **Alternative scartate**: azzerare i fallimenti allo sblocco (la persona
  dice "puoi riprovare", non "la fonte sta bene").

## D-024 — Il plugin pytest

- **Data e tappa**: 2026-10-17, T6
- **Decisione**:
  - il plugin è registrato come entry point `pytest11` (`politefetch`), quindi
    pytest lo carica da solo dove politefetch è installato; per default non
    fa nulla oltre a offrire fixture, un'opzione e un marcatore;
  - `politefetch_barrier = true` nella configurazione di pytest mette ogni
    test dietro la barriera; la fixture `network_barrier` la mette su un solo
    test (o restituisce quella attiva);
  - un test che ha tentato di uscire fallisce in chiusura anche se il codice
    sotto test ha inghiottito `NetworkBlockedError`, a meno del marcatore
    `politefetch_expects_blocked` (i test che provano la barriera stessa);
  - la fixture `source_simulator` avvia un simulatore per un test;
  - la nostra suite usa l'opzione in `pyproject.toml`; `tests/conftest.py`
    non definisce più la barriera; `pytester` è attivato da `addopts`.
- **Alternative scartate**: barriera attiva per default in chi installa la
  libreria (romperebbe le suite che usano la rete di proposito, e l'utente
  non l'ha chiesto); nessun controllo in chiusura (un `except Exception: pass`
  nel codice sotto test renderebbe la barriera invisibile).
- **Conseguenze**: chi vuole disattivarlo usa `-p no:politefetch`.

## D-025 — Il simulatore e il contatore

- **Data e tappa**: 2026-10-17, T6
- **Decisione**:
  - il simulatore è un server su socket grezzi, in un thread, su 127.0.0.1;
    ogni risposta chiude la connessione (`Connection: close`). I socket
    grezzi servono a produrre ciò che un server HTTP normale non sa fare: un
    corpo più corto di `Content-Length`, un reset TCP, nessuna risposta;
  - scenari: `Respond` (qualunque stato, corpo e intestazioni),
    `TooManyRequests`, `Html`, `Truncated`, `NoAnswer` (tiene la connessione
    finché il simulatore non si ferma), `Reset`, `stuck_json`,
    `advancing_json`; lo scenario si cambia fra una richiesta e l'altra;
  - "rifiuta" è una porta chiusa (`closed_port_url()`): lì non arriva nulla a
    un server, quindi il contatore non vede niente e conta il registro del
    Gate;
  - il contatore registra ogni richiesta **prima** che lo scenario risponda,
    con lo scenario attivo: una richiesta a cui la fonte non risponde è
    comunque arrivata.
- **Alternative scartate**: `http.server` della libreria standard (non sa
  rispondere troncato né fare reset); un trasporto finto (D-003).

## D-026 — Il caso peggiore, contato

- **Data e tappa**: 2026-10-17, T6
- **Decisione**: `tests/test_worst_case.py` fa provare ogni minuto, per 24
  ore finte, un chiamante che insiste qualunque cosa succeda, con 50 richieste
  al giorno, 10 minuti dopo una risposta buona e una pausa dopo i fallimenti
  da 1 minuto raddoppiata fino a 1 ora (mai meno dei 10 minuti). Conta quante
  richieste arrivano alla fonte:

  | Scenario | Richieste arrivate |
  |---|---|
  | risponde (anche con dato fermo) | 50 in un giorno (il tetto) |
  | 503, reset, troncato, HTML, rifiuta | 29 in un giorno |
  | non risponde | 11 in sei ore |
  | 429, 403, 451 | 1 |
  | 429 con l'opzione di D-013 | 2 |
  | cento riavvii nello stesso minuto | 1 |
  | processo ucciso a richiesta partita | 1, e nient'altro per l'ora fino alla riconciliazione |

  Il chiamante dei test (`tests/fetching.py`) usa `urllib` dentro
  `gate.attempt` e tratta come fallimenti un corpo troncato e l'HTML: sono le
  scelte che T3 e T5 faranno per bene.
- **Conseguenze**: un cambiamento delle politiche che fa partire più
  richieste fa fallire questi conteggi esatti, e va motivato.

## D-027 — Il budget di byte si prenota all'apertura

- **Data e tappa**: 2026-10-17, T4
- **Contesto**: la tappa vuole il budget "consumato come il tetto, nella
  stessa transazione dell'apertura", ma all'apertura i byte della risposta
  non si conoscono ancora.
- **Decisione**:
  - all'apertura lo store prenota i byte sul filo dell'ultima risposta
    completa (200) dello stesso URL, zero se non ne ha mai vista una, e
    rifiuta (`byte_budget`, fino alla mezzanotte UTC) se i byte del giorno
    hanno già raggiunto il budget o se la prenotazione lo sforerebbe;
  - alla chiusura i byte veri sostituiscono la prenotazione, contati sul
    giorno in cui la richiesta è partita; se la chiusura non porta i byte, o
    se la riga viene riconciliata, la prenotazione resta consumata;
  - si contano i byte del corpo: sul filo (compressi, se lo erano) per il
    budget, e decodificati a parte; le intestazioni no.
- **Alternative scartate**: contare solo alla chiusura (un processo che muore
  a metà non lascerebbe traccia dei byte); una stima fissa per tutte le
  richieste (arbitraria, e troppo bassa o troppo alta a seconda della fonte).
- **Conseguenze**: la prima richiesta a un URL non prenota nulla e può
  sforare il budget della sua grandezza; dalla seconda in poi la prenotazione
  la ferma prima. Un 304 piccolo dopo un 200 grande restituisce al budget la
  differenza.

## D-028 — I validatori per URL

- **Data e tappa**: 2026-10-17, T4
- **Decisione**:
  - lo store ricorda per ogni URL `ETag` e `Last-Modified` (il testo grezzo
    delle intestazioni) e la grandezza dell'ultima risposta completa, in una
    tabella `urls` separata dal registro;
  - un 200 sostituisce i validatori: se non ne porta, quelli vecchi si
    dimenticano. Un 304 tiene ciò che si sapeva e aggiorna solo i validatori
    che porta. Ogni altro esito non cambia nulla;
  - `attempt.conditional_headers` li restituisce come `If-None-Match` e
    `If-Modified-Since`, da mandare con la richiesta.
- **Alternative scartate**: salvare i validatori nelle righe del registro
  (servirebbe cercare l'ultima risposta buona per URL a ogni richiesta).
- **Conseguenze**: un 304 conferma soltanto che il dato non è cambiato, non
  che è fresco: la freschezza si misura sul contenuto (T3).

## D-029 — Come si registra una risposta

- **Data e tappa**: 2026-10-17, T4
- **Decisione**:
  - `attempt.response(status, headers=...)` legge Retry-After, ETag e
    Last-Modified dalle intestazioni, senza badare a maiuscole e minuscole,
    da qualunque mappa (httpx, requests) o da un `email.message.Message`
    (urllib); `retry_after=` esplicito vince sull'intestazione;
  - `wire_bytes` e `decoded_bytes` si passano sia a `response` sia a
    `failure` (per esempio i byte di un corpo troncato);
  - le grandezze si scrivono come numero di byte o con un'unità esatta:
    `kB`, `MB`, `GB` (potenze di 1000) e `KiB`, `MiB`, `GiB` (potenze di
    1024). Scritture ambigue come `"5mb"` o `"5Kb"` sono rifiutate;
  - il simulatore ha lo scenario `Validated`, che risponde 304 a un
    `If-None-Match` o `If-Modified-Since` che combacia.

## D-030 — I controlli sul contenuto

- **Superata da**: D-048, per i tempi del dato nel futuro e per come si
  riconosce l'HTML
- **Data e tappa**: 2026-10-17, T3
- **Decisione**:
  - funzioni pure in `politefetch.checks`, una per controllo, che tornano in
    silenzio o sollevano un `ContentError`; T5 le chiama nell'ordine che
    serve all'adattatore;
  - `check_length` confronta i byte arrivati sul filo con `Content-Length`:
    meno è `SourceNotReady`, di più è `UnexpectedContent`;
  - `check_body` restituisce il contenuto già letto (JSON analizzato, testo
    decodificato, o i byte). Corpo vuoto (o di soli spazi, per JSON e testo)
    è `SourceNotReady`. HTML per `Content-Type` (`text/html`,
    `application/xhtml+xml`) o per l'inizio del corpo (`<!doctype html`,
    `<html`, anche dopo BOM e spazi) dove si aspetta JSON o testo è
    `UnexpectedContent`. JSON servito come `text/plain` passa;
  - un JSON non valido è "troncato" (`SourceNotReady`) se alla fine del testo
    resta aperta una stringa, una parentesi o una graffa; altrimenti è
    "inatteso" (`UnexpectedContent`). Un test basato su proprietà taglia
    documenti JSON casuali in ogni punto e verifica che il risultato sia
    sempre "troncato";
  - un redirect può cambiare schema, porta o percorso, non host;
  - `read_data_time` esegue l'estrattore di chi chiama: se non trova il
    tempo, il contenuto è inatteso; se restituisce un `datetime` senza fuso è
    un errore dell'estrattore (`ValueError`);
  - `check_fresh` confronta l'età con `max_age`; un dato dal futuro passa
    (alcune fonti pubblicano previsioni);
  - ogni `ContentError` ha una `shape` breve e stabile (`not_ready`,
    `unexpected_content`, `stale_data`, `stuck_source`), da registrare come
    forma del fallimento della richiesta.
- **Alternative scartate**: riconoscere il JSON troncato dal messaggio
  d'errore del parser (cambia fra versioni, e per una stringa non chiusa
  indica l'inizio della stringa, non la fine del testo).
- **Conseguenze**: un numero al primo livello troncato (`12` al posto di
  `123`) è JSON valido: lo scopre solo `check_length`.

## D-031 — Lo storico per la fonte ferma

- **Data e tappa**: 2026-10-17, T3
- **Decisione**: lo store tiene per ogni URL il tempo del dato più recente
  visto e quante letture consecutive non l'hanno superato, compresa quella
  che l'ha portato (`note_data_time`, tabella `data_times`). Un tempo più
  recente riparte da una lettura; lo stesso tempo, o uno più vecchio (una
  fonte che torna indietro non avanza), aggiunge una lettura.
  `check_advancing(record, stuck_after=N)` solleva `StuckSource` da N
  letture in poi, con N almeno 2. `gate.note_data_time(url, data_time)` usa
  l'orologio del Gate.
- **Alternative scartate**: tenere la storia in memoria (si perderebbe a ogni
  riavvio, e una fonte ferma da giorni ripartirebbe da zero); conservare
  tutti i tempi letti (cresce senza limite, e serve solo l'ultimo con il suo
  conteggio).
- **Conseguenze**: T5 decide dove si imposta N (per chiamata, come
  `max_age`) e se registrare `StaleData` e `StuckSource` come fallimenti
  della richiesta (la lettura prudente: sì, così la pausa cresce).

## D-032 — httpx 0.28, non httpx2

- **Data e tappa**: 2026-10-17, T5
- **Contesto**: T5 chiedeva di verificare su PyPI lo stato di httpx prima di
  sceglierlo. Il 2026-10-17:
  - `httpx` è a 0.28.1, stabile, del 6 dicembre 2024; la 1.0 è ancora in
    prerelease (1.0.dev6, 31 agosto 2026);
  - `httpx2` è un fork dell'organizzazione Pydantic, con nome d'import e
    classi d'errore propri; esiste da maggio 2026, con 18 versioni (ultima
    2.13.1, 23 settembre 2026).
- **Decisione**: l'extra `httpx` chiede `httpx>=0.28,<1`: si resta sulla
  versione stabile, e la 1.0 resta fuori finché non è rilasciata e provata.
  `httpx2` non si supporta per ora. L'extra `requests` chiede
  `requests>=2.32,<3`; fra le dipendenze di sviluppo entra `types-requests`
  per `mypy --strict`.
- **Alternative scartate**: dipendere dalla 1.0 in prerelease (l'interfaccia
  può cambiare); scegliere `httpx2` (fork giovane, e un nome d'import diverso
  che chi usa già httpx non ha).
- **Conseguenze**: un adattatore per `httpx2` sarebbe una copia di
  `adapters/httpx.py` con le sue classi d'errore; si valuta se qualcuno lo
  chiede.

## D-033 — Come lavora un adattatore

- **Superata da**: D-047, per il trasporto e gli header; D-048, per i dati
  vecchi o fermi, che non fanno più crescere la pausa
- **Data e tappa**: 2026-10-17, T5
- **Decisione**:
  - un nucleo comune (`adapters/base.py`, `Fetcher` e `AsyncFetcher`) fa il
    ciclo intero; un adattatore fornisce solo l'invio di una GET con il suo
    client e il nome dei suoi errori. Adattatori: `UrllibFetcher` (libreria
    standard), `HttpxFetcher` e `AsyncHttpxFetcher`, `RequestsFetcher`
    (requests non ha una versione asincrona);
  - **i redirect non si seguono**: ognuno sarebbe una seconda richiesta alla
    fonte dentro lo stesso tentativo. Un 3xx diverso da 304 è
    `UnexpectedContent`; se punta a un altro host il messaggio lo dice;
  - forme del fallimento uguali per tutti (`FailureShape`):
    `connect_timeout`, `read_timeout`, `refused`, `reset`, `tls`, `dns`,
    `connection`, `http`. Si riconoscono guardando dietro l'errore del client
    (cause, contesti, `reason`), non dal testo dei messaggi;
  - un errore di contenuto si registra come fallimento con la sua `shape`, lo
    stato HTTP e le intestazioni: `Retry-After` vale ancora, i validatori no
    (garantirebbero un corpo rifiutato, e il 304 successivo confermerebbe un
    dato sbagliato). Così anche dati vecchi o fermi fanno crescere la pausa;
  - un 304 si controlla contro l'ultimo tempo del dato visto per l'URL: conta
    come lettura per la fonte ferma e può essere `StaleData`;
  - `stuck_after` e `max_age` si passano a ogni chiamata, insieme
    all'estrattore del tempo del dato;
  - i timeout sono obbligatoriamente finiti: una richiesta che non finisce
    tiene l'host bloccato fino alla riconciliazione. urllib ha un solo
    timeout per connessione e lettura: l'adattatore usa quello di lettura;
  - ogni richiesta porta `User-Agent: politefetch/<versione>`, sostituibile;
  - nessun adattatore ritenta: urllib e httpx non lo fanno, e requests per
    default nemmeno.
- **Alternative scartate**: seguire i redirect sullo stesso host (più
  richieste per tentativo, e il registro ne conterebbe una sola).
- **Conseguenze**: `tests/test_worst_case.py` ora gira con tutti e quattro gli
  adattatori al posto del chiamante ingenuo, con gli stessi conteggi esatti
  (D-026). `tests/fetching.py` resta per i test del Gate di T6.

## D-034 — L'errore `FetchFailed`

- **Data e tappa**: 2026-10-17, T5
- **Decisione**: un terzo ramo sotto `PoliteFetchError`: `FetchFailed`
  (richiesta partita, nessuna risposta usabile o uno stato HTTP di errore),
  con `url`, `shape` e `status`. Accanto a `Refused` (non partita) e
  `ContentError` (partita, risposta inutilizzabile). `Attempt.failure`
  accetta ora anche `status` e `headers`, per registrare una risposta
  arrivata ma rifiutata.
- **Alternative scartate**: restituire un risultato con `ok=False` (chi
  chiama potrebbe non guardarlo e usare un corpo d'errore come dato).

## D-035 — Il driver Postgres è psycopg 3

- **Superata da**: D-049, solo per l'extra, che non chiede più `[binary]`
- **Data e tappa**: 2026-10-17, T7
- **Contesto**: T7 chiedeva di scegliere e motivare il driver. Su PyPI il
  2026-10-17: `psycopg` 3.3.6 (settembre 2026), `psycopg2` 2.9.13, `asyncpg`
  0.32.0, `pg8000` 1.31.5 (settembre 2025).
- **Decisione**: l'extra `postgres` chiede `psycopg[binary]>=3.2,<4`. psycopg
  3 è il successore di psycopg2, mantenuto, con tipi per `mypy --strict`, e
  con le ruote binarie non serve compilare nulla. La licenza LGPL del driver
  non tocca il MIT di politefetch: è una dipendenza opzionale, importata
  solo da `store/postgres.py`.
- **Alternative scartate**: `asyncpg` (solo asincrono, mentre lo store è
  sincrono come gli altri); `psycopg2` (in manutenzione, senza tipi);
  `pg8000` (puro Python, meno diffuso).

## D-036 — Lo store Postgres: uno scrittore alla volta

- **Data e tappa**: 2026-10-17, T7
- **Decisione**:
  - ogni operazione che cambia lo stato è una transazione che prende per
    prima cosa un lock consultivo di transazione (`pg_advisory_xact_lock`)
    legato allo schema: le scritture vanno una alla volta, come con il
    `BEGIN IMMEDIATE` di SQLite. Le letture usano istantanee `REPEATABLE READ`
    in sola lettura e non aspettano;
  - `lock_timeout` (30 secondi, configurabile) limita l'attesa del lock; se
    scade l'operazione solleva `psycopg.errors.LockNotAvailable` e nessuna
    richiesta parte;
  - un processo che muore dentro una transazione perde la connessione:
    Postgres annulla la transazione e rilascia il lock (c'è un test che lo
    uccide con `SIGKILL` mentre tiene il lock);
  - le tabelle stanno in uno schema proprio (`schema=`, default
    `politefetch`), creato all'apertura sotto lo stesso lock, così due
    processi che partono insieme non si scontrano nel crearlo;
  - tempi in `timestamptz` con la sessione in UTC; le regole sono quelle di
    `politefetch.store.base`, le stesse degli altri store.
- **Alternative scartate**: lock di riga per host (più concorrenza, ma la
  riconciliazione e lo storico dei tempi toccano più righe e andrebbero
  ordinati per evitare stalli; ai ritmi di questa libreria, poche richieste
  al minuto, la concorrenza non serve); isolamento `SERIALIZABLE` con
  ritentativi (un ciclo di ritentativi nello store, e gli errori di
  serializzazione da gestire).
- **Conseguenze**: togliere il lock fa superare il tetto a quattro processi
  insieme: il test con più processi lo scopre.

## D-037 — I test contro Postgres

- **Data e tappa**: 2026-10-17, T7
- **Decisione**:
  - i test leggono la stringa di connessione da `POLITEFETCH_TEST_POSTGRES`;
    senza, i test Postgres vengono saltati con il motivo scritto. In CI
    `POLITEFETCH_REQUIRE_POSTGRES=1` trasforma l'assenza in un errore, così
    non si possono saltare in silenzio;
  - ogni test lavora in uno schema suo (`test_<casuale>`) e lo cancella alla
    fine: i test non si pestano i piedi e il database resta pulito;
  - in locale `scripts/postgres-for-tests.sh` accende il cluster 16 e crea
    ruolo e database di prova; in CI c'è un servizio `postgres:16`. La
    password è usa e getta, per un database su loopback, non un segreto;
  - Postgres entra in `KINDS` di `tests/stores.py`: la suite di contratto, i
    test del Gate, la macchina a stati (25 esempi), lo storico dei tempi e i
    test con più processi girano anche su Postgres.
- **Alternative scartate**: avviare un cluster dai test (servirebbe un
  sottoprocesso, che la guardia di D-012 vieta, e i permessi di root per
  `initdb`).

## D-038 — Lo stato per host

- **Data e tappa**: 2026-10-18, T8
- **Contesto**: `SPEC.md` vuole, per ogni host, la prossima richiesta
  consentita, il motivo del blocco, i conteggi e i byte di oggi e le ultime
  righe, serializzabili in JSON.
- **Decisione**:
  - `gate.status(host_or_url, recent=10)` restituisce un `HostStatus`
    (`politefetch.status`) costruito con **la stessa decisione** di
    `may_fetch`: un test basato su proprietà controlla che lo stato non dica
    mai "puoi" quando il gate direbbe "no", e che motivo e "riprova dopo"
    coincidano con quelli del `Refused`;
  - con un URL la decisione conta i byte che quell'URL riserverebbe sul
    budget, come `may_fetch`; con un host non ne conta. Nel dubbio si chiede
    con l'URL;
  - come `may_fetch`, prima riconcilia le righe abbandonate: senza, lo stato
    mostrerebbe "in volo" una richiesta che il prossimo `attempt` chiuderebbe;
  - `next_allowed` è `at` se si può, `None` se l'host è congelato fino allo
    sblocco; `frozen` mostra solo il congelamento in vigore;
  - `recent` sono le ultime righe dell'host, dalla più nuova; lo store prende
    `rows(latest=N)` (tre store, suite di contratto, indice `(host, id)`)
    così una dashboard non legge tutto il registro a ogni giro;
  - `gate.statuses()` copre gli host con una politica propria; gli host
    coperti solo dalla politica di default si chiedono per nome
    (`statuses(hosts)`), perché lo store non ha un elenco degli host;
  - `to_dict()` / `to_json()` danno tempi ISO 8601 con fuso e i nomi salvati
    di motivi e stati; `to_json(statuses)` accetta uno stato o una lista.
- **Alternative scartate**: uno stato che non riconcilia (in sola lettura,
  ma racconterebbe una richiesta morta come viva); un comando a riga di
  comando per lo stato (dovrebbe conoscere le politiche, che stanno nel
  codice di chi usa la libreria).

## D-039 — Rifinitura dell'API pubblica prima di 0.1.0

- **Data e tappa**: 2026-10-18, T8
- **Decisione**:
  - `politefetch.traffic` era rimasto un modulo vuoto: richieste condizionali
    e byte stanno nello store e nel Gate da T4. Lo tolgo invece di lasciare
    un nome pubblico senza niente dentro;
  - `FetchFailed` si importa anche da `politefetch`, accanto a `Refused` e
    `ContentError`: sono i tre esiti che chi chiama `fetch.get` deve gestire;
  - il README ha un esempio completo (un job da cron per il Kp di SWPC, nel
    formato verificato da swpc-kit) che `tests/test_readme.py` esegue così
    com'è, puntato sul simulatore: due volte come `__main__`, per mostrare
    che la promessa regge fra due processi, e con le giornate storte della
    fonte. Gli altri frammenti si controllano: si analizzano e i nomi che
    importano da politefetch esistono; il JSON di esempio ha la forma vera.

## D-040 — La skill per gli agenti dentro il pacchetto

- **Superata da**: D-050
- **Data e tappa**: 2026-10-18, T8 (decisione), T9 (lavoro)
- **Contesto**: l'utente vuole che il pacchetto porti con sé una skill per
  gli assistenti AI, leggibile anche da una persona, aggiornata con il
  codice, disponibile solo nei progetti che usano il pacchetto, senza
  repository o dipendenze in più, per ora solo per Claude Code; e uguale per
  tutti i suoi pacchetti (swpc-kit l'ha già).
- **Decisione**: lo schema comune "skill dei pacchetti" v1, concordato con
  la sessione di swpc-kit, che fa da riferimento (`src/swpc_kit/skill/`,
  PR #11 di swpc-kit):
  - la skill sta in `src/politefetch/skill/politefetch/` (`SKILL.md` scritto
    a mano, `references/api.md` generato e verificato dai test), con
    `metadata.version` uguale alla versione del pacchetto;
  - l'entry point `[project.entry-points.agent_skills] politefetch =
    "politefetch.skill"` la rende scopribile; il modulo espone `bundled()`;
  - `python -m politefetch.skill install | check | show | path | sync`;
    `sync` allinea `.claude/skills/` del progetto ai pacchetti installati
    (aggiunge, aggiorna se la versione cambia, toglie quelle dei pacchetti
    che non dichiarano più l'entry point, mai quelle senza il marcatore
    `.package-skill`), mette un `.gitignore` con `*` nelle cartelle che crea,
    stampa una riga ed esce sempre con 0;
  - chi usa la libreria mette nel `.claude/settings.json` del suo progetto
    un hook `SessionStart` che lancia `sync`: ogni sessione parte con la
    skill della versione installata, solo in quel progetto.
  Verificato con Claude Code 2.1.302: una skill in una cartella ignorata da
  git si carica, e una skill scritta dall'hook è disponibile nella stessa
  sessione.
- **Alternative scartate**: copia in `~/.claude/skills` (vale per tutti i
  progetti del PC); un piccolo strumento di sincronizzazione in un
  repository a sé (una dipendenza in più per poche righe); collegamenti
  simbolici al pacchetto installato (si rompono quando l'ambiente si rifà).
- **Conseguenze**: nuova tappa T9 dopo T8; la versione 0.1.0 passa da T8 a
  T9, così la prima versione esce con la skill. Il modulo `skill` segue lo
  schema comune e non si cambia da soli: una modifica si concorda con gli
  altri pacchetti.

## D-041 — Come la skill resta vera

- **Data e tappa**: 2026-10-18, T9
- **Contesto**: D-040 vuole una skill aggiornata a ogni modifica, che un
  agente possa usare senza leggere il codice.
- **Decisione**:
  - `SKILL.md` è scritto a mano (modello mentale, scelte, esiti, stato, test,
    cosa non fare); `references/api.md` è generato da
    `tools/build_skill.py` per introspezione: ogni modulo pubblico, ogni nome
    del suo `__all__`, firme e primo paragrafo delle docstring, i valori
    degli enum con le loro docstring lette dal sorgente;
  - i test falliscono se: il riferimento non è quello rigenerato; un modulo
    pubblico manca dall'elenco del generatore (lo confronta con i moduli
    trovati nel pacchetto); un nome di `__all__` manca dal riferimento; il
    riferimento cambia fra due generazioni (niente indirizzi di memoria);
    la versione della skill non è quella del pacchetto o manca dal
    CHANGELOG; un nome o un percorso `politefetch.…` citato non esiste; un
    parametro scritto in una chiamata non appartiene a quella funzione; un
    motivo di rifiuto, una forma di fallimento o un errore non sono spiegati;
    un esempio Python non gira (contro il simulatore, con `FakeClock`); un
    sottocomando citato non esiste;
  - la versione sta in `politefetch.__version__` e `pyproject.toml` la legge
    da lì (`dynamic = ["version"]`), come in swpc-kit;
  - il modulo `politefetch.skill` è quello di swpc-kit con le sole costanti
    cambiate (verificato con un `diff`).
  Verificato da capo a fondo con Claude Code 2.1.302: un progetto di prova
  con le wheel di politefetch e swpc-kit e l'hook `SessionStart` che lancia
  il `sync` di politefetch riceve le skill di entrambi i pacchetti, git le
  ignora, e un agente a cui si chiede un job da cron "che non si faccia
  bannare", senza nominare la libreria, consulta la skill da solo e propone
  `Gate`, `SqliteStore`, `HostPolicy` e `UrllibFetcher` senza leggere il
  sorgente.
- **Alternative scartate**: un riferimento scritto a mano (invecchia in
  silenzio); generare anche `SKILL.md` (le docstring dicono cosa fa una
  funzione, non quando usarla e cosa evitare).

## D-042 — Il repository diventa pubblico come `politefetch`

- **Data e tappa**: 2026-10-25, fuori dalle tappe (sessione locale)
- **Contesto**: D-010 lasciava il repository col nome `fetchguard`; il
  progetto si prepara a diventare open source.
- **Decisione**: il repository si chiama `politefetch`. Autore, titolare del
  copyright (`LICENSE`, `pyproject.toml`) e identità git sono
  `Sidereus Team <sidereus.team@gmail.com>`, come in plancia-ui. Prima di
  renderlo pubblico la storia è stata riscritta: ogni commit ha quell'autore,
  senza trailer, e `CLAUDE.md` e `.claude/` ne sono usciti (sono file degli
  strumenti di chi lavora, ignorati in locale). `AGENTS.md` e `SPEC.md` sono
  in inglese; `docs/` resta in italiano come storia dello sviluppo.
- **Alternative scartate**: rinominare il repository esistente e riscriverne
  la storia con un force-push (le pull request fuse tengono raggiungibili i
  commit originali).
- **Conseguenze**: supera il titolare del copyright di D-011 e chiude il
  punto aperto di D-010. I numeri delle pull request citati qui e nel Log si
  riferiscono al repository precedente, privato.

## D-043 — Si pubblica su PyPI dalla CI, con l'approvazione del maintainer

- **Data e tappa**: 2026-10-25, fuori dalle tappe (sessione locale)
- **Contesto**: il nome `politefetch` è libero su PyPI e su TestPyPI il
  2026-10-25 (un "pending publisher" non lo riserva). Una build locale
  metteva nello sdist i file degli strumenti ignorati solo in
  `.git/info/exclude`, che hatchling non legge.
- **Decisione**: PyPI, account Sidereus Team. Pubblica solo
  `.github/workflows/release.yml` con Trusted Publishing (nessun token):
  il tag `vX.Y.Z` costruisce, controlla, aspetta l'approvazione nell'environment
  `pypi`, pubblica con le attestazioni PEP 740 e crea la release su GitHub;
  un avvio manuale pubblica su TestPyPI, come prova generale della prima
  versione. Lo sdist ha un elenco esplicito di file (`only-include`),
  ricontrollato dal workflow. La prima pubblicazione viene dopo la skill v2
  e il plugin pytest che si importa su Windows, perché toglie `sync` e
  l'entry point `agent_skills` prima che qualcuno li adotti.
- **Alternative scartate**: `uv publish` dal PC con un token (un segreto da
  custodire e una build che dipende dai file locali); pubblicare subito la
  0.1.0.
- **Conseguenze**: regole in `AGENTS.md` ("Releases"). Gli environment
  `pypi` (con revisore) e `testpypi` si creano quando il repository è
  pubblico.

## D-044 — politefetch funziona anche su Windows, e la CI lo prova

- **Data e tappa**: 2026-10-25, fuori dalle tappe (sessione locale)
- **Contesto**: su Windows `politefetch.testing.barrier` non si importava
  (`socket.socket` non ha `sendmsg`). Il plugin è un entry point `pytest11`,
  quindi pytest si rompeva in ogni progetto Windows che installa politefetch.
  Corretto l'import, la suite è partita e ha mostrato un buco peggiore: il
  loop di asyncio predefinito su Windows (proactor) apre le connessioni con
  `_overlapped.ConnectEx`/`WSAConnect` e spedisce con `WSASendTo`, senza
  passare da `socket.socket`. La barriera lasciava uscire asyncio, e con lui
  httpx asincrono.
- **Decisione**:
  - la barriera sorveglia solo i metodi del socket che la piattaforma ha;
  - su Windows sorveglia anche `asyncio.windows_events.IocpProactor.connect`
    e `.sendto`, con la stessa regola (passa solo il loopback) e la stessa
    chiusura del socket su un `connect` bloccato. È un'API interna di
    CPython: se cambia, i test asyncio della barriera diventano rossi nel job
    Windows;
  - `Child.join` aspetta il pipe e insieme il processo: su Windows un figlio
    ucciso prima di prendere il suo capo del pipe lo lascia aperto, e l'EOF
    non arriva mai. Su Windows "ucciso" è `TerminateProcess`, che
    `multiprocessing` riporta come `-SIGTERM`;
  - i test si adattano a Windows senza cambiare cosa provano: il rifiuto su
    loopback arriva dopo circa 2 s di SYN ritentati (misurato: 2,05 s), più
    del timeout di connessione dei test; il server "HTTP davanti a TLS" legge
    l'handshake prima di rispondere, altrimenti la chiusura con dati non letti
    manda un reset che su Windows cancella la risposta; il generatore della
    skill toglie anche gli indirizzi esadecimali maiuscoli e scrive sempre LF;
    l'orologio comune ai processi dei test sugli store è `perf_counter`, non
    `monotonic`, che con Python 3.12 su Windows avanza a scatti di 15,6 ms:
    a ogni scatto i tentativi vedevano lo stesso istante e l'intervallo
    minimo li rifiutava (19 ammessi su un tetto di 30, solo nel job 3.12);
  - un job `windows` in CI (3.12 e 3.13) esegue pytest e mypy, che controlla
    i rami `win32` solo su Windows. Postgres resta nel job Linux: i runner
    Windows non hanno i container di servizio.
- **Alternative scartate**: dichiarare il plugin solo per Linux e macOS (un
  utente Windows avrebbe avuto pytest rotto o la barriera disattivata);
  sorvegliare `_overlapped` direttamente (`Overlapped` è un tipo C, i suoi
  metodi non si sostituiscono); avviare un sottoprocesso per provare l'import
  senza `sendmsg` (D-012 lo vieta: il test carica una seconda copia del
  modulo mentre `socket.socket` è una classe senza `sendmsg`).
- **Conseguenze**: la protezione di `main` richiede anche `windows (3.12)` e
  `windows (3.13)`. Su Windows, con un timeout di connessione sotto i 2 s, un
  rifiuto su loopback si presenta come `connect_timeout`: il default di 10 s
  non lo vede.

## D-045 — Prima di pubblicare, quattro tappe dalla revisione scettica

- **Data e tappa**: 2026-10-25, fuori dalle tappe (sessione locale)
- **Contesto**: prima della prima pubblicazione, due revisioni scettiche
  con script contro il simulatore e gli store veri. Il tetto fra processi,
  i crash fra apertura e chiusura e i congelamenti normali reggono. Restano
  però strade per cui la fonte riceve più di quanto promesso, o un 403/429
  non congela:
  - una richiesta lenta riconciliata mentre è viva;
  - un `Retry-After` con cifre non ASCII;
  - una chiusura che fallisce o viene cancellata;
  - i retry del client sotto il Gate.

  Ci sono poi dati sbagliati accettati (gzip troncato, tempi nel futuro) e
  attriti d'integrazione.
- **Decisione**: quattro tappe in `docs/TAPPE.md` (R1 esiti, R2 adattatori,
  R3 contenuto, R4 integrazione), in fila e prima della T11 e della
  pubblicazione. Ogni difetto riprodotto entra come test di regressione
  prima della correzione. Tre scelte di comportamento restano al
  maintainer, con una proposta scritta nella sezione delle tappe:
  - endpoint dello stesso host;
  - client con retry;
  - dati vecchi o fermi.
- **Alternative scartate**: pubblicare la 0.1.0 così e correggere nella 0.2.
  I difetti di R1 e R2 toccano la promessa principale della libreria, che
  la fonte non riceva più di quanto promesso: una versione su PyPI con quei
  buchi verrebbe adottata così com'è.
- **Conseguenze**: la prima versione su PyPI esce dopo R1–R4 e T11. Le tappe
  R2 e R3 cambiano l'API pubblica (`Transport`, `Fetcher`, le eccezioni di
  contenuto), e la T11 la descrive già cambiata.

## D-046 — Esiti che non si perdono (R1)

- **Data e tappa**: 2026-10-25, R1
- **Contesto**: la revisione di D-045 ha trovato più strade per cui un
  403/429 non congelava l'host o la fonte riceveva più richieste di quelle
  promesse. Ognuna è entrata come test rosso prima della correzione.
- **Decisione**:
  - **chiusura tardiva**: `close_request` su una riga riconciliata come
    abbandonata non è più una no-op. L'esito vero si scrive sulla riga, che
    resta `RECONCILED`, e l'host cambia solo con `apply_late_close`: la
    promessa più lontana, il congelamento più lungo, un 429 in più. I
    fallimenti non crescono di nuovo, perché la riconciliazione li ha già
    contati, e un esito buono non azzera nulla. Non si salvano validatori; i
    byte veri si aggiungono solo se superano la prenotazione. Una seconda
    chiusura tardiva non cambia nulla. Questo corregge D-017: lì una
    chiusura tardiva non rovesciava la riconciliazione, qui può solo
    renderla più prudente;
  - **`Retry-After`**: la forma in secondi accetta solo cifre ASCII (RFC
    9110). `"²".isdigit()` è vero, ma `int("²")` solleva: prima l'errore
    usciva da `Attempt.response` e lo stato HTTP si perdeva. Il parsing non
    solleva mai, provato con hypothesis su testo qualsiasi;
  - **chiusura che fallisce**: si ritenta `CLOSE_TRIES` volte (3). Lo store
    è il nostro database, non la fonte: ritentare non manda nulla. Se tutti i
    tentativi falliscono, `OutcomeNotSaved` porta URL, riga, forma e stato,
    con l'errore dello store come `__cause__` e quello del blocco come
    `__context__`. La riga resta in volo e l'host bloccato fino alla
    riconciliazione;
  - **cancellazione asincrona**: la riga si chiude anche durante una
    cancellazione, con l'esito registrato o come `cancelled`. La chiusura
    gira in un thread tenuto vivo da un riferimento, e si aspetta con
    `asyncio.shield`: una seconda cancellazione interrompe l'attesa, non la
    chiusura. Se la chiusura fallisce, la cancellazione resta una
    cancellazione e la riga va alla riconciliazione. Corregge D-022 e la
    frase di `SPEC.md`;
  - **3xx**: in `Attempt.response` sono buone risposte solo i 2xx e il 304.
    Un redirect seguito sarebbe un'altra richiesta;
  - **host**: `normalize_host` mette in minuscolo, toglie il punto finale e
    passa i nomi internazionali in IDNA. Lo usano le chiavi delle politiche,
    gli URL, `status` e `unfreeze`, e rifiuta nomi con etichette vuote.
    `localhost` e `127.0.0.1` restano due host;
  - **`Retry-After` lunghissimo**: oltre la pausa più lunga della politica
    (`max(min_interval, backoff_max)`) diventa un congelamento con scadenza
    pari al `Retry-After`, dopo qualunque risposta, un 200 compreso. Si
    rispetta fino in fondo, ma `status` lo mostra come `frozen` e `unfreeze`
    lo toglie. Una risposta buona può quindi congelare, ma solo a tempo.
- **Alternative scartate**:
  - ignorare un `Retry-After` su un 200, o accorciarlo: più richieste di
    quelle che la fonte ha chiesto;
  - far togliere a `unfreeze` la promessa, che contraddice D-023;
  - chiudere sincrono sul loop durante la cancellazione: bloccherebbe il loop
    per tutto il `busy_timeout`;
  - lasciare la cancellazione alla riconciliazione: un esito già registrato,
    anche un 403, si perdeva.
- **Conseguenze**: un task cancellato ora libera l'host con la pausa di un
  fallimento, come un timeout di lettura, invece di tenerlo in volo per
  `reconcile_after`. Nuovo errore pubblico `OutcomeNotSaved`; nuova funzione
  `politefetch.gate.normalize_host`. La scadenza totale della richiesta, che
  evita le chiusure tardive, arriva in R2.

## D-047 — Adattatori: scadenza, dimensione, decodifica (R2)

- **Data e tappa**: 2026-10-25, R2
- **Contesto**: i trasporti leggevano il corpo in un colpo solo, con un
  timeout per ogni blocco: una fonte che gocciola il corpo teneva viva la
  richiesta oltre `reconcile_after`, il byte budget si consumava solo dopo
  il download, e ogni client decodificava a modo suo (un gzip troncato con
  il framing giusto passava per buono). Un client con retry propri mandava
  alla fonte richieste che il Gate non aveva ammesso.
- **Decisione**:
  - **il trasporto consegna il corpo grezzo, a pezzi**: `send` restituisce
    un context manager con `RawResponse(status, headers, chunks)`; i pezzi
    sono il corpo come trasferito, senza framing ma non decodificato. Il
    core legge, conta i byte, applica scadenza e limite e chiude la
    connessione quando smette di leggere. httpx legge con `iter_raw`,
    requests con `raw.read1(decode_content=False)`, urllib con `read1`:
    tutti consegnano ciò che è arrivato senza aspettare un blocco pieno;
  - **scadenza**: `deadline` per l'intera richiesta, default cinque minuti.
    Si controlla all'arrivo della testa e dopo ogni pezzo, quindi una
    richiesta dura al massimo la scadenza più un timeout di connessione e
    uno di lettura: il `Fetcher` rifiuta alla costruzione una somma che non
    sia minore di `gate.reconcile_after`. Cinque minuti bastano per file di
    molti MB e restano lontani dall'ora di riconciliazione. L'orologio è
    `timer`, iniettabile, di default `time.monotonic`, che non salta
    indietro. Un 2xx oltre la scadenza è `FetchFailed` con forma nuova
    `deadline` e lo stato HTTP; per un errore HTTP la scadenza non cambia
    nulla, il fallimento resta lo stato;
  - **dimensione**: la lettura si ferma oltre il limite più stretto fra
    `max_bytes` (argomento di `get`) e i byte rimasti nel budget del giorno
    (`Attempt.bytes_left`, calcolato all'apertura senza contare la
    prenotazione della richiesta stessa). Un 2xx tagliato è `BodyTooLarge`
    (forma `too_large`) con i byte davvero letti. `max_bytes` vale anche per
    il corpo decodificato, così un gzip di pochi KB non diventa un GB in
    memoria;
  - **decodifica nel core** (`adapters/decoding.py`): gzip, anche in più
    membri, e deflate, zlib o grezzo. I fetcher chiedono
    `Accept-Encoding: gzip, deflate`, urllib compreso. Uno stream compresso
    che non arriva alla fine è `SourceNotReady`; uno non valido, o una
    codifica che non sappiamo togliere (`br`, `zstd`), è
    `UnexpectedContent`;
  - **client con retry** (decisione 2 di TAPPE): `ValueError` alla
    costruzione. Per requests si guardano gli `HTTPAdapter` della sessione
    e si chiede a `Retry` se dopo un fallimento di qualche tipo
    ritenterebbe; per httpx i `retries` del pool di ogni trasporto, anche
    quelli montati. Sono attributi privati di httpx: un test controlla che
    esistano ancora. Un trasporto scritto a mano non si può controllare: lo
    dice la documentazione;
  - **header**: i nostri, poi quelli del chiamante, poi i condizionali,
    senza distinguere maiuscole e minuscole: un `user-agent` del chiamante
    sostituisce il nostro;
  - **`Content-Length`**: solo cifre ASCII, come il `Retry-After` di R1.
- **Alternative scartate**:
  - leggere a blocchi dentro ogni trasporto: tre copie della stessa logica,
    e i byte contati in tre modi;
  - una scadenza con l'orologio del Gate: è un orologio da parete, può
    saltare, e nei test è fermo;
  - un limite sul corpo decodificato preso dal budget: il budget conta i
    byte sul filo;
  - decodificare `br` e `zstd`: servirebbero dipendenze fuori dalla
    libreria standard.
- **Conseguenze**: cambia il protocollo `Transport` (e `TransportError`,
  `IncompleteBody`, `RawResponse`): un trasporto scritto a mano va
  riscritto. Una fonte che gocciola la testa stessa resta limitata solo dal
  timeout di lettura del client su ogni pezzo: la scadenza si controlla
  quando la testa è arrivata. Il simulatore ha lo scenario `Drip` e unisce
  gli header ripetuti, così un header mandato due volte si vede.

## D-048 — Contenuto: tempi del dato, dati vecchi, HTML (R3)

- **Data e tappa**: 2026-10-25, R3
- **Contesto**: la revisione di D-045 ha riprodotto tre difetti del
  contenuto: un solo tempo del dato nel futuro (2099) rendeva `StuckSource`
  ogni lettura successiva, con un backoff che cresceva; dati vecchi o fermi
  facevano crescere il backoff come un guasto della fonte e perdevano
  validatori e contenuto; una pagina d'errore che comincia con `<!--`,
  `<head>` o `<body>` passava per testo.
- **Decisione**:
  - **tempo nel futuro**: un tempo del dato oltre `now + max_ahead` è
    `UnexpectedContent` e non entra nello storico. Default 15 minuti:
    abbastanza per un orologio un po' avanti, non per un campo sbagliato o
    un fuso orario scambiato. Chi legge previsioni alza `max_ahead`. Prima
    `check_fresh` lasciava passare il futuro "per le previsioni"; ora il
    limite c'è e si sposta;
  - **dati vecchi o fermi** (decisione 3 di TAPPE, presa dal maintainer): sono
    risposte arrivate. Il Gate registra un esito buono, quindi vale
    `min_interval`, i fallimenti non crescono e i validatori si salvano;
    poi la fetch solleva `StaleData` o `StuckSource` con il `FetchResult` in
    `result`, anche dopo un 304. Il registro mostra un esito buono: la
    fonte ha fatto la sua parte, e il problema del dato sta nello storico
    dei tempi e nell'eccezione;
  - **`stuck_after` come durata**: oltre a un numero di letture accetta una
    durata (`"6h"`): fermo se il tempo del dato non è avanzato da quando lo
    si è letto la prima volta. Lo storico ha il campo nuovo `since`, nei tre
    store; una riga SQLite o Postgres scritta prima prende come `since`
    l'ultima lettura, il momento più tardi possibile, quindi segnala più
    tardi e non prima. Una lettura sola non è mai ferma. Un 304 conta come
    lettura: il dato non si è mosso;
  - **HTML**: si guarda il primo tag dopo BOM, spazi, commenti e una
    dichiarazione XML, entro il primo kB; sono HTML `<!doctype html`,
    `<html`, `<head` e `<body` seguiti da fine, spazio, `>` o `/`. Un JSON
    non comincia mai con `<`, quindi per JSON non ci sono falsi allarmi; per
    il testo, un XML qualsiasi o un tag diverso passano.
- **Alternative scartate**:
  - scartare in silenzio un tempo nel futuro e tenere i dati: il chiamante
    userebbe un dato con un tempo sbagliato senza saperlo;
  - lasciare il backoff sui dati vecchi: meno richieste, ma una fonte ferma
    per un giorno si rilegge sempre più di rado anche quando riparte;
  - cercare `<html` in tutto il primo kB: un JSON o un testo che lo citano
    diventerebbero pagine;
  - un campo nuovo nel registro per segnare un esito buono ma vecchio:
    cambierebbe lo schema delle richieste per un'informazione che lo
    storico dei tempi ha già.
- **Conseguenze**: `StaleData` e `StuckSource` hanno `result`, `StuckSource`
  anche `since`; `DataTimeRecord` ha `since`; nuova funzione
  `check_not_ahead` e nuovo argomento `max_ahead` di `get`. Un chiamante che
  contava sui fallimenti per rallentare una fonte ferma ora vede il ritmo
  di `min_interval`.

## D-049 — Integrazione e dipendenze (R4)

- **Data e tappa**: 2026-10-26, R4
- **Contesto**: la revisione di D-045 ha trovato attriti per chi adotta la
  libreria: dipendenze più larghe del codice, un cron che legge due
  endpoint dello stesso host e non legge mai il secondo, messaggi che non
  dicono cosa è successo all'host, uno store Postgres che dipende dallo
  stato della sessione.
- **Decisione**:
  - **dipendenze**: l'extra `requests` porta `urllib3>=2.2,<3`. Il 2.2 è il
    primo con `HTTPResponse.read1`, che l'adattatore usa da R2 (provato: 2.0
    e 2.1 non l'hanno); `NameResolutionError` c'è dal 2.0. L'extra
    `postgres` dipende da `psycopg` senza `[binary]`, come psycopg chiede
    alle librerie; il README dice come aggiungere la libpq, e i test usano
    `psycopg[binary]` dal gruppo dev. Un job CI nuovo (`lowest`) installa le
    versioni minime con `--resolution lowest-direct`, con Postgres. Il limite
    `httpx<1` si rivede prima di ogni release: lo dice `AGENTS.md`;
  - **più endpoint dello stesso host** (decisione 1, presa dal maintainer):
    restano un intervallo e un blocco per host. Il README ha un esempio
    eseguito da un test: ogni giro del cron legge l'endpoint letto meno di
    recente, preso dalle righe recenti di `gate.status`;
  - **messaggi**: `FetchFailed` ha `note`, e la fetch la riempie quando la
    risposta ha congelato l'host: fino a quando, e la chiamata
    `gate.unfreeze(host, by=...)`. Un host senza politica solleva
    `NoPolicy`, che resta anche un `ValueError` per chi lo catturava.
    `parse_duration` e `parse_size` rifiutano con `TypeError`, e un messaggio
    con un esempio, un tipo che non sia testo, `timedelta` o `int`: prima
    `HostPolicy(min_interval=60)` cadeva su un `AttributeError`;
  - **Postgres**: niente più stato di sessione. Ogni transazione imposta
    con `SET LOCAL` schema, fuso orario e `lock_timeout`, e le istruzioni
    non si preparano (`prepare_threshold=None`): con pgbouncer in modalità
    transazione la connessione del server può essere azzerata fra due
    transazioni. Ho scelto `SET LOCAL` al posto dei nomi qualificati che
    TAPPE proponeva: stesso effetto, e nessuna delle venti istruzioni SQL da
    riscrivere. Una connessione trovata persa all'inizio di una transazione
    si riapre e la transazione parte su quella nuova: non era partito nulla,
    quindi è sicuro. Una persa a metà solleva, e l'operazione dopo apre una
    connessione nuova. All'apertura lo store confronta l'orologio della
    macchina con quello del server e rifiuta oltre `max_clock_skew` (un
    minuto): le macchine che condividono lo store scrivono le promesse con
    il proprio orologio, e una in anticipo chiederebbe alla fonte troppo
    presto. Rifiutare di aprire è la scelta più cauta; `None` toglie il
    controllo;
  - **riprodotti prima**: i test Postgres sono entrati da soli in una PR in
    bozza e la CI li ha visti rossi (`UndefinedTable` dopo `DISCARD ALL`,
    offset `+02:00` con un altro fuso, `AdminShutdown` dopo
    `pg_terminate_backend`).
- **Alternative scartate**:
  - promesse o gruppi per URL: più richieste alla stessa fonte (decisione 1);
  - un semplice avviso sull'orologio: in produzione non lo legge nessuno;
  - ritentare una transazione interrotta a metà: se il commit era arrivato
    al server, la si applicherebbe due volte.
- **Conseguenze**: `NoPolicy` e `FetchFailed.note` sono nuovi; un'app che usa
  l'extra `postgres` deve aggiungere una libpq; `PostgresStore` ha `clock` e
  `max_clock_skew`. La pulizia del registro (`prune`) resta per la 0.2.

## D-050 — La skill v2 (T11)

- **Data e tappa**: 2026-10-26, T11
- **Contesto**: lo schema v1 della skill (D-040: entry point
  `agent_skills`, `sync` da un hook, copie ignorate da git, confronto per
  versione) faceva entrare nel contesto le istruzioni di qualunque
  dipendenza senza revisione. Lo standard comune `skill-di-libreria` lo
  sostituisce, e swpc-kit è già passato (in `main`, nota in inbox del
  2026-10-25).
- **Decisione**:
  - `politefetch/skill/__init__.py`, `tools/build_skill.py` e
    `tests/test_skill.py` sono copie del modello, con le sole costanti di
    politefetch sopra la riga; sotto la riga il modulo è identico a quello
    di swpc-kit (verificato con `diff`), quindi l'accordo di D-040 regge
    senza un altro scambio;
  - il generatore è quello generico: confrontato con il nostro, non perde
    nessun nome pubblico e aggiunge le tabelle dei campi; il modulo `skill`
    esce dal riferimento, come vuole il modello;
  - i test di dominio (esempi della skill eseguiti contro il simulatore,
    ogni motivo di rifiuto, forma di fallimento ed errore nelle tabelle, le
    keyword che appartengono alla loro chiamata) stanno in
    `tests/test_skill_examples.py`;
  - i valori degli enum nella SKILL.md si scrivono come stringhe
    (`"connect_timeout"`): sono i testi registrati, e il controllo dei nomi
    del modello salta le stringhe;
  - aggiunte le sezioni "Not covered" e il paragrafo "For coding agents" del
    README; la CI costruisce la wheel in ogni job e la passa al test
    (`SKILL_WHEEL`), che in CI non si salta. In locale la wheel non si
    costruisce (CLAUDE.md), quindi quel test lì si salta.
- **Lasciato fuori, con il motivo**:
  - il symlink di dogfooding `.claude/skills/politefetch`: `.claude/` del
    repo è locale, e su Windows un symlink chiede la Modalità sviluppatore;
  - `source_simulator` in `NOT_API`: il generatore elenca le fixture, ma il
    controllo dei nomi del modello non le conta fra i nomi noti. È un
    difetto del modello: va corretto lì e ricopiato, non qui (corretto e
    ricopiato il 2026-10-26, `source_simulator` è uscito da `NOT_API`);
  - la prova d'attivazione con un agente nuovo e la trigger eval: da fare
    quando la skill cambia descrizione o tabella, prima della release.

## D-051 — Niente piano né diario nel repo: il lavoro sta nelle issue

- **Data e tappa**: 2026-10-26, fuori dalle tappe (sessione locale)
- **Contesto**: `docs/TAPPE.md` e `docs/DIARIO.md` servivano alle sessioni
  cloud, una tappa per sessione, che non avevano altro posto dove lasciare lo
  stato. Dal 2026-10-25 si lavora in locale, lo stato si tiene fuori dal
  repo e il diario lo ripeteva, già divergendo. Le tappe erano tutte chiuse,
  e 19 commit su 96 servivano solo a registrare l'avanzamento.
- **Decisione** (scelta del maintainer):
  - tolti `docs/TAPPE.md` e `docs/DIARIO.md`: restano nella storia git, e
    ogni tappa ha la sua PR con le misure;
  - il lavoro previsto sta nelle issue di GitHub, in inglese; un piano di più
    passi è una checklist nella sua issue, e la PR che lo finisce la chiude;
  - una decisione e la voce del CHANGELOG entrano nel commit del codice;
    niente commit di sola registrazione;
  - questo file resta in italiano, perché le voci non si riscrivono; le voci
    fino a D-050 citano tappe (T0–T11, R1–R4) che ora si leggono nella storia.
- **Alternative scartate**: un `docs/ROADMAP.md` corto in inglese (torna un
  file di piano da aggiornare con dei commit); tutto fuori dal repo (chi
  arriva dal repo pubblico non vede cosa è previsto); le voci nuove in
  inglese (un file in due lingue).
- **Conseguenze**: `AGENTS.md` (ciclo di sessione, lingue) e il README
  cambiano nello stesso commit.

## D-052 — Versioni dello schema e migrazioni degli store

- **Data e tappa**: 2026-10-27, issue #11
- **Contesto**: gli store SQLite e Postgres creano le tabelle all'apertura e
  non registrano una versione (D-002, che valeva fino al primo rilascio).
  Diverse issue della 0.2 aggiungono colonne o tabelle, e uno store scritto
  dalla 0.1 deve tenere promesse, conteggi e freeze: ricrearlo li
  dimenticherebbe.
- **Decisione**:
  - una tabella `schema_version` con una riga; la versione 1 è lo schema della
    0.1, e un database con le tabelle ma senza versione è alla versione 1;
  - ogni versione successiva è una migrazione nel codice dello store, una
    lista di istruzioni eseguite in ordine nella stessa transazione che
    aggiorna la versione: una migrazione fallita lascia il database com'era;
  - le migrazioni aggiungono soltanto (tabelle, colonne con un default,
    indici): un processo che gira ancora con la versione vecchia continua ad
    applicare le regole che conosce;
  - un database più nuovo del codice si rifiuta all'apertura (`ValueError`):
    il codice vecchio ignorerebbe uno stato che può trattenere richieste;
  - SQLite migra all'apertura, dentro la transazione `BEGIN IMMEDIATE`, così
    due processi che aprono insieme migrano una volta sola;
  - Postgres crea uno schema nuovo alla versione più recente, ma uno schema
    esistente con migrazioni in attesa si rifiuta finché non lo si apre con
    `migrate=True`: lo possono usare altre macchine, che vanno fermate prima;
  - tornare alla 0.1 su un file migrato non è previsto: la 0.1 non legge la
    versione e ignorerebbe lo stato nuovo.
- **Alternative scartate**: `PRAGMA user_version` per SQLite (collide con
  un'applicazione che usa lo stesso file per le sue tabelle, e Postgres
  avrebbe comunque bisogno di una tabella); migrare Postgres a ogni apertura
  (una macchina aggiornata cambierebbe lo schema sotto le altre senza che
  nessuno lo decida); uno strumento esterno come Alembic (una dipendenza nel
  nucleo, che usa solo la libreria standard).
- **Conseguenze**: `PostgresStore` prende `migrate=`. Le fixture
  `tests/fixtures/store-0.1.1.db`, scritta dalla 0.1.1 installata da PyPI, e
  `store-0.1.1-integration.db`, ripulita da una prima integrazione, provano
  che un file della 0.1 si apre con ogni riga. Le migrazioni della 0.2 si
  aggiungono a `_MIGRATIONS` nel modulo di ciascuno store.
## D-053 — Leggere lo stato non scrive; la riconciliazione sta nell'apertura

- **Data e tappa**: 2026-10-27, issue #29 (revisione dell'architettura, R5)
- **Contesto**: ogni decisione, anche `status` e `may_fetch`, riconciliava
  tutti gli host in una transazione di scrittura (`BEGIN IMMEDIATE`, un
  advisory lock per lo schema in Postgres). Un pannello in un altro processo
  riceveva `database is locked` mentre chi scaricava scriveva (riprodotto), e
  un monitor senza diritto di scrittura non poteva esistere. Un tentativo
  costava due transazioni di scrittura: riconciliazione e apertura.
- **Decisione**:
  - `open_request` prende `abandon_before`: le righe in volo dell'host aperte
    prima si chiudono come abbandonate nella stessa transazione che apre la
    richiesta. La chiusura resta anche se l'apertura viene rifiutata, perché
    quelle righe possono essere proprio ciò che la rifiuta (`in_flight`): il
    rifiuto si solleva dopo il commit;
  - `status`, `statuses` e `may_fetch` leggono lo stato e contano le righe
    oltre la soglia come le conterà la chiusura (un fallimento ciascuna, fuori
    da `in_flight`), senza scrivere; `HostStatus.abandoned` le mostra;
  - `Gate.reconcile()` resta, per chi vuole chiudere tutto a mano.
- **Alternative scartate**: riconciliare in lettura solo quando c'è qualcosa
  da chiudere (un monitor senza permessi fallirebbe proprio dopo un crash);
  mostrare le righe come già riconciliate in `recent` (una chiusura che nel
  database non c'è).
- **Conseguenze**: le righe abbandonate di un host che nessuno interroga più
  restano in volo nel database finché qualcuno non gli chiede una richiesta o
  chiama `Gate.reconcile()`; lo stato letto le conta già.
