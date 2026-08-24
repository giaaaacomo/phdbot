# TODO

## Generalizzare le opportunità accademiche

Estendere PHDBOT oltre le sole posizioni PhD mantenendo la stessa pipeline di
discovery, estrazione, scraping, indicizzazione e ricerca semantica.

- [x] Introdurre un campo strutturato `position_type` per PhD, postdoc,
  fellowship, assistantship, Master/MPH, internship/traineeship, ricerca e docenza.
- Chiarire e modellare separatamente programmi accademici come MPH/MPhil, che
  non sono necessariamente posizioni lavorative.
- Ampliare fonti, parole chiave di discovery e prompt di estrazione per ogni tipo.
- Salvare il tipo nei payload Qdrant e aggiungere il relativo filtro alla GUI.
- Conservare una ricerca semantica unica, con filtri rigidi per tipo, paese,
  università e scadenza.

## Robustezza della pipeline

- [x] Escludere dalla discovery file `.docx`, `.pdf` e altri documenti singoli: non
  sono listing page ripetute e non devono arrivare alla generazione schema.
- [x] Separare il limite delle sorgenti dal limite di pagine, così un test
  rapido su EURAXESS non percorre automaticamente fino a 100 pagine.
- [x] Normalizzare i nomi paese EURAXESS in codici ISO alpha-2 e includere
  correttamente l'istituzione nello schema di estrazione.
- [x] Allineare il container Qdrant alla versione 1.18 del client Python.
- [x] Recuperare le scadenze storiche da `deadline_raw`, indicizzarle in formato
  RFC 3339 e mantenere coerenti database e vettori dopo un aggiornamento.
- [ ] Rieseguire uno scraping completo per aggiornare paese e istituzione nei
  record storici creati prima delle correzioni.
- [ ] Verificare o sostituire le due listing page Trinity che non espongono una
  struttura ripetuta valida e risultano ancora `failed`.
- [x] Aggiungere checkpoint durevoli per paese, università, listing page, pagina
  di scraping e batch di indicizzazione; `Resume` continua sulla stessa run.
- [x] Separare i limiti di università, discovery, schema, sorgenti scrape,
  pagine per sorgente e posizioni da indicizzare.
- [x] Rendere durevoli retry, ultimo errore e backoff esponenziale per ogni unità.
- [x] Rivalidare e riusare gli extraction schema dello stesso host/tipo, evitando
  anche di moltiplicare un intero ciclo tool-feedback già esaurito.
- [ ] Pubblicare progressivamente batch di fonti subito dopo un Quality Gate
  source-scoped; l'indice anticipato prima dell'arricchimento istituzionale è il
  primo passo, ma la coda durevole per fonte resta da implementare.
- [ ] Valutare concorrenza controllata per dominio solo dopo una run sequenziale
  ampia e una misurazione dei rate limit reali.

## Automazione e condivisione

- [x] Review automatica ibrida con tool call validata, soglie conservative,
  audit e override manuale.
- [x] Aggiungere un quality gate post-scrape con quarantena reversibile delle
  fonti e reason code verificabili, senza cancellare i dati grezzi.
- [x] Separare la review IA in triage rapida, recupero mirato dell'evidenza e
  seconda review evidence-first con astensione verso l'umano.
- [x] Verificare come sottostringhe reali le citazioni prodotte dai due tool e
  conservare uno storico append-only dei tentativi.
- [ ] Costruire un gold set stratificato da decisioni manuali e audit casuali,
  quindi misurare recall, precisione e curva rischio/copertura per fonte e lingua.
- [x] Aggiungere famiglie URL versionate e un backtest shadow leakage-safe per
  distinguere la probabilità che una pagina sia un'opportunità dalla sua apertura
  temporale; il Quality Gate registra metriche di famiglia senza produrre verdetti.
- [x] Salvare feedback positivi/negativi per dimensione con snapshot della famiglia
  URL e mostrare un prior conservativo sui fratelli solo dopo 12–20 esempi distinti.
- [ ] Rivalutare l'attivazione del prior URL solo dopo aver raccolto negativi
  evidence-grounded recenti e indipendenti prima di usarlo per saltare review;
  non propagare mai scadenza/apertura e non rifiutare una voce in base ai soli URL vicini.
- [ ] Solo dopo la calibrazione, valutare un router locale leggero (SetFit o
  embedding classifier) per evitare chiamate LLM sui casi facili; niente cloud judge.
- [x] Export portabile HTML, PDF, CSV e JSON della ricerca filtrata.
- [x] Macro durevoli refresh → ricerca → export verso una sottocartella sicura.
- [ ] Aggiungere una pianificazione temporale alle macro dopo aver misurato una
  prima esecuzione manuale end-to-end.
- [ ] Valutare upload Google Drive OAuth diretto; nel frattempo usare una
  cartella `exports/` sincronizzata o condivisa.
- [ ] Implementare backup operativi cifrati e verificati fuori da Git, con
  retention, checksum e prova periodica di ripristino.
- [ ] Dopo una pipeline e un indice completi, generare e auditare un bootstrap
  pubblico minimizzato (manifest + JSONL/Parquet compresso; vettori opzionali),
  verificando licenze/attribuzione per fonte e rimuovendo contatti e dati utente.

## Post-MVP — solo dopo un livello accettabile del bot attuale

Non avviare questi filoni prima di una decisione esplicita dell'utente. La
priorità corrente resta ottenere rapidamente un indice europeo utile,
progressivo e ripetibile.

### Copertura mondiale e onboarding geografico

- Estendere il catalogo alle istituzioni di tutto il mondo, modellando
  continente, macroregione e paese oltre al livello/tipo di istituzione.
- Permettere di selezionare i continenti già nello stadio `universities`, così
  discovery e fasi successive non devono attraversare sempre l'intero mondo.
- Progettare un primo avvio guidato: catalogo istituzioni obbligatorio e isolato,
  scelta delle aree geografiche, poi lancio della pipeline delle opportunità.
- Mostrare prima dell'avvio una stima incrementale di fonti, tempo, traffico e
  calcolo richiesti da ogni area selezionata.

### Modelli locali, cloud e modalità ibrida

- Astrarre il provider IA per supportare modelli locali e servizi online in
  setup, estrazione, review, enrichment e ricerca.
- Rendere la modalità locale completa e privata una scelta di prima classe;
  offrire il cloud a chi non ha hardware sufficiente.
- Valutare in seguito una modalità ibrida per continuità o accelerazione, con
  costi, dati inviati e fallback sempre espliciti. Questa è secondaria rispetto
  alle due modalità pure.

### UX/UI guidata

- Ridisegnare l'esperienza attorno a stato corrente, risultato già utilizzabile,
  prossimo passo consigliato e azioni sicure disponibili.
- Fornire stime aggiornate per ogni fase e simulazioni dell'impatto di nuove
  fonti, paesi o continenti sul tempo complessivo.
- Guidare setup, prima indicizzazione, refresh e recupero errori senza richiedere
  conoscenza interna della pipeline.

### Profilo privato e raccomandazioni personali

- Aggiungere un profilo opzionale con CV, portfolio, ORCID, link e questionario
  o colloquio assistito, mantenendo l'elaborazione locale per impostazione
  predefinita.
- Usare il profilo per ranking, alert e spiegazioni di compatibilità, lasciando
  sempre accessibile la ricerca non personalizzata.
- Studiare un formato trasparente per importare conoscenza fornita da altri
  assistenti tramite prompt/export controllato dall'utente.
- Vietare leakage e vendita a terzi: minimizzazione dei dati, consenso per ogni
  provider remoto, cancellazione/esportazione e chiara separazione tra profilo e
  catalogo pubblico.
