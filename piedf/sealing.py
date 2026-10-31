"""Sigillo digitale dei PDF (#70, «Salva e blocca»), senza Qt.

Il sigillo è una firma digitale di **certificazione** (PAdES, DocMDP «nessuna modifica»): il
PDF resta modificabile da chi ha il file, ma ogni modifica successiva rompe il sigillo e i
lettori che controllano le firme lo mostrano. Ordine (`seal`): il documento (già scritto con
firme, campi e annotazioni da `save_with_signatures`) si appiattisce con `Document.bake()`
(annotazioni e campi diventano contenuto della pagina, il testo resta testo, l'AcroForm
sparisce), si scrive in memoria con `PDF_ENCRYPT_KEEP` e pyHanko vi aggiunge la firma in un
campo invisibile; il file si scrive accanto alla destinazione e poi la sostituisce.

Certificato personale autofirmato in `<dati>/sigillo/`: certificato in chiaro, chiave privata
cifrata con DPAPI dell'utente Windows (`CryptProtectData` via ctypes). Copiata su un altro PC
o utente la chiave non si decifra: `identity_state` → "foreign".

pyHanko (e cryptography) si importano solo in `seal` / `create_identity`, che girano nel
processo del sigillo (`run`): l'avvio di PieDF non cambia e la GUI non si ferma.
"""

import ctypes
import datetime
import hashlib
import hmac
import io
import os
import re
import secrets
import socket
import sys
import types
import urllib.request
from pathlib import Path

import pymupdf

# Marca temporale RFC 3161 (gratuita). È l'unica rete che PieDF usa senza un clic su un link,
# e solo se l'utente sceglie «Aggiungi data certa».
TSA_URL = "http://timestamp.digicert.com"
TSA_TIMEOUT = 10  # secondi
TSA_MAX_BYTES = 1 << 20

SEAL_DIR = "sigillo"
CERT_FILE = "certificato.pem"
KEY_FILE = "chiave.dpapi"
ORGANIZATION = "PieDF – sigillo personale"
NAME_MAX = 64
VALID_DAYS = 3652  # 10 anni
FIELD_NAME = "SigilloPieDF"
TMP_SUFFIX = ".piedf-tmp"
# Entropia aggiuntiva di DPAPI: un altro programma dello stesso utente non decifra la chiave per caso.
ENTROPY = b"PieDF sigillo personale"

ALREADY_SIGNED = "Questo documento ha già firme digitali: bloccarlo le renderebbe non valide."
FOREIGN = "Il sigillo personale è stato creato su un altro PC: ne creo uno nuovo?"
TIMESTAMP_FAILED = "Impossibile ottenere la marca temporale. Bloccare senza data certa?"
ENCRYPTION_UNSUPPORTED = (
    "Il tipo di cifratura di questo PDF non permette di aggiungere il sigillo. "
    "Il documento non è stato bloccato (la cifratura non viene mai tolta)."
)


class SealError(Exception):
    """Il sigillo non è riuscito; il messaggio è pronto per l'utente."""


class AlreadySigned(SealError):
    def __init__(self) -> None:
        super().__init__(ALREADY_SIGNED)


class ForeignIdentity(SealError):
    """La chiave del sigillo non si decifra qui (creata su un altro PC o utente, o alterata)."""

    def __init__(self) -> None:
        super().__init__(FOREIGN)


class TimestampFailed(SealError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(TIMESTAMP_FAILED)
        self.detail = detail


def seal_dir(data: Path) -> Path:
    return Path(data) / SEAL_DIR


# --- Protezione della chiave ------------------------------------------------------------

if sys.platform == "win32":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def _blob(data: bytes) -> tuple["_Blob", ctypes.Array]:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _dpapi(protect: bool, data: bytes) -> bytes:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        func.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p if not protect else wintypes.LPCWSTR,
                         ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(_Blob)]
        func.restype = wintypes.BOOL
        source, keep1 = _blob(data)
        entropy, keep2 = _blob(ENTROPY)
        out = _Blob()
        description = "PieDF sigillo" if protect else None
        if not func(ctypes.byref(source), description, ctypes.byref(entropy), None, None,
                    _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
            raise OSError(ctypes.get_last_error(), "DPAPI")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))

    def protect(data: bytes) -> bytes:
        """Cifra `data` con DPAPI dell'utente Windows (si decifra solo con lo stesso utente sullo stesso PC)."""
        return _dpapi(True, data)

    def unprotect(blob: bytes) -> bytes:
        """Il contrario di `protect`; solleva OSError se non si decifra."""
        return _dpapi(False, blob)

else:
    # Fuori da Windows (sviluppo e controlli) non c'è DPAPI: una chiave legata a utente e
    # macchina, con HMAC, dà lo stesso comportamento (file illeggibile, alterazione = errore).
    # Non è una protezione vera: PieDF si distribuisce solo per Windows.
    def _local_key() -> bytes:
        try:
            machine = Path("/etc/machine-id").read_bytes().strip()
        except OSError:
            machine = b""
        ident = b"|".join([ENTROPY, machine, socket.gethostname().encode(), str(os.getuid()).encode()])
        return hashlib.sha256(ident).digest()

    def _stream(key: bytes, nonce: bytes, size: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < size:
            out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
            counter += 1
        return bytes(out[:size])

    def protect(data: bytes) -> bytes:
        key, nonce = _local_key(), secrets.token_bytes(16)
        body = bytes(a ^ b for a, b in zip(data, _stream(key, nonce, len(data))))
        return nonce + body + hmac.new(key, nonce + body, "sha256").digest()

    def unprotect(blob: bytes) -> bytes:
        key = _local_key()
        nonce, body, tag = blob[:16], blob[16:-32], blob[-32:]
        if len(blob) < 48 or not hmac.compare_digest(tag, hmac.new(key, nonce + body, "sha256").digest()):
            raise OSError("chiave non decifrabile")
        return bytes(a ^ b for a, b in zip(body, _stream(key, nonce, len(body))))


# --- Certificato personale ----------------------------------------------------------------

def identity_state(folder: Path) -> str:
    """"missing" (nessun sigillo), "foreign" (chiave non decifrabile qui) oppure "ok".

    Veloce (niente cryptography): la GUI la chiama prima di chiedere il file.
    """
    folder = Path(folder)
    cert, key = folder / CERT_FILE, folder / KEY_FILE
    if not cert.exists() or not key.exists():
        return "missing"
    try:
        unprotect(key.read_bytes())
    except (OSError, ValueError):
        return "foreign"
    return "ok"


def default_name() -> str:
    """Nome completo dell'utente Windows (GetUserNameExW, NameDisplay), "" se non c'è."""
    if sys.platform != "win32":
        return ""
    try:
        secur32 = ctypes.WinDLL("secur32")
        size = ctypes.c_ulong(0)
        secur32.GetUserNameExW(3, None, ctypes.byref(size))  # 3 = NameDisplay
        if not size.value:
            return ""
        buf = ctypes.create_unicode_buffer(size.value)
        if not secur32.GetUserNameExW(3, buf, ctypes.byref(size)):
            return ""
        return clean_name(buf.value)
    except (OSError, AttributeError):
        return ""


def clean_name(name: str) -> str:
    """Nome del sigillo: spazi normalizzati, massimo NAME_MAX caratteri."""
    return re.sub(r"\s+", " ", name).strip()[:NAME_MAX]


def create_identity(folder: Path, name: str) -> None:
    """Crea (o sostituisce) il certificato personale: EC P-256 autofirmato, 10 anni."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    name = clean_name(name)
    if not name:
        raise SealError("Il nome del sigillo non può essere vuoto.")
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORGANIZATION),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))  # orologi un po' indietro
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=True, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    # Prima la chiave, poi il certificato: un sigillo a metà resta "foreign" e si rifà.
    _write_atomic(folder / KEY_FILE, protect(der))
    _write_atomic(folder / CERT_FILE, cert.public_bytes(serialization.Encoding.PEM))


def load_identity(folder: Path):
    """(certificato, chiave) per pyHanko (oggetti asn1crypto). Solleva `ForeignIdentity` /
    `SealError` se manca."""
    from asn1crypto import keys, pem, x509
    from cryptography.hazmat.primitives import serialization

    folder = Path(folder)
    try:
        cert_data = (folder / CERT_FILE).read_bytes()
        blob = (folder / KEY_FILE).read_bytes()
    except FileNotFoundError as e:
        raise SealError("Il sigillo personale non esiste ancora.") from e
    try:
        der = unprotect(blob)
        key = serialization.load_der_private_key(der, password=None)
        if pem.detect(cert_data):
            cert_data = pem.unarmor(cert_data)[2]
        cert = x509.Certificate.load(cert_data)
        public = key.public_key().public_bytes(serialization.Encoding.DER,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
        if public != cert.public_key.dump():
            raise ValueError("il certificato non è della chiave")
    except (OSError, ValueError, TypeError) as e:
        raise ForeignIdentity() from e
    return cert, keys.PrivateKeyInfo.load(der)


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + TMP_SUFFIX)
    tmp.write_bytes(data)
    os.replace(tmp, path)


# --- Firme già presenti ----------------------------------------------------------------------

_REF = re.compile(r"(\d+)\s+0\s+R")


def has_signatures(path: str, password: str | None = None) -> bool:
    """True se il PDF ha firme digitali di qualunque tipo: campi firma compilati (anche marche
    temporali del documento) o firme nel catalogo (/Perms: DocMDP, diritti d'uso). Un campo
    firma vuoto non conta. Solo PyMuPDF: veloce, la GUI la chiama prima di chiedere il file."""
    doc = pymupdf.open(path)
    try:
        if doc.needs_pass:
            doc.authenticate(password or "")
        return _signed(doc)
    finally:
        doc.close()


def _signed(doc) -> bool:
    catalog = doc.pdf_catalog()
    if doc.xref_get_key(catalog, "Perms")[0] != "null":
        return True
    kind, fields = doc.xref_get_key(catalog, "AcroForm/Fields")
    if kind == "xref":
        fields = doc.xref_object(int(_REF.match(fields).group(1)))
    todo = [(int(n), "") for n in _REF.findall(fields)] if kind in ("array", "xref") else []
    seen = set()
    while todo:
        xref, inherited = todo.pop()
        if xref in seen or not 0 < xref < doc.xref_length():
            continue
        seen.add(xref)
        ft = doc.xref_get_key(xref, "FT")
        ft = ft[1] if ft[0] == "name" else inherited
        if ft in ("/Sig", "/DocTimeStamp") and doc.xref_get_key(xref, "V")[0] != "null":
            return True
        kids = doc.xref_get_key(xref, "Kids")
        if kids[0] == "xref":
            kids = ("array", doc.xref_object(int(_REF.match(kids[1]).group(1))))
        if kids[0] == "array":
            todo += [(int(n), ft) for n in _REF.findall(kids[1])]
    return False


# --- Sigillo --------------------------------------------------------------------------------

def _stub_aiohttp() -> None:
    """pyHanko importa sempre aiohttp (client HTTP che PieDF non usa): l'exe lo esclude, qui
    si mette un modulo vuoto al suo posto. Le classi richieste dalle annotazioni di tipo sono
    classi vuote; usarle davvero darebbe errore, ma PieDF usa solo `UrllibTimeStamper`."""
    if "aiohttp" in sys.modules:
        return
    stub = types.ModuleType("aiohttp")
    stub.__path__ = []

    def missing(name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        cls = type(name, (), {})
        setattr(stub, name, cls)
        return cls

    stub.__getattr__ = missing
    sys.modules["aiohttp"] = stub


def _timestamper(url: str):
    from asn1crypto import tsp
    from pyhanko.sign.timestamps import TimeStamper

    class UrllibTimeStamper(TimeStamper):
        """Client RFC 3161 con urllib (pyHanko userebbe aiohttp). `failed` = qualcosa non è andato."""

        def __init__(self) -> None:
            super().__init__()
            self.failed = ""

        async def async_request_tsa_response(self, req):
            request = urllib.request.Request(url, data=req.dump(), method="POST", headers={
                "Content-Type": "application/timestamp-query", "Accept": "application/timestamp-reply",
            })
            with urllib.request.urlopen(request, timeout=TSA_TIMEOUT) as response:
                body = response.read(TSA_MAX_BYTES + 1)
            if len(body) > TSA_MAX_BYTES:
                raise OSError("risposta troppo grande")
            return tsp.TimeStampResp.load(body)

        async def async_timestamp(self, message_digest, md_algorithm):
            try:
                return await super().async_timestamp(message_digest, md_algorithm)
            except Exception as e:
                self.failed = str(e) or type(e).__name__
                raise

    return UrllibTimeStamper()


def seal(src: str, password: str | None, dest: str, folder: Path, tsa_url: str | None = None,
         name: str | None = None) -> None:
    """Appiattisce `src` e lo scrive in `dest` (anche lo stesso file) con il sigillo.

    `tsa_url` = marca temporale (PAdES B-T), None = senza (B-B). `name` = crea prima un
    certificato personale nuovo con questo nome. Solleva `SealError` (o le sue sottoclassi):
    in quel caso `dest` non è stato toccato.
    """
    _stub_aiohttp()
    from pyhanko.pdf_utils.crypt import AuthStatus
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers
    from pyhanko_certvalidator.registry import SimpleCertificateStore

    if name is not None:
        create_identity(folder, name)
    cert, key = load_identity(folder)

    try:
        doc = pymupdf.open(src)
    except Exception as e:
        raise SealError(f"Impossibile rileggere il documento.\n\nDettagli: {e}") from e
    try:
        if doc.needs_pass and not doc.authenticate(password or ""):
            raise SealError("Password del documento non valida.")
        if _signed(doc):
            raise AlreadySigned()
        doc.bake()  # annotazioni e campi diventano contenuto della pagina; l'AcroForm sparisce
        # pyHanko vuole il dizionario di cifratura come oggetto a parte, MuPDF lo scrive nel
        # trailer: lo si sposta in un oggetto e MuPDF lo conserva così (stessi /O /U).
        kind, value = doc.xref_get_key(-1, "Encrypt")
        if kind == "dict":
            xref = doc.get_new_xref()
            doc.update_object(xref, value)
            doc.xref_set_key(-1, "Encrypt", f"{xref} 0 R")
        data = doc.tobytes(garbage=3, deflate=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    except SealError:
        raise
    except Exception as e:
        raise SealError(f"Errore durante l'appiattimento del documento.\n\nDettagli: {e}") from e
    finally:
        doc.close()

    try:
        writer = IncrementalPdfFileWriter(io.BytesIO(data))
        if writer.security_handler is not None:
            if writer.encrypt(password or "").status == AuthStatus.FAILED:
                raise SealError(ENCRYPTION_UNSUPPORTED)
    except SealError:
        raise
    except Exception as e:
        raise SealError(f"{ENCRYPTION_UNSUPPORTED}\n\nDettagli: {e}") from e

    signer = signers.SimpleSigner(
        signing_cert=cert, signing_key=key, cert_registry=SimpleCertificateStore.from_certs([cert]),
    )
    meta = signers.PdfSignatureMetadata(
        field_name=FIELD_NAME, certify=True, docmdp_permissions=fields.MDPPerm.NO_CHANGES,
        subfilter=fields.SigSeedSubFilter.PADES, md_algorithm="sha256",
    )
    stamper = _timestamper(tsa_url) if tsa_url else None
    tmp = dest + TMP_SUFFIX
    try:
        try:
            with open(tmp, "wb") as out:
                signers.PdfSigner(meta, signer, timestamper=stamper).sign_pdf(writer, output=out)
        except Exception as e:
            if stamper is not None and stamper.failed:  # pyHanko può incapsulare l'errore di rete
                raise TimestampFailed(stamper.failed) from e
            if isinstance(e, OSError):
                raise SealError(_write_message(dest, e)) from e
            raise SealError(f"Errore durante la firma del documento.\n\nDettagli: {e}") from e
        try:
            os.replace(tmp, dest)
        except OSError as e:
            raise SealError(_write_message(dest, e)) from e
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _write_message(dest: str, error: Exception) -> str:
    return (
        f"Impossibile scrivere «{Path(dest).name}».\n\n"
        "La cartella potrebbe non essere scrivibile oppure il file è aperto in un altro programma.\n\n"
        f"Dettagli: {error}"
    )


def cli(args: list[str]) -> int:
    """`PieDF.exe --piedf-sigilla <pdf> <destinazione> <cartella sigillo> <nome>`: sigilla senza
    finestra (B-B), per provare l'exe (`tests/check_exe.py`). L'exe non ha console: un errore
    si scrive in `<destinazione>.errore.txt`. Uscita 0 se riuscito."""
    src, dest, folder, name = args
    try:
        state = identity_state(Path(folder))
        seal(src, None, dest, Path(folder), None, name if state != "ok" else None)
        return 0
    except Exception as e:
        Path(dest + ".errore.txt").write_text(f"{type(e).__name__}: {e}", encoding="utf-8")
        return 1


def run(src, password, dest, folder, tsa_url, name, results) -> None:
    """Processo del sigillo: ("done",) oppure ("error", tipo, messaggio) con tipo
    "signed" / "foreign" / "timestamp" / "error"."""
    try:
        seal(src, password, dest, Path(folder), tsa_url, name)
        results.put(("done",))
    except AlreadySigned as e:
        results.put(("error", "signed", str(e)))
    except ForeignIdentity as e:
        results.put(("error", "foreign", str(e)))
    except TimestampFailed as e:
        results.put(("error", "timestamp", str(e)))
    except SealError as e:
        results.put(("error", "error", str(e)))
    except Exception as e:
        results.put(("error", "error", f"Errore durante il blocco del documento.\n\nDettagli: {e}"))
