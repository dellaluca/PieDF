# Automazione dei ticket con Claude

Quando si apre una issue (o si scrive `@claude` in un commento) Claude la prende in carico da solo su GitHub Actions e apre una PR da rivedere. Il PC può restare spento. È lo stesso sistema del ricettario Colombo.

## Come funziona

```
issue aperta ───────────────┐
commento con @claude ───────┼──▶ ORCHESTRATORE (Opus) ──▶ chiedi  → commento con domande + needs-info
risposta a un needs-info ───┘    legge ticket e CLAUDE.md  ignora  → claude:skip
                                                            esegui  → sceglie Haiku / Sonnet / Opus
                                                                         │
                                                                         ▼
                                                          ESECUTORE ──▶ branch feat/N-… + PR "Closes #N"
                                                                         │
                                                                         ▼
                                                          BUILD (Windows) ──▶ tests/check_*.py nella PR
```

| File | Ruolo |
|---|---|
| `.github/workflows/claude-orchestratore.yml` | Valuta il ticket e decide |
| `.github/workflows/claude-esecutore.yml` | Esegue il ticket con il modello scelto (Linux, Qt offscreen) |
| `.github/workflows/build.yml` | Su ogni branch diverso da `main`: genera i PDF di prova ed esegue tutti i `tests/check_*.py` su **Windows**, poi costruisce l'exe, lo prova (`check_exe.py`) e allega lo zip alla run |
| `.github/workflows/release.yml` | Su un tag `v*`: costruisce l'exe e pubblica la GitHub Release con lo zip |
| `.github/claude/orchestratore.md` | Istruzioni dell'orchestratore |
| `.github/claude/esecutore.md` | Istruzioni dell'esecutore |
| `.github/ISSUE_TEMPLATE/task.yml` | Modulo per aprire i ticket |

Le PR non si uniscono mai da sole: le rivedi e le unisci tu.

**Il Build è obbligatorio per unire** (protezione di `main`, vale anche per gli admin): GitHub non permette il merge finché lo stato **Build Windows** non è verde sull'ultimo commit della PR e il branch non è aggiornato con `main` (pulsante *Update branch*: il merge commit fa ripartire il Build). Così su `main` arriva solo codice provato su Windows insieme a quello già unito. I push dei worker usano il `GITHUB_TOKEN`, che non avvia workflow: l'esecutore avvia il Build sul branch della PR a fine esecuzione (passo «Avvia il build della PR»), con `workflow_dispatch`. I check run di quelle esecuzioni **non contano** per la protezione, per questo il Build pubblica alla fine uno stato del commit «Build Windows» (job `esito`): è quello il controllo obbligatorio. Se una PR resta senza Build (per esempio dopo un'esecuzione annullata), avvialo a mano: `gh workflow run build.yml --ref <branch>`.

Se il **Build su Windows** fallisce su una PR dei worker, il job "ritenta" commenta la PR con le righe `FAIL` e rilancia l'esecutore su quella PR, al massimo **2 volte** per PR. Dopo due tentativi si ferma: serve un'occhiata a mano. I worker lanciano tutti i controlli con `python tests/run_all.py`.

Se la sessione finisce lasciando commit o modifiche non pubblicati sul runner, il workflow li pubblica lui e apre una PR in bozza "Bozza da salvataggio automatico per #N", da controllare. La trascrizione completa di ogni sessione è tra gli artifact dell'esecuzione (14 giorni): serve a capire cosa è andato storto, perché nel log l'output di Claude è nascosto.

### Etichette

| Etichetta | Significato |
|---|---|
| `needs-info` | Claude ha fatto delle domande nel ticket. Quando rispondi riparte da solo. |
| `claude:in-corso` | L'esecutore sta lavorando |
| `claude:haiku` / `claude:sonnet` / `claude:opus` | Modello scelto dall'orchestratore |
| `claude:fatto` | PR aperta (anche in bozza) |
| `claude:errore` | Qualcosa è andato storto, o la sessione è finita senza PR: nel ticket c'è il link al log |
| `claude:skip` | Claude non tocca questo ticket. Mettila tu per escludere un ticket. |

### Comandi nei commenti

- `@claude` in un commento di una issue fa ripartire l'orchestratore: per avviare un ticket aperto prima dell'automazione, dopo un errore, o quando un ticket da cui dipende è stato unito.
- `@claude` in un commento di una PR: Claude applica le modifiche richieste sullo stesso branch.

Solo chi ha accesso in scrittura al repo può far partire Claude.

### Dipendenze tra ticket

Se un ticket dice "Dipende da #N" e #N non è ancora chiuso, l'orchestratore non parte: lo scrive nel ticket. Quando unisci la PR di #N, scrivi `@claude` nel ticket che aspettava.

### Cosa i worker non possono fare

- Modificare `.github/workflows/` (il token delle Actions non ha quel permesso): quelle parti si fanno in sessione con Claude sul PC.
- Verificare l'aspetto reale su Windows o l'uso con mouse e penna: lo scrivono nella PR alla voce "Non verificato".

## Configurazione (una volta sola)

### 1. Unisci la PR di questa automazione

I workflow partono solo quando sono su `main`.

### 2. Token di Claude (`CLAUDE_CODE_OAUTH_TOKEN`)

Usa il tuo abbonamento Claude, quindi niente costi API a consumo.

1. In un terminale lancia `claude setup-token`, accedi nel browser e copia il token `sk-ant-oat01-…` (dura un anno). Puoi anche riusare quello del ricettario, se l'hai conservato.
2. Apri https://github.com/dellaluca/PieDF/settings/secrets/actions/new
3. Name: `CLAUDE_CODE_OAUTH_TOKEN`, Secret: il token. **Add secret**.

### 3. Permessi delle Actions

Apri https://github.com/dellaluca/PieDF/settings/actions, sezione **Workflow permissions**:

- seleziona **Read and write permissions**;
- spunta **Allow GitHub Actions to create and approve pull requests**;
- **Save**.

### 4. Prova

Scrivi `@claude` in un commento di un ticket pronto (vedi l'ordine qui sotto). In **Actions** partono "Claude · orchestratore" e poi "Claude · esecutore"; in qualche minuto trovi nel ticket il piano e poi il link alla PR, con il controllo "Build" che gira su Windows.

## Ordine dei ticket

Prima versione fatta: #4 → #5 → #8 → #13 (build dell'exe nella CI e release, fatto in sessione locale). Dopo: #6 pannello laterale, #7 testo e data, #14 ricerca, #15 selezione del testo.

## Costi e limiti

- **Turni per esecuzione:** Haiku 60, Sonnet 140, Opus 200 (in `claude-esecutore.yml`).
- **Minuti di Actions:** il repo è pubblico, quindi i minuti dei runner standard (anche Windows) sono gratuiti. Il Build dura circa 20 minuti per ogni push su un branch.
- **Uso di Claude:** ogni ticket consuma una valutazione di Opus più un'esecuzione, sui limiti del tuo abbonamento.
- **Scadenze:** quando `CLAUDE_CODE_OAUTH_TOKEN` scade, rigeneralo e aggiorna il secret.
