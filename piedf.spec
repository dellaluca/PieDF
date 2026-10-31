# -*- mode: python -*-
# Spec di PyInstaller per l'exe portatile di PieDF (#8). Si lancia con `python tools/build_exe.py`.
#
#   pyinstaller piedf.spec                 -> dist/PieDF/PieDF.exe (onedir, quello distribuito)
#   pyinstaller piedf.spec -- --onefile    -> dist/PieDF.exe (solo per confronto)
#
# Si sceglie onedir: il onefile si scompatta in una cartella temporanea a ogni avvio
# e il processo di render rilancia l'exe, quindi parte più lento.
import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH)
ONEFILE = "--onefile" in sys.argv[1:]

VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "piedf" / "__init__.py").read_text()).group(1)
NUMBERS = tuple((list(map(int, VERSION.split("."))) + [0, 0, 0, 0])[:4])

# Moduli Qt che PieDF non usa (solo QtCore, QtGui, QtWidgets e QtPrintSupport per la stampa)
# e pacchetti Python inutili.
EXCLUDES = [
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtTest", "PySide6.QtSql",
    "PySide6.QtDBus", "PySide6.QtConcurrent", "PySide6.QtXml",
    "PySide6.QtHelp", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "tkinter", "unittest", "pydoc", "numpy", "PIL",
    # Analisi del layout di PyMuPDF (opzionale, con onnxruntime): PieDF non la usa.
    "pymupdf.layout", "onnxruntime", "yaml", "pandas", "matplotlib", "fontTools",
    # pyHanko (#70): client HTTP asincrono (al suo posto un modulo vuoto, vedi sealing._stub_aiohttp:
    # la marca temporale usa urllib), XML/XAdES e il vecchio backend crittografico non servono al sigillo.
    "aiohttp", "aiohappyeyeballs", "aiosignal", "frozenlist", "multidict", "yarl", "propcache",
    "lxml", "oscrypto", "requests",
]

# Librerie Qt che gli hook di PyInstaller tirano dentro comunque (tramite i plugin).
# Attenzione a non togliere dipendenze di QtGui (su Linux, per esempio, QtDBus).
QT_LIBS_UNUSED = ("Network", "Qml", "Quick", "Pdf", "Svg", "OpenGL", "VirtualKeyboard")
IMAGE_PLUGINS_KEPT = ("qjpeg", "qico", "qpng")  # il PNG è comunque già dentro QtGui


def keep(dest: str) -> bool:
    p = dest.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    if re.match(r"(lib)?Qt6(%s)" % "|".join(QT_LIBS_UNUSED), name):
        return False
    if name.startswith("opengl32sw"):  # OpenGL software (~20 MB): i widget non lo usano
        return False
    if "/imageformats/" in p:
        return name.removeprefix("lib").startswith(IMAGE_PLUGINS_KEPT)
    if "/translations/" in p:
        return name == "qtbase_it.qm"
    # Plugin legati a moduli esclusi (QML, rete, SVG, tastiera virtuale, TUIO).
    if re.search(r"/plugins/(qmltooling|networkinformation|tls|iconengines|virtualkeyboard"
                 r"|platforminputcontexts|generic)/", p):
        return False
    if p.startswith(("tests/", "piedf/tests/")):
        return False
    return True


DATAS = [(str(ROOT / "piedf" / "assets"), "piedf/assets"), (str(ROOT / "LICENSE"), ".")]
HIDDEN = []
if sys.platform == "win32":
    # pyHanko firma con l'ora locale (tzlocal → zoneinfo): su Windows i fusi orari vengono dal
    # pacchetto tzdata, che zoneinfo carica per nome (l'analisi di PyInstaller non lo vede).
    from PyInstaller.utils.hooks import collect_data_files, collect_submodules
    DATAS += collect_data_files("tzdata")
    HIDDEN += collect_submodules("tzdata")

a = Analysis(
    [str(ROOT / "piedf" / "__main__.py")],
    pathex=[str(ROOT)],
    datas=DATAS,
    hiddenimports=HIDDEN,
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
a.binaries = [t for t in a.binaries if keep(t[0])]
a.datas = [t for t in a.datas if keep(t[0])]

pyz = PYZ(a.pure)


def version_info():
    """Informazioni di versione dell'exe (Proprietà → Dettagli); `None` fuori da Windows."""
    if sys.platform != "win32":
        return None
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
    )
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=NUMBERS, prodvers=NUMBERS),
        kids=[
            StringFileInfo([StringTable("041004B0", [  # italiano, Unicode
                StringStruct("ProductName", "PieDF"),
                StringStruct("FileDescription", "PieDF - lettore PDF con firma"),
                StringStruct("FileVersion", VERSION),
                StringStruct("ProductVersion", VERSION),
                StringStruct("InternalName", "PieDF"),
                StringStruct("OriginalFilename", "PieDF.exe"),
                StringStruct("LegalCopyright", "PieDF contributors - AGPL-3.0"),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0410, 1200])]),
        ],
    )


exe_options = dict(
    name="PieDF",
    icon=str(ROOT / "piedf" / "assets" / "piedf.ico"),
    version=version_info(),
    console=False,  # applicazione a finestra: nessun prompt dei comandi
    upx=False,  # UPX fa scattare gli antivirus e rallenta l'avvio
    debug=False,
    strip=False,
)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **exe_options)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_options)
    coll = COLLECT(exe, a.binaries, a.datas, name="PieDF", upx=False, strip=False)
