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
- signed(doc): el PDF lleva alguna firma electrónica (un diccionario de firma con /ByteRange,
  también como valor de un campo de formulario /FT /Sig). Cualquier reescritura la invalida.
- pdfa(doc): la parte y el nivel de PDF/A que declaran los metadatos XMP (pdfaid), o None.
- python3 pdf_check.py info <entrada.pdf> <informe.json>: escribe {"signed": bool, "pdfa":
  "1B" | ... | null}. Sale con EXIT_ENCRYPTED si la entrada necesita contraseña (server.js lo
  lanza lo primero en Comprimir: con la baja ya no hay otro paso antes que lo detecte).
"""
import json
import re
import sys

import pymupdf

EXIT_ENCRYPTED = 3  # server.js responde 422 PDF_ENCRYPTED
EXIT_INVALID = 4  # server.js no entrega la salida


def needs_password(doc):
    """True si el PDF solo se abre con una contraseña de apertura que no tenemos."""
    return bool(doc.needs_pass) and not doc.authenticate("")


def signed(doc):
    """True si hay alguna firma (o sello de tiempo de documento) aplicada: un diccionario con
    /ByteRange y /Contents, sea un objeto propio o el /V directo de un campo /FT /Sig. Un campo
    de firma vacío (sin /V) no cuenta."""
    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(xref, "ByteRange")[0] == "array":
                if doc.xref_get_key(xref, "Contents")[0] != "null":
                    return True
            elif doc.xref_get_key(xref, "FT")[1] == "/Sig":
                if doc.xref_get_key(xref, "V/ByteRange")[0] == "array":
                    return True
        except Exception:  # noqa: BLE001 - un objeto dañado no es una firma
            continue
    return False


PDFAID_NS = "http://www.aiim.org/pdfa/ns/id/"


def pdfa(doc):
    """{"part": 1, "conformance": "B"} según el XMP del catálogo (pdfaid:part y
    pdfaid:conformance, como atributo o como elemento, con el prefijo que tenga el espacio de
    nombres), o None si no declara PDF/A. Solo lo que declara: no valida la conformidad."""
    try:
        xmp = doc.get_xml_metadata() or ""
    except Exception:  # noqa: BLE001
        return None
    prefixes = set(re.findall(r'xmlns:([\w.-]+)\s*=\s*["\']' + re.escape(PDFAID_NS) + '["\']', xmp))
    for prefix in prefixes:
        def value(name):
            p = re.escape(f"{prefix}:{name}")
            m = (re.search(p + r'\s*=\s*["\']\s*([^"\'<\s]+)\s*["\']', xmp)
                 or re.search("<" + p + r">\s*([^<\s]+)\s*</" + p + ">", xmp))
            return m.group(1) if m else None
        part = value("part")
        if part and part.isdigit():
            conformance = value("conformance")
            return {"part": int(part), "conformance": conformance.upper() if conformance else None}
    return None


def pdfa_label(info):
    """"1B", "2U", "3"... para el informe; None si no es PDF/A."""
    return None if info is None else f"{info['part']}{info['conformance'] or ''}"


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


def info(input_path, report_path):
    doc = pymupdf.open(input_path)
    if needs_password(doc):
        print("PDF con contraseña de apertura", file=sys.stderr)
        return EXIT_ENCRYPTED
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"signed": signed(doc), "pdfa": pdfa_label(pdfa(doc))}, f)
    return 0


def main(argv):
    if len(argv) == 4 and argv[1] == "info":
        return info(argv[2], argv[3])
    if len(argv) != 4 or argv[1] != "verify":
        print("Uso: python3 pdf_check.py verify <entrada.pdf> <salida.pdf>\n"
              "     python3 pdf_check.py info <entrada.pdf> <informe.json>", file=sys.stderr)
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
