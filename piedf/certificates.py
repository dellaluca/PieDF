"""Certificati per il sigillo (#75), senza Qt: il sigillo personale di #70 e i certificati
importati da un file .p12/.pfx.

Tutto sta in `<dati>/sigillo/`: il sigillo personale in `certificato.pem` / `chiave.dpapi`
(`sealing`), ogni certificato importato in `<impronta>.crt` (PEM: prima il certificato, poi
la catena del file) e `<impronta>.key` (PKCS#8 cifrato con `sealing.protect`, cioè DPAPI
come il sigillo personale). L'impronta è lo SHA-256 del certificato in DER, in esadecimale.
Il .p12 originale e la sua password non si conservano mai.

Un .p12 esportato da PieDF (autofirmato, O = `sealing.ORGANIZATION`) importato diventa il
sigillo personale di questo PC: così si porta il sigillo su un altro PC.

Le funzioni che servono a «Salva e blocca» prima del dialog (`personal_fingerprint`,
`imported_fingerprints`) leggono solo i PEM: cryptography si importa solo per l'elenco,
l'import e l'export.
"""

import base64
import datetime
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from piedf import sealing

CRT, KEY = ".crt", ".key"
EXPORT_SUFFIX = "_sigillo.p12"
PASSWORD_MIN = 8

BAD_PASSWORD = "Password errata, oppure il file non è un certificato .p12/.pfx."
UNUSABLE = "Questo file non contiene un certificato utilizzabile per firmare."
EXPIRED = "Il certificato è scaduto il {date}."
DUPLICATE = "Questo certificato è già importato."
IMPORTED_FOREIGN = (
    "Il certificato «{name}» è stato importato su un altro PC o da un altro utente: "
    "importalo di nuovo da Strumenti › Certificati…"
)
GONE = "Il certificato scelto non c'è più: scegline un altro da Strumenti › Certificati…"

_PEM = re.compile(rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.S)
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


class CertificateError(Exception):
    """Import o export non riuscito; il messaggio è pronto per l'utente."""


@dataclass(frozen=True)
class CertInfo:
    """Un certificato dell'elenco. `usable` = la chiave si decifra su questo PC."""

    fingerprint: str
    name: str
    issuer: str
    expires: datetime.datetime
    personal: bool
    usable: bool

    @property
    def label(self) -> str:
        """Testo della tendina «Firma con: …»."""
        return f"{self.name} (sigillo personale)" if self.personal else f"{self.name} ({self.issuer})"


@dataclass
class Pending:
    """Un .p12 letto e controllato, da scrivere con `install` (oggetti di cryptography)."""

    cert: object
    key: object
    chain: list
    fingerprint: str
    name: str
    personal: bool  # esportato da PieDF: diventa il sigillo personale
    replaces: bool  # c'è già un altro sigillo personale (si chiede conferma)


# --- Senza cryptography ---------------------------------------------------------------------

def _pem_blocks(data: bytes) -> list[bytes]:
    """I certificati (DER) di un file PEM, nell'ordine."""
    return [base64.b64decode(b"".join(block.split())) for block in _PEM.findall(data)]


def _pem(der: bytes) -> bytes:
    body = base64.encodebytes(der).replace(b"\n", b"")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return b"-----BEGIN CERTIFICATE-----\n" + b"\n".join(lines) + b"\n-----END CERTIFICATE-----\n"


def fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def personal_fingerprint(folder: Path) -> str | None:
    """Impronta del sigillo personale, None se non c'è (anche se la chiave non si decifra qui)."""
    try:
        blocks = _pem_blocks((Path(folder) / sealing.CERT_FILE).read_bytes())
    except OSError:
        return None
    return fingerprint(blocks[0]) if blocks and (Path(folder) / sealing.KEY_FILE).exists() else None


def imported_fingerprints(folder: Path) -> list[str]:
    """Impronte dei certificati importati (con il loro file della chiave), in ordine."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*" + CRT)
                  if _FINGERPRINT.fullmatch(p.stem) and p.with_suffix(KEY).exists())


def key_state(folder: Path, fp: str) -> str:
    """"ok", "foreign" (la chiave non si decifra su questo PC) oppure "missing"."""
    folder = Path(folder)
    if fp == personal_fingerprint(folder):
        return sealing.identity_state(folder)
    key = folder / (fp + KEY)
    if not _FINGERPRINT.fullmatch(fp) or not key.exists() or not (folder / (fp + CRT)).exists():
        return "missing"
    try:
        sealing.unprotect(key.read_bytes())
    except (OSError, ValueError):
        return "foreign"
    return "ok"


def resolve(folder: Path, saved: str | None) -> str | None:
    """Il certificato predefinito: `saved` (da `lock/certificate`) se c'è ancora, altrimenti il
    sigillo personale, altrimenti il primo importato; None se non ce n'è nessuno."""
    personal = personal_fingerprint(folder)
    imported = imported_fingerprints(folder)
    if saved and (saved == personal or saved in imported):
        return saved
    return personal or (imported[0] if imported else None)


def delete(folder: Path, fp: str) -> None:
    """Elimina un certificato (anche il sigillo personale): prima la chiave, poi il certificato."""
    folder = Path(folder)
    if fp == personal_fingerprint(folder):
        files = [folder / sealing.KEY_FILE, folder / sealing.CERT_FILE]
    elif _FINGERPRINT.fullmatch(fp):
        files = [folder / (fp + KEY), folder / (fp + CRT)]
    else:
        return
    for path in files:
        path.unlink(missing_ok=True)


# --- Con cryptography -----------------------------------------------------------------------

def _name(name, fallback: str = "") -> str:
    from cryptography.x509.oid import NameOID
    for oid in (NameOID.COMMON_NAME, NameOID.ORGANIZATION_NAME):
        attrs = name.get_attributes_for_oid(oid)
        if attrs:
            return str(attrs[0].value)
    return fallback or name.rfc4514_string()


def _is_personal(cert) -> bool:
    from cryptography.x509.oid import NameOID
    org = cert.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
    return cert.subject == cert.issuer and bool(org) and org[0].value == sealing.ORGANIZATION


def _info(der: bytes, personal: bool, usable: bool) -> CertInfo:
    from cryptography import x509
    cert = x509.load_der_x509_certificate(der)
    issuer = "Autofirmato" if cert.subject == cert.issuer else _name(cert.issuer)
    return CertInfo(fingerprint(der), _name(cert.subject), issuer, cert.not_valid_after_utc, personal, usable)


def entries(folder: Path) -> list[CertInfo]:
    """Il sigillo personale (se c'è) e i certificati importati, in quest'ordine."""
    folder = Path(folder)
    out = []
    fp = personal_fingerprint(folder)
    if fp is not None:
        der = _pem_blocks((folder / sealing.CERT_FILE).read_bytes())[0]
        out.append(_info(der, True, sealing.identity_state(folder) == "ok"))
    for fp in imported_fingerprints(folder):
        try:
            blocks = _pem_blocks((folder / (fp + CRT)).read_bytes())
            out.append(_info(blocks[0], False, key_state(folder, fp) == "ok"))
        except (OSError, IndexError, ValueError):
            continue  # file illeggibile: non si mostra
    return out


def _date(moment: datetime.datetime) -> str:
    return moment.astimezone().strftime("%d/%m/%Y")


def read_p12(folder: Path, data: bytes, password: str, now: datetime.datetime | None = None) -> Pending:
    """Legge e controlla un .p12/.pfx; solleva `CertificateError` con il messaggio per l'utente.
    Non scrive niente."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, rsa
    from cryptography.hazmat.primitives.serialization import pkcs12

    loaded = None
    for secret in ([password.encode("utf-8")] + ([None] if not password else [])):
        try:
            loaded = pkcs12.load_pkcs12(data, secret)
            break
        except (ValueError, TypeError):
            continue
    if loaded is None:
        raise CertificateError(BAD_PASSWORD)
    key = loaded.key
    cert = loaded.cert.certificate if loaded.cert is not None else None
    if key is None or cert is None or not isinstance(key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey,
                                                            dsa.DSAPrivateKey, ed25519.Ed25519PrivateKey,
                                                            ed448.Ed448PrivateKey)):
        raise CertificateError(UNUSABLE)
    public = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if key.public_key().public_bytes(*public) != cert.public_key().public_bytes(*public):
        raise CertificateError(UNUSABLE)
    try:
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        if not (usage.digital_signature or usage.content_commitment):
            raise CertificateError(UNUSABLE)
    except x509.ExtensionNotFound:
        pass  # nessun limite d'uso
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if cert.not_valid_after_utc < now:
        raise CertificateError(EXPIRED.format(date=_date(cert.not_valid_after_utc)))

    folder = Path(folder)
    der = cert.public_bytes(serialization.Encoding.DER)
    fp = fingerprint(der)
    personal = _is_personal(cert)
    if key_state(folder, fp) == "ok":  # con la chiave non decifrabile qui si reimporta
        raise CertificateError(DUPLICATE)
    current = personal_fingerprint(folder)
    chain = [c.certificate for c in loaded.additional_certs]
    return Pending(cert, key, chain, fp, _name(cert.subject), personal,
                   personal and current is not None and current != fp)


def install(folder: Path, pending: Pending) -> None:
    """Scrive il certificato letto da `read_p12`: prima la chiave, poi il certificato (a metà
    il certificato non compare)."""
    from cryptography.hazmat.primitives import serialization

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    der = pending.key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    certs = [pending.cert, *pending.chain]
    if pending.personal:
        sealing._write_atomic(folder / sealing.KEY_FILE, sealing.protect(der))
        sealing._write_atomic(folder / sealing.CERT_FILE, pending.cert.public_bytes(serialization.Encoding.PEM))
    else:
        sealing._write_atomic(folder / (pending.fingerprint + KEY), sealing.protect(der))
        sealing._write_atomic(folder / (pending.fingerprint + CRT),
                              b"".join(_pem(c.public_bytes(serialization.Encoding.DER)) for c in certs))


def export_name(folder: Path) -> str:
    """Nome proposto per l'export: `<nome>_sigillo.p12`, senza caratteri vietati da Windows."""
    from cryptography import x509
    der = _pem_blocks((Path(folder) / sealing.CERT_FILE).read_bytes())[0]
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", _name(x509.load_der_x509_certificate(der).subject)).strip(" .")
    return (name or "sigillo") + EXPORT_SUFFIX


def export_personal(folder: Path, password: str) -> bytes:
    """Il sigillo personale come .p12 cifrato con `password` (almeno `PASSWORD_MIN` caratteri)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    if len(password) < PASSWORD_MIN:
        raise CertificateError(f"La password deve avere almeno {PASSWORD_MIN} caratteri.")
    folder = Path(folder)
    try:
        der = sealing.unprotect((folder / sealing.KEY_FILE).read_bytes())
        cert_der = _pem_blocks((folder / sealing.CERT_FILE).read_bytes())[0]
    except (OSError, ValueError, IndexError) as e:
        raise CertificateError("Il sigillo personale non si può esportare da questo PC.") from e
    key = serialization.load_der_private_key(der, password=None)
    cert = x509.load_der_x509_certificate(cert_der)
    return pkcs12.serialize_key_and_certificates(
        _name(cert.subject).encode("utf-8"), key, cert, None,
        serialization.BestAvailableEncryption(password.encode("utf-8")),
    )


def load_signer(folder: Path, fp: str):
    """(certificato, chiave, catena) per pyHanko (oggetti asn1crypto) del certificato `fp`.
    Solleva `sealing.SealError` (o `ForeignIdentity` per il sigillo personale)."""
    from asn1crypto import keys, x509

    folder = Path(folder)
    if fp == personal_fingerprint(folder):
        cert, key = sealing.load_identity(folder)
        return cert, key, []
    if key_state(folder, fp) == "missing":
        raise sealing.SealError(GONE)
    blocks = _pem_blocks((folder / (fp + CRT)).read_bytes())
    cert = x509.Certificate.load(blocks[0])
    name = cert.subject.native.get("common_name") or cert.subject.human_friendly
    try:
        der = sealing.unprotect((folder / (fp + KEY)).read_bytes())
    except (OSError, ValueError) as e:
        raise sealing.SealError(IMPORTED_FOREIGN.format(name=name)) from e
    expires = cert["tbs_certificate"]["validity"]["not_after"].native
    if expires < datetime.datetime.now(datetime.timezone.utc):
        raise sealing.SealError(f"Il certificato «{name}» è scaduto il {_date(expires)}.")
    return cert, keys.PrivateKeyInfo.load(der), [x509.Certificate.load(b) for b in blocks[1:]]
