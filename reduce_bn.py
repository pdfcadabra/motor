# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Escaneos en blanco y negro a menos resolución, CON PÉRDIDA (desde la 1.5.0).

Solo se usa si se pide expresamente (`--ppp`, nunca por debajo de MIN_PPP = 150). Lo normal en
Comprimir sigue siendo que las imágenes de 1 bit salgan sin pérdida (lossless_images.py y
jbig2.py).

Qué se reduce: las imágenes de 1 bit que son un escaneo de página (cada vez que una página la
dibuja, tapa al menos la mitad de la página) y cuya resolución en la página pasa de ppp × 1,1
en los dos ejes; no las ImageMask ni las que tienen o son una máscara (/SMask, /Mask). Las
demás de 1 bit (un código de barras o un QR insertados aparte, una firma escaneada pequeña) y
todas las de color o gris (sellos, firmas en color) no se tocan.

Cómo: media por áreas (Pillow BOX) a la resolución pedida y umbral a la mitad (128), sin
tramado; después JBIG2 genérico sin pérdida o CCITT G4, lo que ocupe menos, verificado al
decodificarlo (los bits tienen que ser los de la imagen reducida; si no, la imagen se queda
como estaba).

Códigos (QR, códigos de barras) dentro de la imagen de la página (el CSV de los escaneos de un
juzgado): se buscan con zxing-cpp en la imagen original y cada uno, con un margen de blanco,
se superpone SIN PÉRDIDA (los píxeles originales, recortados) sobre la imagen reducida. Para no
tocar el contenido de las páginas, la imagen pasa a ser un Form XObject del mismo número de
objeto que dibuja la reducida en el mismo cuadrado unidad y encima los recortes. Sin zxing-cpp
no se reduce ninguna imagen (no se sabría dónde están los códigos): `codes_unavailable`.
Después se comprueba que se siguen leyendo: la reducida, ampliada a su tamaño original, con los
recortes encima, tiene que dar con zxing-cpp todos los códigos (formato y texto) del original; si
falta alguno, esa imagen no se reduce (`codes_lost`).

Lo demás, intacto: tras guardar, todas las imágenes de la entrada que no son escaneos reducidos
(sellos y firmas en color, fotos, códigos insertados aparte, máscaras) tienen que estar en la
salida byte a byte; si no, no se escribe la salida (`untouched_changed`).

Legibilidad: con un escaneo sintético de texto de 8, 10 y 12 pt a 300 ppp, el OCR (Tesseract)
pierde alrededor del 1 % de las palabras a 200 ppp (más en cifras y en 8 pt): por eso solo se usa
si se pide, y quien lo pide lo hace sabiendo que pierde calidad.

Uso:
  python3 reduce_bn.py reduce ENTRADA SALIDA INFORME [--ppp N]
      INFORME: {"ppp", "images", "code_patches", "codes_unavailable", "codes_lost",
      "not_identical", "untouched_changed"}; SALIDA solo si `images` > 0.
  python3 reduce_bn.py estimate ENTRADA INFORME [--ppp N]
      INFORME: {"ppp", "images", "bytes", "sampled", "estimate"}: tamaño estimado de ENTRADA
      con sus escaneos reducidos, por la proporción de una muestra (como mucho SAMPLE imágenes
      repartidas por el documento); `estimate` null si no hay nada que reducir.
"""
import collections
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pymupdf
from PIL import Image

import jbig2
import pdf_check

MIN_PPP = 150
MAX_PPP = 1200
DEFAULT_PPP = 200
# Solo si la resolución actual pasa de ppp × MIN_RATIO (reducir de 210 a 200 no compensa).
MIN_RATIO = 1.1
# Parte de la página que tapa cada dibujo de la imagen para ser un escaneo de página.
MIN_PAGE_SHARE = 0.5
# Margen de blanco alrededor de cada código: lo mayor de esto y el 10 % de su lado mayor.
CODE_MARGIN_PX = 16
SAMPLE = 6
# Hilos para lo que no es MuPDF (zxing-cpp, Pillow, jbig2): la API corre hasta 3 trabajos a la
# vez en 4 CPU.
THREADS = 2


class Encrypted(Exception):
    pass


def _key(doc, xref, name):
    return doc.xref_get_key(xref, name)


def _masks(doc):
    """Imágenes que son la /SMask o la /Mask de otra."""
    out = set()
    for x in range(1, doc.xref_length()):
        for name in ("SMask", "Mask"):
            try:
                t, v = _key(doc, x, name)
            except Exception:  # noqa: BLE001
                continue
            if t == "xref":
                out.add(int(v.split()[0]))
    return out


def _bilevel(doc, xref):
    try:
        if _key(doc, xref, "Subtype")[1] != "/Image" or _key(doc, xref, "ImageMask")[1] == "true":
            return False
        if _key(doc, xref, "BitsPerComponent")[1] != "1":
            return False
        return _key(doc, xref, "SMask")[0] == "null" and _key(doc, xref, "Mask")[0] == "null"
    except Exception:  # noqa: BLE001
        return False


def scans(doc, ppp):
    """{xref: (ancho_nuevo, alto_nuevo)} de las imágenes que se reducirían a `ppp`.

    Los dibujos de cada página salen de get_image_info (sin xrefs, que obligaría a decodificar
    cada imagen; get_image_bbox tardaba 46 ms por página): se emparejan con las imágenes de la
    página por su tamaño en píxeles. Si dos imágenes de la página tienen el mismo tamaño, a cada
    una le cuentan los dibujos de las dos (solo puede quedar fuera una que sí se podría reducir)."""
    masks = _masks(doc)
    seen = {}
    bad = set()
    for page in doc:
        area = abs(page.rect)
        draws = None
        for item in page.get_images(full=True):
            xref = item[0]
            if xref <= 0 or xref in bad:
                continue
            if xref in masks or not _bilevel(doc, xref):
                bad.add(xref)
                continue
            if draws is None:
                draws = page.get_image_info()
            w, h = item[2], item[3]
            mine = [d for d in draws if (d["width"], d["height"]) == (w, h)]
            if not mine:  # no se dibuja en la página (o no se encuentra): no se toca
                bad.add(xref)
                continue
            for d in mine:
                a, b, c, dd = d["transform"][:4]
                sx, sy = math.hypot(a, b), math.hypot(c, dd)
                rect = pymupdf.Rect(d["bbox"])
                if not rect.is_valid or sx <= 0 or sy <= 0 or abs(rect & page.rect) < MIN_PAGE_SHARE * area:
                    bad.add(xref)
                    break
                px, py = w * 72 / sx, h * 72 / sy
                old = seen.get(xref, (math.inf, math.inf))
                seen[xref] = (min(old[0], px), min(old[1], py))
    out = {}
    for xref, (px, py) in seen.items():
        if xref in bad or px < ppp * MIN_RATIO or py < ppp * MIN_RATIO:
            continue
        w = int(_key(doc, xref, "Width")[1])
        h = int(_key(doc, xref, "Height")[1])
        out[xref] = (max(1, math.ceil(w * ppp / px)), max(1, math.ceil(h * ppp / py)))
    return out


def _pad_white(raw, width):
    """Bits de relleno del final de cada fila a 1 (blanco), como los dejan los decodificadores."""
    pad = (8 - width % 8) % 8
    if not pad:
        return bytes(raw)
    stride = (width + 7) // 8
    out = bytearray(raw)
    for i in range(stride - 1, len(out), stride):
        out[i] |= (1 << pad) - 1
    return bytes(out)


def downsample(raw, width, height, new_w, new_h):
    """Bitmap de 1 bit (convención de PDF: 1 = blanco) reducido por media de áreas y umbral 128."""
    img = Image.frombytes("1", (width, height), raw).convert("L")
    small = img.resize((new_w, new_h), Image.BOX)
    return _pad_white(small.point(lambda v: 255 if v >= 128 else 0).convert("1", dither=Image.Dither.NONE).tobytes(), new_w)


def _g4(raw, width, height):
    stride = (width + 7) // 8
    buf = pymupdf.mupdf.fz_compress_ccitt_fax_g4(pymupdf.mupdf.python_buffer_data(raw), width, height, stride)
    return buf.fz_buffer_extract()


def _jbig2(raw, width, height, tmpdir):
    return jbig2.encode(raw, width, height, tmpdir) if jbig2.available() else None


def encodings(raw, width, height, tmpdir, jb=False):
    """[(datos, Filter, DecodeParms)] sin pérdida del bitmap, del menor al mayor. `jb`: el JBIG2
    ya calculado (o None si no lo hay); sin él, se calcula aquí. Solo en el hilo principal (G4
    con MuPDF)."""
    out = [(_g4(raw, width, height), "/CCITTFaxDecode", f"<</K -1 /Columns {width} /Rows {height}>>")]
    data = _jbig2(raw, width, height, tmpdir) if jb is False else jb
    if data:
        out.append((data, "/JBIG2Decode", "null"))
    return sorted(out, key=lambda e: len(e[0]))


def _new_image(doc, src_xref, raw, width, height, tmpdir, jb=False):
    """Objeto nuevo con el bitmap `raw` (verificado al decodificarlo) y el diccionario de
    `src_xref` (mismo espacio de color, /Decode, /Interpolate...) con el tamaño nuevo. None si
    ninguna codificación se decodifica igual."""
    xref = doc.get_new_xref()
    src = doc.xref_object(src_xref, compressed=True)
    for data, filt, parms in encodings(raw, width, height, tmpdir, jb):
        doc.update_object(xref, src)
        jbig2.set_stream(doc, xref, data, filt, None if parms == "null" else parms, Width=width, Height=height)
        try:
            if jbig2._rows_equal(doc.xref_stream(xref), raw, width, height):
                return xref
        except Exception:  # noqa: BLE001
            pass
    doc.update_object(xref, "null")
    return None


def _read_codes(img):
    """Códigos que zxing-cpp lee en la imagen (Pillow), como (formato, texto)."""
    import zxingcpp
    return collections.Counter((str(r.format), r.text) for r in zxingcpp.read_barcodes(img))


def find_codes(raw, width, height):
    """([(x0, y0, x1, y1)], códigos) de lo que zxing-cpp lee en la imagen: la caja de cada código
    con su margen, en píxeles (origen arriba a la izquierda), y el recuento de (formato, texto).
    None si zxing-cpp no está."""
    try:
        import zxingcpp
    except ImportError:
        return None
    img = Image.frombytes("1", (width, height), raw).convert("L")
    boxes = []
    found = zxingcpp.read_barcodes(img)
    for r in found:
        p = r.position
        xs = [p.top_left.x, p.top_right.x, p.bottom_right.x, p.bottom_left.x]
        ys = [p.top_left.y, p.top_right.y, p.bottom_right.y, p.bottom_left.y]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        if x1 - x0 < 4 and y1 - y0 < 4:
            continue
        # Un código lineal se da a veces como una línea: se amplía a su alto real con el margen.
        m = max(CODE_MARGIN_PX, round(0.1 * max(x1 - x0, y1 - y0)))
        if y1 - y0 < 4:
            m = max(m, round(0.3 * (x1 - x0)))
        if x1 - x0 < 4:
            m = max(m, round(0.3 * (y1 - y0)))
        boxes.append((max(0, x0 - m), max(0, y0 - m), min(width, x1 + m + 1), min(height, y1 + m + 1)))
    return boxes, collections.Counter((str(r.format), r.text) for r in found)


def codes_still_read(small, new_w, new_h, raw, width, height, boxes, codes):
    """¿Se siguen leyendo todos los códigos? La reducida, ampliada a su tamaño original (vecino
    más próximo: como la dibuja un lector a esa resolución) con los recortes originales encima,
    tiene que dar con zxing-cpp al menos los mismos (formato, texto) que el original."""
    if not codes:
        return True
    img = Image.frombytes("1", (new_w, new_h), small).convert("L").resize((width, height), Image.NEAREST)
    full = Image.frombytes("1", (width, height), raw).convert("L")
    for box in boxes:
        img.paste(full.crop(box), box[:2])
    return not (codes - _read_codes(img))


def _crop(raw, width, box):
    x0, y0, x1, y1 = box
    img = Image.frombytes("1", (width, len(raw) // ((width + 7) // 8)), raw).crop(box)
    return _pad_white(img.tobytes(), x1 - x0)


def prepare(raw, width, height, new_w, new_h, tmpdir):
    """Lo que no usa MuPDF (va en un hilo aparte: zxing-cpp, Pillow y jbig2 sueltan el GIL):
    códigos, imagen reducida, recortes y sus JBIG2. None si falta zxing-cpp; {"codes_lost": True}
    si algún código que se lee en el original dejaría de leerse (entonces no se reduce)."""
    found = find_codes(raw, width, height)
    if found is None:
        return None
    boxes, codes = found
    small = downsample(raw, width, height, new_w, new_h)
    if not codes_still_read(small, new_w, new_h, raw, width, height, boxes, codes):
        return {"codes_lost": True}
    patches = []
    for box in boxes:
        crop = _crop(raw, width, box)
        pw, ph = box[2] - box[0], box[3] - box[1]
        patches.append((box, crop, pw, ph, _jbig2(crop, pw, ph, tmpdir)))
    return {"small": small, "small_jb": _jbig2(small, new_w, new_h, tmpdir), "patches": patches}


def reduce_image(doc, xref, new_w, new_h, prep, tmpdir, report):
    """Pone en `doc` (en memoria) la imagen `xref` reducida con lo de prepare(). Devuelve True si
    la ha cambiado."""
    width = int(_key(doc, xref, "Width")[1])
    height = int(_key(doc, xref, "Height")[1])
    if prep is None:
        report["codes_unavailable"] += 1
        return False
    if prep.get("codes_lost"):
        report["codes_lost"] += 1
        return False
    small = prep["small"]
    old_len = len(doc.xref_stream_raw(xref))
    reduced = _new_image(doc, xref, small, new_w, new_h, tmpdir, prep["small_jb"])
    if reduced is None:
        report["not_identical"] += 1
        return False
    patches = []
    for box, crop, pw, ph, jb in prep["patches"]:
        p = _new_image(doc, xref, crop, pw, ph, tmpdir, jb)
        if p is None:
            for x in [reduced] + [q for q, _ in patches]:
                doc.update_object(x, "null")
            report["not_identical"] += 1
            return False
        patches.append((p, box))
    new_len = len(doc.xref_stream_raw(reduced)) + sum(len(doc.xref_stream_raw(p)) for p, _ in patches)
    if new_len >= old_len:
        for x in [reduced] + [p for p, _ in patches]:
            doc.update_object(x, "null")
        return False
    if not patches:
        # La misma imagen, más pequeña: el objeto `xref` toma el flujo y el diccionario nuevos.
        data = doc.xref_stream_raw(reduced)
        obj = doc.xref_object(reduced, compressed=True)
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, data, compress=False)
        doc.update_object(xref, obj)
        doc.update_object(reduced, "null")
        if not jbig2._rows_equal(doc.xref_stream(xref), small, new_w, new_h):
            raise RuntimeError("la imagen reducida no se decodifica igual")
    else:
        # Form XObject en el mismo cuadrado unidad: la reducida y, encima, cada código sin pérdida.
        names = {"R": reduced, **{f"P{i}": p for i, (p, _) in enumerate(patches)}}
        ops = ["q /R Do Q"]
        for i, (_, (x0, y0, x1, y1)) in enumerate(patches):
            a, d = (x1 - x0) / width, (y1 - y0) / height
            e, f = x0 / width, 1 - y1 / height
            ops.append(f"q {a:.9f} 0 0 {d:.9f} {e:.9f} {f:.9f} cm /P{i} Do Q")
        content = "\n".join(ops).encode()
        xobjects = " ".join(f"/{n} {x} 0 R" for n, x in names.items())
        doc.update_object(xref, f"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] "
                          f"/Resources << /XObject << {xobjects} >> >> >>")
        doc.update_stream(xref, content)
        report["code_patches"] += len(patches)
    report["images"] += 1
    return True


def _images(doc):
    """[(xref, Filter)] de las imágenes del documento."""
    out = []
    for x in range(1, doc.xref_length()):
        try:
            if doc.xref_is_stream(x) and doc.xref_get_key(x, "Subtype")[1] == "/Image":
                out.append((x, doc.xref_get_key(x, "Filter")))
        except Exception:  # noqa: BLE001
            continue
    return out


def _sha(data):
    return hashlib.sha256(data or b"").digest()


def untouched(input_path, output_path, reduced):
    """¿Están en la salida todas las imágenes de la entrada que no son escaneos reducidos (sellos
    y firmas en color, fotos, códigos insertados aparte, máscaras)? Byte a byte, con una sola
    excepción: una que venía sin filtro puede salir en Flate (lo hace el guardado con deflate), y
    entonces sus bytes decodificados tienen que ser exactamente los de la entrada. La salida une las
    repetidas (garbage): basta con que estén."""
    with pymupdf.open(input_path) as a, pymupdf.open(output_path) as b:
        out_raw = {_sha(b.xref_stream_raw(x)): (x, f) for x, f in _images(b)}
        in_raw = set()
        sin_filtro = set()
        for x, f in _images(a):
            if x in reduced:
                continue
            h = _sha(a.xref_stream_raw(x))
            in_raw.add(h)
            if h not in out_raw:
                if f[0] != "null":
                    return False
                sin_filtro.add(h)
        if not sin_filtro:
            return True
        # Las de la salida en Flate que no estaban así en la entrada: sus bytes decodificados.
        deflated = {_sha(b.xref_stream(x)) for h, (x, f) in out_raw.items()
                    if h not in in_raw and f == ("name", "/FlateDecode")}
        return sin_filtro <= deflated


def _check_ppp(value):
    ppp = int(value)
    if not MIN_PPP <= ppp <= MAX_PPP:
        raise ValueError(ppp)
    return ppp


def _open(path):
    doc = pymupdf.open(path)
    if pdf_check.needs_password(doc):
        raise Encrypted()
    return doc


def reduce(input_path, output_path, ppp):
    doc = _open(input_path)
    report = {"ppp": ppp, "images": 0, "code_patches": 0, "codes_unavailable": 0, "codes_lost": 0,
              "not_identical": 0, "untouched_changed": False}
    tmpdir = tempfile.mkdtemp(prefix="bn-")
    try:
        todo = iter(scans(doc, ppp).items())
        pending = collections.deque()
        reduced = set()
        with ThreadPoolExecutor(THREADS) as pool:

            def submit():
                """Decodifica (MuPDF, aquí) la siguiente imagen y manda el resto a un hilo."""
                for xref, (nw, nh) in todo:
                    w = int(_key(doc, xref, "Width")[1])
                    h = int(_key(doc, xref, "Height")[1])
                    try:
                        raw = doc.xref_stream(xref)
                    except Exception:  # noqa: BLE001 - una imagen que no se decodifica se queda
                        continue
                    if raw and len(raw) == (w + 7) // 8 * h:
                        pending.append((xref, nw, nh, pool.submit(prepare, raw, w, h, nw, nh, tmpdir)))
                        return

            # Como mucho 2 × THREADS imágenes decodificadas a la vez (1,1 MB cada página A4 a 300 ppp).
            for _ in range(2 * THREADS):
                submit()
            while pending:
                xref, nw, nh, job = pending.popleft()
                prep = job.result()
                submit()
                if reduce_image(doc, xref, nw, nh, prep, tmpdir, report):
                    reduced.add(xref)
        if report["images"]:
            pdfa = pdf_check.pdfa(doc)
            doc.save(output_path, garbage=4, deflate=True,
                     use_objstms=0 if pdfa and pdfa["part"] == 1 else 1)
            if not untouched(input_path, output_path, reduced):
                os.remove(output_path)
                report.update(images=0, code_patches=0, untouched_changed=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        doc.close()
    return report


def _small_jb(raw, width, height, new_w, new_h, tmpdir):
    small = downsample(raw, width, height, new_w, new_h)
    return small, _jbig2(small, new_w, new_h, tmpdir)


def estimate(input_path, ppp):
    doc = _open(input_path)
    tmpdir = tempfile.mkdtemp(prefix="bn-")
    try:
        found = scans(doc, ppp)
        report = {"ppp": ppp, "images": len(found), "bytes": 0, "sampled": 0, "estimate": None}
        if not found:
            return report
        try:
            import zxingcpp  # noqa: F401 - sin él, reduce no reduciría nada
        except ImportError:
            report["images"] = 0
            return report
        xrefs = sorted(found)
        report["bytes"] = sum(len(doc.xref_stream_raw(x)) for x in xrefs)
        step = max(1, len(xrefs) // SAMPLE)
        sample = xrefs[::step][:SAMPLE]
        old = new = 0
        jobs = []
        with ThreadPoolExecutor(THREADS) as pool:
            for x in sample:
                w = int(_key(doc, x, "Width")[1])
                h = int(_key(doc, x, "Height")[1])
                nw, nh = found[x]
                old += len(doc.xref_stream_raw(x))
                jobs.append((nw, nh, pool.submit(_small_jb, doc.xref_stream(x), w, h, nw, nh, tmpdir)))
            for nw, nh, job in jobs:
                small, jb = job.result()
                new += len(encodings(small, nw, nh, tmpdir, jb)[0][0])
        report["sampled"] = len(sample)
        ratio = new / old if old else 1
        report["estimate"] = round(os.path.getsize(input_path) - report["bytes"] * (1 - min(ratio, 1)))
        return report
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        doc.close()


USAGE = ("Uso: python3 reduce_bn.py reduce <entrada.pdf> <salida.pdf> <informe.json> [--ppp N]\n"
         "     python3 reduce_bn.py estimate <entrada.pdf> <informe.json> [--ppp N]\n"
         f"     (N entre {MIN_PPP} y {MAX_PPP}; por defecto {DEFAULT_PPP})")


def main(argv):
    args = argv[1:]
    ppp = DEFAULT_PPP
    if len(args) >= 2 and args[-2] == "--ppp":
        try:
            ppp = _check_ppp(args[-1])
        except ValueError:
            print(USAGE, file=sys.stderr)
            return 2
        args = args[:-2]
    try:
        if len(args) == 4 and args[0] == "reduce":
            report = reduce(args[1], args[2], ppp)
            out = args[3]
        elif len(args) == 3 and args[0] == "estimate":
            report = estimate(args[1], ppp)
            out = args[2]
        else:
            print(USAGE, file=sys.stderr)
            return 2
    except Encrypted:
        print("PDF con contraseña de apertura", file=sys.stderr)
        return pdf_check.EXIT_ENCRYPTED
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
