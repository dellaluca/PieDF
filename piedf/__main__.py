"""Avvio: python -m piedf [file.pdf]"""

import multiprocessing
import sys

from piedf.app import main

# La guardia serve al processo di render: con "spawn" il figlio reimporta questo modulo.
if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main(sys.argv))
