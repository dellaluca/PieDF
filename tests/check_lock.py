"""Controllo end-to-end di «Salva e blocca» (#70): sigillo, marca temporale, certificato, rifiuti.

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_lock.py

La marca temporale si prova con un servizio finto su 127.0.0.1 (DummyTimeStamper di pyHanko),
mai con quello vero. Fuori da Windows DPAPI non c'è: `sealing` usa una protezione locale con lo
stesso comportamento e il controllo specifico di DPAPI si salta.
"""
import ast
import asyncio
import datetime
import glob
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"
MAX_GAP = 0.15  # s, criterio di #47: la GUI non resta mai ferma di più


def make_ca(name, key_usage, eku=None, issuer=None, ca=False, rsa=False):
    """Certificato di prova (asn1crypto) e chiave: autofirmato o firmato da `issuer` (cert, chiave)."""
    from asn1crypto import keys, x509 as ax509
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa as rsa_keys
    from cryptography.x509.oid import NameOID

    key = rsa_keys.generate_private_key(65537, 2048) if rsa else ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    issuer_name, issuer_key = (subject, key) if issuer is None else (issuer[2], issuer[3])
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer_name)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
               .add_extension(key_usage, critical=True))
    if eku:
        builder = builder.add_extension(x509.ExtendedKeyUsage(eku), critical=True)
    cert = builder.sign(issuer_key, hashes.SHA256())
    a_cert = ax509.Certificate.load(cert.public_bytes(serialization.Encoding.DER))
    a_key = keys.PrivateKeyInfo.load(key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return a_cert, a_key, subject, key


def key_usage(**on):
    from cryptography import x509
    names = ["digital_signature", "content_commitment", "key_encipherment", "data_encipherment",
             "key_agreement", "key_cert_sign", "crl_sign", "encipher_only", "decipher_only"]
    return x509.KeyUsage(**{n: on.get(n, False) for n in names})


class FakeTsa:
    """Servizio RFC 3161 finto su 127.0.0.1, risposte firmate da una TSA di prova."""

    def __init__(self) -> None:
        from cryptography.x509.oid import ExtendedKeyUsageOID
        from pyhanko.sign.timestamps.dummy_client import DummyTimeStamper
        from pyhanko_certvalidator.registry import SimpleCertificateStore

        self.root = make_ca("TSA root di prova", key_usage(key_cert_sign=True, crl_sign=True), ca=True)
        tsa_cert, tsa_key, _, _ = make_ca("TSA di prova", key_usage(digital_signature=True),
                                          [ExtendedKeyUsageOID.TIME_STAMPING], issuer=self.root, rsa=True)  # RSA: lo vuole DummyTimeStamper
        stamper = DummyTimeStamper(tsa_cert, tsa_key, SimpleCertificateStore.from_certs([self.root[0]]))
        self.requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                from asn1crypto import tsp
                owner.requests += 1
                body = self.rfile.read(int(self.headers["Content-Length"]))
                ok = self.headers.get("Content-Type") == "application/timestamp-query"
                resp = asyncio.run(stamper.async_request_tsa_response(tsp.TimeStampReq.load(body))).dump()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "application/timestamp-reply")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/tsa"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def closed_port_url() -> str:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nessuno ascolta più: connessione rifiutata subito
    return f"http://127.0.0.1:{port}/tsa"


def main():
    import pymupdf
    from PySide6.QtCore import QRectF, Qt, QTimer
    from PySide6.QtGui import QAction, QColor, QImage, QPainter, QPen
    from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QInputDialog, QMessageBox

    import piedf.paths as paths
    data = S / "data_blocco"
    work = S / "blocco"
    for d in (data, work):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    paths.data_dir = lambda: data

    import piedf.main_window as main_window
    from piedf import sealing
    from piedf.document import Ink, TextBox
    from piedf.locking import LockDialog, SealNameDialog
    from piedf.main_window import MainWindow
    from piedf.placements import Placement
    from piedf.signatures import Kind
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.fields import MDPPerm
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko.sign.validation.status import SignatureCoverageLevel
    from pyhanko_certvalidator import ValidationContext

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def skip(name, why):
        print("SKIP", name, why, flush=True)

    def pump(sec=0.05):
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.01)

    folder = sealing.seal_dir(data)

    def own_cert():
        from asn1crypto import pem, x509
        return x509.Certificate.load(pem.unarmor((folder / sealing.CERT_FILE).read_bytes())[2])

    def validate(path, password=None, ts_root=None):
        """(stato, firma) della prima firma, con il certificato personale come radice di fiducia."""
        with open(path, "rb") as f:
            reader = PdfFileReader(io.BytesIO(f.read()))
        if reader.security_handler is not None:
            reader.decrypt(password or "")
        sigs = reader.embedded_signatures
        if len(sigs) != 1:
            return None, sigs
        ts_vc = ValidationContext(trust_roots=[ts_root]) if ts_root is not None else None
        status = validate_pdf_signature(sigs[0], ValidationContext(trust_roots=[own_cert()]), ts_vc)
        return status, sigs[0]

    def sealed_ok(status, sig):
        return (status is not None and status.intact and status.valid and status.trusted
                and status.coverage == SignatureCoverageLevel.ENTIRE_FILE
                and sig.docmdp_level == MDPPerm.NO_CHANGES and status.docmdp_ok)

    # --- Dialog finti ---
    save_answers, save_asked = [], []

    def fake_save(parent, title, proposed, *a, **k):
        save_asked.append(proposed)
        return (save_answers.pop(0) if save_answers else ""), ""
    QFileDialog.getSaveFileName = staticmethod(fake_save)
    box_answers, boxes = [], []
    Button = QMessageBox.StandardButton

    def fake_exec(box):
        boxes.append((box.text(), sorted(b.text() for b in box.buttons())))
        return box_answers.pop(0) if box_answers else Button.Cancel
    QMessageBox.exec = fake_exec
    errors, warnings = [], []
    QMessageBox.critical = staticmethod(lambda parent, title, text, *a: errors.append(text))
    QMessageBox.warning = staticmethod(lambda parent, title, text, *a: warnings.append(text))
    QInputDialog.getText = staticmethod(lambda *a, **k: ("pie", True))

    lock_answers, lock_shown = [], []  # risposte: (accetta, marca temporale)

    def fake_lock_exec(dlg):
        lock_shown.append((dlg.timestamp(), dlg.findChildren(type(dlg.timestamp_box))[0].text()))
        accept, timestamp = lock_answers.pop(0) if lock_answers else (False, False)
        dlg.timestamp_box.setChecked(timestamp)
        return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected
    LockDialog.exec = fake_lock_exec
    name_answers, name_shown = [], []  # risposte: nome scritto (None = Annulla)

    def fake_name_exec(dlg):
        name_shown.append(dlg.edit.text())
        answer = name_answers.pop(0) if name_answers else None
        if answer is None:
            return QDialog.DialogCode.Rejected
        dlg.edit.setText(answer)
        return QDialog.DialogCode.Accepted
    SealNameDialog.exec = fake_name_exec

    w = MainWindow(); w.resize(1000, 800); w.show()
    pump(0.2)

    gaps = []

    def wait_job(timeout=60):
        """Aspetta la fine del sigillo misurando le pause della GUI."""
        last = [time.perf_counter()]
        worst = [0.0]

        def tick():
            now = time.perf_counter()
            worst[0] = max(worst[0], now - last[0])
            last[0] = now
        timer = QTimer(); timer.setInterval(10); timer.timeout.connect(tick); timer.start()
        t = time.perf_counter()
        while w._seal_job is not None and time.perf_counter() - t < timeout:
            app.processEvents(); time.sleep(0.005)
        timer.stop()
        pump(0.1)
        gaps.append(worst[0])
        return w._seal_job is None

    def lock(dest, accept=(True, False), names=(), boxes_=()):
        save_answers[:] = [str(dest)] if dest else []
        lock_answers[:] = [accept]
        name_answers[:] = list(names)
        box_answers[:] = list(boxes_)
        started = w.save_and_lock()
        done = wait_job() if started else True
        # Una nuova prova partita da una domanda (marca temporale, sigillo di un altro PC).
        while w._seal_job is not None:
            done = wait_job()
        return started, done

    def no_leftovers(directory):
        return not list(Path(directory).glob(f"*{sealing.TMP_SUFFIX}"))

    temps_before = set(glob.glob(os.path.join(tempfile.gettempdir(), "piedf-blocco-*")))

    # --- Avvio leggero: pyHanko non si carica finché non serve (in un processo pulito) ---
    probe = (
        "import sys; import piedf.app;"
        "print([n for n in sys.modules if n.split('.')[0] in ('cryptography', 'asn1crypto', 'pyhanko_certvalidator',"
        " 'aiohttp') or (n.startswith('pyhanko.') and n != 'pyhanko.version')])"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT).stdout.strip().splitlines()
    check("0. all'avvio la GUI non importa pyHanko né cryptography", out[-1:] == ["[]"], str(out[-1:]))
    check("0. menu File: «Salva e blocca…» subito dopo «Salva con nome…»",
          [a.text() for a in w.menuBar().actions()[0].menu().actions()].index("Salva e &blocca…")
          == [a.text() for a in w.menuBar().actions()[0].menu().actions()].index("Salva &con nome…") + 1)
    check("0. disattivo senza documento", not w.act_lock.isEnabled())

    # --- 1. Sigillo di un PDF con firma, casella di testo, tratto e campo compilato ---
    src = work / "modulo.pdf"
    shutil.copy(PDFS / "modulo.pdf", src)
    w.open_file(str(src))
    pump(0.3)
    check("1. attivo con un documento, anche senza modifiche", w.act_lock.isEnabled() and not w.modified)
    img = QImage(600, 200, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img); p.setPen(QPen(QColor("#1a237e"), 14)); p.drawLine(10, 180, 590, 20); p.end()
    sig = w.signatures.add(img, Kind.SIGNATURE)
    w.placements.add(Placement(0, QRectF(300, 600, 150, 50), sig.path, Kind.SIGNATURE))
    w.annotations.add(TextBox(0, (72, 650, 300, 670), text="Casella sigillata"))
    w.annotations.add(Ink(0, (80, 700, 200, 760), strokes=(((90.0, 710.0), (190.0, 750.0)),), color="#0000ff", width=2.0),
                      select=False)
    name_field = [f for f in w.forms.fields if f.name == "Nome"][0]
    w.forms.set_value(name_field, "Mario Rossi")
    pump(0.1)
    check("1. documento modificato prima del blocco", w.modified)

    check("3. al primo uso nessun certificato", sealing.identity_state(folder) == "missing")
    # Annulla nel dialog del nome: niente certificato, niente file.
    dest = work / "modulo_bloccato.pdf"
    started, _ = lock(dest, names=[None])
    check("3. nome annullato: non parte, niente certificato né file",
          not started and sealing.identity_state(folder) == "missing" and not dest.exists())
    check("1. dialog di conferma con la casella della marca temporale spenta di default",
          lock_shown and lock_shown[0] == (False, "Aggiungi data certa (marca temporale: si collega a internet)"),
          str(lock_shown[:1]))
    check("3. nome precompilato con il nome dell'utente Windows (o vuoto)",
          name_shown == [sealing.default_name()], str(name_shown))

    lock_shown.clear(); name_shown.clear(); save_asked.clear()
    started, done = lock(dest, names=["Mario Rossi"])
    check("1. il sigillo parte e finisce", started and done, str(errors[-1:]))
    check("3. al primo uso si chiede il nome", len(name_shown) == 1)
    check("1. proposta <nome>_bloccato.pdf", save_asked and Path(save_asked[0]).name == "modulo_bloccato.pdf", str(save_asked))
    check("1. la GUI non resta ferma più di 150 ms", gaps and gaps[-1] < MAX_GAP, f"{gaps[-1] * 1000:.0f} ms")
    check("1. scelta della marca temporale ricordata (spenta)",
          w._settings.value("lock/timestamp", type=bool) is False)
    check("1. file scritto, nessun errore", dest.exists() and not errors, str(errors))
    check("1. riaperto il file bloccato, senza modifiche",
          w._path == str(dest) and not w.modified and "modulo_bloccato.pdf" in w.windowTitle(), w.windowTitle())
    check("1. barra di stato «Salvato e bloccato in …»",
          w.statusBar().currentMessage() == "Salvato e bloccato in modulo_bloccato.pdf", w.statusBar().currentMessage())
    check("1. nessun temporaneo accanto alla destinazione", no_leftovers(work))
    check("3. certificato creato", sealing.identity_state(folder) == "ok")

    status, emb = validate(dest)
    check("1. pyHanko: firma integra e valida", status is not None and status.intact and status.valid, str(status and status.summary()))
    check("1. pyHanko: di certificazione con DocMDP «nessuna modifica»",
          status is not None and emb.docmdp_level == MDPPerm.NO_CHANGES and status.docmdp_ok)
    check("1. pyHanko: copre l'intero file", status is not None and status.coverage == SignatureCoverageLevel.ENTIRE_FILE)
    check("1. pyHanko: PAdES, senza marca temporale (B-B)",
          status is not None and emb.sig_object["/SubFilter"] == "/ETSI.CAdES.detached" and status.timestamp_validity is None)
    check("1. sigillo fidato con il certificato personale come radice", sealed_ok(status, emb))

    doc = pymupdf.open(dest)
    annots = [(a.type[1], a.info.get("title")) for page in doc for a in page.annots()]
    widgets = [(wd.field_name, wd.field_type, tuple(wd.rect)) for page in doc for wd in page.widgets()]
    text = doc[0].get_text()
    signer = own_cert().subject.native
    check("1. nessuna annotazione rimasta (firme, caselle, tratti appiattiti)", not annots, str(annots))
    check("1. nessun campo compilabile: solo il campo del sigillo",
          len(widgets) == 1 and widgets[0][0] == sealing.FIELD_NAME and widgets[0][1] == pymupdf.PDF_WIDGET_TYPE_SIGNATURE,
          str(widgets))
    check("1. campo del sigillo invisibile", widgets and pymupdf.Rect(widgets[0][2]).is_empty, str(widgets[:1]))
    check("1. testo della casella estraibile", "Casella sigillata" in text)
    check("1. valore del campo estraibile", "Mario Rossi" in text)
    check("1. testo originale ancora testo", "Modulo di prova" in text and "Pagina ruotata" in doc[1].get_text())
    check("1. nessuna pagina rasterizzata (nessuna immagine a pagina intera)",
          all(img[2] < 1000 for page in doc for img in page.get_images()), str([page.get_images() for page in doc]))
    check("3. certificato: CN = nome scelto, O = «PieDF – sigillo personale»",
          signer.get("common_name") == "Mario Rossi" and signer.get("organization_name") == sealing.ORGANIZATION, str(signer))
    cert = own_cert()
    years = (cert["tbs_certificate"]["validity"]["not_after"].native
             - cert["tbs_certificate"]["validity"]["not_before"].native).days / 365.25
    check("3. certificato: EC P-256, 10 anni, firma + non ripudio",
          cert.public_key.algorithm == "ec" and cert.public_key.curve[1] == "secp256r1" and 9.9 < years < 10.1
          and set(cert.key_usage_value.native) == {"digital_signature", "non_repudiation"},
          f"{cert.public_key.algorithm} {years:.2f} {cert.key_usage_value.native}")
    doc.close()

    # --- 4. Rifiuto: un PDF già bloccato (o firmato) ---
    warnings.clear(); lock_shown.clear(); save_asked.clear()
    again = work / "di_nuovo.pdf"
    started, _ = lock(again)
    # Il documento bloccato si legge soltanto (#71): «Salva e blocca» è disattivato e non fa niente.
    check("4. un PDF già bloccato non si blocca di nuovo (azione disattivata)",
          not started and not w.act_lock.isEnabled() and not warnings, str(warnings))
    check("4. nessun dialog né file", not lock_shown and not save_asked and not again.exists())

    signed = work / "firmato_pyhanko.pdf"
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers
    from pyhanko_certvalidator.registry import SimpleCertificateStore
    other = make_ca("Firmatario esterno", key_usage(digital_signature=True))
    with open(PDFS / "semplice.pdf", "rb") as f:
        writer = IncrementalPdfFileWriter(io.BytesIO(f.read()))
    with open(signed, "wb") as out:
        signers.PdfSigner(signers.PdfSignatureMetadata(field_name="Altro"), signers.SimpleSigner(
            signing_cert=other[0], signing_key=other[1], cert_registry=SimpleCertificateStore.from_certs([other[0]]),
        )).sign_pdf(writer, output=out)
    w.open_file(str(signed))
    pump(0.2)
    warnings.clear()
    refused = work / "firmato_bloccato.pdf"
    started, _ = lock(refused)
    check("4. PDF con una firma digitale di altri: messaggio e nessun file",
          not started and warnings == [sealing.ALREADY_SIGNED] and not refused.exists(), str(warnings))
    try:
        sealing.seal(str(signed), None, str(refused), folder)
        refused_too = False
    except sealing.AlreadySigned:
        refused_too = True
    check("4. anche il processo del sigillo rifiuta (nessun file)", refused_too and not refused.exists() and no_leftovers(work))
    check("4. un campo firma vuoto non è una firma", not sealing.has_signatures(str(PDFS / "firmato_digitale.pdf")))

    # --- 2. Marca temporale: servizio finto ---
    tsa = FakeTsa()
    main_window.TSA_URL = tsa.url
    plain = work / "semplice.pdf"
    shutil.copy(PDFS / "semplice.pdf", plain)
    w.open_file(str(plain))
    pump(0.2)
    name_shown.clear(); lock_shown.clear()
    stamped = work / "semplice_bloccato.pdf"
    started, done = lock(stamped, accept=(True, True))
    check("2. con la marca temporale: bloccato", started and done and stamped.exists(), str(errors[-1:]))
    check("2. la GUI non resta ferma più di 150 ms (con la marca temporale)", gaps[-1] < MAX_GAP, f"{gaps[-1] * 1000:.0f} ms")
    check("2. il servizio è stato chiamato", tsa.requests > 0, str(tsa.requests))
    check("3. al secondo uso il nome non si chiede", not name_shown)
    check("2. scelta della marca temporale ricordata (accesa)", w._settings.value("lock/timestamp", type=bool) is True)
    status, emb = validate(stamped, ts_root=tsa.root[0])
    ts = status.timestamp_validity if status is not None else None
    check("2. sigillo valido", sealed_ok(status, emb))
    check("2. B-T: marca temporale presente, integra, valida e fidata",
          ts is not None and ts.intact and ts.valid and ts.trusted, str(ts and ts.summary()))

    # Servizio irraggiungibile: domanda; Annulla = nessun file.
    main_window.TSA_URL = closed_port_url()
    w.open_file(str(plain))  # quello aperto ora è bloccato
    pump(0.2)
    boxes.clear(); lock_shown.clear()
    unreachable = work / "irraggiungibile.pdf"
    started, done = lock(unreachable, accept=(True, True), boxes_=[Button.Cancel])
    check("2. il dialog successivo parte con la casella accesa", lock_shown and lock_shown[0][0] is True, str(lock_shown))
    asked = [b for b in boxes if b[0] == sealing.TIMESTAMP_FAILED]
    check("2. servizio irraggiungibile: domanda «Bloccare senza data certa?»",
          asked and asked[0][1] == ["Annulla", "Blocca senza"], str(boxes))
    check("2. Annulla: nessun file", not unreachable.exists() and no_leftovers(work) and w._path == str(plain))
    boxes.clear()
    started, done = lock(unreachable, accept=(True, True), boxes_=[Button.Ok])
    status, emb = validate(unreachable) if unreachable.exists() else (None, None)
    check("2. «Blocca senza»: bloccato senza marca temporale",
          len(boxes) == 1 and sealed_ok(status, emb) and status.timestamp_validity is None, str(boxes))

    # --- 3. Certificato: chiave protetta, cartella copiata non decifrabile ---
    key_file = folder / sealing.KEY_FILE
    blob = key_file.read_bytes()
    _, private = sealing.load_identity(folder)
    der = private.dump()
    check("3. la chiave nel file non è in chiaro",
          der not in blob and private["private_key"].parsed["private_key"].contents not in blob
          and b"PRIVATE KEY" not in blob, f"{len(blob)} byte")
    if sys.platform == "win32":
        check("3. la chiave è un blob DPAPI dell'utente", sealing.unprotect(blob) == der)
    else:
        skip("3. blob DPAPI", "(solo Windows)")
    altered = bytearray(blob)
    altered[len(altered) // 2] ^= 0xFF
    key_file.write_bytes(bytes(altered))
    check("3. chiave alterata = sigillo di un altro PC", sealing.identity_state(folder) == "foreign")
    old_cert = (folder / sealing.CERT_FILE).read_bytes()
    w.open_file(str(plain))
    pump(0.2)
    boxes.clear(); name_shown.clear()
    other_pc = work / "altro_pc.pdf"
    started, _ = lock(other_pc, boxes_=[Button.Cancel])
    check("3. domanda «…creato su un altro PC: ne creo uno nuovo?»; Annulla = niente",
          boxes and boxes[0][0] == sealing.FOREIGN and not other_pc.exists() and not name_shown, str(boxes))
    boxes.clear()
    started, done = lock(other_pc, boxes_=[Button.Ok], names=["Luca Bianchi"])
    status, emb = validate(other_pc) if other_pc.exists() else (None, None)
    check("3. «Crea nuovo»: nome chiesto, certificato nuovo, PDF bloccato",
          len(name_shown) == 1 and (folder / sealing.CERT_FILE).read_bytes() != old_cert and sealed_ok(status, emb)
          and own_cert().subject.native["common_name"] == "Luca Bianchi", str(name_shown))

    # --- 4. PDF cifrato con password utente: resta cifrato con la stessa password ---
    enc = work / "cifrato.pdf"
    shutil.copy(PDFS / "modulo_cifrato.pdf", enc)
    w.open_file(str(enc))  # password "pie" dal dialog finto
    pump(0.2)
    w.forms.set_value([f for f in w.forms.fields if f.name == "Nome"][0], "Anna Verdi")
    enc_dest = work / "cifrato_bloccato.pdf"
    started, done = lock(enc_dest)
    d = pymupdf.open(enc_dest) if enc_dest.exists() else None
    check("4. PDF cifrato: bloccato", d is not None and not errors, str(errors))
    if d is not None:
        check("4. ancora cifrato con la stessa password utente",
              d.needs_pass and not d.authenticate("") and d.authenticate("pie") and "Anna Verdi" in d[0].get_text(),
              str(d.metadata.get("encryption")))
        d.close()
        d = pymupdf.open(enc_dest)
        check("4. e con la stessa password del proprietario", d.authenticate("owner") == 4)
        d.close()
        status, emb = validate(enc_dest, "pie")
        check("4. pyHanko valida il sigillo del PDF cifrato", sealed_ok(status, emb))
    check("4. riaperto con la password", w._path == str(enc_dest) and w._password == "pie")

    # --- Sovrascrivere il file aperto ---
    over = work / "sovrascritto.pdf"
    shutil.copy(PDFS / "semplice.pdf", over)
    w.open_file(str(over))
    pump(0.3)
    started, done = lock(over)
    status, emb = validate(over)
    check("1. bloccare sullo stesso file aperto", started and done and sealed_ok(status, emb) and w._path == str(over),
          str(errors[-1:]))

    # --- Annullare durante il sigillo ---
    cancel_src = work / "annulla.pdf"
    shutil.copy(PDFS / "lungo_350.pdf", cancel_src)
    w.open_file(str(cancel_src))
    pump(0.2)
    cancelled = work / "annullato.pdf"
    save_answers[:] = [str(cancelled)]
    lock_answers[:] = [(True, False)]
    w.save_and_lock()
    job = w._seal_job
    if job is not None:
        job.cancel()
    pump(0.2)
    check("annullato: nessun file e nessun temporaneo",
          w._seal_job is None and not cancelled.exists() and no_leftovers(work), w.statusBar().currentMessage())
    check("annullato: barra di stato «Blocco annullato»", w.statusBar().currentMessage() == "Blocco annullato")
    check("annullato: il documento resta aperto", w._path == str(cancel_src))

    # === #71: stato del sigillo all'apertura e sola lettura ===
    import re
    import piedf.locking as locking
    from PySide6.QtTest import QTest
    from piedf.document import open_document
    v = w.view

    def wait_seal(timeout=60):
        """Aspetta la fine della verifica del sigillo misurando le pause della GUI."""
        last = [time.perf_counter()]
        worst = [0.0]

        def tick():
            now = time.perf_counter()
            worst[0] = max(worst[0], now - last[0])
            last[0] = now
        timer = QTimer(); timer.setInterval(10); timer.timeout.connect(tick); timer.start()
        t = time.perf_counter()
        while w._seal_check is not None and time.perf_counter() - t < timeout:
            app.processEvents(); time.sleep(0.005)
        timer.stop()
        pump(0.05)
        return worst[0]

    def open_locked(path, password=None):
        w.open_file(str(path), password)
        checking = w.seal_bar.isVisible() and w.seal_label.text() == sealing.CHECKING_TEXT
        gap = wait_seal()
        return checking, gap

    def bar():
        return w.seal_label.text() if w.seal_bar.isVisible() else None

    def alert():
        return w.seal_bar.styleSheet() == main_window.SEAL_BAR_ALERT

    def certify(src_pdf, dest_pdf, who):
        """Firma di certificazione DocMDP 1 «di un altro programma» (pyHanko, senza appiattire)."""
        with open(src_pdf, "rb") as f:
            writer = IncrementalPdfFileWriter(io.BytesIO(f.read()))
        with open(dest_pdf, "wb") as out:
            signers.PdfSigner(signers.PdfSignatureMetadata(
                field_name="Certificazione", certify=True, docmdp_permissions=MDPPerm.NO_CHANGES,
            ), signers.SimpleSigner(
                signing_cert=who[0], signing_key=who[1], cert_registry=SimpleCertificateStore.from_certs([who[0]]),
            )).sign_pdf(writer, output=out)

    DATE = r"il \d\d/\d\d/\d{4} alle \d\d:\d\d"

    # --- 1. Integro ---
    mine = other_pc  # bloccato con il certificato personale attuale (Luca Bianchi)
    checking, gap = open_locked(mine)
    check("#71 1. all'apertura la barra dice «Controllo del sigillo…»", checking, bar())
    check("#71 1. bloccato da me: «Firmato e bloccato il … alle … da te (Luca Bianchi)»",
          bar() is not None and re.fullmatch(f"Firmato e bloccato {DATE} da te \\(Luca Bianchi\\)", bar()) and not alert(),
          str(bar()))
    check("#71 1. la GUI non resta ferma più di 150 ms durante la verifica", gap < MAX_GAP, f"{gap * 1000:.0f} ms")
    check("#71 1. sola lettura", w._locked and v.read_only)

    personal = work / "sigillo_di_anna.pdf"
    sealing.seal(str(PDFS / "semplice.pdf"), None, str(personal), work / "sigillo_anna", None, "Anna Neri")
    open_locked(personal)
    check("#71 1. sigillo personale di un altro: nome e «identità non verificata»",
          bar() is not None and re.fullmatch(f"Firmato e bloccato {DATE} da Anna Neri "
                                             "\\(sigillo personale, identità non verificata\\)", bar()), str(bar()))

    outsider = make_ca("Ditta Esterna Srl", key_usage(digital_signature=True, content_commitment=True))
    foreign = work / "certificato_altro.pdf"
    certify(PDFS / "modulo.pdf", foreign, outsider)  # i campi restano (non appiattito)
    check("#71 seal_status: certificazione di un altro programma integra",
          getattr(sealing.seal_status(str(foreign), None, folder), "state", None) == sealing.INTACT)
    # Come nell'exe: i moduli che piedf.spec esclude (oscrypto, requests, aiohttp…) non ci sono.
    # Senza `trust_roots=[]` pyHanko caricava le radici di sistema con oscrypto e nell'exe ogni
    # sigillo risultava «non valido» (Build Windows della PR #82).
    spec = ast.parse((ROOT / "piedf.spec").read_text(encoding="utf-8"))
    excluded = next(ast.literal_eval(n.value) for n in spec.body
                    if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "EXCLUDES")
    blocked = sorted({m for m in excluded if not m.startswith("PySide6")})
    probe = (
        "import sys, importlib.abc\n"
        f"BLOCKED = {blocked!r}\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path, target=None):\n"
        "        if name.split('.')[0] in BLOCKED: raise ModuleNotFoundError(name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "from pathlib import Path\n"
        "from piedf import sealing\n"
        "s = sealing.seal_status(sys.argv[1], None, Path(sys.argv[2]))\n"
        "print(s.state if s else None, s.own if s else None)\n"
    )
    out = subprocess.run([sys.executable, "-c", probe, str(personal), str(work / "sigillo_anna")],
                         capture_output=True, text=True, cwd=ROOT).stdout.strip().splitlines()
    check("#71 seal_status senza i moduli esclusi dall'exe (oscrypto…): integro e «da te»",
          out[-1:] == [f"{sealing.INTACT} True"], f"{out[-1:]} bloccati: {blocked}")
    # #75: un certificato importato con la chiave decifrabile qui è «da te»; con la chiave
    # di un altro PC (non decifrabile) no.
    from piedf import certificates
    imported = work / "sigillo_importato"
    imported.mkdir()
    fp = certificates.fingerprint(outsider[0].dump())
    (imported / (fp + certificates.CRT)).write_bytes(certificates._pem(outsider[0].dump()))
    (imported / (fp + certificates.KEY)).write_bytes(sealing.protect(outsider[1].dump()))
    status = sealing.seal_status(str(foreign), None, imported)
    check("#71/#75 certificato importato con la chiave di questo PC: «da te (Ditta Esterna Srl)»",
          getattr(status, "own", None) is True
          and sealing.describe(status).startswith("Firmato e bloccato il ") and "da te (Ditta Esterna Srl)" in sealing.describe(status),
          sealing.describe(status) if status else None)
    (imported / (fp + certificates.KEY)).write_bytes(b"chiave di un altro PC")
    status = sealing.seal_status(str(foreign), None, imported)
    check("#71/#75 certificato importato con la chiave di un altro PC: non «da te»",
          getattr(status, "own", None) is False, str(status))
    open_locked(foreign)
    check("#71 1. certificato di un altro programma: nome e «identità non verificata da PieDF»",
          bar() is not None and re.fullmatch(f"Firmato e bloccato {DATE} da Ditta Esterna Srl "
                                             "\\(identità non verificata da PieDF\\)", bar()), str(bar()))

    open_locked(stamped)  # semplice_bloccato.pdf: con la marca temporale, certificato di Mario Rossi (ora non più mio)
    check("#71 1. con la marca temporale: «data certificata»",
          bar() is not None and re.fullmatch(f"Firmato e bloccato {DATE} da Mario Rossi "
                                             "\\(sigillo personale, identità non verificata, data certificata\\)", bar()),
          str(bar()))

    # --- 2. Modificato dopo / non valido ---
    changed = work / "modificato_dopo.pdf"
    shutil.copy(mine, changed)
    d = pymupdf.open(changed)
    d[0].insert_text((72, 72), "Aggiunto dopo il blocco")
    d.save(changed, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    d.close()
    open_locked(changed)
    check("#71 2. salvataggio incrementale: barra rossa «modificato dopo essere stato bloccato»",
          bar() is not None and alert() and re.fullmatch(
              r"Attenzione: il documento è stato modificato dopo essere stato bloccato il \d\d/\d\d/\d{4} "
              r"da te \(Luca Bianchi\)\.", bar()), str(bar()))
    check("#71 2. sola lettura anche con il sigillo rotto", w._locked and not w.act_text.isEnabled())
    rewritten = work / "riscritto.pdf"
    d = pymupdf.open(mine)
    d.save(rewritten, garbage=3)
    d.close()
    open_locked(rewritten)
    check("#71 2. riscritto per intero: «Il sigillo di questo documento non è valido.» in rosso",
          bar() == "Il sigillo di questo documento non è valido." and alert(), str(bar()))
    check("#71 2. sola lettura anche con il sigillo non valido", w._locked and v.read_only)

    # --- 3. Sola lettura ---
    class FakeOrganizer:
        opened = []

        def __init__(self, *a, **k):
            FakeOrganizer.opened.append(a)
            self.sources, self.refs, self.changed = [], [], False

        def add_document(self, *a, **k):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

        def deleteLater(self):
            pass
    real_organizer = main_window.PageOrganizer
    main_window.PageOrganizer = FakeOrganizer
    open_locked(foreign)
    disabled = {
        "Salva": w.act_save, "Salva e blocca": w.act_lock, "Inserisci firma": w._insert_sign_menu.menuAction(),
        "Testo": w.act_text, "Data": w.act_date, "Disegna": w.act_draw, "Organizza pagine": w.act_organize,
        "Unisci PDF": w.act_merge, "Ruota pagina a sinistra": w.act_rotate_left, "Ruota pagina a destra": w.act_rotate_right,
        **{f"strumento {t}": a for t, a in w.draw_actions.items()},
    }
    on = [n for n, a in disabled.items() if a.isEnabled()]
    check("#71 3. disattivate: Salva, Salva e blocca, Inserisci firma, Testo/Data, Disegna, Organizza, Ruota pagina",
          not on and not w.sign_button.isEnabled() and not w.draw_button.isEnabled(), str(on))
    tips = [n for n, a in disabled.items() if a.toolTip() != "Il documento è bloccato"]
    check("#71 3. tooltip «Il documento è bloccato»",
          not tips and w.sign_button.toolTip() == "Il documento è bloccato"
          and w.draw_button.toolTip() == "Il documento è bloccato", str(tips))
    enabled = {
        "Salva con nome": w.act_save_as, "Stampa": w.act_print, "Trova": w.act_find, "Copia": w.act_copy,
        "Seleziona tutto": w.act_select_all, "Pannello laterale": w.act_sidebar,
        "Ruota vista a destra": w.act_view_right, "Ruota vista a sinistra": w.act_view_left,
    }
    off = [n for n, a in enabled.items() if not a.isEnabled()]
    check("#71 3. attive: Salva con nome, stampa, ricerca, copia, pannello laterale, Ruota vista", not off, str(off))

    save_asked.clear(); save_answers.clear(); errors.clear()
    v.setFocus()
    pump(0.05)
    for key, mod in ((Qt.Key.Key_T, Qt.KeyboardModifier.ControlModifier), (Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier),
                     (Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier),
                     (Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)):
        QTest.keyClick(v, key, mod)
        pump(0.02)
    for action in disabled.values():
        action.trigger()
    pump(0.05)
    check("#71 3. scorciatoie e azioni disattivate non fanno niente",
          not v.text_tool and v.draw_tool is None and not v.placing and not save_asked and not FakeOrganizer.opened
          and w._page_job is None and w._seal_job is None and not w.modified and w._path == str(foreign),
          f"{save_asked} {FakeOrganizer.opened}")
    v.start_text_tool(); v.start_draw_tool("pen"); v.start_placing(sig); w.set_draw_tool("pen")
    w.organize_pages(); w.rotate_current(90)
    check("#71 3. anche chiamati direttamente: niente strumenti, niente pagine",
          not v.text_tool and v.draw_tool is None and not v.placing and not FakeOrganizer.opened and w._page_job is None)
    check("#71 3. Salva e Salva e blocca non partono", not w.save() and not w.save_and_lock() and not save_asked)

    fields = [f for f in w.forms.fields if f.name == "Nome"]
    check("#71 3. il PDF certificato ha ancora i campi", bool(fields))
    check("#71 3. barra dei moduli nascosta (i campi non si compilano)", not w.form_bar.isVisible())
    if fields:
        f = fields[0]
        v.scroll_to_rect(f.page, QRectF(f.rect[0], f.rect[1], f.rect[2] - f.rect[0], f.rect[3] - f.rect[1]))
        pump(0.1)
        x0, y0, x1, y1 = f.rect
        pos = v.page_rect_to_viewport(f.page, QRectF(x0, y0, x1 - x0, y1 - y0)).center().toPoint()
        QTest.mouseMove(v.viewport(), pos)
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
        pump(0.05)
        check("#71 3. clic su un campo: l'editor non si apre", v.editor is None and v.focus_field is None)
        v.setFocus()
        QTest.keyClick(v, Qt.Key.Key_Tab)
        pump(0.05)
        check("#71 3. Tab: nessun campo raggiunto", v.editor is None and v.focus_field is None)
        QTest.keyClick(v, Qt.Key.Key_Space)
        pump(0.05)
        check("#71 3. nessun valore cambiato", not w.forms.changed() and not w.modified)

    w.act_view_right.trigger()
    pump(0.05)
    rotated = v.rotation == 90
    w.act_view_left.trigger()
    pump(0.05)
    check("#71 3. Ruota vista funziona", rotated and v.rotation == 0)

    copy = work / "copia_identica.pdf"
    save_answers[:] = [str(copy)]
    saved = w.save_as()
    check("#71 3. Salva con nome: copia identica byte per byte",
          saved and copy.exists() and copy.read_bytes() == foreign.read_bytes() and no_leftovers(work),
          str(save_asked) + str(errors))
    check("#71 3. Salva con nome propone <nome>_copia.pdf", save_asked and Path(save_asked[-1]).name == "certificato_altro_copia.pdf",
          str(save_asked))
    check("#71 3. il documento aperto resta quello, ancora bloccato", w._path == str(foreign) and w._locked)
    check("#71 3. sigillo della copia integro",
          getattr(sealing.seal_status(str(copy), None, folder), "state", None) == sealing.INTACT)

    w.open_file(str(plain))
    pump(0.2)
    back = [n for n, a in disabled.items() if not a.isEnabled() and a is not w.act_save]
    check("#71 3. aprendo un PDF normale tutto torna attivo",
          not back and w.sign_button.isEnabled() and w.draw_button.isEnabled() and not w._locked and not v.read_only
          and w.seal_bar.isHidden(), str(back))
    check("#71 3. tooltip di nuovo quelli di prima",
          w.act_text.toolTip() == "Aggiungi testo (Ctrl+T)"
          and w.act_organize.toolTip() == QAction(w.act_organize.text()).toolTip()
          and w.sign_button.toolTip() == "Metti una firma sul documento", w.act_organize.toolTip())
    main_window.PageOrganizer = real_organizer
    check("#71 3. chiudendo il documento la sola lettura si azzera",
          (open_locked(mine), w.close_document(), not w._locked and not v.read_only and w.seal_bar.isHidden())[-1])

    # --- 4. Non bloccante ---
    locking.CHECK_DELAY = 3.0
    t = time.perf_counter()
    w.open_file(str(mine))
    opened = time.perf_counter() - t
    shown = False
    while time.perf_counter() - t < 2.5 and not shown:
        app.processEvents(); time.sleep(0.01)
        shown = 0 in v._cache
    check("#71 4. con la verifica rallentata la finestra mostra subito le pagine e «Controllo del sigillo…»",
          shown and opened < 1.0 and w._seal_check is not None and bar() == sealing.CHECKING_TEXT,
          f"apertura {opened:.2f} s, pagina {shown}, {bar()}")
    w.open_file(str(plain))
    pump(4.0)
    check("#71 4. aperto un altro file prima della fine: il risultato vecchio non compare",
          bar() is None and not w._locked and w._seal_check is None, str(bar()))
    w.open_file(str(mine))
    pump(0.5)
    w.open_file(str(rewritten))  # un altro PDF bloccato: vale solo il suo risultato
    wait_seal()
    pump(0.5)
    check("#71 4. due PDF bloccati di seguito: resta il risultato del secondo",
          bar() == sealing.INVALID_TEXT, str(bar()))
    locking.CHECK_DELAY = 0.0

    # --- 5. Firma di approvazione normale: niente barra, modificabile ---
    w.open_file(str(signed))
    pump(0.3)
    check("#71 5. firma di approvazione: niente barra né verifica",
          w.seal_bar.isHidden() and w._seal_check is None and not w._locked, str(bar()))
    check("#71 5. resta modificabile come oggi", w.act_text.isEnabled() and w.act_draw.isEnabled() and not v.read_only)
    check("#71 5. seal_status: nessuna certificazione = None",
          sealing.seal_status(str(signed)) is None and sealing.seal_status(str(PDFS / "semplice.pdf")) is None)

    # --- 6. Apertura di un PDF senza firme: solo il controllo rapido, nessun processo ---
    w.open_file(str(plain))
    pump(0.1)
    check("#71 6. PDF senza firme: nessun processo di verifica", w._seal_check is None and not w._locked)
    doc = pymupdf.open(PDFS / "semplice.pdf")
    t = time.perf_counter()
    for _ in range(100):
        sealing.docmdp_level(doc)
    quick = (time.perf_counter() - t) / 100
    doc.close()
    check("#71 6. controllo rapido sotto 1 ms", quick < 0.001, f"{quick * 1e6:.0f} µs")
    check("#71 6. open_document riconosce i PDF bloccati",
          open_document(str(mine)).certified and open_document(str(changed)).certified
          and not open_document(str(signed)).certified and not open_document(str(plain)).certified)

    temps_after = set(glob.glob(os.path.join(tempfile.gettempdir(), "piedf-blocco-*")))
    check("temporanei delle modifiche cancellati", temps_after <= temps_before, str(temps_after - temps_before))
    check("nessun errore inatteso", not errors, str(errors))

    w.close()
    pump(0.2)
    tsa.server.shutdown()
    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
