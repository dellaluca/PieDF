## Cosa cambia
- **Strumenti › Certificati…** (`piedf/certificates_dialog.py`): elenco dei certificati per il sigillo (sigillo personale + importati) con nome, emittente, scadenza e «predefinito». Pulsanti Importa…, Esporta… (solo sigillo personale), Usa come predefinito, Elimina (con conferma, testo del ticket per il sigillo personale).
- **`piedf/certificates.py`** (senza Qt): import da `.p12`/`.pfx` con `cryptography…pkcs12`, controlli (password, chiave privata che corrisponde al certificato, keyUsage per firmare, scadenza, doppione per impronta SHA-256), file `sigillo/<impronta>.crt` (con la catena del file) / `.key` (PKCS#8 cifrato con `sealing.protect`, cioè DPAPI come il sigillo personale), export del sigillo personale in `.p12` con password, predefinito (`lock/certificate`), elimina, `load_signer` per pyHanko.
- Un `.p12` esportato da PieDF (autofirmato, O = «PieDF – sigillo personale») importato diventa il **sigillo personale** di questo PC, con conferma se ce n'è già un altro.
- Il dialog «Il sigillo personale è stato creato su un altro PC…» offre anche **[Importa il sigillo…]**: dopo l'import il blocco continua senza chiedere il nome.
- **Salva e blocca**: usa il predefinito; con più certificati il dialog di conferma ha la tendina **«Firma con: …»**. L'impronta va a `SealJob` → `sealing.seal(cert_id=…)`. La catena del `.p12` entra nella firma.
- `CLAUDE.md` (decisione «Sigillo», struttura) e README («Cosa fa», Privacy) aggiornati.

## Come verificarlo
`python tests/run_all.py` → 19/19 controlli superati (il nuovo `check_certificates.py` 55/55). In `check_signatures.py` ho aggiornato l'elenco atteso del menu Strumenti (c'è «Certificati…»).

Punti di «Fatto quando» in `check_certificates.py`:
1. Import di un `.pfx` creato al volo (RSA, firmato da una CA di prova) → nell'elenco; nel `.key` né il DER né l'esponente privato né `PRIVATE KEY`; né il `.p12` né la password conservati.
2. Password sbagliata (si richiede dicendolo; Annulla = niente), file senza chiave, keyUsage solo cifratura, scaduto, doppione → i messaggi del ticket, nessun file scritto.
3. Export (password < 8 caratteri o diverse: non si esporta; proposta `Mario Rossi_sigillo.p12`) e reimport in un'altra cartella dati → stessa impronta; PDF bloccato dopo il reimport con stesso firmatario e impronta, valido. Anche: sostituzione con conferma e [Importa il sigillo…] dal dialog «altro PC».
4. Due certificati: tendina sul predefinito; scelto il personale → CN «Mario Rossi», scelto l'importato → CN «Anna Firmataria», valido e fidato con la CA della catena. La scelta non cambia il predefinito. Un importato non decifrabile qui → messaggio e niente blocco. Un solo certificato → niente tendina.
5. Elimina rimuove `.crt`/`.key` (e `certificato.pem`/`chiave.dpapi` per il sigillo personale) e toglie il predefinito.

## Scelte fatte
- `cryptography` era già una dipendenza (#70): niente di nuovo in `requirements.txt`. Si carica solo per l'elenco (anche per la tendina, se ci sono importati), l'import e l'export: all'avvio niente (`check_lock` punto 0 passa).
- Password sbagliata: cryptography non distingue tra password errata e file non valido, quindi un messaggio unico («Password errata, oppure il file non è un certificato .p12/.pfx.») e la password si richiede.
- Senza estensione keyUsage il certificato si accetta (nessun limite d'uso). ExtendedKeyUsage non si controlla.
- Il primo certificato importato (non un sigillo personale) diventa predefinito se l'utente non ne ha mai scelto uno: chi importa un certificato vero di solito vuole usarlo. Con la tendina si cambia volta per volta senza toccare il predefinito.
- Doppione = stessa impronta **con la chiave decifrabile qui**: un certificato copiato da un altro PC (per esempio con l'import dei dati della versione precedente, che copia tutta `sigillo/`) si può reimportare.
- Senza sigillo personale e con certificati importati, Salva e blocca non crea un sigillo personale (usa l'importato). Per crearne uno bisogna eliminare gli importati.
- Un certificato importato scaduto resta nell'elenco con la sua scadenza; bloccare con quello dà «Il certificato «…» è scaduto il …».
- #71, #73 e #74 non sono ancora uniti: come previsto dal piano, il punto 3 controlla impronta e firmatario. «Da te» nella barra (#71) e nella scheda Firme (#73) e la tendina di Firma digitalmente (#74) restano a quei ticket; `CLAUDE.md` lo annota.

## Non verificato
- DPAPI vera e testo dei dialog su Windows (qui Linux: `sealing` usa la protezione locale; il build della PR esegue i controlli su Windows).
- Import di un `.pfx` esportato da Windows o da un'autorità vera (cifratura vecchia RC2/3DES): cryptography carica il provider legacy di OpenSSL, ma non l'ho provato con un file reale.
- Se il `.p12` esportato (AES-256, PBKDF2) si importa anche nell'archivio certificati di Windows o in altri programmi: serve solo per reimportarlo in PieDF.
- Aspetto del dialog Certificati.

Closes #75

🤖 Generated with [Claude Code](https://claude.com/claude-code)
