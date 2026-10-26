# Firma del codice (SignPath)

Senza firma, Windows SmartScreen mostra «Autore sconosciuto» all'avvio di `PieDF.exe` scaricato. PieDF è open source (AGPL-3.0), quindi può usare la firma gratuita di [SignPath Foundation](https://signpath.org/) per i progetti open source. Il certificato è intestato a "SignPath Foundation", non a una persona. SmartScreen smette di avvisare man mano che l'app accumula download: non succede dal primo giorno.

Il workflow "Release" (`.github/workflows/release.yml`) è già pronto. Finché non c'è il secret `SIGNPATH_API_TOKEN` le release escono non firmate, come prima. Quando c'è, a ogni tag:

1. costruisce l'exe e lo prova (`check_exe.py`);
2. carica `PieDF.exe` come artifact e chiede la firma a SignPath, aspettando l'approvazione (massimo 30 minuti);
3. rimette l'exe firmato nella cartella, controlla che la firma sia `Valid`, rifà lo zip e riprova l'exe;
4. pubblica la release.

Si firma solo `PieDF.exe`: è anche il processo di render (lo stesso exe con `--multiprocessing-fork`). Le DLL di Python e Qt arrivano già firmate dai rispettivi autori.

## Da fare una volta sola

1. **Rendere pubblico il repo** (Settings → General → Danger Zone → Change visibility). SignPath Foundation firma solo progetti open source con il codice pubblico e la build su un sistema di CI pubblico come GitHub Actions.
2. **Fare domanda** a SignPath Foundation: modulo su https://signpath.org/apply. Servono il link al repo, la licenza (AGPL-3.0) e la descrizione del progetto. La domanda viene valutata a mano.
3. Una volta accettati, nel portale SignPath (https://app.signpath.io):
   - progetto con slug `PieDF`, con "Trusted build system" = GitHub.com collegato al repo `dellaluca/PieDF`;
   - **artifact configuration** (l'artifact caricato dal workflow è uno zip che contiene solo `PieDF.exe`):
     ```xml
     <?xml version="1.0" encoding="utf-8"?>
     <artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
       <parameters>
         <parameter name="version" required="true" />
       </parameters>
       <zip-file>
         <pe-file path="PieDF.exe" product-name="PieDF" product-version="${version}"
                  file-version="${version}" original-filename="PieDF.exe">
           <authenticode-sign />
         </pe-file>
       </zip-file>
     </artifact-configuration>
     ```
     Le restrizioni sui metadati sono obbligatorie per SignPath Foundation: nome del prodotto e versione dell'exe (da `piedf.spec`, cioè `piedf.__version__`) devono essere quelli attesi. `release.yml` passa `version` alla richiesta di firma.
   - signing policy `release-signing` (con approvazione manuale: ogni release va approvata nel portale o dalla mail di SignPath);
   - un utente CI con il suo API token.
4. **Su GitHub** (Settings → Secrets and variables → Actions):
   - secret `SIGNPATH_API_TOKEN` = token dell'utente CI;
   - variabile `SIGNPATH_ORGANIZATION_ID` = ID dell'organizzazione SignPath;
   - solo se diversi dai predefiniti: variabili `SIGNPATH_PROJECT_SLUG` (predefinito `PieDF`) e `SIGNPATH_SIGNING_POLICY` (predefinito `release-signing`).
5. Pubblicare una versione nuova ([sviluppo.md](sviluppo.md#pubblicare-una-versione)) e approvare la richiesta di firma su SignPath.

## Regole di SignPath Foundation da rispettare

- La sezione "Code signing policy" del README (crediti, ruoli, privacy) deve restare aggiornata, con questo titolo: SignPath la cerca nella home page e nella pagina di download. Anche le note di ogni Release contengono il link (le scrive `release.yml`).
- Si firma solo quello che il workflow costruisce dal codice del repo: niente binari aggiunti a mano.
- Autenticazione a due fattori attiva sull'account GitHub di chi può unire nel repo e sull'account SignPath.

## Verificare una release firmata

Tasto destro su `PieDF.exe` → Proprietà → scheda **Firme digitali**: firmatario "SignPath Foundation". Oppure in PowerShell:

```powershell
Get-AuthenticodeSignature .\PieDF\PieDF.exe | Format-List Status, SignerCertificate
```
