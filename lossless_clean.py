# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Nivel «baja» de Comprimir: limpieza SIN PÉRDIDA, sin Ghostscript.

No recodifica, no reduce ni toca ninguna imagen, ni los flujos de contenido de las páginas:
- capa Flate sobre los JPEG que así ocupen menos (jpeg_flate.wrap_jpegs: los bytes del JPEG
  no cambian);
- subconjunto de las fuentes TrueType completas (subset_fonts de MuPDF), con las salvedades
  de abajo;
- guardado con garbage=4 (quita objetos sin usar y une los repetidos, p. ej. la misma fuente
  o el mismo logo en cada escrito de un expediente unido), Flate para los flujos sin
  comprimir, flujos de objetos y zlib al máximo (compression_effort=100). Sin `clean`: el
  filtro de contenido de MuPDF 1.28.2 rompe los operadores ' y " (ver redact.py).

Fuentes que NO se recortan (se ocultan a subset_fonts y se dejan byte a byte como estaban):
- Type1 (FontFile), CFF y OpenType (FontFile3): MuPDF 1.28.2 no recorta su programa (ocupa lo
  mismo), pero en las Type1 simples pone a 0 los /Widths de los caracteres que no se usan y
  les añade un prefijo de subconjunto falso. Las Type3 no tienen programa: no cambian.
- las del formulario (/AcroForm /DR): un campo que se rellene después necesita la fuente
  entera;
- las que comparten el flujo del programa con una de las anteriores.
De las recortadas se quita /CIDSet: MuPDF no lo actualiza y diría que hay glifos que ya no
están.

Comprobación: si alguna fuente se ha recortado (ha cambiado su nombre o su programa), el texto extraíble de cada página tiene que
ser idéntico al de la entrada; si no lo es, o si subset_fonts falla, se guarda otra vez sin
recortar ninguna.

PDF/A (pdfaid en el XMP, pdf_check.pdfa): sin recorte de fuentes en ninguna parte (el /CIDSet
y los /Widths dejarían de cuadrar con el programa de la fuente) y, en PDF/A-1, sin flujos de
objetos (ISO 19005-1, 6.1.4: no admite flujos de referencias; veraPDF lo rechaza). El
metadato XMP queda sin comprimir (MuPDF no comprime los /Metadata), como pide PDF/A.

Uso: python3 lossless_clean.py <entrada.pdf> <salida.pdf>
"""
import re
import sys

import pymupdf

import jpeg_flate
import pdf_check

FONT_FILES = ("FontFile", "FontFile2", "FontFile3")
_REF = re.compile(r"(\d+) 0 R")


class Encrypted(Exception):
    """La entrada necesita contraseña de apertura: no se procesa (código 3)."""


def _refs(value):
    return [int(n) for n in _REF.findall(value)]


def _descriptor(doc, font):
    """xref del /FontDescriptor de un diccionario de fuente (también de una Type0, por su
    DescendantFonts), o None."""
    typ, value = doc.xref_get_key(font, "FontDescriptor")
    if typ == "xref":
        return _refs(value)[0]
    typ, value = doc.xref_get_key(font, "DescendantFonts")
    if typ == "array" and _refs(value):
        typ, value = doc.xref_get_key(_refs(value)[0], "FontDescriptor")
        if typ == "xref":
            return _refs(value)[0]
    return None


def _form_descriptors(doc):
    """Descriptores de las fuentes de /AcroForm /DR /Font."""
    typ, value = doc.xref_get_key(doc.pdf_catalog(), "AcroForm/DR/Font")
    if typ != "dict":
        return set()
    return {d for d in (_descriptor(doc, f) for f in _refs(value)) if d is not None}


def _program(doc, descriptor):
    """(clave, xref) del programa de fuente del descriptor, o None si no está incrustado."""
    for k in FONT_FILES:
        typ, value = doc.xref_get_key(descriptor, k)
        if typ == "xref":
            return k, _refs(value)[0]
    return None


def subset_truetype(doc):
    """Recorta las TrueType completas de `doc` (en memoria). Devuelve cuántas ha recortado."""
    form = _form_descriptors(doc)
    programs = {}
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "Type")[1] == "/FontDescriptor" and _program(doc, xref):
            programs[xref] = _program(doc, xref)
    # No se recortan: otro tipo de programa, las del formulario y cualquiera que comparta su
    # programa con una de esas (al recortar el flujo compartido, también se recortaría la otra).
    kept = {x for x, (k, _) in programs.items() if k != "FontFile2" or x in form}
    kept_programs = {programs[x][1] for x in kept}
    kept |= {x for x, (_, prog) in programs.items() if prog in kept_programs}
    before = {x: (doc.xref_get_key(x, "FontName")[1], len(doc.xref_stream_raw(prog) or b""))
              for x, (_, prog) in programs.items() if x not in kept}
    # Sin su programa, subset_fonts la toma por no incrustada y no la toca.
    hidden = {x: doc.xref_object(x, compressed=True) for x in kept}
    for x in kept:
        doc.xref_set_key(x, programs[x][0], "null")
    try:
        if before:
            doc.subset_fonts()
    finally:
        for xref, obj in hidden.items():
            doc.update_object(xref, obj)
    done = []
    for x, (name, size) in before.items():
        prog = _program(doc, x)
        if (doc.xref_get_key(x, "FontName")[1], len(doc.xref_stream_raw(prog[1]) or b"") if prog else -1) != (name, size):
            done.append(x)
    for xref in done:
        if doc.xref_get_key(xref, "CIDSet")[0] != "null":
            doc.xref_set_key(xref, "CIDSet", "null")
    return len(done)


def page_texts(path):
    with pymupdf.open(path) as doc:
        return [" ".join(page.get_text().split()) for page in doc]


def _save(input_path, output_path, subset, object_streams):
    """Una pasada. Devuelve cuántas fuentes ha recortado."""
    doc = pymupdf.open(input_path)
    jpeg_flate.wrap_jpegs(doc)
    subsetted = subset_truetype(doc) if subset else 0
    doc.save(output_path, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True,
             use_objstms=1 if object_streams else 0, compression_effort=100)
    doc.close()
    return subsetted


def clean(input_path, output_path):
    """Escribe la limpieza en output_path. Devuelve un dict para el log y los tests:
    {"pdfa": "1B" | None, "fonts_subset": n, "object_streams": bool, "subset_reverted": bool}."""
    with pymupdf.open(input_path) as doc:
        if pdf_check.needs_password(doc):
            raise Encrypted()
        pdfa = pdf_check.pdfa(doc)
    object_streams = not (pdfa and pdfa["part"] == 1)
    result = {"pdfa": pdf_check.pdfa_label(pdfa), "fonts_subset": 0,
              "object_streams": object_streams, "subset_reverted": False}
    if pdfa is None:
        try:
            result["fonts_subset"] = _save(input_path, output_path, True, object_streams)
            if result["fonts_subset"] == 0 or page_texts(output_path) == page_texts(input_path):
                return result
        except Exception:  # noqa: BLE001 - subset_fonts es «nuevo, puede tener errores»
            pass
        result.update(fonts_subset=0, subset_reverted=True)
    _save(input_path, output_path, False, object_streams)
    return result


def main(argv):
    if len(argv) != 3:
        print("Uso: python3 lossless_clean.py <entrada.pdf> <salida.pdf>", file=sys.stderr)
        return 2
    try:
        result = clean(argv[1], argv[2])
    except Encrypted:
        print("PDF con contraseña de apertura", file=sys.stderr)
        return pdf_check.EXIT_ENCRYPTED
    if result["subset_reverted"]:
        print("Recorte de fuentes descartado: el texto cambiaba o subset_fonts falló", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
