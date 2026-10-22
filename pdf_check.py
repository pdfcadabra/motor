# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Comprobaciones de PDF que comparten server.js, lossless_images.py y redact.py.

- needs_password(doc): el PDF necesita contraseña de apertura. Los de solo contraseña de
  propietario (se abren sin clave, con permisos restringidos, como muchos de sedes judiciales)
  se abren con la contraseña vacía y NO cuentan: se siguen procesando.
- python3 pdf_check.py verify <entrada.pdf> <salida.pdf>: red de seguridad de Comprimir antes
  de entregar el resultado. Sale con 0 si la salida se abre sin errores, tiene las mismas
  páginas que la entrada y ninguna página con contenido ha quedado vacía; con EXIT_ENCRYPTED si
  la entrada necesita contraseña; con EXIT_INVALID en cualquier otro caso (server.js devuelve
  entonces el original sin tocar). Motivo: con un PDF con contraseña de apertura, Ghostscript
  sale con 0 y escribe una página en blanco, que llegaba al usuario como "comprimido".
"""
import sys

import pymupdf

EXIT_ENCRYPTED = 3  # server.js responde 422 PDF_ENCRYPTED
EXIT_INVALID = 4  # server.js no entrega la salida


def needs_password(doc):
    """True si el PDF solo se abre con una contraseña de apertura que no tenemos."""
    return bool(doc.needs_pass) and not doc.authenticate("")


def _has_content(page):
    return bool(page.get_images(full=False)) or bool(page.get_text("text").strip())


def verify(input_path, output_path):
    """Motivo por el que la salida no vale (texto para el log, sin rutas), o None si vale."""
    src = pymupdf.open(input_path)
    if needs_password(src):
        return "encrypted"
    try:
        out = pymupdf.open(output_path)
    except Exception as e:  # noqa: BLE001 - cualquier fallo al abrir invalida la salida
        return f"la salida no se abre ({type(e).__name__})"
    if needs_password(out):
        return "la salida está cifrada"
    if out.page_count != src.page_count or out.page_count == 0:
        return f"páginas: entrada {src.page_count}, salida {out.page_count}"
    for i in range(out.page_count):
        try:
            page = out.load_page(i)
            empty = not _has_content(page)
        except Exception as e:  # noqa: BLE001
            return f"la página {i + 1} de la salida no se abre ({type(e).__name__})"
        if empty and _has_content(src.load_page(i)):
            return f"la página {i + 1} ha quedado vacía"
    return None


def main(argv):
    if len(argv) != 4 or argv[1] != "verify":
        print("Uso: python3 pdf_check.py verify <entrada.pdf> <salida.pdf>", file=sys.stderr)
        return 2
    try:
        problem = verify(argv[2], argv[3])
    except Exception as e:  # noqa: BLE001 - p. ej. la entrada no se abre
        problem = f"no se pudo comprobar ({type(e).__name__})"
    if problem == "encrypted":
        print("PDF con contraseña de apertura", file=sys.stderr)
        return EXIT_ENCRYPTED
    if problem:
        print(f"Resultado de la compresión no válido: {problem}", file=sys.stderr)
        return EXIT_INVALID
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
