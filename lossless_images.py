# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Imágenes que Comprimir no puede tocar con pérdida: códigos de barras, QR, sellos.

Ghostscript (pdfwrite) solo distingue color, gris y blanco y negro, y con los niveles
recomendada y extrema reduce y pasa a JPEG todo lo que no es 1 bit en DeviceGray: una barra
de 1 bit en ICCBased o Indexed salía como JPEG RGB reducido a la mitad o a un tercio. En los
documentos judiciales esas imágenes suelen ser el código de barras o el QR del CSV, que debe
seguir siendo escaneable.

Se protegen (sin pérdida y sin reducir, sea cual sea el nivel):
- las de 1 bit (DeviceGray, ICCBased, Indexed de 2 colores, ImageMask...);
- las Indexed de hasta 16 colores;
- las de 8 bits sin JPEG (Flate, LZW, sin filtro...) que usan hasta 16 colores: un PNG con
  paleta o un QR en gris insertado por otra herramienta suele acabar así en el PDF. También
  las Indexed con una paleta mayor (hival de 16 o más) que de verdad usan hasta 16 índices:
  sellos, logos o gráficos guardados con una paleta de 256 entradas, a veces con el QR
  dentro, que Ghostscript reducía (en extrema, un QR de módulos de 2 px dejaba de leerse).

Cómo: `protect` sustituye cada una por una máscara diminuta (ImageMask de 64x2) que lleva
su número de objeto, Ghostscript la copia sin tocarla (sin pérdida, sin reducir: la salida
de B/N no se reduce) y `restore` vuelve a poner en su lugar la imagen original, con su
flujo comprimido tal cual. Las máscaras de las imágenes (/SMask, /Mask) no se sustituyen.

Uso: python3 lossless_images.py protect <entrada.pdf> <salida.pdf>
     (escribe en stdout cuántas protegió; con 0 no escribe la salida)
     La restauración la hace jpeg_flate.py con --originals.
"""
import re
import sys

import pymupdf

import pdf_check

MAX_COLORS = 16
# Las de 8 bits se decodifican para contar colores: por encima de esto no compensa.
MAX_COUNT_PIXELS = 40_000_000
MAGIC = b"PdCq"  # cabecera del marcador (4 bytes)
MARK_W, MARK_H = 64, 2
LOSSY_FILTERS = ("DCTDecode", "JPXDecode")


# Niveles de referencias que sigue _copy al copiar una imagen original y lo que referencia
# (espacio de color, perfil ICC, máscara...: pocos). Una cadena de cientos agotaba la pila de
# Python (RecursionError hacia los 330 niveles, y /v1/compress respondía 500).
MAX_NESTING = 200


class RestoreError(Exception):
    pass


class NestingTooDeep(RestoreError):
    """La imagen original referencia una cadena de más de MAX_NESTING objetos: no se copia."""


def _key(doc, xref, name):
    return doc.xref_get_key(xref, name)


def _resolve(doc, typ, value):
    """Valor de una clave siguiendo una referencia indirecta (como texto)."""
    if typ == "xref":
        return doc.xref_object(int(value.split()[0]), compressed=True)
    return value


def _is_image(doc, xref):
    try:
        return _key(doc, xref, "Subtype") == ("name", "/Image")
    except Exception:
        return False


def _mask_xrefs(doc, images):
    """Imágenes usadas como /SMask o /Mask de otras: se quedan como están."""
    masks = set()
    for xref in images:
        for name in ("SMask", "Mask"):
            typ, value = _key(doc, xref, name)
            if typ == "xref":
                masks.add(int(value.split()[0]))
    return masks


def _indexed_hival(doc, xref):
    """hival si el espacio de color es Indexed; None si no."""
    cs = _resolve(doc, *_key(doc, xref, "ColorSpace"))
    m = re.match(r"\s*\[\s*/(?:Indexed|I)\b\s*(?:/\w+|\[[^\]]*\]|\d+ 0 R)\s*(\d+)", cs)
    return int(m.group(1)) if m else None


def _few_indices(doc, xref):
    """Indexed de 8 bits con paleta grande: ¿usa hasta MAX_COLORS índices? Cada índice es un
    color como mucho. Busca cada valor en los datos decodificados y para en cuanto pasa de
    MAX_COLORS: una foto con paleta sale enseguida, sin decodificar la imagen a RGB (con
    Pixmap y color_count, Comprimir tardaba un 20-27 % más en un PDF de dos imágenes así)."""
    try:
        data = doc.xref_stream(xref)
    except Exception:
        return False
    if not data:
        return False
    used = 0
    for i in range(256):
        if bytes((i,)) in data:
            used += 1
            if used > MAX_COLORS:
                return False
    return True


def is_protected(doc, xref):
    if _key(doc, xref, "ImageMask")[1] == "true":
        return True
    if _key(doc, xref, "BitsPerComponent")[1] == "1":
        return True
    hival = _indexed_hival(doc, xref)
    if hival is not None and hival < MAX_COLORS:
        return True
    if hival is not None and _key(doc, xref, "BitsPerComponent")[1] == "8":
        return _few_indices(doc, xref)
    filters = _key(doc, xref, "Filter")[1]
    if any(f in filters for f in LOSSY_FILTERS):
        return False
    try:
        width = int(_key(doc, xref, "Width")[1])
        height = int(_key(doc, xref, "Height")[1])
    except ValueError:
        return False
    if width * height > MAX_COUNT_PIXELS:
        return False
    try:
        pix = pymupdf.Pixmap(doc, xref)
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
        return pix.color_count() <= MAX_COLORS
    except Exception:
        return False


def marker_bytes(xref):
    row = MAGIC + xref.to_bytes(4, "big")
    return row + bytes(255 - b for b in row)


def marker_id(data):
    """Número de objeto de un marcador decodificado (también invertido), o None."""
    if len(data) != len(marker_bytes(0)):
        return None
    for d in (data, bytes(255 - b for b in data)):
        if d[:4] == MAGIC and d == marker_bytes(int.from_bytes(d[4:8], "big")):
            return int.from_bytes(d[4:8], "big")
    return None


def protect(doc):
    """Sustituye en `doc` las imágenes protegidas por marcadores. Devuelve sus xrefs."""
    images = [x for x in range(1, doc.xref_length()) if _is_image(doc, x)]
    masks = _mask_xrefs(doc, images)
    protected = [x for x in images if x not in masks and is_protected(doc, x)]
    for xref in protected:
        doc.update_object(
            xref,
            f"<< /Type /XObject /Subtype /Image /Width {MARK_W} /Height {MARK_H} "
            "/ImageMask true /BitsPerComponent 1 >>",
        )
        doc.update_stream(xref, marker_bytes(xref), compress=False)
    return protected


_TOKEN = re.compile(rb"\((?:\\.|[^\\)])*\)|<(?!<)[0-9A-Fa-f\s]*>|(\d+) 0 R")


def _copy(src, dst, xref, memo, depth):
    """Copia el objeto `xref` de `src` (y todo lo que referencia) a `dst`. Devuelve el xref nuevo.
    NestingTooDeep si hay que seguir más de MAX_NESTING referencias: nunca se copia a medias."""
    if xref not in memo:
        if depth > MAX_NESTING:
            raise NestingTooDeep(f"más de {MAX_NESTING} referencias anidadas")
        memo[xref] = dst.get_new_xref()
        dst.update_object(memo[xref], "null")
        _put(src, dst, xref, memo[xref], memo, depth)
    return memo[xref]


def _put(src, dst, xref, target, memo, depth=0):
    """Escribe en `dst[target]` el objeto `src[xref]`, con el flujo comprimido tal cual."""
    obj = src.xref_object(xref, compressed=True).encode("latin-1")

    def repl(m):
        if m.group(1) is None:
            return m.group(0)
        return b"%d 0 R" % _copy(src, dst, int(m.group(1)), memo, depth + 1)

    text = _TOKEN.sub(repl, obj).decode("latin-1")
    if src.xref_is_stream(xref):
        # update_stream(compress=False) quita /Filter y /DecodeParms: el diccionario va después
        # (con la misma /Length, porque los bytes son los mismos).
        dst.update_object(target, "<<>>")
        dst.update_stream(target, src.xref_stream_raw(xref), compress=False)
    dst.update_object(target, text)


def _try_g4(doc, xref):
    """Imagen de 1 bit en Flate o sin comprimir -> CCITT G4, si ocupa menos y los bits
    decodificados son idénticos (Ghostscript también las pasaba a CCITT)."""
    filters = _key(doc, xref, "Filter")
    if "CCITT" in filters[1] or "JBIG2" in filters[1] or _key(doc, xref, "BitsPerComponent")[1] != "1":
        return
    width = int(_key(doc, xref, "Width")[1])
    height = int(_key(doc, xref, "Height")[1])
    raw = doc.xref_stream(xref)
    stride = (width + 7) // 8
    if not raw or len(raw) != stride * height:
        return
    buf = pymupdf.mupdf.fz_compress_ccitt_fax_g4(pymupdf.mupdf.python_buffer_data(raw), width, height, stride)
    encoded = buf.fz_buffer_extract()
    old = doc.xref_stream_raw(xref)
    if len(encoded) >= len(old):
        return
    parms = _key(doc, xref, "DecodeParms")
    doc.update_stream(xref, encoded, compress=False)
    doc.xref_set_key(xref, "Filter", "/CCITTFaxDecode")
    doc.xref_set_key(xref, "DecodeParms", f"<</K -1 /Columns {width} /Rows {height}>>")
    if doc.xref_stream(xref) != raw:  # no debería pasar: se deja como estaba
        doc.update_stream(xref, old, compress=False)
        doc.xref_set_key(xref, "Filter", filters[1])
        doc.xref_set_key(xref, "DecodeParms", parms[1])


def _check_no_markers(doc):
    """Ningún marcador puede quedar en la salida: ni como imagen ni dentro de un contenido
    (imagen en línea). Si Ghostscript hubiese transformado alguno, la página tendría una
    mancha en lugar del código de barras."""
    for xref in range(1, doc.xref_length()):
        try:
            if not doc.xref_is_stream(xref) or _is_image(doc, xref):
                continue
            data = doc.xref_stream(xref) or b""
        except Exception:
            continue
        if MAGIC in data:
            raise RestoreError("queda un marcador sin restaurar")


def restore(doc, original):
    """Pone en `doc` (salida de Ghostscript) las imágenes originales en lugar de los marcadores.

    Devuelve cuántas restauró. RestoreError si un marcador no se reconoce o queda alguno.
    """
    memo = {}
    restored = 0
    for xref in range(1, doc.xref_length()):
        if not _is_image(doc, xref) or _key(doc, xref, "ImageMask")[1] != "true":
            continue
        if (_key(doc, xref, "Width")[1], _key(doc, xref, "Height")[1]) != (str(MARK_W), str(MARK_H)):
            continue
        orig = marker_id(doc.xref_stream(xref) or b"")
        if orig is None:
            continue  # una máscara de 64x2 de verdad
        if not (0 < orig < original.xref_length()) or not _is_image(original, orig):
            raise RestoreError(f"marcador con un objeto que no es una imagen ({orig})")
        _put(original, doc, orig, xref, memo)
        _try_g4(doc, xref)
        restored += 1
    _check_no_markers(doc)
    return restored


def main(argv):
    if len(argv) != 4 or argv[1] != "protect":
        print("Uso: python3 lossless_images.py protect <entrada.pdf> <salida.pdf>", file=sys.stderr)
        return 2
    doc = pymupdf.open(argv[2])
    # Con contraseña de apertura no se procesa nada: server.js responde 422 PDF_ENCRYPTED. Antes
    # se daba por hecho que Ghostscript fallaría, pero sale con 0 y escribe una página en blanco.
    # Los de solo contraseña de propietario se abren con la vacía y siguen como cualquier otro.
    if pdf_check.needs_password(doc):
        print("PDF con contraseña de apertura", file=sys.stderr)
        return pdf_check.EXIT_ENCRYPTED
    protected = protect(doc)
    if protected:
        doc.save(argv[3])
    print(len(protected))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
