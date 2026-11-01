"""Controllo end-to-end dei certificati del sigillo (#75): import di un .p12, rifiuti, export
del sigillo personale e reimport, scelta del certificato in «Salva e blocca», elimina.

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_certificates.py

I .p12 di prova si creano al volo. Fuori da Windows DPAPI non c'è: `sealing` usa una
protezione locale con lo stesso comportamento.
"""
import datetime
import io
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"


def make_cert(name, issuer=None, usage=None, days=(-1, 30), ca=False, rsa=False, org=None):
    """(certificato, chiave) di cryptography: autofirmato o firmato da `issuer` (cert, chiave)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, rsa as rsa_keys
    from cryptography.x509.oid import NameOID

    key = rsa_keys.generate_private_key(65537, 2048) if rsa else ec.generate_private_key(ec.SECP256R1())
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, name)]
    if org:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, org))
    subject = x509.Name(attrs)
    now = datetime.datetime.now(datetime.timezone.utc)
    issuer_name, issuer_key = (subject, key) if issuer is None else (issuer[0].subject, issuer[1])
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer_name)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now + datetime.timedelta(days=days[0]))
               .not_valid_after(now + datetime.timedelta(days=days[1]))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if usage is not None:
        names = ["digital_signature", "content_commitment", "key_encipherment", "data_encipherment",
                 "key_agreement", "key_cert_sign", "crl_sign", "encipher_only", "decipher_only"]
        builder = builder.add_extension(x509.KeyUsage(**{n: n in usage for n in names}), critical=True)
    return builder.sign(issuer_key, hashes.SHA256()), key


def p12(cert, key, password, chain=()):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    return pkcs12.serialize_key_and_certificates(
        b"prova", key, cert, list(chain) or None, serialization.BestAvailableEncryption(password.encode()))


def main():
    from asn1crypto import x509 as ax509
    from cryptography.hazmat.primitives import serialization
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFileDialog, QInputDialog, QMessageBox

    import piedf.paths as paths
    data = S / "data_certificati"
    data2 = S / "data_certificati_2"
    work = S / "certificati"
    for d in (data, data2, work):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    paths.data_dir = lambda: data

    from piedf import certificates, sealing
    from piedf.certificates_dialog import (
        DEFAULT_KEY, DELETE_IMPORTED, DELETE_PERSONAL, EXPORT_TEXT, REPLACE_PERSONAL, CertificatesDialog,
        ExportPasswordDialog,
    )
    from piedf.locking import LockDialog, SealNameDialog
    from piedf.main_window import MainWindow
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def pump(sec=0.05):
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.01)

    folder = sealing.seal_dir(data)
    folder2 = sealing.seal_dir(data2)
    settings = QSettings(str(work / "settings.ini"), QSettings.Format.IniFormat)
    settings2 = QSettings(str(work / "settings2.ini"), QSettings.Format.IniFormat)

    # --- Dialog finti ---
    open_answers, save_answers, save_asked = [], [], []
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ((open_answers.pop(0) if open_answers else ""), ""))

    def fake_save(parent, title, proposed, *a, **k):
        save_asked.append(proposed)
        return (save_answers.pop(0) if save_answers else ""), ""
    QFileDialog.getSaveFileName = staticmethod(fake_save)
    password_answers, password_prompts = [], []

    def fake_text(parent, title, label, *a, **k):
        password_prompts.append(label)
        answer = password_answers.pop(0) if password_answers else None
        return (answer or ""), answer is not None
    QInputDialog.getText = staticmethod(fake_text)
    box_answers, boxes = [], []
    Button = QMessageBox.StandardButton

    def fake_exec(box):
        boxes.append((box.text(), sorted(b.text() for b in box.buttons())))
        answer = box_answers.pop(0) if box_answers else Button.Cancel
        if isinstance(answer, str):  # pulsante non standard: lo si preme
            next(b for b in box.buttons() if b.text() == answer).click()
            return 0
        return answer
    QMessageBox.exec = fake_exec
    errors, warnings = [], []
    QMessageBox.critical = staticmethod(lambda parent, title, text, *a: errors.append(text))
    QMessageBox.warning = staticmethod(lambda parent, title, text, *a: warnings.append(text))
    export_answers, export_shown = [], []  # risposte: (password, ripetuta) o None

    def fake_export_exec(dlg):
        texts = [label.text() for label in dlg.findChildren(type(dlg.hint))]
        answer = export_answers.pop(0) if export_answers else None
        if answer is None:
            export_shown.append((texts, None))
            return QDialog.DialogCode.Rejected
        dlg.first.setText(answer[0]); dlg.second.setText(answer[1])
        ok = dlg.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        export_shown.append((texts, ok))
        return QDialog.DialogCode.Accepted if ok else QDialog.DialogCode.Rejected
    ExportPasswordDialog.exec = fake_export_exec

    def files(directory):
        return sorted(p.name for p in Path(directory).glob("*")) if Path(directory).exists() else []

    def rows(dlg):
        return [[dlg.tree.topLevelItem(i).text(c) for c in range(4)] for i in range(dlg.tree.topLevelItemCount())]

    def select(dlg, fp):
        for i in range(dlg.tree.topLevelItemCount()):
            item = dlg.tree.topLevelItem(i)
            if item.data(0, 256) == fp:
                dlg.tree.setCurrentItem(item)

    def der(cert):
        return cert.public_bytes(serialization.Encoding.DER)

    # --- 1. Import di un .p12 con catena ---
    root = make_cert("CA di prova", usage={"key_cert_sign", "crl_sign"}, ca=True, days=(-1, 365))
    real = make_cert("Anna Firmataria", issuer=root, usage={"digital_signature", "content_commitment"}, rsa=True)
    real_fp = certificates.fingerprint(der(real[0]))
    real_p12 = work / "anna.pfx"
    real_p12.write_bytes(p12(*real, "segreta", chain=[root[0]]))

    dlg = CertificatesDialog(folder, settings)
    check("1. senza certificati: messaggio, nessuna riga", not rows(dlg) and dlg.empty.isVisibleTo(dlg)
          and not dlg.tree.isVisibleTo(dlg))
    check("1. senza selezione: Esporta, predefinito ed Elimina spenti",
          not dlg.export_button.isEnabled() and not dlg.default_button.isEnabled() and not dlg.delete_button.isEnabled())
    open_answers[:] = [str(real_p12)]
    password_answers[:] = ["segreta"]
    dlg.import_certificate()
    check("1. import: nessun errore", not warnings and not errors, str(warnings + errors))
    check("1. compare nell'elenco con nome, emittente, scadenza, predefinito",
          rows(dlg) == [["Anna Firmataria", "CA di prova",
                         real[0].not_valid_after_utc.astimezone().strftime("%d/%m/%Y"), "✓"]], str(rows(dlg)))
    check("1. file <impronta>.crt / .key in sigillo/", files(folder) == [real_fp + ".crt", real_fp + ".key"], str(files(folder)))
    check("1. importato senza predefinito scelto: diventa il predefinito",
          settings.value(DEFAULT_KEY, "", type=str) == real_fp)
    blob = (folder / (real_fp + ".key")).read_bytes()
    key_der = real[1].private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    check("1. la chiave nel file non è in chiaro",
          key_der not in blob and b"PRIVATE KEY" not in blob and real[1].private_numbers().d.to_bytes(256, "big").lstrip(b"\0") not in blob)
    check("1. la chiave si rilegge (DPAPI)", sealing.unprotect(blob) == key_der)
    check("1. né il .p12 né la password conservati",
          not any(p.suffix in (".p12", ".pfx") for p in folder.iterdir())
          and not any(b"segreta" in p.read_bytes() for p in folder.iterdir())
          and (not (work / "settings.ini").exists() or b"segreta" not in (work / "settings.ini").read_bytes()))
    crt = (folder / (real_fp + ".crt")).read_bytes()
    check("1. catena del file conservata nel .crt", crt.count(b"BEGIN CERTIFICATE") == 2)

    # --- 2. Rifiuti: nessun file scritto ---
    before = files(folder)
    warnings.clear(); password_prompts.clear()
    open_answers[:] = [str(real_p12)]
    password_answers[:] = ["sbagliata", None]
    dlg.import_certificate()
    check("2. password sbagliata: la richiede dicendolo, Annulla = niente",
          len(password_prompts) == 2 and password_prompts[1].startswith("Password errata") and not warnings
          and files(folder) == before, str(password_prompts))
    try:
        certificates.read_p12(folder, real_p12.read_bytes(), "sbagliata")
        msg = ""
    except certificates.CertificateError as e:
        msg = str(e)
    check("2. password sbagliata: messaggio", msg == certificates.BAD_PASSWORD, msg)

    def refused(name, data_, password, expected):
        path = work / name
        path.write_bytes(data_)
        warnings.clear()
        open_answers[:] = [str(path)]
        password_answers[:] = [password]
        dlg.import_certificate()
        check(f"2. {name}: messaggio e nessun file", warnings == [expected] and files(folder) == before,
              str(warnings))

    from cryptography.hazmat.primitives.serialization import pkcs12
    only_cert = make_cert("Senza chiave", usage={"digital_signature"})
    refused("senza_chiave.p12", pkcs12.serialize_key_and_certificates(
        b"x", None, None, [only_cert[0]], serialization.BestAvailableEncryption(b"pw")), "pw", certificates.UNUSABLE)
    refused("solo_cifratura.p12", p12(*make_cert("Cifratura", usage={"key_encipherment"}, rsa=True), "pw"), "pw",
            certificates.UNUSABLE)
    expired = make_cert("Scaduto", usage={"digital_signature"}, days=(-400, -10))
    refused("scaduto.p12", p12(*expired, "pw"), "pw",
            certificates.EXPIRED.format(date=expired[0].not_valid_after_utc.astimezone().strftime("%d/%m/%Y")))
    check("2. testo dello scaduto: «Il certificato è scaduto il …»",
          certificates.EXPIRED.startswith("Il certificato è scaduto il "))
    refused("doppione.pfx", real_p12.read_bytes(), "segreta", certificates.DUPLICATE)
    no_usage = make_cert("Senza keyUsage", usage=None)
    check("2. senza estensione keyUsage: si può firmare",
          certificates.read_p12(folder, p12(*no_usage, "pw"), "pw").name == "Senza keyUsage")

    # --- 3. Export del sigillo personale e reimport in un'altra cartella dati ---
    sealing.create_identity(folder, "Mario Rossi")
    personal_fp = certificates.personal_fingerprint(folder)
    dlg.refresh()
    check("3. elenco: prima il sigillo personale",
          [r[0] for r in rows(dlg)] == ["Mario Rossi (sigillo personale)", "Anna Firmataria"]
          and rows(dlg)[0][1] == "Autofirmato", str(rows(dlg)))
    select(dlg, real_fp)
    check("3. Esporta solo per il sigillo personale", not dlg.export_button.isEnabled())
    select(dlg, personal_fp)
    check("3. Esporta attivo sul sigillo personale", dlg.export_button.isEnabled())
    export_answers[:] = [("corta", "corta")]
    dlg.export_selected()
    check("3. password di meno di 8 caratteri: non si esporta",
          export_shown and export_shown[-1][1] is False and not save_asked)
    check("3. testo dell'export", EXPORT_TEXT in export_shown[-1][0], str(export_shown[-1][0]))
    export_answers[:] = [("password1", "password2")]
    dlg.export_selected()
    check("3. password diverse: non si esporta", export_shown[-1][1] is False and not save_asked)
    exported = work / "esportato.p12"
    export_answers[:] = [("password123", "password123")]
    save_answers[:] = [str(exported)]
    dlg.export_selected()
    check("3. proposta <nome>_sigillo.p12", save_asked and Path(save_asked[-1]).name == "Mario Rossi_sigillo.p12",
          str(save_asked))
    loaded = pkcs12.load_pkcs12(exported.read_bytes(), b"password123") if exported.exists() else None
    check("3. file esportato, cifrato con la password",
          loaded is not None and loaded.key is not None and certificates.fingerprint(der(loaded.cert.certificate)) == personal_fp)

    dlg2 = CertificatesDialog(folder2, settings2)
    open_answers[:] = [str(exported)]
    password_answers[:] = ["password123"]
    boxes.clear()
    dlg2.import_certificate()
    check("3. reimport in un'altra cartella dati: sigillo personale con la stessa impronta",
          certificates.personal_fingerprint(folder2) == personal_fp and sealing.identity_state(folder2) == "ok"
          and files(folder2) == [sealing.CERT_FILE, sealing.KEY_FILE] and not boxes, str(files(folder2)))
    check("3. reimportato: nell'elenco come sigillo personale",
          [r[0] for r in rows(dlg2)] == ["Mario Rossi (sigillo personale)"], str(rows(dlg2)))
    check("3. sigillo personale: il predefinito non si scrive (resta il sigillo)", not settings2.value(DEFAULT_KEY, ""))
    locked2 = work / "dopo_reimport.pdf"
    sealing.seal(str(PDFS / "semplice.pdf"), None, str(locked2), folder2)

    def signer(path, password=None):
        with open(path, "rb") as f:
            reader = PdfFileReader(io.BytesIO(f.read()))
        sig = reader.embedded_signatures[0]
        return sig, sig.signer_cert

    sig, cert = signer(locked2)
    check("3. PDF bloccato dopo il reimport: stesso firmatario e stessa impronta",
          certificates.fingerprint(cert.dump()) == personal_fp and cert.subject.native["common_name"] == "Mario Rossi")
    status = validate_pdf_signature(sig, ValidationContext(trust_roots=[cert]))
    check("3. sigillo valido", status.intact and status.valid and status.trusted, status.summary())

    # Un altro sigillo personale c'è già: si chiede prima di sostituirlo.
    other = make_cert("Luca Bianchi", usage={"digital_signature"}, org=sealing.ORGANIZATION, days=(0, 3650))
    other_p12 = work / "luca_sigillo.p12"
    other_p12.write_bytes(p12(*other, "password123"))
    boxes.clear()
    open_answers[:] = [str(other_p12)]
    password_answers[:] = ["password123"]
    box_answers[:] = [Button.Cancel]
    dlg2.import_certificate()
    check("3. un altro sigillo personale: conferma, Annulla = invariato",
          boxes and boxes[0][0] == REPLACE_PERSONAL and certificates.personal_fingerprint(folder2) == personal_fp, str(boxes))
    open_answers[:] = [str(other_p12)]
    password_answers[:] = ["password123"]
    box_answers[:] = [Button.Ok]
    dlg2.import_certificate()
    check("3. Sostituisci: il sigillo personale è quello importato",
          certificates.personal_fingerprint(folder2) == certificates.fingerprint(der(other[0]))
          and sealing.identity_state(folder2) == "ok")

    # Sigillo copiato da un altro PC: «Importa il sigillo…» dal dialog di Salva e blocca.
    w = MainWindow(); w.resize(900, 700); w.show()
    pump(0.2)
    tools = [m for m in w.menuBar().actions() if m.text() == "&Strumenti"][0].menu()
    check("menu Strumenti › Certificati…", "&Certificati…" in [a.text() for a in tools.actions()])
    paths.data_dir = lambda: data2
    foreign_key = folder2 / sealing.KEY_FILE
    blob = bytearray(foreign_key.read_bytes()); blob[len(blob) // 2] ^= 0xFF
    foreign_key.write_bytes(bytes(blob))
    check("3. chiave alterata: sigillo «di un altro PC»", sealing.identity_state(folder2) == "foreign")
    src = work / "semplice.pdf"
    shutil.copy(PDFS / "semplice.pdf", src)
    w.open_file(str(src))
    pump(0.3)

    lock_answers, lock_shown = [], []  # risposte: impronta da scegliere (None = lascia)

    def fake_lock_exec(d):
        lock_shown.append(([d.cert_box.itemText(i) for i in range(d.cert_box.count())],
                           d.cert_box.currentData(), d.cert_box.isVisibleTo(d)))
        answer = lock_answers.pop(0) if lock_answers else None
        if answer is not None:
            d.cert_box.setCurrentIndex(d.cert_box.findData(answer))
        return QDialog.DialogCode.Accepted
    LockDialog.exec = fake_lock_exec
    names_shown = []

    def fake_name_exec(d):
        names_shown.append(d.edit.text())
        return QDialog.DialogCode.Rejected
    SealNameDialog.exec = fake_name_exec

    def lock(dest, choose=None, boxes_=()):
        save_answers[:] = [str(dest)]
        lock_answers[:] = [choose]
        box_answers[:] = list(boxes_)
        started = w.save_and_lock()
        t = time.perf_counter()
        while w._seal_job is not None and time.perf_counter() - t < 60:
            app.processEvents(); time.sleep(0.01)
        pump(0.1)
        return started

    boxes.clear(); errors.clear()
    open_answers[:] = [str(exported)]
    password_answers[:] = ["password123"]
    from_import = work / "importato_e_bloccato.pdf"
    lock(from_import, boxes_=["Importa il sigillo…", Button.Ok])
    check("3. «…creato su un altro PC» offre anche [Importa il sigillo…]",
          boxes and boxes[0][0] == sealing.FOREIGN and boxes[0][1] == ["Annulla", "Crea nuovo", "Importa il sigillo…"],
          str(boxes[:1]))
    check("3. importato il sigillo esportato, al posto di quello non decifrabile (con conferma)",
          len(boxes) == 2 and boxes[1][0] == REPLACE_PERSONAL and certificates.personal_fingerprint(folder2) == personal_fp
          and sealing.identity_state(folder2) == "ok", str(boxes))
    check("3. poi il blocco continua senza chiedere il nome", from_import.exists() and not names_shown and not errors,
          str(errors))
    if from_import.exists():
        _, cert = signer(from_import)
        check("3. bloccato con il sigillo importato", certificates.fingerprint(cert.dump()) == personal_fp)

    # --- 4. Due certificati: Salva e blocca usa quello scelto nella tendina ---
    paths.data_dir = lambda: data
    w._settings.setValue(DEFAULT_KEY, real_fp)
    w.open_file(str(src))
    pump(0.3)
    lock_shown.clear(); errors.clear()
    chosen_personal = work / "scelto_personale.pdf"
    lock(chosen_personal, choose=personal_fp)
    check("4. tendina «Firma con: …» visibile con i due certificati, sul predefinito",
          lock_shown and lock_shown[0] == (["Mario Rossi (sigillo personale)", "Anna Firmataria (CA di prova)"], real_fp, True),
          str(lock_shown))
    if chosen_personal.exists():
        sig, cert = signer(chosen_personal)
        check("4. scelto il sigillo personale: CN nella firma", cert.subject.native["common_name"] == "Mario Rossi")
    else:
        check("4. scelto il sigillo personale: bloccato", False, str(errors))
    w.open_file(str(src))
    pump(0.3)
    chosen_real = work / "scelto_importato.pdf"
    lock(chosen_real, choose=real_fp)
    if chosen_real.exists():
        sig, cert = signer(chosen_real)
        status = validate_pdf_signature(sig, ValidationContext(trust_roots=[
            ax509.Certificate.load(der(root[0]))]))
        check("4. scelto il certificato importato: CN nella firma",
              cert.subject.native["common_name"] == "Anna Firmataria", cert.subject.human_friendly)
        check("4. firma valida e fidata con la CA della catena", status.intact and status.valid and status.trusted,
              status.summary())
        check("4. la catena è nella firma", any(c.subject.native.get("common_name") == "CA di prova"
                                                for c in sig.other_embedded_certs), "")
    else:
        check("4. scelto il certificato importato: bloccato", False, str(errors))
    check("4. la scelta nella tendina non cambia il predefinito", w._settings.value(DEFAULT_KEY, "", type=str) == real_fp)

    # Un certificato importato non decifrabile qui: messaggio, niente blocco.
    real_key = (folder / (real_fp + ".key")).read_bytes()
    (folder / (real_fp + ".key")).write_bytes(real_key[:-1] + bytes([real_key[-1] ^ 1]))
    w.open_file(str(src))
    pump(0.3)
    warnings.clear()
    not_here = work / "non_qui.pdf"
    started = lock(not_here, choose=real_fp)
    check("4. importato su un altro PC: messaggio e niente blocco",
          not started and warnings == [certificates.IMPORTED_FOREIGN.format(name="Anna Firmataria")] and not not_here.exists(),
          str(warnings))
    dlg.refresh()
    check("4. nell'elenco segnato come non utilizzabile",
          rows(dlg)[1][0] == "Anna Firmataria – non utilizzabile su questo PC", str(rows(dlg)))
    (folder / (real_fp + ".key")).write_bytes(real_key)

    # Un solo certificato: niente tendina.
    lock_shown.clear()
    w.open_file(str(src))
    pump(0.3)
    paths.data_dir = lambda: data2
    lock(work / "uno_solo.pdf")
    check("4. con un solo certificato niente tendina", lock_shown and lock_shown[0][2] is False, str(lock_shown))
    paths.data_dir = lambda: data

    # --- 5. Predefinito ed Elimina ---
    dlg.refresh()
    select(dlg, personal_fp)
    dlg.make_default()
    check("5. «Usa come predefinito»: impostazione e segno nell'elenco",
          settings.value(DEFAULT_KEY, "", type=str) == personal_fp and rows(dlg)[0][3] == "✓" and rows(dlg)[1][3] == "")
    check("5. sul predefinito il pulsante è spento", not dlg.default_button.isEnabled())
    select(dlg, real_fp)
    boxes.clear()
    box_answers[:] = [Button.Cancel]
    dlg.delete_selected()
    check("5. Elimina chiede conferma; Annulla = niente",
          boxes and boxes[0][0] == DELETE_IMPORTED.format(name="Anna Firmataria") and (folder / (real_fp + ".key")).exists())
    box_answers[:] = [Button.Ok]
    dlg.delete_selected()
    check("5. Elimina rimuove .crt e .key da sigillo/",
          not (folder / (real_fp + ".crt")).exists() and not (folder / (real_fp + ".key")).exists()
          and [r[0] for r in rows(dlg)] == ["Mario Rossi (sigillo personale)"], str(files(folder)))
    select(dlg, personal_fp)
    boxes.clear()
    box_answers[:] = [Button.Ok]
    dlg.delete_selected()
    check("5. sigillo personale: testo «I PDF già bloccati restano validi…»", boxes and boxes[0][0] == DELETE_PERSONAL)
    check("5. Elimina del sigillo personale: nessun file in sigillo/, predefinito tolto",
          files(folder) == [] and not settings.value(DEFAULT_KEY, "") and not rows(dlg), str(files(folder)))

    check("nessun errore inatteso", not errors, str(errors))
    w.close()
    pump(0.2)
    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
