"""Avvio: python -m piedf [file.pdf]"""

import multiprocessing
import sys

# La guardia serve al processo di render: con "spawn" il figlio reimporta questo modulo.
if __name__ == "__main__":
    # Nell'exe il processo di render rilancia l'exe stesso: freeze_support() lo intercetta
    # prima che si importino Qt e la finestra.
    multiprocessing.freeze_support()

    if sys.argv[1:2] == ["--piedf-sigilla"]:  # solo per tests/check_exe.py, senza finestra
        from piedf.sealing import cli

        sys.exit(cli(sys.argv[2:]))

    from piedf.app import main

    sys.exit(main(sys.argv))
