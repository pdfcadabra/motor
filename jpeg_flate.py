# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Vuelve a envolver en Flate las imágenes JPEG de un PDF, sin pérdida.

Ghostscript (pdfwrite) copia los JPEG tal cual pero les quita la capa Flate
exterior (Filter [/FlateDecode /DCTDecode]). En los escaneos de juzgado esa capa
reduce mucho los fondos JPEG, así que sin ella el PDF "comprimido" pesa más que el
original. Este paso la restaura: comprime con zlib el JPEG ya codificado (los
píxeles no cambian) y solo lo sustituye si así ocupa menos. No toca nada más del
documento.

Con --originals, antes vuelve a poner las imágenes que lossless_images.py sustituyó por
marcadores antes de Ghostscript, copiándolas del PDF de entrada (sin marcadores).

Uso: python3 jpeg_flate.py <entrada.pdf> <salida.pdf> [--originals <original.pdf>]
"""
import sys
import zlib

import pymupdf

import lossless_images

# Las imágenes protegidas no se han podido restaurar (cualquier lossless_images.RestoreError:
# un marcador sin restaurar, uno cuya imagen original no existe o una imagen demasiado anidada
# para copiarla): no se escribe la salida y server.js devuelve el original sin tocar.
EXIT_RESTORE_FAILED = 4


def main(input_path, output_path, originals_path=None):
    doc = pymupdf.open(input_path)
    if originals_path:
        lossless_images.restore(doc, pymupdf.open(originals_path))
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "Subtype") != ("name", "/Image"):
            continue
        filter_type, filter_value = doc.xref_get_key(xref, "Filter")
        if not (
            (filter_type == "name" and filter_value == "/DCTDecode")
            or (filter_type == "array" and filter_value.replace(" ", "") == "[/DCTDecode]")
        ):
            continue

        jpeg = doc.xref_stream_raw(xref)
        wrapped = zlib.compress(jpeg, 9)
        if len(wrapped) >= len(jpeg):
            continue

        # Los DecodeParms van por filtro: el nuevo /FlateDecode no lleva (null).
        parms_type, parms_value = doc.xref_get_key(xref, "DecodeParms")
        doc.update_stream(xref, wrapped, compress=False)
        doc.xref_set_key(xref, "Filter", "[/FlateDecode /DCTDecode]")
        if parms_type == "dict":
            doc.xref_set_key(xref, "DecodeParms", f"[null {parms_value}]")
        elif parms_type == "array":
            doc.xref_set_key(xref, "DecodeParms", "[null " + parms_value.strip()[1:])

    doc.save(output_path)


if __name__ == "__main__":
    args = sys.argv[1:]
    originals = None
    if len(args) == 4 and args[2] == "--originals":
        originals = args[3]
        args = args[:2]
    if len(args) != 2:
        print("Uso: python3 jpeg_flate.py <entrada.pdf> <salida.pdf> [--originals <original.pdf>]", file=sys.stderr)
        sys.exit(2)
    try:
        main(args[0], args[1], originals)
    except lossless_images.RestoreError as e:
        # Solo el tipo de fallo: ni rutas ni datos del documento.
        print(f"No se pueden restaurar las imágenes protegidas ({type(e).__name__})", file=sys.stderr)
        sys.exit(EXIT_RESTORE_FAILED)
