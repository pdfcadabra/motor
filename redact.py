# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
import bisect
import contextlib
import hashlib
import io
import json
import math
import os
import re
import struct
import sys
import unicodedata
import uuid
import zlib
from collections import Counter, namedtuple

import pymupdf as fitz
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat, JpegImagePlugin

import detectores
import pdf_check

fitz.TOOLS.mupdf_display_errors(False)

# Metadatos que escribimos en la salida: la verificación final no los cuenta como fuga
# aunque un término censurado coincida con ellos.
OUTPUT_METADATA = {"creator": "PDFcadabra", "producer": "PDFcadabra LegalTech"}

# Términos más cortos no se buscan fuera del texto de página (metadatos, marcadores...):
# darían falsos positivos ("de", "y"). El texto de página se comprueba por geometría.
MIN_TERM_LEN = 3

MANUAL_CATEGORY = "Censura Manual"

# search con patrones que no son un objeto de cadenas no vacías: server.js responde 400.
EXIT_BAD_PATTERNS = 4


class BadPatterns(ValueError):
    pass


# ==========================================
# 1. OPERADORES ' Y " (fallo de MuPDF 1.28.2)
# ==========================================
# El filtro de contenido de MuPDF (apply_redactions, clean_contents y scrub) reescribe
# "14 TL 60 780 Td (texto) '" como "60 780 TD T* (texto)Tj": pierde el TL y el TD fija el
# interlineado a -780, así que cada línea salta 780 pt y todo el texto de la página queda
# fuera de ella. Con T* + Tj explícitos no pasa. Antes de censurar reescribimos
#   (s) '         ->  T* (s) Tj
#   aw ac (s) "   ->  aw Tw ac Tc T* (s) Tj
# que es la definición de ambos operadores en la norma (ISO 32000-1, 9.4.3).

_WS = b"\x00\t\n\x0c\r "
_DELIM = b"()<>[]{}/%"
_LITERALS = {b"true", b"false", b"null"}
_NUMBER = re.compile(rb"^[+-]?(\d+\.?\d*|\.\d+)$")


class _ContentSyntaxError(Exception):
    pass


def _skip_string(data, i):
    """i apunta a '('; devuelve el índice tras el ')' que lo cierra."""
    depth = 0
    n = len(data)
    while i < n:
        c = data[i]
        if c == 0x5C:  # barra invertida: el siguiente byte no cuenta
            i += 2
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise _ContentSyntaxError("cadena sin cerrar")


def _skip_inline_image(data, i):
    """i apunta justo tras el operador ID; devuelve el índice tras el EI final."""
    n = len(data)
    j = i + 1  # un byte de espacio tras ID
    while True:
        k = data.find(b"EI", j)
        if k == -1:
            raise _ContentSyntaxError("imagen en línea sin EI")
        before_ok = k > 0 and data[k - 1] in _WS
        after_ok = k + 2 >= n or data[k + 2] in _WS or data[k + 2] in _DELIM
        if before_ok and after_ok:
            return k + 2
        j = k + 2


def rewrite_quote_operators(data):
    """Devuelve el flujo de contenido con ' y " reescritos, o None si no hay ninguno.

    Lanza _ContentSyntaxError si el flujo no se puede analizar.
    """
    if b"'" not in data and b'"' not in data:
        return None
    out = bytearray()
    copied = 0  # bytes de data ya copiados a out
    operands = []  # (inicio, fin) de los operandos desde el último operador
    group_start = None  # inicio del array o diccionario de primer nivel en curso
    depth = 0
    changed = False
    i = 0
    n = len(data)

    def add_operand(start, end):
        if depth == 0:
            operands.append((start, end))

    while i < n:
        c = data[i]
        if c in _WS:
            i += 1
            continue
        if c == 0x25:  # comentario
            while i < n and data[i] not in b"\r\n":
                i += 1
            continue
        start = i
        if c == 0x28:
            i = _skip_string(data, i)
            add_operand(start, i)
            continue
        if data.startswith(b"<<", i) or c == 0x5B:  # inicio de diccionario o array
            if depth == 0:
                group_start = start
            depth += 1
            i += 2 if c == 0x3C else 1
            continue
        if data.startswith(b">>", i) or c == 0x5D:
            depth -= 1
            i += 2 if c == 0x3E else 1
            if depth < 0:
                raise _ContentSyntaxError("cierre sin apertura")
            if depth == 0:
                operands.append((group_start, i))
            continue
        if c == 0x3C:  # cadena hexadecimal
            k = data.find(b">", i)
            if k == -1:
                raise _ContentSyntaxError("cadena hexadecimal sin cerrar")
            i = k + 1
            add_operand(start, i)
            continue
        if c in b")>]{}":
            raise _ContentSyntaxError("delimitador inesperado")
        # Nombre, número, true/false/null u operador.
        i += 1
        while i < n and data[i] not in _WS and data[i] not in _DELIM:
            i += 1
        token = data[start:i]
        if c == 0x2F or token in _LITERALS or _NUMBER.match(token) or depth > 0:
            add_operand(start, i)
            continue
        # Es un operador.
        if token == b"'" and len(operands) >= 1:
            s0, s1 = operands[-1]
            out += data[copied:s0] + b"T* " + data[s0:s1] + b" Tj"
            copied = i
            changed = True
        elif token == b'"' and len(operands) >= 3:
            (a0, a1), (b0, b1), (s0, s1) = operands[-3:]
            out += (
                data[copied:a0]
                + data[a0:a1]
                + b" Tw "
                + data[b0:b1]
                + b" Tc T* "
                + data[s0:s1]
                + b" Tj"
            )
            copied = i
            changed = True
        elif token == b"ID":
            i = _skip_inline_image(data, i)
        operands = []
    if depth != 0:
        raise _ContentSyntaxError("array o diccionario sin cerrar")
    if not changed:
        return None
    out += data[copied:]
    return bytes(out)


def _rewrite_page_contents(doc, page):
    """Reescribe ' y " en el contenido de la página, analizado ya unido: los flujos de
    /Contents son un único contenido partido por donde sea entre dos tokens (el operando
    de un ' puede estar en un flujo y el operador en el siguiente). True si cambió algo."""
    xrefs = page.get_contents()
    streams = [doc.xref_stream(x) or b"" for x in xrefs]
    new = rewrite_quote_operators(b"\n".join(streams))
    if new is None:
        return False
    if len(xrefs) == 1:
        doc.update_stream(xrefs[0], new)
        return True
    # Si ningún operador cruza de un flujo a otro, cada flujo se reescribe por separado,
    # como antes; si no, la página pasa a tener un único flujo con el contenido unido.
    try:
        parts = [rewrite_quote_operators(s) for s in streams]
    except _ContentSyntaxError:
        parts = None
    if parts is not None and b"\n".join(p if p is not None else s for p, s in zip(parts, streams)) == new:
        for x, p in zip(xrefs, parts):
            if p is not None:
                doc.update_stream(x, p)
        return True
    joined = doc.get_new_xref()
    doc.update_object(joined, "<<>>")
    doc.update_stream(joined, new)
    doc.xref_set_key(page.xref, "Contents", f"{joined} 0 R")
    return True


def normalize_quote_operators(doc):
    """Reescribe ' y " en los contenidos de página y en los Form XObjects.

    Devuelve cuántos contenidos se reescribieron. Lanza _ContentSyntaxError si alguno con
    ' o " no se puede analizar: no se puede saber si MuPDF sacaría el texto de la página
    ni dónde quedaría, así que no se censura.
    """
    rewritten = 0
    for page in doc:
        rewritten += _rewrite_page_contents(doc, page)
    for xref in range(1, doc.xref_length()):
        try:
            if not (doc.xref_is_stream(xref) and doc.xref_get_key(xref, "Subtype") == ("name", "/Form")):
                continue
        except Exception:
            continue
        data = doc.xref_stream(xref)
        new = rewrite_quote_operators(data) if data else None
        if new is not None:
            doc.update_stream(xref, new)
            rewritten += 1
    return rewritten


# ==========================================
# 2. APLANADO DE FORMULARIOS Y ANOTACIONES
# ==========================================
def flatten_document(doc):
    """Convierte campos de formulario y anotaciones en contenido de página.

    Así la censura alcanza los valores de los campos (que no son texto de página) y el
    PDF deja de tener campos con el dato. Las anotaciones de censura que ya traiga el
    documento (marcadas en Acrobat sin aplicar) no se aplanan: se devuelven para
    aplicarlas junto con las nuestras, como hacía doc.scrub(redactions=True).
    """
    pending = []
    for page in doc:
        for annot in list(page.annots(types=[fitz.PDF_ANNOT_REDACT])):
            pending.append((page.number, fitz.Rect(annot.rect)))
            page.delete_annot(annot)
    doc.bake(annots=True, widgets=True)
    # Sin campos ya no hay formulario: fuera el /AcroForm (y con él un posible XFA, cuyos
    # datos son XML con los valores de los campos).
    cat = doc.pdf_catalog()
    if doc.xref_get_key(cat, "AcroForm")[0] != "null":
        doc.xref_set_key(cat, "AcroForm", "null")
    return pending


def compact_xref(doc):
    """doc.scrub() aborta ("bad xref - clean PDF before scrubbing") si la tabla xref tiene
    huecos: números de objeto sin objeto, algo válido en un PDF (p. ej. tras una
    actualización incremental o al quitar objetos al unir). En ese caso se reabre el
    documento serializado con garbage=3, que renumera los objetos sin huecos."""
    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_object(xref):
                continue
        except Exception:
            pass
        compact = fitz.open("pdf", doc.tobytes(garbage=3))
        doc.close()
        return compact
    return doc


# ==========================================
# 3. TEXTO Y TÉRMINOS
# ==========================================
def norm(s):
    """Forma comparable de un texto: NFC, minúsculas y espacios simples."""
    s = unicodedata.normalize("NFC", s or "").casefold()
    return re.sub(r"\s+", " ", s).strip()


class ZoneIndex:
    """Zonas de una página ordenadas por y0, para no comparar cada palabra con todas."""

    def __init__(self, zones, margin=0.0):
        self.zones = sorted(
            (fitz.Rect(z.x0 - margin, z.y0 - margin, z.x1 + margin, z.y1 + margin) for z in zones),
            key=lambda z: z.y0,
        )
        self.y0s = [z.y0 for z in self.zones]
        self.max_h = max((z.height for z in self.zones), default=0)

    def candidates(self, y0, y1):
        """Zonas que pueden solaparse verticalmente con [y0, y1]."""
        lo = bisect.bisect_left(self.y0s, y0 - self.max_h)
        hi = bisect.bisect_right(self.y0s, y1)
        return [z for z in self.zones[lo:hi] if z.y1 >= y0]

    def touches(self, r):
        return any(r.intersects(z) for z in self.candidates(r.y0, r.y1))

    def containing_point(self, x, y):
        return [z for z in self.candidates(y, y) if z.x0 <= x <= z.x1]


def page_words(page):
    """Palabras de la página como [(texto, Rect)], de get_text("words") (en C, rápido).

    Incluye el texto invisible (la capa de un OCR): scrub se llama con hidden_text=False,
    así que también debe conservarse fuera de las zonas.
    """
    return [(text, fitz.Rect(x0, y0, x1, y1)) for x0, y0, x1, y1, text, *_ in page.get_text("words")]


def chars_in_zones(page, zones):
    """¿Queda algún carácter con el centro dentro de una zona censurada?

    Primero, con las palabras, se buscan las que tocan una zona (tras una censura correcta,
    solo las vecinas). Solo si hay alguna se mira carácter a carácter (rawdict, más lento).
    """
    if not zones:
        return False
    index = ZoneIndex(zones)
    if not any(index.touches(r) for _, r in page_words(page)):
        return False
    for block in page.get_text("rawdict").get("blocks", ()):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                for ch in span.get("chars", ()):
                    if ch["c"].isspace():
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    if index.containing_point((x0 + x1) / 2, (y0 + y1) / 2):
                        return True
    return False


_ALL_TEXT_FLAGS = fitz.TEXTFLAGS_WORDS & ~fitz.TEXT_MEDIABOX_CLIP


def _word_key(word):
    x0, y0, x1, y1, text, *_ = word
    return (text, round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1))


def unrotated_rect(page):
    """Lo que se ve de la página (CropBox recortada a la MediaBox), sin girar: el espacio de
    search_for, get_text, get_drawings y add_redact_annot, y de los rectángulos de la web.

    No sirve page.rect * page.derotation_matrix: con /Rotate, PyMuPDF 1.28.2 calcula esa
    matriz con la CropBox sin recortar, y con una CropBox que se sale de la MediaBox la zona
    salía desplazada (un hallazgo visible se daba por fuera de la página). page.rect sí es
    correcto: es el mismo rectángulo, girado y en el origen.
    """
    w, h = page.rect.width, page.rect.height
    return fitz.Rect(0, 0, h, w) if page.rotation % 180 else fitz.Rect(0, 0, w, h)


def hidden_words(page_visible, page_all):
    """Palabras de la página que no se ven, como [(texto, Rect)]: fuera del CropBox o del
    MediaBox, desplazadas fuera de la página o en una capa OCG apagada. `page_all` es la
    misma página extraída sin recorte y con todas las capas visibles (documento sin
    /OCProperties).

    Cada palabra visible (misma posición y texto) descuenta una de `page_all`; lo que queda
    es el texto oculto, en el orden de la página. Las palabras llegan sin girar: la zona
    visible es unrotated_rect (con page.rect, en una página a 90 o 270 grados el texto
    visible de abajo contaba como oculto).
    """
    shown = unrotated_rect(page_visible)
    visible = Counter(_word_key(w) for w in page_visible.get_text("words", clip=shown))
    hidden = []
    for w in page_all.get_text("words", clip=fitz.INFINITE_RECT(), flags=_ALL_TEXT_FLAGS):
        key = _word_key(w)
        if visible[key]:
            visible[key] -= 1
        else:
            hidden.append((w[4], fitz.Rect(w[:4])))
    return hidden


def hidden_page_text(page_visible, page_all):
    """El texto oculto de la página (hidden_words) en una cadena."""
    return " ".join(w for w, _ in hidden_words(page_visible, page_all))


def hidden_term_rects(page_visible, page_all, terms):
    """Rectángulos de las palabras ocultas que forman parte de un término.

    Se busca igual que en la verificación (cada término en el texto oculto normalizado y
    unido por espacios), así que lo que queda tras censurar estas palabras ya no contiene
    ningún término. Se censura la palabra entera: no se ve.
    """
    words = hidden_words(page_visible, page_all)
    if not words or not terms:
        return []
    normed = [norm(w) for w, _ in words]
    starts, pos = [], 0
    for w in normed:
        starts.append(pos)
        pos += len(w) + 1
    joined = " ".join(normed)
    hit = set()
    for t in terms:
        i = joined.find(t)
        while i != -1:
            hit.update(range(bisect.bisect_right(starts, i) - 1, bisect.bisect_left(starts, i + len(t))))
            i = joined.find(t, i + 1)
    return [words[k][1] for k in sorted(hit)]


def open_all_layers(path):
    """`path` abierto con todas las capas visibles: sin /OCProperties MuPDF no oculta ningún
    contenido opcional (activar las capas no basta: un OCMD /AllOff se ocultaría). Hay que
    reabrirlo (cambiar la clave en memoria no basta), así que se guarda una copia junto a
    `path`, en disco y no en memoria. Devuelve (documento, ruta de la copia o None)."""
    doc = fitz.open(path)
    if doc.xref_get_key(doc.pdf_catalog(), "OCProperties")[0] == "null":
        return doc, None
    copy_path = path + ".capas.pdf"
    doc.xref_set_key(doc.pdf_catalog(), "OCProperties", "null")
    doc.save(copy_path)
    doc.close()
    return fitz.open(copy_path), copy_path


def text_under_zones(words, zones):
    """Texto bajo cada zona (palabras con el centro dentro), una cadena por zona."""
    index = ZoneIndex(zones)
    by_zone = {}
    for w, r in words:
        for z in index.containing_point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2):
            by_zone.setdefault(id(z), []).append(w)
    return [" ".join(ws) for ws in by_zone.values()]


def words_outside(words, zones, margin=1.0):
    """Palabras que no tocan ninguna zona: deben sobrevivir a la censura."""
    index = ZoneIndex(zones, margin)
    return Counter(w for w, r in words if not index.touches(r))


# ==========================================
# 4. RECOMPRESIÓN DE LAS IMÁGENES CENSURADAS
# ==========================================
# apply_redactions(PDF_REDACT_IMAGE_PIXELS) sustituye cada imagen que toca una zona por una
# copia nueva SIN comprimir (con save(deflate=True) acaba en Flate): un escaneo JPEG en gris
# pasaba de 24 a 67 MB y uno en color se multiplicaba por 5. Aquí se vuelve a codificar cada
# imagen sustituida como su original, y 1 bit (CCITT, JBIG2...) en CCITT G4 sin pérdida. Los
# píxeles tapados ya son negros en la copia, así que recomprimir no puede devolver nada de lo
# censurado. Solo lleva pérdida lo que ya la tenía (lote 2d, 2026-10-15; con Pillow):
# - JPEG gris y RGB: con las tablas DQT exactas del original y su submuestreo (Pillow). Solo si
#   no se pueden leer o Pillow no reproduce el submuestreo, también la copia de MuPDF (calidad
#   estimada por la DQT, siempre 4:4:4), y la que ocupe menos. Solo con MuPDF, un escaneo 4:2:0
#   crecía un 11 %.
# - JPEG CMYK: con las DQT del original, comprobando los colores al decodificar (_same_colors);
#   si no pasan, Flate sin pérdida. Antes siempre Flate: ×3,3.
# - JPEG 2000 sin pérdida (5/3 o en duda, jpx_reversible): JPEG 2000 sin pérdida o Flate, lo que
#   ocupe menos, con píxeles idénticos. Con pérdida (9/7): JPEG 2000 9/7 a la misma tasa de
#   compresión que el original. Antes, los dos a JPEG q85. Una 5/3 truncada por tasa también
#   tiene pérdida y la cabecera no lo distingue: se mide (JPX_TRUNCATED_RATIO).

# Tabla de cuantización de luminancia de referencia (IJG, calidad 50).
_STD_LUMA_SUM = sum((
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
))
DEFAULT_JPEG_QUALITY = 85
# Con estos colores o menos, una imagen censurada nunca pasa a JPEG.
MAX_LOSSLESS_COLORS = 16


def jpeg_quality(data):
    """Calidad IJG aproximada (30-95) de un JPEG, por su tabla de luminancia; None si no hay."""
    i = 2
    try:
        while i + 4 <= len(data) and data[i] == 0xFF:
            marker = data[i + 1]
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker == 0xDB:
                seg = data[i + 4:i + 2 + length]
                j = 0
                while j < len(seg):
                    precision, table = seg[j] >> 4, seg[j] & 15
                    size = 128 if precision else 64
                    values = seg[j + 1:j + 1 + size]
                    if precision:
                        values = struct.unpack(">64H", values)
                    if table == 0:
                        scale = sum(values) * 100 / _STD_LUMA_SUM
                        q = (200 - scale) / 2 if scale <= 100 else 5000 / scale
                        return max(30, min(95, round(q)))
                    j += 1 + size
            if marker == 0xDA:  # empiezan los datos: no hay más tablas
                return None
            i += 2 + length
    except (struct.error, IndexError):
        return None
    return None


def _image_places(page, xrefs):
    """{xref: posiciones redondeadas} de esas imágenes en la página.

    Por la huella MD5 de los píxeles, como get_image_rects, pero con un TextPage nuevo:
    get_image_info/get_image_rects guardan en caché las imágenes de antes de censurar.
    Decodifica cada imagen: solo se usa cuando hay varias del mismo tamaño.
    """
    by_digest = {}
    for info in page.get_textpage(flags=fitz.TEXT_PRESERVE_IMAGES).extractIMGINFO(hashes=True):
        by_digest.setdefault(info["digest"], []).append(tuple(round(v) for v in info["bbox"]))
    return {x: tuple(sorted(by_digest.get(fitz.Pixmap(page.parent, x).digest, []))) for x in xrefs}


def _image_slots(page):
    """Imágenes de la página: [(xref, ancho, alto, bits por componente, /Filter, posiciones)].

    El /Filter completo ("null" si no tiene, "[/FlateDecode/DCTDecode]"...): get_images solo
    da el primer filtro de la cadena. Posiciones: dónde se dibuja (la copia que deja
    apply_redactions va en el mismo sitio que su original), solo para las imágenes que
    comparten ancho, alto y bits con otra; para el resto, ().
    """
    doc = page.parent
    slots = [(im[0], im[2], im[3], im[4], doc.xref_get_key(im[0], "Filter")[1])
             for im in page.get_images(full=True)]
    sizes = Counter(s[1:4] for s in slots)
    ambiguous = [s[0] for s in slots if sizes[s[1:4]] > 1]
    places = _image_places(page, ambiguous) if ambiguous else {}
    return [s + (places.get(s[0], ()),) for s in slots]


def _encode_g4(doc, xref, width, height):
    """Imagen de 1 bit sin comprimir -> CCITT G4. True si los bits decodificados son idénticos."""
    raw = doc.xref_stream(xref)
    stride = (width + 7) // 8
    if not raw or len(raw) != stride * height:
        return False
    buf = fitz.mupdf.fz_compress_ccitt_fax_g4(fitz.mupdf.python_buffer_data(raw), width, height, stride)
    encoded = buf.fz_buffer_extract()
    doc.update_stream(xref, encoded, compress=False)
    doc.xref_set_key(xref, "Filter", "/CCITTFaxDecode")
    doc.xref_set_key(xref, "DecodeParms", f"<</K -1 /Columns {width} /Rows {height}>>")
    if doc.xref_stream(xref) == raw:
        # En 1 bit, el gris ICC que pone MuPDF equivale a DeviceGray y ahorra el perfil (~3 KB).
        # Las máscaras (ImageMask) no llevan espacio de color.
        if doc.xref_get_key(xref, "ImageMask")[1] != "true":
            doc.xref_set_key(xref, "ColorSpace", "/DeviceGray")
        return True
    # No debería pasar: se vuelve a la copia sin pérdida.
    doc.update_stream(xref, raw)
    doc.xref_set_key(xref, "DecodeParms", "null")
    return False


_PIL_MODES = {1: "L", 3: "RGB", 4: "CMYK"}

# JPEG 2000 5/3 (reversible según su cabecera) truncado por tasa: también tiene pérdida, y la
# cabecera no lo distingue. Se mide (decisión de diseño, 2026-10-16): si la copia censurada,
# codificada sin pérdida, ocupa más de JPX_TRUNCATED_RATIO veces el original, el original no era
# sin pérdida y se codifica 5/3 a su misma tasa de compresión. Un sin pérdida real recodificado
# sin pérdida ocupa más o menos lo mismo (×1,00 con OpenJPEG; con zonas en negro, menos); uno
# truncado, con texto sobre blanco a tasa 20, ×2,5-4. Por debajo del umbral se queda sin
# pérdida: un truncado de foto a tasa 20 da ×1,09.
JPX_TRUNCATED_RATIO = 1.5

# Comprobación de colores de una JPEG CMYK recodificada (_same_colors), por canal y en niveles
# de 0 a 255, entre la copia censurada y su JPEG decodificada por MuPDF: diferencia media
# absoluta y diferencia de las medias (sesgo). Medido con dos escaneos sintéticos, de calidad 95
# a 30: la buena da 0,1-3,3 de diferencia y 0,8 de sesgo como mucho; la mal interpretada (marca
# Adobe o /Decode al revés), 255 en algún canal.
CMYK_MAX_MEAN_DIFF = 8
CMYK_MAX_BIAS = 2


def _filters(doc, xref):
    return re.findall(r"/(\w+)", doc.xref_get_key(xref, "Filter")[1])


def _original_jpeg(doc, xref):
    """Bytes del JPEG de la imagen original, sin la capa Flate si la lleva; None si no se puede.

    No sirve extract_image: devuelve las CMYK convertidas a RGB, con otras tablas.
    """
    filters = _filters(doc, xref)
    if not filters or filters[-1] != "DCTDecode" or any(f != "FlateDecode" for f in filters[:-1]):
        return None
    data = doc.xref_stream_raw(xref)
    try:
        for _ in filters[:-1]:
            data = zlib.decompress(data)
    except zlib.error:
        return None
    return data if data[:2] == b"\xff\xd8" else None


def _original_jpx(doc, xref):
    """Bytes del JPEG 2000 de la imagen original (solo /JPXDecode, sin más filtros) o None."""
    return doc.xref_stream_raw(xref) if _filters(doc, xref) == ["JPXDecode"] else None


def jpx_reversible(data):
    """¿Usa este JPEG 2000 la transformada 5/3 reversible (sin pérdida)?

    True si algún COD o COC (cabecera principal o de tile-part) la usa; False si todos usan la
    9/7 irreversible; None si no se puede leer o no hay COD (en duda: se trata como sin pérdida).
    """
    start = data.find(b"\xff\x4f\xff\x51")  # SOC + SIZ: inicio del codestream (también en JP2)
    if start < 0:
        return None
    transforms = []
    try:
        csiz = int.from_bytes(data[start + 40:start + 42], "big")
        ccoc = 1 if csiz < 257 else 2

        def header(pos, end_marker):
            """Lee segmentos desde pos hasta end_marker; devuelve su posición."""
            while data[pos:pos + 2] != end_marker:
                if data[pos] != 0xFF or pos + 4 > len(data):
                    raise ValueError
                marker = data[pos + 1]
                length = int.from_bytes(data[pos + 2:pos + 4], "big")
                if marker == 0x52:  # COD: transformada en SPcod, tras Scod, SGcod (4) y 4 bytes
                    transforms.append(data[pos + 13])
                elif marker == 0x53:  # COC: igual, tras Ccoc (1 o 2 bytes) y Scoc
                    transforms.append(data[pos + 9 + ccoc])
                pos += 2 + length
            return pos

        pos = header(start + 2, b"\xff\x90")  # cabecera principal, hasta el primer SOT
        while data[pos:pos + 2] == b"\xff\x90":  # cada tile-part: su cabecera hasta SOD
            psot = int.from_bytes(data[pos + 6:pos + 10], "big")
            header(pos + 12, b"\xff\x93")
            if psot == 0:  # el último, hasta EOC
                break
            pos += psot
    except (IndexError, ValueError):
        return None
    if not transforms or any(t not in (0, 1) for t in transforms):
        return None
    return 1 in transforms


def _pil(pix):
    return Image.frombytes(_PIL_MODES[pix.n], (pix.width, pix.height), pix.samples)


def _few_colors(img):
    """¿Tiene img (Pillow) MAX_LOSSLESS_COLORS colores distintos o menos?

    getcolors(N) devuelve None en cuanto pasa de N y no sigue contando. Antes,
    Pixmap.color_count() contaba todos: 2,2-2,5 s por foto de página entera a 300 ppp, el 84 %
    de apply con un fondo fotográfico (docs/estudio-anonimizar-tamano.md). Misma decisión.
    """
    return img.getcolors(MAX_LOSSLESS_COLORS) is not None


def _jpeg_like(img, src):
    """img en JPEG con las tablas de cuantización y el submuestreo de src (abierto con Pillow).

    Progresivo y con Huffman optimizado: los dos sin pérdida. Submuestreo desconocido: 4:4:4.
    """
    kw = {"qtables": src.quantization, "optimize": True, "progressive": True}
    if img.mode == "RGB":
        sampling = JpegImagePlugin.get_sampling(src)
        kw["subsampling"] = sampling if sampling >= 0 else 0
    buf = io.BytesIO()
    img.save(buf, "JPEG", **kw)
    return buf.getvalue()


def _store(doc, xref, data, filter_name):
    """Pone data como flujo de la imagen, con su filtro. Un JPEG, con capa Flate si ahorra
    (como jpeg_flate.py)."""
    if filter_name == "/DCTDecode":
        wrapped = zlib.compress(data, 9)
        if len(wrapped) < len(data):
            data, filter_name = wrapped, "[/FlateDecode /DCTDecode]"
    doc.update_stream(xref, data, compress=False)
    doc.xref_set_key(xref, "Filter", filter_name)
    doc.xref_set_key(xref, "DecodeParms", "null")
    doc.xref_set_key(xref, "BitsPerComponent", "8")


def _restore_raw(doc, xref, raw):
    """Vuelve a la copia sin pérdida (Flate al guardar)."""
    doc.update_stream(xref, raw)
    doc.xref_set_key(xref, "DecodeParms", "null")


def _same_colors(doc, xref, expected):
    """¿La imagen xref, decodificada por MuPDF, tiene los colores de `expected` (Pillow)?"""
    got = fitz.Pixmap(doc, xref)
    if (got.n, got.width, got.height, got.alpha) != (len(expected.getbands()), *expected.size, 0):
        return False
    got = _pil(got)
    diff = ImageStat.Stat(ImageChops.difference(expected, got)).mean
    bias = [abs(a - b) for a, b in zip(ImageStat.Stat(expected).mean, ImageStat.Stat(got).mean)]
    return max(diff) <= CMYK_MAX_MEAN_DIFF and max(bias) <= CMYK_MAX_BIAS


def _encode_dct(doc, xref, original_xref, pix, img):
    """Copia de una JPEG -> JPEG con la calidad del original (ver el comentario de la sección).
    img: la misma copia en Pillow."""
    original = _original_jpeg(doc, original_xref)
    src = None
    if original is not None:
        try:
            src = Image.open(io.BytesIO(original))
            src.load()  # valida el JPEG entero
        except Exception:
            src = None
    if pix.n == 4:
        # CMYK: solo con las tablas del original. Pillow escribe la marca Adobe (CMYK invertido);
        # si MuPDF no lee los colores tal cual, se prueba con los valores invertidos y, si
        # tampoco, Flate sin pérdida.
        if src is None:
            return False
        raw = doc.xref_stream(xref)
        for variant in (img, ImageChops.invert(img)):
            _store(doc, xref, _jpeg_like(variant, src), "/DCTDecode")
            if _same_colors(doc, xref, img):
                return True
        _restore_raw(doc, xref, raw)
        return False
    candidates = []
    if src is not None:
        try:
            candidates.append(_jpeg_like(img, src))
        except Exception as e:  # p. ej. tablas que Pillow no acepta: queda la de MuPDF
            print(f"Aviso en redact.py: JPEG con las tablas del original: {e}", file=sys.stderr)
    if not candidates or not _same_layout(img, src):
        # Sin las tablas o el submuestreo del original, también la de MuPDF, y la menor.
        candidates.append(pix.tobytes("jpg", jpg_quality=(original and jpeg_quality(original)) or DEFAULT_JPEG_QUALITY))
    _store(doc, xref, min(candidates, key=len), "/DCTDecode")
    return True


def _same_layout(img, src):
    """¿Reproduce _jpeg_like las tablas y el submuestreo de src? Entonces basta su JPEG.

    Gris: no hay submuestreo. RGB: Pillow lee y escribe 4:4:4, 4:2:2, 4:2:0 y 4:1:1. Con los
    escaneos del lote 2d (2026-10-16), solo Pillow da los mismos bytes y apply tarda un 22-37 %
    menos: la copia de MuPDF no ganaba en ninguno.
    """
    if not src.quantization:
        return False
    return img.mode == "L" or JpegImagePlugin.get_sampling(src) >= 0


def _encode_jpx(doc, xref, original_xref, pix, img):
    """Copia de un JPEG 2000: sin pérdida si el original lo era (o en duda); si no, 9/7.
    img: la misma copia en Pillow."""
    original = _original_jpx(doc, original_xref)
    raw = doc.xref_stream(xref)
    reversible = original is None or jpx_reversible(original) is not False
    if reversible:
        # Sin pérdida: JPEG 2000 reversible o Flate, lo que ocupe menos, con píxeles idénticos.
        buf = io.BytesIO()
        try:
            img.save(buf, "JPEG2000", irreversible=False, no_jp2=True)
        except Exception:
            return False  # p. ej. un modo que Pillow no codifica en JPEG 2000: Flate
        lossless = buf.getvalue()
        if original is None or len(lossless) <= JPX_TRUNCATED_RATIO * len(original):
            if len(lossless) >= len(zlib.compress(raw, 6)):
                return False
            _store(doc, xref, lossless, "/JPXDecode")
            if fitz.Pixmap(doc, xref).samples == pix.samples:
                return True
            _restore_raw(doc, xref, raw)
            return False
        # 5/3 truncado por tasa (ver JPX_TRUNCATED_RATIO): 5/3 a su misma tasa.
    # Con pérdida: otra vez JPEG 2000, con la transformada del original (9/7, o 5/3 truncada) y
    # a su misma tasa de compresión (bytes sin comprimir / bytes del original). Con un escaneo
    # 9/7 de 20 páginas tarda lo mismo que pasarlo a JPEG q85 como antes (50-63 s frente a
    # 60-66 s) y ocupa como la entrada (antes, +76 %).
    ratio = len(raw) / len(original)
    buf = io.BytesIO()
    try:
        img.save(buf, "JPEG2000", irreversible=not reversible, quality_mode="rates",
                 quality_layers=[ratio], no_jp2=True)
    except Exception:
        return False  # p. ej. un modo que Pillow no codifica en JPEG 2000: Flate
    _store(doc, xref, buf.getvalue(), "/JPXDecode")
    return True


def _encode_jpeg(doc, xref, original_xref, original_filter):
    """Imagen de 8 bits gris, RGB o CMYK sin comprimir, copia de una JPEG o JPEG 2000 ->
    codificada como su original (ver el comentario de la sección). False: queda en Flate."""
    pix = fitz.Pixmap(doc, xref)
    if pix.alpha or pix.n not in _PIL_MODES:
        return False  # otros espacios de color: se queda sin pérdida (Flate)
    img = _pil(pix)
    if _few_colors(img):
        # Código de barras, QR o sello (o una Indexed que MuPDF ha pasado a RGB): sin pérdida,
        # aunque se haya emparejado con un JPEG (como en Comprimir, lossless_images.py).
        return False
    if "JPX" in original_filter:
        return _encode_jpx(doc, xref, original_xref, pix, img)
    return _encode_dct(doc, xref, original_xref, pix, img)


def recompress_redacted_images(doc, page, before):
    """Recomprime las imágenes que apply_redactions acaba de sustituir en `page`.

    `before`: _image_slots(page) antes de censurar. Devuelve cuántas se recomprimieron.
    """
    return recompress_pairs(doc, redacted_pairs(page, before))


def redacted_pairs(page, before):
    """[(copia, original)] de las imágenes que apply_redactions acaba de sustituir en `page`,
    como entradas de _image_slots. `before`: _image_slots(page) antes de censurar."""
    after = _image_slots(page)
    after_xrefs = {b[0] for b in after}
    before_xrefs = {b[0] for b in before}
    # La copia nueva cambia de xref y de nombre de recurso: se empareja con la imagen que ha
    # desaparecido de la página y tiene las mismas dimensiones y bits por componente (en un
    # escaneo, el fondo en color y la máscara de texto suelen medir lo mismo) y, si hay
    # varias, con la que se dibujaba en el mismo sitio: por orden, un QR podía emparejarse con
    # la foto JPEG de al lado y salir en JPEG.
    # (Si alguna no se encuentra por su sitio, después se empareja por orden, como antes.)
    replaced = [b for b in before if b[0] not in after_xrefs]
    copies = [a for a in after if a[0] not in before_xrefs and a[4] == "null"]  # nuevas, sin comprimir
    pairs = []
    for by_place in (True, False):
        for copy in list(copies):
            match = next((b for b in replaced if b[1:4] == copy[1:4] and (b[5] == copy[5] or not by_place)), None)
            if match is not None:
                replaced.remove(match)
                copies.remove(copy)
                pairs.append((copy, match))
    return pairs


def recompress_pairs(doc, pairs):
    """Recomprime cada copia como su original ([(copia, original)] de redacted_pairs).
    Devuelve cuántas se recomprimieron."""
    return sum(recompress_copy(doc, copy[0], copy[1], copy[2], match[0], match[4])
               for copy, match in pairs)


def recompress_copy(doc, xref, width, height, original_xref, original_filter):
    """Recomprime la copia sin comprimir xref como su original. False: queda sin pérdida."""
    bpc = doc.xref_get_key(xref, "BitsPerComponent")[1]
    try:
        if bpc == "1":
            return _encode_g4(doc, xref, width, height)
        if bpc == "8" and ("DCT" in original_filter or "JPX" in original_filter):
            return _encode_jpeg(doc, xref, original_xref, original_filter)
    except Exception as e:  # nunca tumba la censura: la imagen queda sin pérdida
        print(f"Aviso en redact.py: no se pudo recomprimir una imagen: {e}", file=sys.stderr)
    return False


# ==========================================
# 4b. PÍXELES CENSURADOS OCULTOS EN OTRAS PÁGINAS (imágenes compartidas)
# ==========================================
# Incidencia (2026-10-27, caso «fondo con recortes» del estudio): apply_redactions borra los
# píxeles de una zona solo en la copia de la imagen que deja en la página censurada; las demás
# páginas que comparten la imagen siguen usando la original, entera. Si en alguna de ellas esa
# parte no se ve (otra CropBox, un recorte, fuera de la página, o la imagen no se dibuja), los
# píxeles censurados seguían en el archivo sin que el usuario los hubiera visto.
# Contención (sin cambiar lo que se ve en ninguna página):
# - Antes de censurar (hidden_pixels_plan), cada dibujo de una imagen que toca una zona se sitúa
#   con su matriz y sus recortes (trazado de MuPDF), en la página sin girar. En píxeles de la
#   imagen: los que toca cada zona, redondeados hacia fuera (todo píxel que comparte área con la
#   zona), y los que se ven en cada página que la usa.
# - Cada página que usa la imagen y no muestra entera alguna de esas partes recibe su propia
#   copia con los píxeles que no muestra borrados (HiddenPixels): lo que se ve no cambia. Lo que
#   se ve de una zona de otra página se queda, como una aparición visible que el usuario no ha
#   marcado. Las copias las hace MuPDF en una página auxiliar (_Scratch), igual que las de
#   apply_redactions, y se recomprimen como la original.
# - En las copias de apply_redactions se borra también cualquier píxel tocado por una zona que
#   haya quedado con su valor (MuPDF redondea hacia fuera, pero con una tolerancia de 0,001 px).
# La verificación final lo comprueba píxel a píxel en la salida (hidden_image_pixels,
# HIDDEN_PIXELS_LEAK). Si una zona toca una imagen que no se puede copiar por página,
# UNSUPPORTED_REFUSAL.

# Un píxel (columna c, fila f) ocupa [c, c+1] × [f, f+1] en el espacio de la imagen de MuPDF
# (fila 0 arriba). Cuenta como tocado por una zona, o visible por lo que deja ver la página, si
# se adentra en él más de PIXEL_EPS px: menos es el error de las matrices de MuPDF (en float32,
# 19,9999988 en vez de 20), no un píxel tocado. MuPDF, al censurar, usa 0,001 px.
PIXEL_EPS = 1e-4
# Segmentos con que se aproxima cada curva de un recorte.
CURVE_STEPS = 8
# Lado máximo (pt) de la página auxiliar en que MuPDF hace las copias.
SCRATCH_MAX_SIDE = 14400

# Un dibujo de imagen en una página: xrefs, las imágenes que pueden ser (una si se ha
# identificado; vacío para una imagen en línea); matrix, del cuadrado unidad a la página sin
# girar; clips, los recortes activos [(subtrazados, par_impar)] en espacio de página, o None si
# alguno no se puede representar como polígono (entonces la imagen se da por no visible).
Placement = namedtuple("Placement", "xrefs width height matrix clips")

_TRACE_TAG = re.compile(r"\s*<(/?)([a-z_]+)")
_TRACE_ATTR = re.compile(r'\b([a-z0-9_]+)="([^"]*)"')
# Recortes por texto o trazo: lo que dibujan dentro se da por no visible. Una máscara de imagen
# (clip_image_mask) también, salvo la de la propia imagen: MuPDF dibuja una imagen con SMask o
# /Mask como su máscara con la misma matriz y, dentro, la imagen (ver parse_trace).
_OPAQUE_CLIPS = ("clip_stroke_path", "clip_text", "clip_stroke_text")


def _trace(page):
    """Llamadas de dibujo de la página (dispositivo de trazado de MuPDF), como texto."""
    m = fitz.mupdf
    buf = m.fz_new_buffer(1 << 16)
    out = m.FzOutput(buf)
    dev = m.fz_new_trace_device(out)
    try:
        m.fz_run_page(page.this, dev, m.FzMatrix(), m.FzCookie())
        m.fz_close_device(dev)
    finally:
        out.fz_close_output()
    return m.fz_buffer_extract(buf).decode("utf-8", "replace")


def _trace_matrix(value):
    return fitz.Matrix(*(float(v) for v in value.split()))


def _path_op(path, tag, attrs):
    """Añade al recorte en lectura un elemento del trazado (moveto, lineto, curveto...)."""
    ctm, subpaths = path["ctm"], path["subpaths"]

    def point(x, y):
        p = fitz.Point(float(attrs[x]), float(attrs[y])) * ctm
        return (p.x, p.y)

    try:
        if tag == "moveto":
            subpaths.append([point("x", "y")])
        elif tag == "lineto" and subpaths:
            subpaths[-1].append(point("x", "y"))
        elif tag == "curveto" and subpaths:
            p0, p1, p2, p3 = subpaths[-1][-1], point("x1", "y1"), point("x2", "y2"), point("x3", "y3")
            for i in range(1, CURVE_STEPS + 1):
                t = i / CURVE_STEPS
                a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
                subpaths[-1].append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                                     a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
        elif tag != "closepath":
            path["ok"] = False
    except (KeyError, ValueError):
        path["ok"] = False


def parse_trace(text):
    """Dibujos de imagen del trazado de una página, en orden: [(es máscara, ancho, alto, matriz,
    recortes)]. recortes: los activos, [(subtrazados, par_impar)], o None si alguno no se puede
    representar (texto, trazo, máscara de imagen o suave) o la imagen se dibuja dentro de un
    patrón o de la definición de una máscara."""
    draws, stack = [], []
    opaque = 0   # dentro de un patrón (<tile>) o de la definición de una máscara (<clip_mask>)
    skip = None  # etiqueta cuyo contenido se salta
    path = None  # recorte en lectura
    for line in text.splitlines():
        m = _TRACE_TAG.match(line)
        if not m:
            continue
        closing, tag = m.group(1) == "/", m.group(2)
        empty = line.rstrip().endswith("/>")
        if skip:
            if closing and tag == skip:
                skip = None
            continue
        if path is not None:
            if closing and tag == "clip_path":
                subpaths = [sp for sp in path["subpaths"] if len(sp) >= 3]
                stack.append((subpaths, path["evenodd"]) if path["ok"] else None)
                path = None
            else:
                _path_op(path, tag, dict(_TRACE_ATTR.findall(line)))
            continue
        if closing:
            if tag in ("tile", "clip_mask"):
                opaque -= 1
                if tag == "clip_mask":
                    stack.append(None)  # lo que se dibuja con la máscara, hasta su pop_clip
            continue
        if tag == "clip_path":
            attrs = dict(_TRACE_ATTR.findall(line))
            if empty:
                stack.append(([], False))  # recorte vacío: no se ve nada
            else:
                path = {"ctm": _trace_matrix(attrs.get("transform", "1 0 0 1 0 0")),
                        "evenodd": attrs.get("winding") == "eofill", "subpaths": [], "ok": True}
        elif tag in _OPAQUE_CLIPS:
            stack.append(None)
            if not empty:
                skip = tag
        elif tag == "clip_image_mask":
            stack.append(("mask", dict(_TRACE_ATTR.findall(line)).get("transform")))
        elif tag == "pop_clip":
            if stack:
                stack.pop()
        elif tag in ("tile", "clip_mask"):
            if not empty:
                opaque += 1
            elif tag == "clip_mask":
                stack.append(None)
        elif tag in ("fill_image", "fill_image_mask"):
            attrs = dict(_TRACE_ATTR.findall(line))
            # La máscara de la propia imagen (misma matriz) es su transparencia, no un recorte de
            # la página: no cuenta. Cualquier otra máscara de imagen sí, como no visible.
            own = ("mask", attrs.get("transform"))
            active = [c for c in stack if c != own]
            clips = None if opaque or any(c is None or c[0] == "mask" for c in active) else active
            draws.append((tag == "fill_image_mask", int(attrs["width"]), int(attrs["height"]),
                          _trace_matrix(attrs["transform"]), clips))
    return draws


_INLINE_IMAGE = re.compile(rb"(?:^|\s)BI\s")


def _xobject_refs(doc, holder, key):
    """{nombre: xref} del diccionario /XObject de holder[key] (key, p. ej. "Resources")."""
    return _resource_refs(doc, holder, "XObject", key)


def _resource_refs(doc, holder, kind, key="Resources"):
    """{nombre: xref} del diccionario /kind (XObject, Pattern...) de holder[key]."""
    t, v = doc.xref_get_key(holder, f"{key}/{kind}")
    if t == "xref":
        v = doc.xref_object(int(v.split()[0]), compressed=True)
    elif t != "dict":
        return {}
    return {name: int(x) for name, x in re.findall(r"/([^\s/<>\[\]()%]+)\s*(\d+)\s+\d+\s+R", v)}


def _is_form(doc, xref):
    return doc.xref_get_key(xref, "Subtype")[1] == "/Form"


def _page_forms(doc, page, patterns=False):
    """Form XObjects que puede dibujar la página (también anidados) y, con patterns, también sus
    patrones de mosaico (que se dibujan como un Form)."""
    def refs(holder):
        out = list(_xobject_refs(doc, holder, "Resources").values())
        if patterns:
            out += [x for x in _resource_refs(doc, holder, "Pattern").values()
                    if doc.xref_get_key(x, "PatternType")[1] == "1"]
        return out

    seen, todo = set(), refs(page.xref)
    while todo:
        x = todo.pop()
        if x in seen or not (_is_form(doc, x) or patterns and doc.xref_get_key(x, "PatternType")[1] == "1"):
            continue
        seen.add(x)
        todo.extend(refs(x))
    return seen


def _may_have_inline_images(doc, page):
    if _INLINE_IMAGE.search(page.read_contents()):
        return True
    return any(_INLINE_IMAGE.search(doc.xref_stream(x) or b"") for x in _page_forms(doc, page))


_DO = re.compile(rb"/([^\s/\[\]()<>{}%]+)\s*Do\b")


def _do_order(doc, page):
    """xrefs de las imágenes en el orden en que las dibuja el contenido de la página ("/Nombre
    Do"); None si dibuja algún Form XObject o puede tener imágenes en línea (el orden no se
    puede saber así)."""
    data = page.read_contents()
    if _INLINE_IMAGE.search(data):
        return None
    names = _xobject_refs(doc, page.xref, "Resources")
    order = []
    for name in _DO.findall(data):
        x = names.get(name.decode("latin-1"))
        if x is None or _is_form(doc, x):
            return None
        order.append(x)
    return order


def placements(page):
    """Dibujos de imagen de la página, en orden y sin girar: ([Placement], rectángulo visible de
    la página).

    Las imágenes se identifican por su ancho, alto y si son máscara entre las de la página
    (get_images, y las de sus patrones de mosaico: pattern_images). Si hay varias iguales, por el orden de los Do del contenido (_do_order) o, si
    no se puede, por la huella de sus píxeles, como _image_places (decodifica esas imágenes;
    dos imágenes con los mismos píxeles quedan sin identificar). Sin get_image_info(xrefs=True):
    calcula la huella de todas.
    """
    doc = page.parent
    with _unrotated(page):
        visible = fitz.Rect(page.rect)
        draws = parse_trace(_trace(page))
        if not draws:
            return [], visible
        candidates = {}
        for x, kind in page_image_kinds(doc, page):
            candidates.setdefault(kind, set()).add(x)
        inline = _may_have_inline_images(doc, page)
        order = None
        if any(len(xs) > 1 for xs in candidates.values()) or inline:
            order = _do_order(doc, page)
            if order is not None and (len(order) != len(draws) or any(
                    x not in candidates.get((w, h, mask), ()) for x, (mask, w, h, _, _) in zip(order, draws))):
                order = None
        digests = by_digest = None
        out = []
        for i, (mask, w, h, matrix, clips) in enumerate(draws):
            xrefs = candidates.get((w, h, mask), set())
            if order is not None:
                xrefs = {order[i]}
            elif xrefs and (len(xrefs) > 1 or inline):
                if digests is None:
                    infos = page.get_textpage(flags=fitz.TEXT_PRESERVE_IMAGES).extractIMGINFO(hashes=True)
                    digests = [info["digest"] for info in infos] if len(infos) == len(draws) else []
                    by_digest = {}
                    for key, xs in candidates.items():
                        if len(xs) > 1 or inline:
                            for x in xs:
                                by_digest.setdefault(fitz.Pixmap(doc, x).digest, set()).add(x)
                if i < len(digests):
                    found = by_digest.get(digests[i], set()) & xrefs
                    if found or inline:
                        xrefs = found  # vacío: una imagen en línea del mismo tamaño
            out.append(Placement(frozenset(xrefs), w, h, matrix, clips))
    return out, visible


def _placement_bbox(p):
    return fitz.Rect(0, 0, 1, 1) * p.matrix


def _to_pixels(p):
    """Función de un punto de la página (sin girar) a píxeles de la imagen; None si la matriz
    no se puede invertir."""
    m = p.matrix
    if abs(m.a * m.d - m.b * m.c) < 1e-12:
        return None
    inv, w, h = ~m, p.width, p.height
    return lambda pt: ((pt[0] * inv.a + pt[1] * inv.c + inv.e) * w,
                       (pt[0] * inv.b + pt[1] * inv.d + inv.f) * h)


def _corners(r):
    return [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]


def _area(pts):
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1])) / 2


def _axis_rect(pts):
    """(x0, y0, x1, y1) si el polígono es un rectángulo alineado con los ejes; si no, None."""
    if len(pts) == 5 and abs(pts[0][0] - pts[4][0]) < 1e-6 and abs(pts[0][1] - pts[4][1]) < 1e-6:
        pts = pts[:4]
    if len(pts) != 4:
        return None
    for a, b in zip(pts, pts[1:] + pts[:1]):
        if abs(a[0] - b[0]) > 1e-6 and abs(a[1] - b[1]) > 1e-6:
            return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _touched(x0, y0, x1, y1, box):
    """Píxeles (rectángulo entero, recortado a box) que comparten área con el rectángulo."""
    c0, c1 = max(math.floor(x0 + PIXEL_EPS), box[0]), min(math.ceil(x1 - PIXEL_EPS), box[2])
    r0, r1 = max(math.floor(y0 + PIXEL_EPS), box[1]), min(math.ceil(y1 - PIXEL_EPS), box[3])
    return (c0, r0, c1, r1) if c0 < c1 and r0 < r1 else None


def _new_mask(box, value=0):
    return Image.new("1", (box[2] - box[0], box[3] - box[1]), value)


def _minus(a, b):
    """Máscara de a sin b (las dos "1", del mismo tamaño)."""
    return ImageChops.logical_xor(a, ImageChops.logical_and(a, b))


def _draw_polygon(mask, box, pts):
    """Marca en mask (que cubre box, en píxeles de la imagen) los píxeles que toca el polígono.
    Un rectángulo alineado con los ejes, exacto; cualquier otro, con su relleno y su borde."""
    r = _axis_rect(pts)
    if r is not None:
        t = _touched(*r, box)
        if t:
            ImageDraw.Draw(mask).rectangle(
                (t[0] - box[0], t[1] - box[1], t[2] - 1 - box[0], t[3] - 1 - box[1]), fill=1)
        return
    if abs(_area(pts)) < PIXEL_EPS:
        return
    # En Pillow el centro del píxel c está en c (aquí, en c + 0,5).
    ImageDraw.Draw(mask).polygon([(x - box[0] - 0.5, y - box[1] - 0.5) for x, y in pts], fill=1, outline=1)


def _zone_box(p, zones):
    """Píxeles (rectángulo entero) que tocan las zonas en este dibujo; None si ninguno."""
    f = _to_pixels(p)
    if f is None:
        return None
    full, box = (0, 0, p.width, p.height), None
    bbox = _placement_bbox(p)
    for z in zones:
        if not z.intersects(bbox):
            continue
        pts = [f(c) for c in _corners(z)]
        t = _touched(min(x for x, _ in pts), min(y for _, y in pts),
                     max(x for x, _ in pts), max(y for _, y in pts), full)
        if t:
            box = t if box is None else (min(box[0], t[0]), min(box[1], t[1]),
                                         max(box[2], t[2]), max(box[3], t[3]))
    return box


def _draw_zones(mask, box, p, zones):
    """Marca en mask los píxeles que tocan las zonas en este dibujo."""
    f = _to_pixels(p)
    if f is None:
        return
    bbox = _placement_bbox(p)
    for z in zones:
        if z.intersects(bbox):
            _draw_polygon(mask, box, [f(c) for c in _corners(z)])


def _visible_mask(p, visible, box):
    """Píxeles (en box) de la imagen que se ven en este dibujo: los que comparten área con lo
    visible de la página y con todos sus recortes. Con recortes de varios subtrazados, par-impar
    (o con sentidos distintos en no nulo) como una o exclusiva; en no nulo con un solo sentido,
    como su unión."""
    f = _to_pixels(p)
    if f is None or p.clips is None:
        return _new_mask(box)
    items = [([_corners(visible)], False)] + p.clips
    # Lo habitual: la página y los recortes son rectángulos alineados también en píxeles de la
    # imagen. Entonces basta su intersección.
    rects = [_axis_rect([f(pt) for pt in sp[0]]) if len(sp) == 1 else None for sp, _ in items]
    if all(r is not None for r in rects):
        x0, y0 = max(r[0] for r in rects), max(r[1] for r in rects)
        x1, y1 = min(r[2] for r in rects), min(r[3] for r in rects)
        result = _new_mask(box)
        if x0 < x1 and y0 < y1:
            _draw_polygon(result, box, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
        return result
    result = _new_mask(box, 1)
    for subpaths, evenodd in items:
        polys = [[f(pt) for pt in sp] for sp in subpaths]
        item = _new_mask(box)
        xor = evenodd or len({_area(q) > 0 for q in polys if abs(_area(q)) >= PIXEL_EPS}) > 1
        for q in polys:
            one = _new_mask(box)
            _draw_polygon(one, box, q)
            item = ImageChops.logical_xor(item, one) if xor else ImageChops.logical_or(item, one)
        result = ImageChops.logical_and(result, item)
        if result.getbbox() is None:
            break
    return result


def mask_rects(mask, box):
    """La máscara como rectángulos enteros (en píxeles de la imagen) que no se solapan: segmentos
    de cada fila, unidos con los iguales de las filas siguientes."""
    w, h = mask.size
    data = mask.convert("L").tobytes()
    rects, open_runs = [], {}
    for y in range(h + 1):
        runs = set()
        if y < h:
            row = data[y * w:(y + 1) * w]
            x = row.find(b"\xff")
            while x >= 0:
                end = row.find(b"\x00", x)
                end = w if end < 0 else end
                runs.add((x, end))
                x = row.find(b"\xff", end)
        for run in [r for r in open_runs if r not in runs]:
            rects.append((run[0] + box[0], open_runs.pop(run) + box[1], run[1] + box[0], y + box[1]))
        for run in runs:
            open_runs.setdefault(run, y)
    return rects


def hidden_pixels_plan(doc, zones_by_page):
    """Antes de censurar: (plan, sin_copia, imágenes por página).

    plan: {xref: entrada} de cada imagen (XObject) que toca una zona, con
    - size: (ancho, alto); kind: (ancho, alto, es máscara);
    - box: rectángulo entero de los píxeles que tocan las zonas;
    - zone_draws: [(página, Placement)] de los dibujos que tocan una zona;
    - draws: {página: [Placement]} de cada página que usa la imagen (aunque no la dibuje);
    - visible: {página: rectángulo visible}.
    sin_copia: True si una zona toca una imagen que no se puede copiar por página (en línea en un
    Form XObject, o dentro de un patrón, que usa otra página): apply no censura. Imágenes por
    página: {página: {kind: xrefs}}, para hidden_image_pixels.
    """
    images_by_page = {page.number: page.get_images(full=True) for page in doc}
    tiling = _has_tiling_patterns(doc)
    pages_using, kinds, in_patterns = {}, {}, {}
    for page in doc:
        n = page.number
        # También las imágenes dibujadas desde patrones de mosaico (p. ej. un fondo CSS en mosaico
        # que Chrome imprime con un patrón por página y la misma imagen en todos).
        for x, kind in page_image_kinds(doc, page, images_by_page[n], tiling):
            pages_using.setdefault(x, set()).add(n)
            kinds.setdefault(n, {}).setdefault(kind, set()).add(x)
        if tiling:
            for x in pattern_images(doc, page):
                in_patterns.setdefault(x, set()).add(n)
    traced = {}

    def page_placements(n):
        if n not in traced:
            traced[n] = placements(doc[n])
        return traced[n]

    plan, unsupported, holders = {}, False, {}
    for n, zones in zones_by_page.items():
        if not zones or not (images_by_page.get(n) or _may_have_inline_images(doc, doc[n])
                             or _page_forms(doc, doc[n], patterns=True)):
            continue
        for p in page_placements(n)[0]:
            bbox = _placement_bbox(p)
            if not any(z.intersects(bbox) for z in zones):
                continue
            if not p.xrefs:  # imagen en línea
                unsupported = unsupported or _inline_in_shared_form(doc, n, holders)
            for x in p.xrefs:
                entry = plan.setdefault(x, {"size": (p.width, p.height), "zone_draws": [],
                                            "kind": (p.width, p.height, doc.xref_get_key(x, "ImageMask")[1] == "true")})
                entry["zone_draws"].append((n, p))
    # La contención no puede copiar por página una imagen dibujada desde un patrón: si la usa más
    # de una página, censurarla podría dejar sus píxeles ocultos en las otras (comprobado con un
    # fondo CSS en mosaico de Chrome cuya pieza mide tres páginas). Cierra en fallo.
    for x in plan:
        if x in in_patterns and len(pages_using[x]) > 1:
            unsupported = True
    for x, entry in list(plan.items()):
        box = None
        for n, p in entry["zone_draws"]:
            t = _zone_box(p, zones_by_page[n])
            if t:
                box = t if box is None else (min(box[0], t[0]), min(box[1], t[1]),
                                             max(box[2], t[2]), max(box[3], t[3]))
        if box is None:
            del plan[x]
            continue
        entry["box"] = box
        entry["draws"], entry["visible"] = {}, {}
        for n in sorted(pages_using.get(x, ())):
            ps, visible = page_placements(n)
            entry["draws"][n] = [p for p in ps if p.xrefs == {x}]
            entry["visible"][n] = visible
    return plan, unsupported, kinds


def _is_tiling(doc, xref):
    try:
        return doc.xref_get_key(xref, "PatternType")[1] == "1"
    except Exception:  # un hueco en la tabla xref (PDF salidos de Unir): no es nada
        return False


def pattern_images(doc, page):
    """Imágenes (XObject) que la página dibuja desde patrones de mosaico, también desde los Form
    que usan: get_images no las da, y la contención no puede copiarlas por página (retarget_image
    solo sigue /XObject)."""
    holders = [page.xref] + sorted(_page_forms(doc, page))
    todo = [x for h in holders for x in _resource_refs(doc, h, "Pattern").values() if _is_tiling(doc, x)]
    seen, images = set(), set()
    while todo:
        x = todo.pop()
        if x in seen:
            continue
        seen.add(x)
        for r in _xobject_refs(doc, x, "Resources").values():
            subtype = doc.xref_get_key(r, "Subtype")[1]
            if subtype == "/Image":
                images.add(r)
            elif subtype == "/Form":
                todo.append(r)
        todo.extend(r for r in _resource_refs(doc, x, "Pattern").values() if _is_tiling(doc, r))
    return images


def page_image_kinds(doc, page, images=None, tiling=True):
    """[(xref, (ancho, alto, es máscara))] de las imágenes que usa la página: las de get_images
    (images, si ya se tienen) y, con tiling, las de sus patrones de mosaico."""
    out = {}
    for im in images if images is not None else page.get_images(full=True):
        out[im[0]] = (im[2], im[3], doc.xref_get_key(im[0], "ImageMask")[1] == "true")
    if tiling:
        for x in pattern_images(doc, page):
            if x not in out:
                out[x] = (int(doc.xref_get_key(x, "Width")[1] or 0), int(doc.xref_get_key(x, "Height")[1] or 0),
                          doc.xref_get_key(x, "ImageMask")[1] == "true")
    return list(out.items())


def _has_tiling_patterns(doc):
    return any(_is_tiling(doc, x) for x in range(1, doc.xref_length()))


def _inline_in_shared_form(doc, number, holders_by_page):
    """¿Dibuja la página un Form XObject (o patrón) con imágenes en línea que usa también otra
    página? holders_by_page: caché {página: Form y patrones} que se va llenando."""
    if not holders_by_page:
        holders_by_page.update({page.number: _page_forms(doc, page, patterns=True) for page in doc})
    holders = holders_by_page[number]
    if not holders:
        return False
    others = set().union(*(h for n, h in holders_by_page.items() if n != number))
    return any(_INLINE_IMAGE.search(doc.xref_stream(x) or b"") for x in holders & others)


def zone_mask(entry, zones_by_page):
    """Píxeles de la imagen que tocan las zonas (en todas las páginas), en entry["box"]."""
    box = entry["box"]
    mask = _new_mask(box)
    for n, p in entry["zone_draws"]:
        _draw_zones(mask, box, p, zones_by_page[n])
    return mask


def erase_mask(entry, number, zones, zone_pixels):
    """Píxeles que hay que borrar en la imagen que usa la página `number`: los de las zonas
    (zone_pixels, de zone_mask) que la página no muestra. Los que tocan una zona de la propia
    página no cuentan como mostrados (están bajo el negro). None si no hay ninguno."""
    box = entry["box"]
    shown = _new_mask(box)
    for p in entry["draws"].get(number, ()):
        vis = _visible_mask(p, entry["visible"][number], box)
        if zones and vis.getbbox() is not None:
            covered = _new_mask(box)
            _draw_zones(covered, box, p, zones)
            vis = _minus(vis, covered)
        shown = ImageChops.logical_or(shown, vis)
    erase = _minus(zone_pixels, shown)
    return erase if erase.getbbox() is not None else None


def _same_mask(a, b):
    return ImageChops.logical_xor(a, b).getbbox() is None


def contention_copies(plan, zones_by_page):
    """Copias que hará la contención (además de las de apply_redactions), para el aviso previo:
    {xref: número}. Una por cada borrado distinto en las páginas en que apply_redactions no hace
    copia y se ve parte de las zonas; y una con todas las zonas borradas para las que no ven
    ninguna, salvo que la deje alguna página censurada."""
    out = {}
    for x, entry in plan.items():
        zp = zone_mask(entry, zones_by_page)
        partial, full_needed, full_done = [], False, False
        for n in entry["draws"]:
            zones = zones_by_page.get(n, [])
            draws = entry["draws"][n]
            erase = erase_mask(entry, n, zones, zp)
            redacted = any(z.intersects(_placement_bbox(p)) for p in draws for z in zones)
            if redacted:
                full_done = full_done or (erase is not None and _same_mask(erase, zp))
            elif erase is not None:
                if not draws or _same_mask(erase, zp):
                    full_needed = True
                elif not any(_same_mask(erase, e) for e in partial):
                    partial.append(erase)
        count = len(partial) + (full_needed and not full_done)
        if count:
            out[x] = count
    return out


def _free(doc, xref):
    """Vacía una copia que ya no usa nadie (save(garbage=4) la quitaría, pero ocupa memoria)."""
    smask = doc.xref_get_key(xref, "SMask")
    doc.update_object(xref, "null")
    if smask[0] == "xref":
        doc.update_object(int(smask[1].split()[0]), "null")


def _own_dict(doc, holder, key):
    """Pone en holder[key] una copia propia del diccionario que tenía (directo o por
    referencia) y devuelve su xref; None si no había."""
    t, v = doc.xref_get_key(holder, key)
    if t == "xref":
        v = doc.xref_object(int(v.split()[0]), compressed=True)
    elif t != "dict":
        return None
    new = doc.get_new_xref()
    doc.update_object(new, v)
    doc.xref_set_key(holder, key, f"{new} 0 R")
    return new


def _reaches(doc, form, target):
    """¿Dibuja el Form XObject (o los que dibuja) la imagen target?"""
    seen, todo = set(), [form]
    while todo:
        x = todo.pop()
        if x in seen:
            continue
        seen.add(x)
        for ref in _xobject_refs(doc, x, "Resources").values():
            if ref == target:
                return True
            if _is_form(doc, ref):
                todo.append(ref)
    return False


def retarget_image(doc, page, old, new):
    """La página usa la imagen new donde usaba old, también dentro de sus Form XObjects, sin
    cambiar las demás páginas: se copian los diccionarios de recursos y los Form del camino.
    Devuelve cuántas referencias ha cambiado (0 si old no estaba en sus recursos: p. ej., solo
    en un patrón; entonces queda, y lo detecta la verificación final)."""
    holder = page.xref
    if doc.xref_get_key(holder, "Resources")[0] == "null":  # heredados de /Pages
        parent = doc.xref_get_key(holder, "Parent")
        while parent[0] == "xref":
            p = int(parent[1].split()[0])
            if doc.xref_get_key(p, "Resources")[0] != "null":
                doc.xref_set_key(holder, "Resources", doc.xref_get_key(p, "Resources")[1])
                break
            parent = doc.xref_get_key(p, "Parent")
    todo, changed = [(holder, 0)], 0
    while todo:
        holder, depth = todo.pop()
        res = _own_dict(doc, holder, "Resources")
        xobjects = _own_dict(doc, res, "XObject") if res else None
        if xobjects is None:
            continue
        for name in doc.xref_get_keys(xobjects):
            t, v = doc.xref_get_key(xobjects, name)
            if t != "xref":
                continue
            ref = int(v.split()[0])
            if ref == old:
                doc.xref_set_key(xobjects, name, f"{new} 0 R")
                changed += 1
            elif depth < MAX_FORM_DEPTH and _is_form(doc, ref) and _reaches(doc, ref, old):
                clone = doc.get_new_xref()
                doc.update_object(clone, "<<>>")
                doc.xref_copy(ref, clone)
                doc.xref_set_key(xobjects, name, f"{clone} 0 R")
                todo.append((clone, depth + 1))
    return changed


# Form XObjects anidados que sigue retarget_image (más es un archivo hecho para esto: lo que
# quede lo detecta la verificación final).
MAX_FORM_DEPTH = 64


class _Scratch:
    """Página auxiliar, al final del documento, en la que MuPDF hace copias de una imagen con
    píxeles borrados (las mismas que apply_redactions: sin comprimir, con su SMask). Se quita
    con close()."""

    def __init__(self, doc):
        # Se crea ya: añadir una página deja inválidos los objetos Page que haya abiertos.
        self.doc, self.page = doc, doc.new_page()

    def copy(self, xref, width, height, rects):
        """xref de una copia nueva de la imagen con esos píxeles (rectángulos enteros) borrados;
        None si MuPDF no la hace."""
        doc = self.doc
        s = min(1.0, SCRATCH_MAX_SIDE / max(width, height))
        page = self.page
        page.set_mediabox(fitz.Rect(0, 0, width * s, height * s))
        res, contents = doc.get_new_xref(), doc.get_new_xref()
        doc.update_object(res, f"<</XObject<</I {xref} 0 R>>>>")
        doc.update_object(contents, "<<>>")
        doc.update_stream(contents, f"q {width * s:.6f} 0 0 {height * s:.6f} 0 0 cm /I Do Q".encode())
        doc.xref_set_key(page.xref, "Resources", f"{res} 0 R")
        doc.xref_set_key(page.xref, "Contents", f"{contents} 0 R")
        for r in rects:
            page.add_redact_annot(fitz.Rect(r) * s, fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE, text=fitz.PDF_REDACT_TEXT_NONE)
        new = [im[0] for im in page.get_images(full=True) if im[0] != xref]
        return new[0] if len(new) == 1 else None

    def close(self):
        """Quita la página auxiliar (deja inválidos los objetos Page abiertos)."""
        if self.page is not None:
            self.doc.delete_page(self.doc.page_count - 1)
            self.page = None


def _pix_bands(pix):
    """(imagen Pillow de los colores, alfa o None, valor de un píxel borrado) de un Pixmap; None
    si sus colores no son gris, RGB o CMYK. Una máscara (ImageMask) es solo alfa: borrado = 0
    (no pinta). CMYK borrado = sin tinta."""
    size = (pix.width, pix.height)
    colorants = pix.n - pix.alpha
    if colorants == 0:
        return Image.frombytes("L", size, pix.samples), None, 0
    name = pix.colorspace.name if pix.colorspace else ""
    mode = {1: "L", 3: "RGB", 4: "CMYK"}.get(colorants)
    if mode is None or any(s in name for s in ("Separation", "DeviceN", "Lab", "Indexed")):
        return None
    alpha = None
    if pix.alpha:
        alpha = Image.frombytes("L", size, fitz.Pixmap(None, pix).samples)
        pix = fitz.Pixmap(pix, 0)
    return Image.frombytes(mode, size, pix.samples), alpha, _ERASED[mode]


# Valor de un píxel borrado por MuPDF, por modo de Pillow: blanco, o sin tinta en CMYK.
_ERASED = {"L": 255, "RGB": (255, 255, 255), "CMYK": (0, 0, 0, 0)}


def _max_band(img):
    bands = img.split()
    out = bands[0]
    for b in bands[1:]:
        out = ImageChops.lighter(out, b)
    return out


def _differs(img, value):
    """Máscara ("1") de los píxeles de img distintos de value."""
    const = Image.new(img.mode, img.size, value)
    return _max_band(ImageChops.difference(img, const)).point(lambda v: 255 if v else 0).convert("1")


def _all_equal(img, mask, value):
    """¿Valen value todos los píxeles de img bajo mask? (En una pasada: los extremos de cada
    canal bajo la máscara.)"""
    values = value if isinstance(value, tuple) else (value,)
    return all(lo == hi == v for (lo, hi), v in zip(ImageStat.Stat(img, mask=mask).extrema, values))


def _tight(box, mask):
    """(box, máscara) recortados a lo que marca la máscara; None si no marca nada."""
    b = mask.getbbox()
    if b is None:
        return None
    return (box[0] + b[0], box[1] + b[1], box[0] + b[2], box[1] + b[3]), mask.crop(b)


def _scaled_box(box, size, to_size):
    """box (de una imagen de tamaño size) en otra de tamaño to_size, redondeado hacia fuera."""
    sx, sy = to_size[0] / size[0], to_size[1] / size[1]
    return (max(0, math.floor(box[0] * sx)), max(0, math.floor(box[1] * sy)),
            min(to_size[0], math.ceil(box[2] * sx)), min(to_size[1], math.ceil(box[3] * sy)))


def _scaled_mask(mask, box, size, to_size):
    """(box, máscara) en otra resolución, contando cada píxel que toque uno de la máscara."""
    if size == to_size:
        return box, mask
    to_box = _scaled_box(box, size, to_size)
    out = _new_mask(to_box)
    for r in mask_rects(mask, box):
        t = _scaled_box(r, size, to_size)
        ImageDraw.Draw(out).rectangle((t[0] - to_box[0], t[1] - to_box[1],
                                       t[2] - 1 - to_box[0], t[3] - 1 - to_box[1]), fill=1)
    return to_box, out


def _smask_xref(doc, xref):
    t, v = doc.xref_get_key(xref, "SMask")
    return int(v.split()[0]) if t == "xref" else None


def erased_in(doc, xref, box, mask):
    """¿Están borrados en la imagen xref (y en su SMask, opaca) todos los píxeles de mask? False
    si no se puede saber (otro espacio de color)."""
    tight = _tight(box, mask)
    if tight is None:
        return True
    box, mask = tight
    try:
        bands = _pix_bands(fitz.Pixmap(doc, xref))
        if bands is None:
            return False
        img, _, value = bands
        if not _all_equal(img.crop(box), mask, value):
            return False
        smask = _smask_xref(doc, xref)
        if smask is not None:
            alpha = fitz.Pixmap(doc, smask)
            a = Image.frombytes("L", (alpha.width, alpha.height), alpha.samples)
            abox, amask = _scaled_mask(mask, box, img.size, a.size)
            if not _all_equal(a.crop(abox), amask, 255):
                return False
    except Exception:
        return False
    return True


class HiddenPixels:
    """La contención en apply (ver la sección), página a página."""

    def __init__(self, doc, plan, zones_by_page):
        self.doc, self.plan, self.zones = doc, plan, zones_by_page
        self.scratch = _Scratch(doc) if plan else None
        self.cache = {}      # xref -> [(máscara de borrado, copia)]
        self.full = {}       # xref -> copia con todos los píxeles de las zonas borrados
        self.deferred = {}   # xref -> páginas que no muestran nada de las zonas
        self.checked = set()  # copias comprobadas con erased_in antes de recomprimirlas
        self._zone_pixels = {}

    def zone_pixels(self, x):
        if x not in self._zone_pixels:
            self._zone_pixels[x] = zone_mask(self.plan[x], self.zones)
        return self._zone_pixels[x]

    def _copy(self, xref, x, mask):
        box = self.plan[x]["box"]
        w, h = self.plan[x]["size"]
        return self.scratch.copy(xref, w, h, mask_rects(mask, box))

    def after_redaction(self, page, pairs):
        """Tras apply_redactions: en cada copia que ha dejado, borra lo que la página no muestra
        de las zonas (también lo que MuPDF haya dejado de una zona de la página). Devuelve las
        parejas (copia, original) con las copias que la sustituyen."""
        doc, out = self.doc, []
        zones = self.zones.get(page.number, [])
        for copy, original in pairs:
            x = original[0]
            if x in self.plan:
                zp = self.zone_pixels(x)
                box = self.plan[x]["box"]
                erase = erase_mask(self.plan[x], page.number, zones, zp)
                # apply_redactions solo borra lo que tocan las zonas de esta página: si hay que
                # borrar más (lo que no muestra de las de otras), la copia se rehace sin mirarla.
                own = _new_mask(box)
                for p in self.plan[x]["draws"].get(page.number, ()):
                    _draw_zones(own, box, p, zones)
                ok = erase is None or (_minus(erase, own).getbbox() is None
                                       and erased_in(doc, copy[0], box, erase))
                if not ok:
                    new = self._copy(copy[0], x, erase)
                    if new is not None and retarget_image(doc, page, copy[0], new):
                        _free(doc, copy[0])
                        copy = (new,) + tuple(copy[1:])
                        ok = erased_in(doc, new, box, erase)
                if ok:
                    self.checked.add(copy[0])
                if erase is not None and _same_mask(erase, zp):
                    self.full.setdefault(x, copy[0])
                # Si la página sigue usando la original (otro dibujo que no tocaba la zona),
                # también la copia: lo que no muestra ya está borrado en ella.
                if any(im[0] == x for im in page.get_images(full=True)):
                    retarget_image(doc, page, x, copy[0])
            out.append((copy, original))
        return out

    def other_pages(self, page, redacted=()):
        """Imágenes de las zonas que la página usa sin que apply_redactions las haya copiado en
        ella: si no muestra entera alguna zona, su propia copia (o, si no muestra ninguna, la
        común con todas borradas, al final)."""
        if not self.plan:
            return
        doc = self.doc
        zones = self.zones.get(page.number, [])
        used = {im[0] for im in page.get_images(full=True)}
        for x in used & self.plan.keys():
            if x in redacted:
                continue
            entry = self.plan[x]
            zp = self.zone_pixels(x)
            erase = erase_mask(entry, page.number, zones, zp)
            if erase is None:
                continue
            if not entry["draws"].get(page.number) or _same_mask(erase, zp):
                self.deferred.setdefault(x, []).append(page.number)
                continue
            copy = next((c for m, c in self.cache.get(x, ()) if _same_mask(m, erase)), None)
            if copy is None:
                copy = self._copy(x, x, erase)
                if copy is None:
                    continue
                if erased_in(doc, copy, entry["box"], erase):
                    self.checked.add(copy)
                recompress_copy(doc, copy, *entry["size"], x, doc.xref_get_key(x, "Filter")[1])
                self.cache.setdefault(x, []).append((erase, copy))
            retarget_image(doc, page, x, copy)

    def finish(self):
        """Las páginas que no muestran nada de las zonas: la copia con todas borradas (la de una
        página censurada, si hay alguna así). Quita la página auxiliar."""
        doc = self.doc
        for x, numbers in self.deferred.items():
            copy = self.full.get(x)
            if copy is None:
                copy = self._copy(x, x, self.zone_pixels(x))
                if copy is None:
                    continue
                if erased_in(doc, copy, self.plan[x]["box"], self.zone_pixels(x)):
                    self.checked.add(copy)
                recompress_copy(doc, copy, *self.plan[x]["size"], x, doc.xref_get_key(x, "Filter")[1])
            for n in numbers:
                retarget_image(doc, doc[n], x, copy)
        if self.scratch:
            self.scratch.close()

    def checked_digests(self):
        """Huellas (SHA-256 de los bytes del flujo, ya recomprimido) de las copias comprobadas
        con erased_in que siguen en el documento: hidden_image_pixels no las vuelve a decodificar
        (recomprimir un píxel borrado no devuelve el original). Una copia que save() vuelva a
        comprimir (las que quedan sin comprimir) cambia de bytes y se decodifica."""
        doc, out = self.doc, set()
        for x in self.checked:
            try:
                if doc.xref_is_stream(x):
                    out.add(hashlib.sha256(doc.xref_stream_raw(x)).hexdigest())
            except Exception:
                pass
        return out


# Comparación con la original en hidden_image_pixels. Copias sin pérdida (o la original sin
# tocar): un píxel de una zona con el mismo valor que en la original y distinto del borrado es
# un resto. Copias con pérdida (JPEG, JPEG 2000), en gris: a LOSSY_CLOSE o menos de la
# original, donde la original está a LOSSY_FAR o más del blanco del borrado, y a LOSSY_MARGIN px
# o más del borde de la parte borrada (allí la recompresión mezcla los dos lados). Una copia que
# conserva la zona la conserva entera: basta un píxel así.
LOSSY_CLOSE = 16
LOSSY_FAR = 64
LOSSY_MARGIN = 2
# Una imagen de la salida viene de una original si, reducidas a SIMILAR_SIZE px de lado y fuera
# de las zonas, se diferencian de media SIMILAR_MAX_DIFF niveles de gris o menos.
SIMILAR_SIZE = 64
SIMILAR_MAX_DIFF = 8


def _gray(img):
    return img if img.mode == "L" else img.convert("RGB").convert("L")


def _similar(a, b, box):
    """¿Viene b de a? Las dos en gris, reducidas, comparadas fuera de box. Si todo es zona, sí."""
    size = (min(SIMILAR_SIZE, a.width), min(SIMILAR_SIZE, a.height))
    ta, tb = _gray(a).resize(size, Image.BOX), _gray(b).resize(size, Image.BOX)
    outside = Image.new("L", size, 255)
    t = _scaled_box(box, a.size, size)
    ImageDraw.Draw(outside).rectangle((t[0], t[1], t[2] - 1, t[3] - 1), fill=0)
    if outside.getbbox() is None:
        return True
    return ImageStat.Stat(ImageChops.difference(ta, tb), mask=outside).mean[0] <= SIMILAR_MAX_DIFF


def _kept(orig, out, value, box, mask, lossy):
    """¿Conserva out (una imagen que viene de orig) algún píxel de mask con su valor original?"""
    a, b = orig.crop(box), out.crop(box)
    if lossy:
        ga, gb = _gray(a), _gray(b)
        close = ImageChops.difference(ga, gb).point(lambda v: 255 if v <= LOSSY_CLOSE else 0)
        far = ga.point(lambda v: 255 if v <= 255 - LOSSY_FAR else 0)
        inner = mask.convert("L").filter(ImageFilter.MinFilter(2 * LOSSY_MARGIN + 1))
        hit = ImageChops.multiply(ImageChops.multiply(close, far), inner)
        return hit.getbbox() is not None
    same = _max_band(ImageChops.difference(a, b)).point(lambda v: 255 if v == 0 else 0).convert("1")
    hit = ImageChops.logical_and(ImageChops.logical_and(same, _differs(a, value)), mask)
    return hit.getbbox() is not None


def _alpha_of(doc, xref, bands):
    """Alfa de una imagen: su SMask, su /Mask de máscara o el del Pixmap (/Mask de colores)."""
    for key in ("SMask", "Mask"):
        t, v = doc.xref_get_key(xref, key)
        if t == "xref":
            pix = fitz.Pixmap(doc, int(v.split()[0]))
            return Image.frombytes("L", (pix.width, pix.height), pix.samples)
    return bands[1]


def hidden_image_pixels(input_path, output_path, plan, zones_by_page, input_images, checked=frozenset()):
    """¿Queda en la salida algún píxel de una zona con su valor original que no se vea?

    Por cada imagen original que toca una zona (plan, de hidden_pixels_plan) y cada imagen de la
    salida que viene de ella (mismo tamaño y tipo, y parecida fuera de las zonas): ningún píxel
    de las zonas puede conservar su valor original, salvo los que se ven en alguna página que la
    usa (no los que caen bajo una zona de esa página). input_images: {página: {(ancho, alto,
    máscara): xrefs}} de la entrada, para saber de qué original viene cada imagen. checked:
    huellas de las copias ya comprobadas antes de recomprimirlas (HiddenPixels.checked_digests),
    que no se vuelven a decodificar. Cierra en fallo: una imagen que no se puede decodificar o
    comparar cuenta como resto.
    """
    entries = plan
    if not entries:
        return False
    by_kind = {}
    for x, e in entries.items():
        by_kind.setdefault(e["kind"], []).append(x)
    out_doc, in_doc = fitz.open(output_path), fitz.open(input_path)
    try:
        # original -> {imagen de la salida: [páginas que la usan, viene seguro de la original]}
        candidates = {}
        for page in out_doc:
            for xref, kind in page_image_kinds(out_doc, page):
                if kind not in by_kind:
                    continue
                # Viene de una imagen del mismo tipo que usaba la página en la entrada (seguro, si
                # solo usaba esa); si no usaba ninguna, de cualquiera.
                inputs = input_images.get(page.number, {}).get(kind)
                for x in (inputs & entries.keys() if inputs else by_kind[kind]):
                    c = candidates.setdefault(x, {}).setdefault(xref, [set(), False])
                    c[0].add(page.number)
                    c[1] = c[1] or inputs == {x}
        traced = {}
        for x, outs in candidates.items():
            entry = entries[x]
            box = entry["box"]
            zp = zone_mask(entry, zones_by_page)
            orig = orig_alpha = None
            for j, (numbers, sure) in outs.items():
                if checked and hashlib.sha256(out_doc.xref_stream_raw(j)).hexdigest() in checked:
                    continue
                shown = _new_mask(box)
                for n in numbers:
                    if n not in traced:
                        traced[n] = placements(out_doc[n])
                    ps, visible = traced[n]
                    view = {"box": box, "draws": {n: [p for p in ps if p.xrefs == {j}]},
                            "visible": {n: visible}}
                    erase = erase_mask(view, n, zones_by_page.get(n, []), zp)
                    shown = ImageChops.logical_or(shown, zp if erase is None else _minus(zp, erase))
                tight = _tight(box, _minus(zp, shown))
                if tight is None:
                    continue
                hbox, hidden = tight
                try:
                    if orig is None:
                        orig = _pix_bands(fitz.Pixmap(in_doc, x))
                        if orig is None:
                            return True
                        orig_alpha = _alpha_of(in_doc, x, orig)
                    bands = _pix_bands(fitz.Pixmap(out_doc, j))
                except Exception:
                    return True
                # Otro espacio de color: no viene de esta original (una copia lo conserva).
                if bands is None or bands[0].mode != orig[0].mode:
                    continue
                if not sure and not _similar(orig[0], bands[0], box):
                    continue
                lossy = any(f in out_doc.xref_get_key(j, "Filter")[1] for f in ("DCT", "JPX"))
                if _kept(orig[0], bands[0], orig[2], hbox, hidden, lossy):
                    return True
                smask = _smask_xref(out_doc, j)
                if orig_alpha is not None and smask is not None:
                    pix = fitz.Pixmap(out_doc, smask)
                    alpha = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                    if alpha.size != orig_alpha.size:
                        return True
                    abox, amask = _scaled_mask(hidden, hbox, orig[0].size, alpha.size)
                    if _kept(orig_alpha, alpha, 255, abox, amask, False):
                        return True
    finally:
        out_doc.close()
        in_doc.close()
    return False


# ==========================================
# 5. MARCADORES
# ==========================================
def remove_outline_terms(doc, terms):
    """Quita los marcadores cuyo título contiene un término; sus hijos suben de nivel."""
    toc = doc.get_toc(simple=False)
    if not toc or not terms:
        return 0
    kept, removed = [], 0
    stack = []  # (nivel original, nivel nuevo) de los antepasados conservados
    for level, title, page, *rest in toc:
        while stack and stack[-1][0] >= level:
            stack.pop()
        if any(t in norm(title) for t in terms):
            removed += 1
            continue
        new_level = stack[-1][1] + 1 if stack else 1
        stack.append((level, new_level))
        kept.append([new_level, title, page, *rest])
    if removed:
        doc.set_toc(kept)
    return removed


# ==========================================
# 6. ADJUNTOS POR /AF
# ==========================================
def remove_associated_files(doc):
    """Quita /AF (archivos asociados: PDF/A-3, Factur-X) de todos los objetos que lo tengan.

    scrub(embedded_files=True) vacía el árbol de nombres /EmbeddedFiles, pero un adjunto
    también puede colgar de /AF (del catálogo, de una página...), con o sin entrada en el
    árbol. Se tratan igual que los del árbol: fuera. Sin referencias, el Filespec y su
    flujo se quedan huérfanos y save(garbage=4) los elimina.
    """
    removed = 0
    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(xref, "AF")[0] == "null":
                continue
        except Exception:
            continue
        doc.xref_set_key(xref, "AF", "null")
        removed += 1
    return removed


# ==========================================
# 7. CADENAS CON UN TÉRMINO FUERA DE LA PÁGINA (capas, acciones, estructura...)
# ==========================================
# Se recorren los objetos con la API de MuPDF. Las cadenas se comparan con term_matcher,
# el mismo criterio que el barrido final (objects_with_terms): lo que se limpia aquí es
# exactamente lo que la verificación daría por fuga.
_m = fitz.mupdf


def _catalog(doc):
    return _m.pdf_dict_get(_m.pdf_trailer(fitz._as_pdf_document(doc)), _m.PDF_ENUM_NAME_Root)


def _string_bytes(obj):
    """Bytes de una cadena PDF (pdf_to_string corta en el primer byte nulo)."""
    src = fitz.JM_object_to_buffer(_m.pdf_resolve_indirect(obj), 1, 0).fz_buffer_extract()
    return b"".join(_pdf_strings(src.decode("latin-1")))


# No se sigue hacia el padre ni hacia la página: se saldría del objeto que se examina.
_UPWARD_KEYS = {"Parent", "P"}

# Niveles de arrays, diccionarios y referencias que recorren _walk_strings y
# _clean_named_dests. Los dos usan una pila propia: la profundidad no gasta la de Python (antes,
# recursivos, una cadena de unas 1.000 referencias daba RecursionError y 500) y con `seen` cada
# objeto se visita una vez, así que el recorrido siempre acaba. El tope solo es una red de
# seguridad que falla cerrado (422), muy por encima de cualquier PDF real: la cadena más larga
# que puede tener uno normal es una lista enlazada (las cuentas /N de un artículo, a las que
# llega un marcador que lo abre), de una o pocas por página. Con anidamiento directo, MuPDF ya
# no lee un objeto de más de unos 256 niveles (ni lo escribe en la salida).
MAX_NESTING = 10_000


class NestingTooDeep(Exception):
    """Un objeto más anidado que MAX_NESTING: no se ha podido recorrer entero."""


def _walk_frame(obj, pages, seen, depth):
    """Marco de la pila de _walk_strings para obj ([obj, siguiente índice, longitud, nivel,
    es array]), o None si no hay nada que recorrer (página, ya visitado, ni array ni dict)."""
    if depth > MAX_NESTING:
        raise NestingTooDeep()
    if _m.pdf_is_indirect(obj):
        num = _m.pdf_to_num(obj)
        if num in pages or num in seen:
            return None
        seen.add(num)
    if _m.pdf_is_array(obj):
        return [obj, 0, _m.pdf_array_len(obj), depth, True]
    if _m.pdf_is_dict(obj):
        return [obj, 0, _m.pdf_dict_len(obj), depth, False]
    return None


def _walk_strings(obj, pages, seen):
    """(contenedor, clave o índice, cadena) de cada cadena de obj y de lo que referencia,
    salvo páginas y Parent/P, en profundidad y en orden. `seen`: números de objeto ya visitados.
    NestingTooDeep si pasa de MAX_NESTING niveles: lo que haya más abajo no se puede dar por
    limpio."""
    stack = []
    frame = _walk_frame(obj, pages, seen, 0)
    if frame:
        stack.append(frame)
    while stack:
        frame = stack[-1]
        container, i, length, depth, is_array = frame
        if i >= length:
            stack.pop()
            continue
        frame[1] = i + 1
        if is_array:
            key, val = i, _m.pdf_array_get(container, i)
        else:
            key = _m.pdf_dict_get_key(container, i)
            if _m.pdf_to_name(key) in _UPWARD_KEYS:
                continue
            val = _m.pdf_dict_get_val(container, i)
        if _m.pdf_is_string(val):
            yield container, key, val
            continue
        child = _walk_frame(val, pages, seen, depth + 1)
        if child:
            stack.append(child)


def _has_term(obj, found, pages):
    """¿Alguna cadena de obj, o de lo que referencia (salvo páginas y Parent/P), contiene
    un término?"""
    if _m.pdf_is_string(obj):  # p. ej. un destino con nombre: /Dest (nombre)
        return found(_string_bytes(obj))
    return any(found(_string_bytes(s)) for _, _, s in _walk_strings(obj, pages, set()))


def _blank_term_strings(obj, found, pages):
    """Vacía cada cadena con un término de obj y de lo que referencia. Devuelve cuántas."""
    hits = [(c, k) for c, k, s in _walk_strings(obj, pages, set()) if found(_string_bytes(s))]
    for container, key in hits:
        empty = _m.pdf_new_text_string("")
        if _m.pdf_is_array(container):
            _m.pdf_array_put(container, key, empty)
        else:
            _m.pdf_dict_put(container, key, empty)
    return len(hits)


def clean_layer_names(doc, found):
    """Capas OCG: el nombre de una capa con un término pasa a "Capa N"; cualquier otra
    cadena con un término en /OCProperties (nombre de configuración, etiquetas de /Order,
    /Usage de las capas...) se vacía. Devuelve cuántas cadenas cambió."""
    pages = {p.xref for p in doc}
    changed = layer = 0
    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(xref, "Type") != ("name", "/OCG"):
                continue
        except Exception:
            continue
        layer += 1
        ocg = _m.pdf_load_object(fitz._as_pdf_document(doc), xref)
        name = _m.pdf_dict_gets(ocg, "Name")
        if _m.pdf_is_string(name) and found(_string_bytes(name)):
            _m.pdf_dict_puts(ocg, "Name", _m.pdf_new_text_string(f"Capa {layer}"))
            changed += 1
        changed += _blank_term_strings(ocg, found, pages)
    ocp = _m.pdf_dict_gets(_catalog(doc), "OCProperties")
    if _m.pdf_is_dict(ocp):
        changed += _blank_term_strings(ocp, found, pages)
    return changed


def _clean_outline_targets(doc, found, pages):
    """Marcadores (el título ya lo mira remove_outline_terms) cuya acción o destino tiene un
    término: una URI, o un destino con nombre. Si apuntaban a una página, pasan a un destino
    explícito a esa página; si no (URI), se quedan sin destino."""
    pdf = fitz._as_pdf_document(doc)
    changed = 0
    for idx, (_, _, pno, dest) in enumerate(doc.get_toc(simple=False)):
        item = _m.pdf_load_object(pdf, dest["xref"])
        targets = [_m.pdf_dict_gets(item, k) for k in ("A", "Dest")]
        if not any(_has_term(t, found, pages) for t in targets if not _m.pdf_is_null(t)):
            continue
        # set_toc_item escribe /A pero no quita /Dest, que tiene prioridad: fuera las dos.
        _m.pdf_dict_dels(item, "A")
        _m.pdf_dict_dels(item, "Dest")
        if pno >= 1 and dest.get("kind") in (fitz.LINK_GOTO, fitz.LINK_NAMED):
            doc.set_toc_item(idx, kind=fitz.LINK_GOTO, pno=pno, to=dest.get("to"))
        else:
            doc.set_toc_item(idx, kind=fitz.LINK_NONE)
        changed += 1
    return changed


def _clean_named_dests(doc, found, pages):
    """Quita de /Names /Dests los destinos cuyo nombre tiene un término. Si quita alguno, el
    árbol se reescribe plano (una sola hoja con los que quedan, en el mismo orden)."""
    names = _m.pdf_dict_gets(_catalog(doc), "Names")
    dests = _m.pdf_dict_gets(names, "Dests")
    if not _m.pdf_is_dict(dests):
        return 0
    kept, removed, seen = [], 0, set()

    def enter(node, depth):
        """Marco [nodo, siguiente hijo, nº de hijos, nivel], o None si ya se visitó."""
        if depth > MAX_NESTING:
            raise NestingTooDeep()
        if _m.pdf_is_indirect(node):
            if _m.pdf_to_num(node) in seen:
                return None
            seen.add(_m.pdf_to_num(node))
        return [node, 0, _m.pdf_array_len(_m.pdf_dict_gets(node, "Kids")), depth]

    # En profundidad con pila propia (como _walk_strings): primero los hijos de cada nodo, en
    # orden, y después sus propios /Names, así que `kept` sale en el orden del árbol.
    stack = [enter(dests, 0)]
    while stack:
        frame = stack[-1]
        node, i, nkids, depth = frame
        if i < nkids:
            frame[1] = i + 1
            child = enter(_m.pdf_array_get(_m.pdf_dict_gets(node, "Kids"), i), depth + 1)
            if child:
                stack.append(child)
            continue
        stack.pop()
        pairs = _m.pdf_dict_gets(node, "Names")
        for i in range(0, _m.pdf_array_len(pairs) - 1, 2):
            key, val = _m.pdf_array_get(pairs, i), _m.pdf_array_get(pairs, i + 1)
            if (_m.pdf_is_string(key) and found(_string_bytes(key))) or _has_term(val, found, pages):
                removed += 1
            else:
                kept.append((key, val))
    if removed:
        pdf = fitz._as_pdf_document(doc)
        flat = _m.pdf_new_array(pdf, 2 * len(kept))
        for key, val in kept:
            _m.pdf_array_push(flat, key)
            _m.pdf_array_push(flat, val)
        root = _m.pdf_new_dict(pdf, 1)
        _m.pdf_dict_puts(root, "Names", flat)
        _m.pdf_dict_puts(names, "Dests", root)
    return removed


def _clean_page_labels(doc, found):
    """Etiquetas de página (/PageLabels) con un término en el prefijo: prefijo vacío."""
    labels = doc.get_page_labels()
    hits = [lab for lab in labels if found(lab.get("prefix", "").encode("utf-8"))]
    for lab in hits:
        lab["prefix"] = ""
    if hits:
        doc.set_page_labels(labels)
    return len(hits)


def _clean_threads(doc, found, pages):
    """Artículos (/Threads): quita el diccionario de información (/I: título, autor...) del
    que tenga un término."""
    threads = _m.pdf_dict_gets(_catalog(doc), "Threads")
    changed = 0
    for i in range(_m.pdf_array_len(threads)):
        thread = _m.pdf_array_get(threads, i)
        info = _m.pdf_dict_gets(thread, "I")
        if not _m.pdf_is_null(info) and _has_term(info, found, pages):
            _m.pdf_dict_dels(thread, "I")
            changed += 1
    return changed


# Texto de la estructura lógica (StructElem): texto alternativo, texto real, título y
# expansión de una abreviatura. Se vacían.
_STRUCT_TEXT_KEYS = ("Alt", "ActualText", "T", "E")


def clean_structures(doc, found):
    """Quita o vacía lo que contenga un término en: destinos y URI de los marcadores,
    /Names /Dests, /OpenAction, /AA (acciones de catálogo, página, anotación o campo),
    /PieceInfo, texto de la estructura lógica (/Alt, /ActualText, /T, /E), /PageLabels y
    /Threads. Devuelve cuántos cambios hizo."""
    pages = {p.xref for p in doc}
    pdf = fitz._as_pdf_document(doc)
    changed = _clean_outline_targets(doc, found, pages)
    changed += _clean_named_dests(doc, found, pages)
    changed += _clean_page_labels(doc, found)
    changed += _clean_threads(doc, found, pages)
    cat = _catalog(doc)
    action = _m.pdf_dict_gets(cat, "OpenAction")
    if not _m.pdf_is_null(action) and _has_term(action, found, pages):
        _m.pdf_dict_dels(cat, "OpenAction")
        changed += 1
    for xref in range(1, doc.xref_length()):
        try:
            obj = _m.pdf_load_object(pdf, xref)
        except Exception:
            continue
        if not _m.pdf_is_dict(obj):
            continue
        aa = _m.pdf_dict_gets(obj, "AA")
        if _m.pdf_is_dict(aa):
            triggers = [_m.pdf_dict_get_key(aa, i) for i in range(_m.pdf_dict_len(aa))]
            for trigger in triggers:
                if _has_term(_m.pdf_dict_get(aa, trigger), found, pages):
                    _m.pdf_dict_del(aa, trigger)
                    changed += 1
            if _m.pdf_dict_len(aa) == 0:
                _m.pdf_dict_dels(obj, "AA")
        piece = _m.pdf_dict_gets(obj, "PieceInfo")
        if not _m.pdf_is_null(piece) and _has_term(piece, found, pages):
            _m.pdf_dict_dels(obj, "PieceInfo")
            changed += 1
        for key in _STRUCT_TEXT_KEYS:
            val = _m.pdf_dict_gets(obj, key)
            if _m.pdf_is_string(val) and found(_string_bytes(val)):
                _m.pdf_dict_puts(obj, key, _m.pdf_new_text_string(""))
                changed += 1
    return changed


# ==========================================
# 8. RELLENOS LISOS QUE TOCA UNA ZONA
# ==========================================
# REMOVE_IF_TOUCHED quita entero todo trazo que toque una zona, también un fondo de página o
# el sombreado de una celda (la página quedaba en blanco). Antes de censurar se guardan los
# rectángulos de un solo color sin trazo que tocan una zona y, después, los que han
# desaparecido se vuelven a pintar debajo de todo el contenido: el negro de la censura y el
# texto quedan encima. Un rectángulo liso solo lleva su color y su sitio, nada que censurar.
# Cualquier otro trazo (curvas, firmas, sellos, degradados, formas irregulares) se sigue
# quitando.
#
# Solo se repinta debajo un relleno que no tapaba nada de lo dibujado antes (salvo otros
# rellenos que también se repintan debajo y lo que cae entero dentro de una zona). Uno que sí
# tapaba algo (decisión de diseño, 2026-10-07: censura falsa, un rectángulo puesto encima de un
# dato para ocultarlo) se repinta ENCIMA de todo, en su sitio, con su color y su opacidad:
# el documento nunca queda menos tapado que el original, aunque así tape también lo que se
# le dibujaba encima. El negro de las zonas que toca se vuelve a pintar sobre él. Solo
# rectángulos lisos: una firma o un sello rellenos que tapan texto se siguen quitando.

def _path_rects(items):
    """Rectángulos (sin girar) que forman el trazado, o None si no es solo rectángulos
    alineados con los ejes: "re", un "qu" rectangular o 3-4 líneas horizontales y
    verticales que cierran un rectángulo."""
    if items and all(it[0] == "re" for it in items):
        return [fitz.Rect(it[1]) for it in items]
    if len(items) == 1 and items[0][0] == "qu":
        points = list(items[0][1])
    elif 3 <= len(items) <= 4 and all(it[0] == "l" for it in items):
        if any(a.x != b.x and a.y != b.y for _, a, b in items):
            return None
        points = [p for it in items for p in it[1:]]
    else:
        return None
    xs, ys = {round(p.x, 3) for p in points}, {round(p.y, 3) for p in points}
    if len(xs) != 2 or len(ys) != 2:
        return None
    return [fitz.Rect(min(xs), min(ys), max(xs), max(ys))]


@contextlib.contextmanager
def _unrotated(page):
    """La página con /Rotate 0 mientras dura el bloque (al salir, la rotación de antes)."""
    rotation = page.rotation
    if rotation:
        page.set_rotation(0)
    try:
        yield page
    finally:
        if rotation:
            page.set_rotation(rotation)


def paint_rects(page, fills, overlay=True):
    """Pinta rectángulos lisos dados sin girar (como los de search_for): fills es
    [(rects, color, opacidad, con borde)]; encima de todo o, con overlay=False, debajo.

    Con /Rotate 0 mientras se dibuja: con otra rotación, PyMuPDF 1.28.2 pasa a espacio de
    usuario con Matrix(1, 0, 0, -1, 0, alto de la CropBox) (Page.transformation_matrix), que
    no tiene en cuenta el origen de la CropBox, y con una CropBox desplazada respecto a la
    MediaBox el rectángulo salía desplazado o fuera de la página. Le pasaba al negro de la
    censura (apply_redactions lo pinta así) y a los rellenos repintados. Con 0 usa la matriz
    de MuPDF, que sí lo tiene en cuenta.
    """
    with _unrotated(page):
        shape = page.new_shape()
        for rects, color, opacity, border in fills:
            for r in rects:
                shape.draw_rect(r)
            if border:
                shape.finish(fill=color, color=color, fill_opacity=opacity)
            else:
                shape.finish(fill=color, color=None, fill_opacity=opacity, width=0)
        shape.commit(overlay=overlay)


# El negro de la censura, como lo pintaba apply_redactions: relleno y borde de 1 pt.
BLACK = (0, 0, 0)


def paint_zones(page, zones):
    paint_rects(page, [(zones, BLACK, 1, True)])


def _content_bboxlog(page):
    """Como page.get_bboxlog() (orden de dibujo, sin girar), pero solo del contenido de la
    página: get_bboxlog incluye las anotaciones, y la de censura que ya hemos puesto
    parecería dibujada encima de todo lo que toca."""
    log = []
    with _unrotated(page):
        dev = fitz.JM_new_bbox_device(log, False)
        _m.fz_run_page_contents(page.this, dev, _m.FzMatrix(), _m.FzCookie())
        _m.fz_close_device(dev)
    return log


def _fill_key(path):
    return (tuple(round(v, 1) for v in path["rect"]), path["fill"], round(path.get("fill_opacity") or 1, 3))


# Relleno liso guardado antes de censurar. over: tapaba algo dibujado antes y se repinta
# encima; later: además, algo dibujado después se le superpone (queda tapado).
SavedFill = namedtuple("SavedFill", "key rects color opacity over later")


def solid_fills_touching(page, zones):
    """Rellenos lisos que tocan una zona y se pueden repintar, en orden de dibujo
    ([SavedFill], con los rectángulos ya recortados)."""
    if not zones:
        return []
    index = ZoneIndex(zones)
    log = _content_bboxlog(page)
    stack = []  # recortes y grupos activos: (nivel, entrada)
    candidates = []
    for path in page.get_drawings(extended=True):
        level = path.get("level", 0)
        while stack and stack[-1][0] >= level:
            stack.pop()
        if path["type"] in ("clip", "group"):
            stack.append((level, path))
            continue
        if path["type"] != "f" or path.get("fill") is None or not index.touches(fitz.Rect(path["rect"])):
            continue
        rects = _path_rects(path["items"])
        opacity = path.get("fill_opacity") or 1
        if rects is None or (len(rects) > 1 and opacity < 1):
            continue
        clip, ok = fitz.INFINITE_RECT(), True
        for _, active in stack:
            if active["type"] == "clip":
                clip_rects = _path_rects(active.get("items") or [])
                if clip_rects is None or len(clip_rects) != 1:
                    ok = False  # recorte no rectangular: no se puede reproducir
                    break
                clip &= fitz.Rect(active["scissor"])
            elif active.get("opacity") not in (None, 1) or active.get("blendmode") not in (None, "Normal"):
                ok = False  # grupo transparente: el color no sería el mismo
                break
        rects = [r & clip for r in rects]
        rects = [r for r in rects if not r.is_empty]
        seqno = path.get("seqno")
        if not ok or not rects or seqno is None or seqno >= len(log) or log[seqno][0] != "fill-path":
            continue
        candidates.append((seqno, _fill_key(path), rects, path["fill"], opacity))
    # ¿Tapaba algo dibujado antes? Lo que cae entero dentro de una zona desaparece igual, y el
    # texto invisible (capa OCR) no se ve. Si tapaba algo, va encima (censura falsa: un
    # rectángulo puesto sobre un dato para ocultarlo); si no, debajo.
    def overlapping(indexes, rects, skip):
        for i in indexes:
            kind, bbox = log[i]
            if i in skip or kind == "ignore-text":
                continue
            box = fitz.Rect(bbox)
            if box.is_empty or not any(box.intersects(r) for r in rects):
                continue
            if any(z.contains(box) for z in index.candidates(box.y0, box.y1)):
                continue
            yield i

    under, saved = set(), []
    for seqno, key, rects, color, opacity in candidates:
        if next(overlapping(range(seqno), rects, under), None) is None:
            under.add(seqno)
            saved.append(SavedFill(key, rects, color, opacity, False, False))
        else:
            # Lo dibujado después que se le superpone queda tapado al repintarlo encima.
            later = next(overlapping(range(seqno + 1, len(log)), rects, ()), None) is not None
            saved.append(SavedFill(key, rects, color, opacity, True, later))
    return saved


def restore_fills(page, saved, zones):
    """Vuelve a pintar los rellenos de `saved` que apply_redactions ha quitado: debajo de
    todo, o encima de todo los que tapaban algo dibujado antes, para que el documento nunca
    quede menos tapado que el original. Sobre estos últimos se vuelve a pintar el negro de
    las zonas que tocan. Devuelve cuántos repintó."""
    if not saved:
        return 0
    remaining = Counter(_fill_key(p) for p in page.get_drawings() if p["type"] == "f" and p.get("fill") is not None)
    missing = []
    for fill in saved:
        if remaining[fill.key]:
            remaining[fill.key] -= 1
        else:
            missing.append(fill)
    for over in (False, True):
        fills = [f for f in missing if f.over == over]
        if not fills:
            continue
        paints = [(f.rects, f.color, f.opacity, False) for f in fills]
        if over:
            touched = [z for z in zones if any(z.intersects(r) for f in fills for r in f.rects)]
            paints.append((touched, BLACK, 1, False))
        paint_rects(page, paints, overlay=over)
    return len(missing)


# ==========================================
# 9. VERIFICACIÓN FINAL
# ==========================================
def _contains_term(value, terms):
    v = norm(value if isinstance(value, str) else str(value or ""))
    return bool(v) and any(t in v for t in terms)


def _decode_any(data):
    texts = []
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            texts.append(data.decode(enc))
        except Exception:
            pass
    return texts


# Barrido de objetos: guiones y espacios no cuentan ("12345678-Z" = "12345678 Z" = "12345678Z").
_SEPARATORS = re.compile(r"[\s\-‐-―−­]+")
_ESCAPES = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}


def _compact(s):
    return _SEPARATORS.sub("", norm(s))


def _pdf_strings(source):
    """Cadenas literales y hexadecimales (como bytes) de un objeto en sintaxis PDF."""
    out = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c == "(":
            buf, depth, i = bytearray(), 1, i + 1
            while i < n:
                c = source[i]
                if c == "\\" and i + 1 < n:
                    e = source[i + 1]
                    if e in "01234567":
                        j = i + 1
                        while j < n and j < i + 4 and source[j] in "01234567":
                            j += 1
                        buf.append(int(source[i + 1:j], 8) & 0xFF)
                        i = j
                        continue
                    if e in "\r\n":  # continuación de línea
                        i += 3 if source[i + 1:i + 3] == "\r\n" else 2
                        continue
                    buf.append(_ESCAPES.get(ord(e), ord(e) & 0xFF))
                    i += 2
                    continue
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                buf.append(ord(c) & 0xFF)
                i += 1
            out.append(bytes(buf))
        elif c == "<" and source.startswith("<<", i):
            i += 2
        elif c == "<":
            k = source.find(">", i)
            if k == -1:
                break
            digits = re.sub(r"[^0-9A-Fa-f]", "", source[i + 1:k])
            out.append(bytes.fromhex(digits + "0" * (len(digits) % 2)))
            i = k + 1
        else:
            i += 1
    return out


def _texts(data):
    """Lecturas posibles de unos bytes: PDFDocEncoding/latin-1, UTF-8 y UTF-16 (con o sin
    BOM; sin BOM, el latin-1 sin los bytes nulos cubre el ASCII en UTF-16BE y LE)."""
    texts = []
    if data[:2] in (b"\xfe\xff", b"\xff\xfe"):
        try:
            texts.append(data.decode("utf-16"))
        except UnicodeDecodeError:
            pass
    latin = data.decode("latin-1")
    texts += [latin, latin.replace("\x00", "")]
    try:
        texts.append(data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        pass
    if len(data) % 2 == 0 and b"\x00" in data:
        for enc in ("utf-16-be", "utf-16-le"):
            try:
                texts.append(data.decode(enc))
            except UnicodeDecodeError:
                pass
    return texts


def _excluded_streams(doc):
    """Flujos que el barrido no lee: contenidos (página, Form XObjects, patrones, glifos
    Type3: el texto de página se verifica por geometría y por texto oculto), fuentes y sus
    CMaps, e imágenes y perfiles ICC (binarios: un término corto aparecería por azar)."""
    excluded = set()
    for page in doc:
        excluded.update(page.get_contents())

    def ref(value):
        kind, v = value
        return int(v.split()[0]) if kind == "xref" else None

    for xref in range(1, doc.xref_length()):
        try:
            typ = doc.xref_get_key(xref, "Type")[1]
            subtype = doc.xref_get_key(xref, "Subtype")[1]
            if typ == "/Font":
                for key in ("ToUnicode", "Encoding", "CIDToGIDMap"):
                    x = ref(doc.xref_get_key(xref, key))
                    if x:
                        excluded.add(x)
                procs = doc.xref_get_key(xref, "CharProcs")  # glifos Type3
                if procs[0] == "xref":
                    procs = ("dict", doc.xref_object(ref(procs)))
                if procs[0] == "dict":
                    excluded.update(int(m) for m in re.findall(r"(\d+) 0 R", procs[1]))
            elif typ == "/FontDescriptor":
                for key in ("FontFile", "FontFile2", "FontFile3", "CIDSet"):
                    x = ref(doc.xref_get_key(xref, key))
                    if x:
                        excluded.add(x)
            if not doc.xref_is_stream(xref):
                continue
            if (subtype in ("/Form", "/Image") or typ in ("/Pattern", "/CMap", "/ObjStm", "/XRef")
                    or doc.xref_get_key(xref, "PatternType")[0] != "null"
                    or doc.xref_get_key(xref, "ShadingType")[0] != "null"
                    or doc.xref_get_key(xref, "FunctionType")[0] != "null"):
                excluded.add(xref)
            elif doc.xref_get_key(xref, "N")[0] == "int" and (doc.xref_stream(xref) or b"")[36:40] == b"acsp":
                excluded.add(xref)  # perfil ICC
        except Exception:
            continue
    return excluded


_FONT_OBJECTS = ("/Font", "/FontDescriptor", "/Encoding")


def term_matcher(terms):
    """Función bytes -> bool: ¿contienen esos bytes un término, sin contar guiones ni
    espacios y en cualquiera de las lecturas de _texts? Las cadenas que escribimos nosotros
    (OUTPUT_METADATA) no cuentan. None si no hay términos que buscar fuera de la página."""
    compact_terms = {c for c in (_compact(t) for t in terms) if len(c) >= MIN_TERM_LEN}
    if not compact_terms:
        return None
    ours = {_compact(v) for v in OUTPUT_METADATA.values()}

    def found(data):
        for text in _texts(data):
            c = _compact(text)
            if c and c not in ours and any(t in c for t in compact_terms):
                return True
        return False

    return found


def objects_with_terms(doc, terms):
    """¿Aparece un término (sin contar guiones ni espacios) en algún objeto del PDF?

    Recorre todos los objetos descomprimidos (también los de object streams): las cadenas
    literales y hexadecimales de cada diccionario o array, y el contenido de los flujos que
    no son contenido de página, fuentes ni imágenes. Las cadenas que escribimos nosotros
    (OUTPUT_METADATA) no cuentan.
    """
    found = term_matcher(terms)
    if found is None:
        return False
    excluded = _excluded_streams(doc)

    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(xref, "Type")[1] in _FONT_OBJECTS:
                continue
            source = doc.xref_object(xref, compressed=False)
        except Exception:
            continue
        if any(found(s) for s in _pdf_strings(source)):
            return True
        if xref not in excluded and doc.xref_is_stream(xref) and found(doc.xref_stream(xref) or b""):
            return True
    return False


# Comprobación por render de las zonas sobre imágenes (zones_not_black): resolución y gris
# máximo (0 negro, 255 blanco) de los píxeles que caen enteros dentro de la zona.
RENDER_DPI = 36
RENDER_MAX_GRAY = 60


def zones_not_black(page, zones):
    """¿Alguna zona que toca una imagen no se ve negra? Solo en las páginas donde una zona
    toca una imagen: en el texto ya mira la geometría chars_in_zones, pero los píxeles de una
    imagen no se pueden buscar. (Con /Rotate y la CropBox desplazada, el negro llegó a salir
    lejos de su zona: fix/censura-rotate-cropbox, 2026-10-17.)

    Página entera a RENDER_DPI en gris, con /Rotate 0 (sin las matrices de PyMuPDF que fallan
    con rotación, ver paint_rects): cada zona, recortada a lo visible y reducida 1 px, debe
    tener todos sus píxeles a RENDER_MAX_GRAY o menos. Una zona que se queda sin píxeles
    enteros a esa resolución (menos de unos 4 pt) no se puede comprobar así.
    """
    if not zones:
        return False
    with _unrotated(page):
        visible = page.rect
        images = [fitz.Rect(i["bbox"]) for i in page.get_image_info()]
        targets = [z & visible for z in zones if any(z.intersects(b) for b in images)]
        targets = [t for t in targets if not t.is_empty]
        if not targets:
            return False
        pix = page.get_pixmap(dpi=RENDER_DPI, colorspace=fitz.csGRAY, alpha=False)
    scale = RENDER_DPI / 72
    samples, stride = pix.samples, pix.stride
    for t in targets:
        x0 = max(math.ceil(t.x0 * scale + 1) - pix.x, 0)
        y0 = max(math.ceil(t.y0 * scale + 1) - pix.y, 0)
        x1 = min(math.floor(t.x1 * scale - 1) - pix.x, pix.width)
        y1 = min(math.floor(t.y1 * scale - 1) - pix.y, pix.height)
        for y in range(y0, y1):
            if x0 < x1 and max(samples[y * stride + x0:y * stride + x1]) > RENDER_MAX_GRAY:
                return True
    return False


def verify_redaction(path, zones_by_page, terms):
    """Busca de nuevo en el PDF de salida. Devuelve la lista de sitios con fugas.

    - Texto de página visible: ningún carácter puede tener el centro dentro de una zona
      censurada (geometría: una aparición visible que el usuario decidió no censurar no es
      una fuga).
    - Texto de página oculto (fuera del CropBox o del MediaBox, desplazado fuera de la
      página o en una capa apagada): no puede aparecer ningún término censurado. El
      usuario no lo ha visto, así que no ha podido decidir dejarlo.
    - Campos, anotaciones, enlaces, metadatos (Info y XMP), marcadores y adjuntos: no
      puede aparecer ningún término censurado.
    - Zonas que tocan una imagen: deben verse negras en el render (zones_not_black).
    Nunca devuelve los términos: el resultado va al log.
    """
    leaks = set()
    doc = fitz.open(path)
    all_layers, copy_path = open_all_layers(path)
    try:
        for page in doc:
            if chars_in_zones(page, zones_by_page.get(page.number)):
                leaks.add("page_text")
            if zones_not_black(page, zones_by_page.get(page.number)):
                leaks.add("render_not_black")
            if _contains_term(hidden_page_text(page, all_layers[page.number]), terms):
                leaks.add("hidden_text")
            for w in page.widgets():
                if any(_contains_term(v, terms) for v in (w.field_value, w.field_label, w.field_name)):
                    leaks.add("form_fields")
            for a in page.annots():
                info = a.info or {}
                if any(_contains_term(v, terms) for v in info.values()):
                    leaks.add("annotations")
            for link in page.get_links():
                if any(_contains_term(link.get(k), terms) for k in ("uri", "file", "nameddest")):
                    leaks.add("links")

        if doc.xref_get_key(doc.pdf_catalog(), "AcroForm")[0] != "null":
            leaks.add("form_fields")

        for key, value in (doc.metadata or {}).items():
            if OUTPUT_METADATA.get(key) == value:
                continue
            if key not in ("format", "encryption") and _contains_term(value, terms):
                leaks.add("metadata_info")
        if _contains_term(doc.get_xml_metadata(), terms):
            leaks.add("metadata_xmp")

        if any(_contains_term(entry[1], terms) for entry in doc.get_toc(simple=False)):
            leaks.add("outline")

        for name in doc.embfile_names():
            info = doc.embfile_info(name)
            texts = [name, info.get("filename"), info.get("ufilename"), info.get("desc")]
            texts += _decode_any(doc.embfile_get(name))
            if any(_contains_term(t, terms) for t in texts):
                leaks.add("attachments")

        # Todo lo demás (adjuntos por /AF, /Names /Dests, acciones URI, StructTreeRoot,
        # capas, /PieceInfo, /PageLabels, /Threads...): cualquier objeto con un término.
        if objects_with_terms(doc, terms):
            leaks.add("pdf_objects")
    finally:
        doc.close()
        all_layers.close()
        if copy_path:
            os.remove(copy_path)
    return sorted(leaks)


def text_loss_pages(path, zones_by_page, words_before):
    """Páginas (desde 1) que han perdido texto (visible u OCR) fuera de las zonas censuradas."""
    doc = fitz.open(path)
    try:
        return [
            page.number + 1
            for page in doc
            if words_before.get(page.number, Counter())
            - words_outside(page_words(page), zones_by_page.get(page.number, []))
        ]
    finally:
        doc.close()


# ==========================================
# ACCIONES
# ==========================================
def load_patterns(patterns_path):
    """{categoría: patrón} del JSON de search. BadPatterns si no es un objeto o algún patrón
    no es una cadena no vacía (una vacía casaría en cada posición sin encontrar nada). Un
    patrón que no compila sí vale: search lo devuelve como error de su categoría."""
    try:
        with open(patterns_path, "r", encoding="utf-8") as f:
            patterns = json.load(f)
    except ValueError as e:
        raise BadPatterns(f"los patrones no son JSON: {e}") from None
    if not isinstance(patterns, dict):
        raise BadPatterns("los patrones no son un objeto")
    if not all(isinstance(p, str) and p for p in patterns.values()):
        raise BadPatterns("hay un patrón que no es una cadena no vacía")
    return patterns


def page_chars(page):
    """[(carácter, bbox, línea)] del texto de la página, con un ("\\n", None, línea) al final
    de cada línea: unidos dan lo mismo que page.get_text("text"), y las cajas están en el
    espacio de search_for (página sin girar)."""
    chars = []
    line_no = 0
    for block in page.get_text("rawdict", flags=fitz.TEXTFLAGS_TEXT)["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                for ch in span["chars"]:
                    chars.append((ch["c"], ch["bbox"], line_no))  # tupla: un Rect por carácter es lento
            chars.append(("\n", None, line_no))
            line_no += 1
    return chars


def match_rects(chars, start, end):
    """Rectángulos de los caracteres chars[start:end]: uno por línea, y otro más dentro de la
    misma línea si entre dos caracteres hay un hueco de más de media altura (como
    search_for, que no une lo que está lejos: dos celdas de una tabla en la misma línea). Los
    espacios cuentan para la cercanía pero no para el rectángulo."""
    rects = []
    current = None   # [(x0, y0, x1, y1) o None, línea, caja del último carácter]
    for c, box, line in chars[start:end]:
        if box is None:
            continue
        x0, y0, x1, y1 = box
        if current is not None and current[1] == line:
            fuzz = (y1 - y0) / 2
            px0, py0, px1, py1 = current[2]
            near = px0 - fuzz < x1 and x0 < px1 + fuzz and py0 - fuzz < y1 and y0 < py1 + fuzz
        else:
            near = False
        if not near:
            current = [None, line, box]
            rects.append(current)
        current[2] = box
        if not c.isspace():
            r = current[0]
            current[0] = box if r is None else (min(r[0], x0), min(r[1], y0), max(r[2], x1), max(r[3], y1))
    return [fitz.Rect(r) for r, _, _ in rects if r is not None and not fitz.Rect(r).is_empty]


def parse_detectors(arg):
    """Nombres de --detectores (separados por comas; vacío, ninguno). BadPatterns si alguno no
    es un detector de detectores.DETECTORS o está repetido."""
    names = [n for n in arg.split(",") if n] if arg else []
    unknown = [n for n in names if n not in detectores.DETECTORS]
    if unknown:
        raise BadPatterns("detector desconocido")
    if len(set(names)) != len(names):
        raise BadPatterns("detector repetido")
    return set(names)


def search(input_path, patterns_path, results_path, detectors=()):
    patterns = load_patterns(patterns_path)

    doc = fitz.open(input_path)
    try:
        results, errors = _find_all(doc, patterns, detectors)
    finally:
        doc.close()

    with open(results_path, 'w', encoding='utf-8') as f:
        json.dump({"results": results, "errors": errors}, f)


def _find_all(doc, patterns, detectors=()):
    """Hallazgos (results) y errores de patrón (errors) de search en doc, que queda aplanado.

    `detectors`: nombres de detectores.DETECTORS que buscar además de los patrones; su
    categoría es el nombre del detector."""
    # Se busca sobre el documento aplanado, igual que el que censura apply: así también
    # aparecen los valores de los campos de formulario y el texto de las anotaciones.
    # Sus posiciones coinciden con las del original (el aplanado dibuja la apariencia del
    # campo en su mismo rectángulo).
    try:
        flatten_document(doc)
    except Exception as e:
        print(f"Aviso en redact.py: no se pudo aplanar para buscar: {e}", file=sys.stderr)
    results = []
    errors = []

    # Precompilamos los patrones una sola vez: un regex inválido se
    # reporta como error de categoría en vez de tumbar la búsqueda o
    # descartarse en silencio en cada página.
    compiled_patterns = {}
    for category, pattern in patterns.items():
        try:
            compiled_patterns[category] = re.compile(pattern)
        except re.error:
            errors.append({
                "category": category,
                "message": f"El patrón de búsqueda para '{category}' no es válido: revisa paréntesis o caracteres especiales."
            })

    seen_coordinates = set()
    det_pages = {}    # página -> {"text", "rects" de los detectores, "counts" por valor plegado}
    det_values = {}   # valor plegado -> (detector, text): lo que se propaga (_propagated)

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")

        p_width = page.rect.width
        p_height = page.rect.height

        raw_rects = []
        # Valores ya buscados en esta página: search_for(val) devuelve todas sus apariciones,
        # así que repetirlo por cada match del mismo valor solo daba copias que la
        # deduplicación de abajo descartaba (con k repeticiones, k llamadas y k² hallazgos).
        # Se marca tras terminar: si search_for falla, otra categoría lo vuelve a intentar.
        searched = set()

        def add_matches(category, val):
            """Hallazgos de val en la página (de un patrón): uno por rectángulo, cada uno con
            su propio group."""
            if not val or val in searched:
                return

            # Avanzamos la búsqueda de contexto usando text.find con offset
            # Esto garantiza un contexto real si el dato aparece múltiples veces.
            start_search = 0

            areas = page.search_for(val)
            for area in areas:
                # Buscar el índice del texto a partir de start_search
                idx = text.find(val, start_search)
                context = ""

                if idx != -1:
                    start = max(0, idx - 30)
                    end = min(len(text), idx + len(val) + 30)
                    context = text[start:end].replace('\n', ' ').strip()
                    start_search = idx + len(val)

                raw_rects.append({
                    "id": str(uuid.uuid4()),
                    "page": page_num,
                    "page_width": p_width,
                    "page_height": p_height,
                    "text": val,
                    "context": f"...{context}...",
                    "category": category,
                    "rect": [area.x0, area.y0, area.x1, area.y1],
                    "group": str(uuid.uuid4())
                })
            searched.add(val)

        for category, compiled_pattern in compiled_patterns.items():
            try:
                # Encontrar matches y obtener las posiciones reales en el texto
                for match in compiled_pattern.finditer(text):
                    val = match.group().strip()
                    add_matches(category, val)
            except Exception:
                pass

        # Detectores: cada aparición se sitúa por sus propios caracteres (no con search_for,
        # que da todas las apariciones de un valor juntas), así que sus rectángulos, uno por
        # línea si está partida, comparten un group y dos datos iguales no se mezclan.
        if detectors:
            try:
                chars = page_chars(page)
                dtext = "".join(c for c, _, _ in chars)
                det_page = det_pages[page_num] = {"text": dtext, "rects": [], "counts": Counter()}
                for name, start, end in detectores.find(dtext, detectors):
                    group = str(uuid.uuid4())
                    shown = detectores.canonical(name, dtext[start:end])
                    context = dtext[max(0, start - 30):end + 30].replace('\n', ' ').strip()
                    raw = _fold(dtext[start:end])
                    det_page["counts"][raw] += 1
                    for needle in (raw, _fold(shown)):
                        det_values.setdefault(needle, (name, shown))
                    for area in match_rects(chars, start, end):
                        det_page["rects"].append(area)
                        raw_rects.append({
                            "id": str(uuid.uuid4()),
                            "page": page_num,
                            "page_width": p_width,
                            "page_height": p_height,
                            "text": shown,
                            "context": f"...{context}...",
                            "category": name,
                            "rect": [area.x0, area.y0, area.x1, area.y1],
                            "group": group
                        })
            except Exception:
                pass

        # 2. Deduplicación estricta usando el área completa y el texto
        for r in raw_rects:
            coord_hash = _coord_hash(r)
            if coord_hash not in seen_coordinates:
                seen_coordinates.add(coord_hash)
                results.append(r)

    if det_values:
        for r in _propagated(doc, det_pages, det_values):
            coord_hash = _coord_hash(r)
            if coord_hash not in seen_coordinates:
                seen_coordinates.add(coord_hash)
                results.append(r)

    return results, errors


def _coord_hash(r):
    return f"{r['page']}_{round(r['rect'][0], 1)}_{round(r['rect'][1], 1)}_{round(r['rect'][2], 1)}_{round(r['rect'][3], 1)}_{r['text']}"


def _fold(text):
    """Forma de comparar un valor con el texto como lo compara search_for: sin distinguir
    mayúsculas y con cualquier separación de espacios o saltos de línea como un espacio."""
    return " ".join(text.lower().split())


# search_for por defecto (PyMuPDF): une las palabras partidas con guion al final de línea.
SEARCH_FLAGS = (fitz.TEXT_DEHYPHENATE | fitz.TEXT_PRESERVE_WHITESPACE
                | fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_MEDIABOX_CLIP)


def _occurrences(text, needles):
    """{needle: [posiciones]} de todas las apariciones de cada needle en text, en una sola
    pasada: cada posición se mira solo contra los needles que empiezan igual (sus K primeros
    caracteres). Un str.find por needle sobre todo el documento costaba ~0,65 ms con 300
    páginas: con miles de valores distintos, segundos."""
    found = {}
    if not needles:
        return found
    k = min(6, min(len(n) for n in needles))
    by_prefix = {}
    for n in needles:
        by_prefix.setdefault(n[:k], []).append(n)
    get = by_prefix.get
    for i in range(len(text) - k + 1):
        candidates = get(text[i:i + k])
        if candidates:
            for n in candidates:
                if text.startswith(n, i):
                    found.setdefault(n, []).append(i)
    return found


def _per_page(texts, needles):
    """{(página, needle): apariciones} de needles en los textos ya plegados de cada página."""
    pages = sorted(texts)
    starts, pos = [], 0
    for p in pages:
        starts.append(pos)
        pos += len(texts[p]) + 1
    whole = "\x00".join(texts[p] for p in pages)   # \x00: ninguna aparición cruza de página
    counts = Counter()
    for needle, positions in _occurrences(whole, needles).items():
        for i in positions:
            counts[(pages[bisect.bisect_right(starts, i) - 1], needle)] += 1
    return counts


def _propagated(doc, det_pages, det_values):
    """Hallazgos de las otras apariciones de cada valor detectado, en todo el documento.

    En Anonimizar, un dato identificado tiene que desaparecer en todas partes: también donde
    el detector no lo reconoce (en minúsculas, pegado a una letra...). Por cada valor
    distinto (su texto tal cual y su forma de `text`, plegados con _fold) se buscan las
    páginas donde aparece más veces de las que lo encontró el detector, y solo en esas se usa
    search_for(valor), como con los patrones: sin distinguir mayúsculas y con lo que el
    detector ya tapaba fuera (rectángulos dentro de los suyos). Cada rectángulo es un hallazgo
    con su propio group y `propagated: true`; la categoría y el text son los del valor."""
    folded = {p: _fold(d["text"]) for p, d in det_pages.items()}
    dehyphenated = {p: _fold(re.sub(r"-[ \t]*\n[ \t]*", "", d["text"])) for p, d in det_pages.items()}
    needles = [n for n in det_values if n]
    counts = _per_page(folded, needles)
    counts_dehyphenated = _per_page(dehyphenated, needles)

    todo = {}
    for (p, needle), n in counts.items():
        if n > det_pages[p]["counts"][needle]:
            todo.setdefault(p, set()).add(needle)
    for (p, needle), n in counts_dehyphenated.items():
        if n > counts[(p, needle)]:   # partida con guion al final de línea: solo la ve search_for
            todo.setdefault(p, set()).add(needle)

    out = []
    for p in sorted(todo):
        page = doc[p]
        textpage = page.get_textpage(flags=SEARCH_FLAGS)
        taken = [fitz.Rect(r) + (-0.5, -0.5, 0.5, 0.5) for r in det_pages[p]["rects"]]
        text = det_pages[p]["text"]
        for needle in sorted(todo[p]):
            name, shown = det_values[needle]
            contexts = [text[max(0, m.start() - 30):m.end() + 30].replace('\n', ' ').strip()
                        for m in re.finditer(r"\s+".join(map(re.escape, needle.split(" "))), text, re.IGNORECASE)]
            for i, area in enumerate(page.search_for(needle, textpage=textpage)):
                if any(t.contains(area) for t in taken):
                    continue
                taken.append(area + (-0.5, -0.5, 0.5, 0.5))
                out.append({
                    "id": str(uuid.uuid4()),
                    "page": p,
                    "page_width": page.rect.width,
                    "page_height": page.rect.height,
                    "text": shown,
                    "context": f"...{contexts[min(i, len(contexts) - 1)] if contexts else ''}...",
                    "category": name,
                    "rect": [area.x0, area.y0, area.x1, area.y1],
                    "group": str(uuid.uuid4()),
                    "propagated": True
                })
    return out


def item_zone(doc, item):
    """(página, Rect) de un hallazgo; ValueError si no se puede censurar tal cual.

    Un rectángulo invertido o vacío no tapa nada aunque MuPDF lo acepte, y uno fuera de la
    página tampoco: se dan por censura fallida (y server.js no entrega nada). Tampoco vale
    una página negativa (en Python, -1 sería la última). Los hallazgos llegan en
    coordenadas de la página sin girar, como los da search_for.
    """
    number = item["page"]
    if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number < doc.page_count:
        raise ValueError(f"page {number} not in document")
    page = doc[number]
    coords = [float(v) for v in item["rect"]]
    if len(coords) != 4 or not all(math.isfinite(v) for v in coords):
        raise ValueError("rect no válido")
    x0, y0, x1, y1 = coords
    if x0 >= x1 or y0 >= y1:
        raise ValueError("rect invertido o vacío")
    rect = fitz.Rect(coords)
    if not rect.intersects(unrotated_rect(page)):
        raise ValueError("rect fuera de la página")
    return page, rect


# Aviso previo con imágenes compartidas (decisión del 2026-10-27). apply_redactions deja en
# cada página censurada que usa una imagen compartida su propia copia entera de la imagen: con
# un fondo de página común a muchas páginas, la salida se multiplica (×80 con 120 páginas en
# docs/estudio-anonimizar-tamano.md). Antes de censurar se estima la salida, el tiempo y la
# memoria de esas copias y, si pasan de estos límites, apply no censura nada y server.js
# responde 422 SHARED_IMAGE_TOO_LARGE (en vez de acabar en un 504 tras el tope).
SHARED_REFUSAL = "SHARED_IMAGE_TOO_LARGE"
# Una zona toca una imagen que la contención de la sección 4b no puede copiar por página (en
# línea en un Form XObject común, o dentro de un patrón común): apply no censura nada.
UNSUPPORTED_REFUSAL = "SHARED_IMAGE_UNSUPPORTED"
# Motivo de la verificación final (leaks) cuando hidden_image_pixels encuentra un resto: server.js
# lo trata como cualquier otro (422 REDACT_NOT_VERIFIED) y solo lo escribe en el log.
HIDDEN_PIXELS_LEAK = "REDACT_HIDDEN_PIXELS"
SHARED_MAX_BYTES = 150_000_000
SHARED_MAX_SECONDS = 180
# Segundos de apply por copia y megapíxel en producción: medidos en local con el fondo de 120
# páginas del estudio (2480 × 3508, una copia por página, zonas en todas): 57,6 s, 0,0552 s por
# megapíxel y copia, por 1,3 (producción frente a local, CLAUDE.md). Con pico_memoria.py de
# scripts/estudio-anonimizar-tamano/ (2026-10-27).
SHARED_SECONDS_PER_MPX = 0.072
# Pico de memoria de apply / salida estimada, con el mismo caso: 804 MB (RSS máximo) / 103 MB
# estimados.
SHARED_MEMORY_FACTOR = 7.8
# Segundos de la contención (sección 4b) por página que usa una imagen compartida y megapíxel de
# sus zonas (las máscaras de lo que no muestra cada página): medido con 11_form_compartido del
# banco (120 págs., foto de 4000 × 2160, zonas en todas): +5,7 s en local frente a la rama base,
# 0,0055 s, por 1,3 de producción (2026-10-28, fix/anonimizar-imagen-compartida).
SHARED_PAGE_SECONDS_PER_MPX = 0.007
# Límite de memoria por trabajo: el 70 % de la memoria del contenedor (su mem_limit, leído del
# cgroup) entre los trabajos que la API deja correr a la vez, que server.js pasa en
# MOTOR_MAX_JOBS (su HEAVY_MAX_CONCURRENT). En producción, 6 GiB y 3: 1,5 GB por trabajo, unos
# 193 MB de salida estimada.
SHARED_MEMORY_SHARE = 0.7
MAX_JOBS_ENV = "MOTOR_MAX_JOBS"
CGROUP_MEMORY_FILES = ("/sys/fs/cgroup/memory.max",  # cgroup v2
                       "/sys/fs/cgroup/memory/memory.limit_in_bytes")  # cgroup v1


def container_memory():
    """Bytes de memoria del contenedor: el límite del cgroup (v2 o v1) o, sin límite ("max" o
    un valor por encima de la RAM, como el de v1 sin límite), la RAM física. None si no se sabe
    (p. ej. en Windows, sin cgroup ni os.sysconf)."""
    try:
        physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        physical = None
    for path in CGROUP_MEMORY_FILES:
        try:
            with open(path, encoding="ascii") as f:
                value = f.read().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if value.isdigit() and (physical is None or int(value) < physical):
            return int(value)
        break
    return physical


def max_jobs():
    """Trabajos que pueden correr a la vez (MOTOR_MAX_JOBS); 1 sin la variable o si no vale."""
    try:
        return max(1, int(os.environ.get(MAX_JOBS_ENV, "1")))
    except ValueError:
        return 1


def shared_max_memory():
    """Memoria máxima de un trabajo para el aviso previo; None si no se sabe la del contenedor."""
    memory = container_memory()
    return None if memory is None else int(SHARED_MEMORY_SHARE * memory / max_jobs())


def shared_image_estimate(doc, zones_by_page, input_bytes):
    """Estimación de lo que añaden las imágenes compartidas entre páginas, antes de censurar.

    Por cada imagen que usa más de una página (también desde un Form XObject compartido): una
    copia del tamaño de su flujo, y su coste en tiempo por megapíxel, por cada página con una
    zona que la toca. Dónde se dibuja: las imágenes de la página con su mismo ancho y alto (sin
    decodificar nada: get_image_rects calcula la huella de los píxeles, 0,1-0,2 s por página
    con un fondo de 300 ppp). En una página girada, o si no se encuentra, cualquier zona de la
    página cuenta como que la toca. Devuelve {bytes, seconds, memory, copies, max_memory}:
    bytes es la entrada más las copias; max_memory, el límite de memoria de este trabajo
    (shared_max_memory).
    """
    pages_by_image = {}
    for page in doc:
        for xref in {im[0] for im in page.get_images(full=True)}:
            pages_by_image.setdefault(xref, []).append(page.number)
    shared = {x: n for x, n in pages_by_image.items() if len(n) > 1}
    infos = {}  # página -> imágenes dibujadas, con un TextPage nuevo (get_image_info guarda caché)

    def places(n, size):
        page = doc[n]
        if page.rotation:
            return []
        if n not in infos:
            infos[n] = page.get_textpage(flags=fitz.TEXT_PRESERVE_IMAGES).extractIMGINFO()
        return [fitz.Rect(i["bbox"]) for i in infos[n] if (i["width"], i["height"]) == size]

    est = {"bytes": input_bytes, "seconds": 0.0, "copies": 0}
    for xref, numbers in shared.items():
        width = int(doc.xref_get_key(xref, "Width")[1] or 0)
        height = int(doc.xref_get_key(xref, "Height")[1] or 0)
        copies = 0
        for n in numbers:
            zones = zones_by_page.get(n)
            if not zones:
                continue
            rects = places(n, (width, height))
            if not rects or any(z.intersects(r) for z in zones for r in rects):
                copies += 1
        if copies < 2:
            continue
        est["copies"] += copies
        est["bytes"] += copies * len(doc.xref_stream_raw(xref) or b"")
        est["seconds"] += copies * width * height / 1e6 * SHARED_SECONDS_PER_MPX
    est["memory"] = est["bytes"] * SHARED_MEMORY_FACTOR
    est["seconds"] = round(est["seconds"], 1)
    est["max_memory"] = shared_max_memory()
    return est


def add_contention_copies(doc, est, plan, zones_by_page):
    """Suma a la estimación de shared_image_estimate lo de la contención (sección 4b): sus copias
    (contention_copies), con el mismo coste por copia, y lo que cuesta calcular, en cada página
    que usa una imagen compartida, qué no muestra (SHARED_PAGE_SECONDS_PER_MPX por megapíxel de
    las zonas). Devuelve cuántas copias ha sumado."""
    extra = contention_copies(plan, zones_by_page)
    seconds = 0.0
    for x, entry in plan.items():
        n = extra.get(x, 0)
        w, h = entry["size"]
        b = entry["box"]
        est["copies"] += n
        est["bytes"] += n * len(doc.xref_stream_raw(x) or b"")
        seconds += n * w * h / 1e6 * SHARED_SECONDS_PER_MPX
        if len(entry["draws"]) > 1:
            seconds += len(entry["draws"]) * (b[2] - b[0]) * (b[3] - b[1]) / 1e6 * SHARED_PAGE_SECONDS_PER_MPX
    est["seconds"] = round(est["seconds"] + seconds, 1)
    est["memory"] = est["bytes"] * SHARED_MEMORY_FACTOR
    return sum(extra.values())


def shared_image_too_large(est):
    too_much_memory = est["max_memory"] is not None and est["memory"] > est["max_memory"]
    return est["copies"] > 0 and (est["bytes"] > SHARED_MAX_BYTES or est["seconds"] > SHARED_MAX_SECONDS
                                  or too_much_memory)


def apply(input_path, output_path, items):
    """Censura, limpia y verifica. Devuelve el informe que lee server.js.

    Si la verificación final encuentra un término censurado (o un píxel de una zona oculto en una
    imagen, HIDDEN_PIXELS_LEAK), borra la salida y devuelve verified=False: server.js no envía
    nada. Si las copias de las imágenes compartidas entre páginas pasarían de los límites
    (shared_image_too_large), no censura nada y devuelve refused=SHARED_REFUSAL; si una zona toca
    una imagen que no se puede copiar por página, refused=UNSUPPORTED_REFUSAL.
    """
    doc = fitz.open(input_path)

    # 1. Antes de tocar nada: ' y " explícitos (si no, MuPDF saca el texto de la página).
    # Un contenido con ' o " que no se puede analizar no se censura: server.js responde 422.
    try:
        normalize_quote_operators(doc)
    except _ContentSyntaxError:
        doc.close()
        return {"success": False, "verified": False, "leaks": ["content_syntax"],
                "applied": 0, "failed": []}
    # 2. Campos y anotaciones a contenido de página, para que la censura los alcance.
    pending = flatten_document(doc)

    failed_items = []
    applied_count = 0
    zones_by_page = {}
    terms = set()
    for item in items:
        if item.get("category") != MANUAL_CATEGORY:
            t = norm(item.get("text"))
            if len(t) >= MIN_TERM_LEN:
                terms.add(t)
    for page_num, rect in pending:
        zones_by_page.setdefault(page_num, []).append(rect)

    # Blindaje anticaídas con registro de fallos
    for item in items:
        try:
            page, rect = item_zone(doc, item)
            # Sin relleno: el negro lo pinta paint_zones tras apply_redactions (el de
            # PyMuPDF salía desplazado con /Rotate y la CropBox desplazada).
            page.add_redact_annot(rect, fill=False)
            zones_by_page.setdefault(page.number, []).append(rect)
            applied_count += 1
        except Exception as e:
            failed_items.append({"id": item.get("id"), "error": str(e)})

    # Antes de censurar: imágenes compartidas que se copiarían en demasiadas páginas.
    estimate = shared_image_estimate(doc, zones_by_page, os.path.getsize(input_path))
    if shared_image_too_large(estimate):
        doc.close()
        return {"success": False, "verified": False, "refused": SHARED_REFUSAL,
                "estimate": {k: None if v is None else round(v) for k, v in estimate.items()},
                "applied": 0, "failed": failed_items}

    for page_num, rect in pending:
        doc[page_num].add_redact_annot(rect, fill=False)

    # Píxeles de las zonas que otras páginas no muestran de una imagen compartida (sección 4b):
    # lo que hay que copiar, también para el aviso previo.
    # Si una zona toca una imagen que no se puede copiar por página, no se censura nada:
    # server.js responde 422 SHARED_IMAGE_UNSUPPORTED (como el aviso previo).
    plan, unsupported, input_images = hidden_pixels_plan(doc, zones_by_page)
    if unsupported:
        doc.close()
        return {"success": False, "verified": False, "refused": UNSUPPORTED_REFUSAL,
                "applied": 0, "failed": failed_items}
    if plan:
        add_contention_copies(doc, estimate, plan, zones_by_page)
        if shared_image_too_large(estimate):
            doc.close()
            return {"success": False, "verified": False, "refused": SHARED_REFUSAL,
                    "estimate": {k: None if v is None else round(v) for k, v in estimate.items()},
                    "applied": 0, "failed": failed_items}

    # Lo que hay bajo cada zona también es un término: cubre las censuras manuales (cuyo
    # "text" es una etiqueta) y el texto real bajo cada hallazgo.
    words_before = {}
    for page in doc:
        zones = zones_by_page.get(page.number, [])
        words = page_words(page)
        for s in text_under_zones(words, zones):
            t = norm(s)
            if len(t) >= MIN_TERM_LEN:
                terms.add(t)
        words_before[page.number] = words_outside(words, zones)

    # Texto oculto con un término (decisión de diseño, 2026-10-07): se censura siempre,
    # porque el usuario no lo ve y no ha podido decidir dejarlo. Se busca antes de
    # censurar, en el documento tal como lo vio el usuario. Con capas OCG, las palabras de
    # todas las capas salen de una copia en disco sin /OCProperties (como en la verificación).
    hidden_by_page = {}
    if terms:
        all_layers, copy_path, snapshot = doc, None, None
        if doc.xref_get_key(doc.pdf_catalog(), "OCProperties")[0] != "null":
            snapshot = output_path + ".pre.pdf"
            doc.save(snapshot)
        try:
            if snapshot:
                all_layers, copy_path = open_all_layers(snapshot)
            for page in doc:
                rects = hidden_term_rects(page, all_layers[page.number], terms)
                if rects:
                    hidden_by_page[page.number] = rects
        finally:
            if all_layers is not doc:
                all_layers.close()
            for p in (snapshot, copy_path):
                if p and os.path.exists(p):
                    os.remove(p)

    page_count = doc.page_count
    hidden = HiddenPixels(doc, plan, zones_by_page)
    for number in range(page_count):  # sin la página auxiliar que añade HiddenPixels
        page = doc[number]
        redacted = set()
        if page.number in zones_by_page:
            # CRÍTICO: images=fitz.PDF_REDACT_IMAGE_PIXELS garantiza la destrucción
            # a nivel de píxel del escaneo subyacente. No se puede recuperar el dato tapado.
            # Trazos (decisión de diseño, 2026-10-07): fuera todo trazo que toque una zona, no
            # solo los que cubre entera (por defecto, REMOVE_IF_COVERED dejaba bajo el negro
            # una firma que entra y sale de la zona). Se acepta perder las líneas de tabla
            # que rocen la zona. Los rellenos lisos (fondos, celdas) se repintan debajo.
            before = _image_slots(page)
            fills = solid_fills_touching(page, zones_by_page[page.number])
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)
            # Opacidad absoluta, encima de todo y donde se ve cada zona (paint_rects).
            paint_zones(page, zones_by_page[page.number])
            restore_fills(page, fills, zones_by_page[page.number])
            # Copias de imágenes compartidas sin lo que la página no muestra (sección 4b).
            pairs = hidden.after_redaction(page, redacted_pairs(page, before))
            recompress_pairs(doc, pairs)
            redacted = {original[0] for _, original in pairs}
        hidden.other_pages(page, redacted)
        if page.number in hidden_by_page:
            # Solo el texto y sin relleno: la zona no se ve, y no debe tocar imágenes ni
            # trazos que sí se vean (un fondo de página que llega fuera del CropBox). Estas
            # zonas no van a zones_by_page: si se llevaran texto visible, text_loss_pages
            # lo avisa.
            for rect in hidden_by_page[page.number]:
                page.add_redact_annot(rect, fill=False)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
    hidden.finish()
    checked = hidden.checked_digests()

    remove_outline_terms(doc, terms)
    doc = compact_xref(doc)

    # Limpieza forense. Todo a True salvo:
    # - hidden_text: conserva la capa de texto invisible de un OCR (si no, un expediente
    #   escaneado deja de poder buscarse). Lo que hay bajo las zonas ya lo ha borrado
    #   apply_redactions (también el texto invisible) y lo comprueba verify_redaction.
    # - redactions: ya aplicadas arriba, con PDF_REDACT_IMAGE_PIXELS.
    # - redact_images: no se pasa; solo afectaría a las censuras que aplicase scrub.
    doc.scrub(
        attached_files=True,
        clean_pages=True,
        embedded_files=True,
        hidden_text=False,
        javascript=True,
        metadata=True,
        redactions=False,
        remove_links=True,
        reset_fields=True,
        reset_responses=True,
        thumbnails=True,
        xml_metadata=True,
    )
    remove_associated_files(doc)
    found = term_matcher(terms)
    if found is not None:
        # Un objeto más anidado que MAX_NESTING no se ha podido limpiar entero: no se da por
        # verificado (server.js responde 422) y no se escribe la salida.
        try:
            clean_layer_names(doc, found)
            clean_structures(doc, found)
        except NestingTooDeep:
            doc.close()
            return {"success": False, "verified": False, "leaks": ["nesting_depth"],
                    "applied": applied_count, "failed": failed_items}
    doc.set_metadata({
        "creator": OUTPUT_METADATA["creator"],
        "producer": OUTPUT_METADATA["producer"],
        "author": "",
        "title": "",
        "subject": "",
        "keywords": ""
    })

    # use_objstms: los objetos que no son flujos van en flujos de objetos comprimidos (-0 a -8 %,
    # sin coste de tiempo; docs/estudio-anonimizar-tamano.md). La salida no conserva PDF/A
    # (scrub quita el XMP): no hay que respetar la restricción de PDF/A-1.
    doc.save(output_path, garbage=4, deflate=True, clean=True, use_objstms=1)
    doc.close()

    # Sobre los bytes ya guardados, con sus flujos de objetos.
    leaks = verify_redaction(output_path, zones_by_page, terms)
    if hidden_image_pixels(input_path, output_path, plan, zones_by_page, input_images, checked):
        leaks = sorted(set(leaks) | {HIDDEN_PIXELS_LEAK})
    if leaks:
        os.remove(output_path)
        return {"success": False, "verified": False, "leaks": leaks,
                "applied": applied_count, "failed": failed_items}
    return {"success": True, "verified": True, "applied": applied_count, "failed": failed_items,
            "text_loss_pages": text_loss_pages(output_path, zones_by_page, words_before)}


def main(argv):
    action = argv[1]
    input_path = argv[2]
    # Con contraseña de apertura no se busca ni se censura nada: server.js responde 422
    # PDF_ENCRYPTED. Los de solo propietario se abren con la contraseña vacía y siguen.
    with fitz.open(input_path) as doc:
        encrypted = pdf_check.needs_password(doc)
    if encrypted:
        print("PDF con contraseña de apertura", file=sys.stderr)
        sys.exit(pdf_check.EXIT_ENCRYPTED)
    if action == "search":
        try:
            detectors = set()
            if len(argv) > 5:
                if argv[5] != "--detectores" or len(argv) != 7:
                    raise BadPatterns("argumentos de search no válidos")
                detectors = parse_detectors(argv[6])
            search(input_path, argv[3], argv[4], detectors)
        except BadPatterns as e:
            print(f"Patrones no válidos en redact.py: {e}", file=sys.stderr)
            sys.exit(EXIT_BAD_PATTERNS)
    elif action == "apply":
        output_path = argv[3]
        with open(argv[4], "r", encoding="utf-8") as f:
            items = json.load(f)
        report = apply(input_path, output_path, items)
        if len(argv) > 5:
            with open(argv[5], "w", encoding="utf-8") as f:
                json.dump(report, f)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception as e:
        # Imprimir para que server.js lo capture en el stderr
        print(f"Error crítico en redact.py: {str(e)}", file=sys.stderr)
        sys.exit(1)
