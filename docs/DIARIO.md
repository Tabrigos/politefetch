# Diario

## Da dove ripartire

Riscritto da capo a ogni fine sessione, mai accodato.

- **Stato**: specifica e tappe scritte, nessun codice. Tappe riviste: barriera
  in T0, suite di contratto dello store in T1, simulatore come server locale
  in T6, nuovo ordine (T5 dopo T6, T7 dopo T3 e T4).
- **Prossimo passo**: T0, che trascrive in `docs/DECISIONI.md` le tre decisioni
  del Log del 2026-10-15.
- **Dubbi aperti**: il nome (`fetchguard` non risultava su PyPI il
  2026-10-15, da riverificare in T0); lo stato di httpx (vedi T5); 429 con
  `Retry-After` (la regola 3 dice sia "minimo" sia "congela": la lettura
  prudente è congelare, da scrivere in T2); il push di `.github/workflows/`
  potrebbe essere rifiutato dalle credenziali dell'ambiente (T0 lo scopre).

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
