# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""JBIG2 genérico SIN PÉRDIDA para imágenes de 1 bit (desde la 1.5.0).

Codifica con jbig2enc (el programa `jbig2`, licencia Apache-2.0) en su modo genérico: región
genérica con codificación aritmética, sin diccionario de símbolos. NUNCA el modo de símbolos
(`-s`): sustituye letras parecidas por la misma y puede cambiar un 6 por un 8. Tampoco
umbraliza ni amplía nada: recibe la imagen de 1 bit tal cual (PBM).

Verificación obligatoria: la imagen nueva se decodifica con MuPDF y tiene que dar exactamente
los mismos bits que la original (salvo los de relleno del final de cada fila, que no son
píxeles); si no, se deja la original. Solo se sustituye si ocupa menos.

jbig2enc es un programa aparte (se lanza un proceso por imagen, con un PBM en una carpeta
temporal que se borra al terminar). Con `setpriv --pdeathsig KILL` (util-linux), si a este
proceso de Python se le mata (tope de tiempo, cliente que se va), jbig2 muere con él y no
queda huérfano. Sin el programa `jbig2` en el PATH, no se convierte nada (`available()`).
"""
import os
import shutil
import subprocess
import tempfile

import pymupdf

# Tope por imagen (una página A4 a 300 ppp tarda unos 60 ms).
TIMEOUT = 60
_SETPRIV = shutil.which("setpriv")


def available():
    return shutil.which("jbig2") is not None


def _rows_equal(a, b, width, height):
    """¿Los mismos píxeles? Bytes de dos bitmaps de 1 bit empaquetados por filas, sin mirar
    los bits de relleno del final de cada fila (cada decodificador los pone a su manera)."""
    stride = (width + 7) // 8
    if len(a) != stride * height or len(b) != stride * height:
        return False
    if a == b:
        return True
    pad = (8 - width % 8) % 8
    if pad == 0:
        return False
    keep = (0xFF << pad) & 0xFF
    mv_a, mv_b = memoryview(a), memoryview(b)
    for r in range(height):
        s = r * stride
        if mv_a[s:s + stride - 1] != mv_b[s:s + stride - 1]:
            return False
        if (mv_a[s + stride - 1] ^ mv_b[s + stride - 1]) & keep:
            return False
    return True


def encode(raw, width, height, tmpdir=None):
    """JBIG2 genérico (datos para un flujo /JBIG2Decode, sin cabecera de archivo ni globales)
    de un bitmap de 1 bit con la convención de PDF (1 = blanco: DeviceGray). None si falla."""
    stride = (width + 7) // 8
    if len(raw) != stride * height:
        return None
    # PBM: 1 = negro. Se invierte: el filtro JBIG2Decode de PDF entrega 1 = blanco.
    inverted = _invert(raw)
    fd, path = tempfile.mkstemp(suffix=".pbm", dir=tmpdir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b"P4\n%d %d\n" % (width, height))
            f.write(inverted)
        cmd = ["jbig2", "-p", path]
        if _SETPRIV:
            cmd = [_SETPRIV, "--pdeathsig", "KILL", "--"] + cmd
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=TIMEOUT, check=False)
        return r.stdout if r.returncode == 0 and r.stdout else None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _invert(raw):
    n = len(raw)
    return (int.from_bytes(raw, "big") ^ ((1 << (8 * n)) - 1)).to_bytes(n, "big")


def set_stream(doc, xref, data, filt, parms=None, **changes):
    """Pone en `xref` el flujo `data` (ya codificado) con /Filter `filt` y /DecodeParms `parms`
    (None: sin la clave), y cambia las claves de `changes` (p. ej. Width=...). El diccionario se
    reescribe entero: con `xref_set_key(..., "null")` queda `/DecodeParms null`, que es lo mismo
    que no tenerla según la norma, pero Ghostscript 10.05.1 dibuja entonces la imagen JBIG2 en
    blanco (test test_ghostscript_ve_los_mismos_pixeles)."""
    entries = []
    for k in doc.xref_get_keys(xref):
        if k in ("Filter", "DecodeParms", "Length", "DL") or k in changes:
            continue
        typ, value = doc.xref_get_key(xref, k)
        if typ == "null":
            continue
        entries.append(f"/{k} {pymupdf.get_pdf_str(value) if typ == 'string' else value}")
    entries += [f"/{k} {v}" for k, v in changes.items()]
    entries.append(f"/Filter {filt}")
    if parms is not None:
        entries.append(f"/DecodeParms {parms}")
    doc.update_stream(xref, data, compress=False)
    doc.update_object(xref, f"<< {' '.join(entries)} /Length {len(data)} >>")


def is_candidate(doc, xref):
    """Imagen de 1 bit que no está ya en JBIG2."""
    try:
        if doc.xref_get_key(xref, "Subtype")[1] != "/Image":
            return False
        mask = doc.xref_get_key(xref, "ImageMask")[1] == "true"
        if not mask and doc.xref_get_key(xref, "BitsPerComponent")[1] != "1":
            return False
        return "JBIG2" not in doc.xref_get_key(xref, "Filter")[1]
    except Exception:  # noqa: BLE001
        return False


def convert(doc, xref, tmpdir=None):
    """Pasa la imagen `xref` de `doc` (en memoria) a JBIG2 genérico si ocupa menos y se
    decodifica con los mismos píxeles. Devuelve True si la ha sustituido."""
    if not is_candidate(doc, xref):
        return False
    try:
        width = int(doc.xref_get_key(xref, "Width")[1])
        height = int(doc.xref_get_key(xref, "Height")[1])
        raw = doc.xref_stream(xref)
        old = doc.xref_stream_raw(xref)
    except Exception:  # noqa: BLE001 - una imagen que no se puede decodificar se queda como está
        return False
    if not raw or width <= 0 or height <= 0:
        return False
    encoded = encode(raw, width, height, tmpdir)
    if encoded is None or len(encoded) >= len(old):
        return False
    before = doc.xref_object(xref, compressed=True)
    set_stream(doc, xref, encoded, "/JBIG2Decode")
    try:
        same = _rows_equal(doc.xref_stream(xref), raw, width, height)
    except Exception:  # noqa: BLE001
        same = False
    if not same:  # no debería pasar: se deja la original, byte a byte
        doc.update_stream(xref, old, compress=False)
        doc.update_object(xref, before)
        return False
    return True


def convert_all(doc, xrefs, tmpdir=None):
    """convert() de cada xref. Devuelve cuántas ha pasado a JBIG2."""
    if not available():
        return 0
    own = tmpdir is None
    if own:
        tmpdir = tempfile.mkdtemp(prefix="jbig2-")
    try:
        return sum(1 for x in xrefs if convert(doc, x, tmpdir))
    finally:
        if own:
            shutil.rmtree(tmpdir, ignore_errors=True)


def same_pixels(doc_a, xref_a, doc_b, xref_b):
    """Para los tests: ¿los mismos píxeles decodificados (MuPDF) en dos imágenes de 1 bit?"""
    w = int(doc_a.xref_get_key(xref_a, "Width")[1])
    h = int(doc_a.xref_get_key(xref_a, "Height")[1])
    if (str(w), str(h)) != (doc_b.xref_get_key(xref_b, "Width")[1], doc_b.xref_get_key(xref_b, "Height")[1]):
        return False
    return _rows_equal(doc_a.xref_stream(xref_a), doc_b.xref_stream(xref_b), w, h)

