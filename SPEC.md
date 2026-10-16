# politefetch — specifica

## Obiettivo

Una libreria Python che sta fra un servizio di ingest e le fonti esterne che
interroga (API pubbliche, file aggiornati periodicamente, server istituzionali
senza contratto) e garantisce due cose:

1. **la fonte non viene mai martellata**, nemmeno se il processo muore a metà,
   si riavvia in ciclo o il codice di chi chiama ha un bug;
2. **un dato rotto o vecchio non passa per buono**: troncato, HTML al posto del
   JSON, fermo da ore anche se la richiesta "riesce".

Le librerie esistenti (tenacity, stamina, pybreaker, aiolimiter, hishel…)
partono dall'idea che ritentare sia un bene e tengono lo stato in memoria.
Questa parte dall'idea opposta, nata da fonti che hanno messo l'indirizzo del
chiamante nel firewall: **ogni richiesta è un costo, e la memoria di cosa si è
promesso deve sopravvivere al processo**.

## Le regole che la libreria rende automatiche

1. **La promessa si scrive prima della chiamata.** Prima di ogni richiesta si
   annota nello stato persistente "non richiamare questo host prima di T", con
   T calcolato come se la richiesta fallisse. Una risposta buona accorcia T;
   un crash a metà lascia la promessa prudente. È l'unico ordine che regge a
   un processo che muore.
2. **Un solo tentativo.** Niente retry automatici: un timeout non distingue
   "la richiesta non è arrivata" da "il firewall della fonte scarta i
   pacchetti". Chi vuole riprovare richiama più tardi, e la libreria decide se
   può.
3. **La pausa dopo un errore cresce** (esponenziale con tetto, configurabile);
   `Retry-After` è un minimo. **403, 429 e 451 congelano l'host** finché una
   persona non lo sblocca (o fino a una scadenza configurata): non si ritentano
   da soli.
4. **Un tetto giornaliero per host**, nello stato persistente, consumato
   *prima* della richiesta da un unico punto per cui passa tutto. Non si
   azzera al riavvio. È la rete di sicurezza che non dipende da nessun'altra
   logica.
5. **I test sono un percorso in uscita.** La libreria offre una barriera sul
   socket per pytest, con la prova che una chiamata vera fallisce e una
   simulata passa (la prova usa un IP pubblico vero: le reti TEST-NET contano
   come private).
6. **Il caso peggiore si conta.** Un simulatore di fonte con scenari (rifiuta,
   non risponde, 429, risponde troncato, HTML, dato fermo, processo ucciso a
   richiesta partita, riavvii in ciclo) e un contatore che verifica quante
   richieste partono davvero in ciascuno.
7. **Una richiesta aperta non è invisibile.** Ogni richiesta ha uno stato
   `in-flight` che si chiude in un `finally`; quelle che nessun `finally` può
   chiudere (processo ucciso, cancellazione) le chiude una **riconciliazione**
   con soglia larga, e contano come fallite. Nessuna statistica le esclude.

## Controlli sul contenuto

- **Troncato**: corpo più corto di `Content-Length`, JSON che finisce a metà,
  corpo vuoto dove serve un dato → `SourceNotReady` (transitorio).
- **Contenuto inatteso**: HTML (per `Content-Type` o per l'inizio del corpo)
  dove ci si aspetta JSON o testo, redirect verso un altro host →
  `UnexpectedContent`.
- **Freschezza sul contenuto**: chi chiama dà una funzione che estrae dal
  corpo il tempo del dato; la libreria lo confronta con `max_age`. Un `304`
  o un `Last-Modified` recente non dicono che il dato è nuovo.
- **Fonte ferma**: se il tempo del dato non avanza per N letture consecutive,
  la libreria lo segnala (`StuckSource`), anche se ogni richiesta è riuscita.

## Traffico

- Richieste condizionali: ETag e Last-Modified salvati per URL e rimandati.
- Byte per host e per giorno, sia sul filo (compressi) sia decodificati.
- Budget di byte per host, consumato come il tetto delle richieste.

## Architettura

- **Core** (solo libreria standard): `Gate` decide se una richiesta può
  partire, apre e chiude le righe del registro, applica le politiche; i
  controlli sul contenuto sono funzioni pure.
- **Store**: interfaccia con tre implementazioni: memoria (test), SQLite
  (default, `sqlite3` della libreria standard), Postgres (extra `postgres`).
  Le operazioni critiche sono atomiche: due processi che condividono lo store
  non superano insieme il tetto.
- **Adattatori di trasporto**: `urllib` (libreria standard, nel core), `httpx`
  e `requests` (extra). Il core non importa nessun client HTTP: cambiare
  libreria tocca solo l'adattatore.
- **Kit di test** (`politefetch.testing`): barriera sul socket, simulatore di
  fonte, contatore del caso peggiore.
- **Stato leggibile**: per ogni host, prossima richiesta consentita, motivo
  dell'eventuale blocco, conteggi e byte di oggi, ultime righe; serializzabile
  in JSON per una dashboard.

Sincrono e asincrono con la stessa semantica. Durante una cancellazione
asincrona non si può attendere in sicurezza: la chiusura della riga in quel
caso la fa la riconciliazione, e la specifica lo dichiara invece di fingere.

```python
from politefetch import Gate, HostPolicy
from politefetch.store import SqliteStore
from politefetch.adapters.httpx import HttpxFetcher

gate = Gate(SqliteStore("ingest.db"), policies={
    "services.swpc.noaa.gov": HostPolicy(daily_cap=2000, min_interval="60s"),
    "celestrak.org": HostPolicy(daily_cap=40, min_interval="2h"),
})
fetch = HttpxFetcher(gate)
result = fetch.get(url, expect="json", data_time=lambda body: ..., max_age="3h")
```

I nomi esatti si decidono nelle tappe; questo è l'impianto.

## Cosa non fa

- Non è un crawler e non pianifica: chi chiama decide quando provare, la
  libreria dice se può.
- Non mette in cache i corpi (per quello c'è hishel): ricorda solo validatori,
  promesse e conteggi.
- Niente autenticazione, niente proxy rotanti, niente aggiramento dei limiti
  di una fonte: lo scopo è l'opposto.
- Non distingue da sola un blocco da un disservizio (servirebbe una verifica
  da più reti): registra la forma del fallimento (timeout di connessione,
  reset, rifiuto, TLS, codice HTTP) in modo che una persona o uno strumento
  esterno possa farlo.

## Qualità

- Python 3.12 o successivo; `uv` e `hatchling`; `mypy --strict`, `ruff`,
  `pytest`, CI su GitHub Actions.
- Le invarianti (il tetto non si supera, una promessa non si accorcia senza
  una risposta buona, ogni riga si chiude) hanno test basati su proprietà
  (`hypothesis`), con crash e riavvii simulati.
- Lo store Postgres si prova contro un Postgres vero (nell'ambiente cloud c'è
  PostgreSQL 16).
