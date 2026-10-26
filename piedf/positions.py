import json
import os
import time
from pathlib import Path

MAX_ENTRIES = 500
FITS = ("width", "page", None)


def file_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


class Positions:
    """Ultima posizione di lettura di ogni file (pagina, punto nella pagina, zoom), in un JSON."""

    def __init__(self, file: Path) -> None:
        self._file = file
        self._entries: dict[str, dict] = {}
        self._load()

    def get(self, path: str) -> dict | None:
        return self._entries.get(file_key(path))

    def set(self, path: str, page: int, y: float, fit: str | None, zoom: float) -> None:
        key = file_key(path)
        self._entries.pop(key, None)  # in coda: l'ordine di inserimento segue `visto`
        self._entries[key] = {"pagina": page, "y": y, "fit": fit, "zoom": zoom, "visto": time.time()}
        if len(self._entries) > MAX_ENTRIES:
            # Sort stabile: a parità di `visto` si tolgono i più vecchi per inserimento.
            newest = sorted(self._entries.items(), key=lambda item: item[1]["visto"])[-MAX_ENTRIES:]
            self._entries = dict(newest)
        self._write()

    def same(self, path: str, page: int, y: float, fit: str | None, zoom: float) -> bool:
        """True se la posizione salvata per `path` è già questa (si evita di riscrivere il file)."""
        entry = self.get(path)
        return (
            entry is not None and entry["pagina"] == page and entry["fit"] == fit
            and abs(entry["y"] - y) < 0.01 and abs(entry["zoom"] - zoom) < 1e-6
        )

    def clear(self) -> None:
        self._entries = {}
        self._write()

    def reload(self) -> None:
        """Rilegge il file (dopo aver copiato i dati di un'altra versione)."""
        self._entries = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("formato non valido")
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            # Illeggibile: si riparte da vuoto e si riscrive un file valido.
            self._write()
            return
        for key, entry in data.items():
            entry = _valid(entry)
            if entry is not None:
                self._entries[key] = entry
        self._entries = dict(sorted(self._entries.items(), key=lambda item: item[1]["visto"]))

    def _write(self) -> None:
        tmp = self._file.with_name(self._file.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._entries, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._file)
        except OSError:
            pass  # le posizioni sono comode, non vale un errore all'utente


def _valid(entry) -> dict | None:
    try:
        page, y, zoom, seen = int(entry["pagina"]), float(entry["y"]), float(entry["zoom"]), float(entry["visto"])
        fit = entry["fit"]
    except (KeyError, TypeError, ValueError):
        return None
    if fit not in FITS or page < 0 or not zoom > 0 or y != y:
        return None
    return {"pagina": page, "y": y, "fit": fit, "zoom": zoom, "visto": seen}
