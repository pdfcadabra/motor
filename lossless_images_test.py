# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Tests de lossless_images.py (y de su uso en Comprimir y Anonimizar) con PDFs sintéticos.

Uso: python -m unittest lossless_images_test.py

- Necesita PyMuPDF (como redact.py) y Ghostscript (`gs`, `gswin64c`, o la variable GS) para
  las pruebas de extremo a extremo con los argumentos reales de server.js (y `node`).
- zxing-cpp y Pillow (`pip install -r requirements-test.txt`, solo para tests: no van en la
  imagen) generan y decodifican los QR y códigos de barras. Sin ellos, esas pruebas se saltan
  con un aviso y las demás usan un patrón de 1 bit en su lugar.
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings

import pymupdf

import jpeg_flate
import lossless_images as li
import redact

try:  # dependencias solo de tests (requirements-test.txt)
    import zxingcpp
    from PIL import Image
except ImportError:
    zxingcpp = None
    warnings.warn("zxing-cpp o Pillow no están instalados (pip install -r requirements-test.txt): "
                  "se saltan las pruebas que decodifican QR y códigos de barras.")

API_DIR = os.path.dirname(os.path.abspath(__file__))
GS = os.environ.get("GS") or shutil.which("gs") or shutil.which("gswin64c")
NODE = shutil.which("node")
# server.js de la API, en el directorio padre de motor/. Fuera de ese repositorio (el motor
# solo) no está y las pruebas con sus argumentos de Ghostscript se saltan.
SERVER_DIR = os.path.dirname(API_DIR)
HAS_SERVER = os.path.isfile(os.path.join(SERVER_DIR, "server.js"))
QR_TEXT = "CSV:ABCD-1234-EFGH-5678 https://sede.example/csv"
BAR_TEXT = "CSV0123456789ABCDEF"


def tmp_dir(test):
    d = tempfile.mkdtemp(prefix="lossless-test-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


def code_bits(kind, module):
    """Matriz de booleanos (True = blanco) de un QR o un Code128 con módulos de `module` px.

    Sin zxing-cpp, un patrón pseudoaleatorio de las mismas proporciones (no decodificable,
    pero sirve para comparar píxeles)."""
    if zxingcpp is not None:
        fmt = zxingcpp.BarcodeFormat.QRCode if kind == "qr" else zxingcpp.BarcodeFormat.Code128
        img = zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(QR_TEXT if kind == "qr" else BAR_TEXT, fmt),
                                              scale=1, add_quiet_zones=True)
        w, h = img.shape[1], img.shape[0]
        rows = [[bytes(img)[y * w + x] > 127 for x in range(w)] for y in range(h)]
    else:
        rng = random.Random(kind)
        w, h = (37, 37) if kind == "qr" else (231, 1)
        rows = [[rng.random() < 0.5 for _ in range(w)] for _ in range(h)]
    if kind == "bar":
        rows = [rows[0]] * 40
    return [[v for v in row for _ in range(module)] for row in rows for _ in range(module)]


def pack_1bit(bits):
    out = bytearray()
    for row in bits:
        for i in range(0, len(row), 8):
            chunk = row[i:i + 8] + [True] * (8 - len(row[i:i + 8]))
            out.append(sum(1 << (7 - j) for j, v in enumerate(chunk) if v))
    return bytes(out)


def gray_icc():
    """Perfil ICC gris mínimo (el de MuPDF), para el caso "1 bit en ICCBased" del expediente."""
    pix = pymupdf.Pixmap(pymupdf.csGRAY, 1, 1, b"\x00", 0)
    doc = pymupdf.open()
    doc.new_page().insert_image(pymupdf.Rect(0, 0, 10, 10), pixmap=pix)
    for x in range(1, doc.xref_length()):
        if doc.xref_get_key(x, "N") == ("int", "1") and doc.xref_is_stream(x):
            return doc.xref_stream(x)
    return None


def photo_jpeg(w, h, seed=1):
    rng = random.Random(seed)
    pix = pymupdf.Pixmap(pymupdf.csRGB, w, h, bytes(rng.randrange(256) for _ in range(w * h * 3)), 0)
    return pix.tobytes("jpg", jpg_quality=90)


STAMP_COLORS = {bytes.fromhex("ffffff"), bytes.fromhex("000000"), bytes.fromhex("c01020")}


def stamp256(bits, pad=30, frame=8):
    """(ancho, alto, diccionario, datos) de una Indexed de 8 bits y hival 255 que solo usa los
    tres primeros colores de la paleta (STAMP_COLORS): marco rojo y el código en el centro."""
    palette = bytearray.fromhex("ffffff 000000 c01020")
    for i in range(3, 256):  # entradas sin usar, todas distintas
        palette += bytes(((i * 37) % 256, (i * 91) % 256, (i * 53) % 256))
    qw, qh = len(bits[0]), len(bits)
    w, h = qw + 2 * pad, qh + 2 * pad
    idx = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            if min(x, y, w - 1 - x, h - 1 - y) < frame:
                idx[y * w + x] = 2
            elif pad <= x < pad + qw and pad <= y < pad + qh and not bits[y - pad][x - pad]:
                idx[y * w + x] = 1
    return w, h, f"/BitsPerComponent 8 /ColorSpace [/Indexed /DeviceRGB 255 <{bytes(palette).hex()}>]", bytes(idx)


def make_codes_pdf(path):
    """Una imagen por página, colocada a 300 ppp. Devuelve [(nombre, xref)] en orden de página.

    Los códigos tienen módulos finos (QR de 3 px, barras de 2 px a 300 ppp): con la extrema
    de antes (Bicubic a 100 ppp y JPEG) el Code128 dejaba de leerse."""
    doc = pymupdf.open()
    qr, bar = code_bits("qr", 3), code_bits("bar", 2)
    icc = gray_icc()
    icc_xref = doc.get_new_xref()
    doc.update_object(icc_xref, "<< /N 1 /Alternate /DeviceGray >>")
    doc.update_stream(icc_xref, icc or b"")
    two = "[/Indexed /DeviceRGB 1 <000000FFFFFF>]"
    images = []

    def add(name, w, h, dict_, data, compress=True, filt=None):
        x = doc.get_new_xref()
        doc.update_object(x, f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} {dict_} >>")
        doc.update_stream(x, data, compress=compress)
        if filt:
            doc.xref_set_key(x, "Filter", filt)
        images.append((name, x, w, h))

    for kind, bits in (("qr", qr), ("bar", bar)):
        w, h = len(bits[0]), len(bits)
        data = pack_1bit(bits)
        add(f"{kind}-gray", w, h, "/BitsPerComponent 1 /ColorSpace /DeviceGray", data)
        add(f"{kind}-icc", w, h, f"/BitsPerComponent 1 /ColorSpace [/ICCBased {icc_xref} 0 R]", data)
        add(f"{kind}-indexed", w, h, f"/BitsPerComponent 1 /ColorSpace {two}", data)
        add(f"{kind}-mask", w, h, "/BitsPerComponent 1 /ImageMask true", data)
        # QR en gris de 8 bits y 2 colores (un PNG insertado por otra herramienta).
        add(f"{kind}-gray8", w, h, "/BitsPerComponent 8 /ColorSpace /DeviceGray",
            bytes(255 if v else 0 for row in bits for v in row))
    # Sello Indexed de 16 colores (4 bits) y foto JPEG (esta sí debe comprimirse).
    rng = random.Random(16)
    palette = bytes(rng.randrange(256) for _ in range(48)).hex()
    sw = sh = 300
    idx = [((x - 150) ** 2 + (y - 150) ** 2) // 300 % 16 for y in range(sh) for x in range(sw)]
    add("stamp", sw, sh, f"/BitsPerComponent 4 /ColorSpace [/Indexed /DeviceRGB 15 <{palette}>]",
        bytes((idx[i] << 4) | idx[i + 1] for i in range(0, len(idx), 2)))
    # Sello con paleta de 256 entradas (hival 255) que solo usa 3: blanco, negro y el rojo del
    # marco, con un QR de módulos de 2 px dentro. Antes se reducía y en extrema no se leía.
    add("qr-stamp256", *stamp256(code_bits("qr", 2)))
    add("photo", 1200, 800, "/BitsPerComponent 8 /ColorSpace /DeviceRGB", photo_jpeg(1200, 800),
        compress=False, filt="/DCTDecode")

    for name, x, w, h in images:
        page = doc.new_page(width=595, height=842)
        pw, ph = w * 72 / 300, h * 72 / 300
        page.insert_text((0, 0), " ")  # crea /Contents y /Resources
        doc.xref_set_key(page.xref, "Resources", f"<< /XObject << /Im {x} 0 R >> >>")
        # Las máscaras se pintan en negro (no con el color por defecto de otro sitio).
        doc.update_stream(page.get_contents()[0],
                          f"q 0 g {pw:.3f} 0 0 {ph:.3f} 40 {842 - 40 - ph:.3f} cm /Im Do Q".encode())
    doc.save(path)
    return [(name, x) for name, x, _, _ in images]


def image_pixels(doc, page_no):
    """(ancho, alto, muestras RGB) de la única imagen de la página, a su resolución."""
    xref = doc[page_no].get_images(full=True)[0][0]
    if doc.xref_get_key(xref, "ImageMask")[1] == "true":
        # Una máscara no tiene color: se compara el render de la página.
        pix = doc[page_no].get_pixmap(dpi=300, colorspace=pymupdf.csGRAY)
    else:
        pix = pymupdf.Pixmap(doc, xref)
        if pix.colorspace is None or pix.colorspace.n != 3 or pix.alpha:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return pix.width, pix.height, pix.samples


def decode_page(doc, page_no, dpi=300):
    pix = doc[page_no].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return {r.text for r in zxingcpp.read_barcodes(Image.frombytes("L", (pix.width, pix.height), pix.samples))}


def compress_args(level, inp, out):
    js = ("const s = require('./server.js');"
          f"console.log(JSON.stringify(s.compressArgs(s.COMPRESS_LEVELS[{json.dumps(level)}], "
          f"{json.dumps(inp)}, {json.dumps(out)})));process.exit(0)")
    return json.loads(subprocess.run([NODE, "-e", js], cwd=SERVER_DIR, check=True,
                                     capture_output=True, text=True).stdout)


def compress(inp, level, d):
    """Lo mismo que /v1/compress: protect -> gs (argumentos de server.js) -> jpeg_flate."""
    marked, gs_out, out = (os.path.join(d, f"{level}-{n}.pdf") for n in ("marked", "gs", "out"))
    doc = pymupdf.open(inp)
    n = len(li.protect(doc))
    if n:
        doc.save(marked)
    doc.close()
    subprocess.run([GS, *compress_args(level, marked if n else inp, gs_out)], check=True, capture_output=True)
    jpeg_flate.main(gs_out, out, inp if n else None)
    return out


class Clasificacion(unittest.TestCase):
    def setUp(self):
        self.d = tmp_dir(self)
        self.path = os.path.join(self.d, "codes.pdf")
        self.names = dict(make_codes_pdf(self.path))
        self.doc = pymupdf.open(self.path)

    def test_se_protegen_1bit_indexed_y_pocos_colores(self):
        for name, xref in self.names.items():
            self.assertEqual(li.is_protected(self.doc, xref), name != "photo", name)

    def test_gris_8bits_con_muchos_colores_y_indexed_de_256_no(self):
        doc = pymupdf.open()
        rng = random.Random(3)
        gray = doc.get_new_xref()
        doc.update_object(gray, "<< /Type /XObject /Subtype /Image /Width 64 /Height 64 "
                                "/BitsPerComponent 8 /ColorSpace /DeviceGray >>")
        doc.update_stream(gray, bytes(rng.randrange(256) for _ in range(64 * 64)))
        pal = doc.get_new_xref()
        doc.update_object(pal, "<< /Type /XObject /Subtype /Image /Width 16 /Height 16 /BitsPerComponent 8 "
                               f"/ColorSpace [/Indexed /DeviceRGB 255 <{bytes(range(256)).hex() * 3}>] >>")
        doc.update_stream(pal, bytes(range(256)))
        self.assertFalse(li.is_protected(doc, gray))
        self.assertFalse(li.is_protected(doc, pal))

    def test_indexed_de_paleta_grande_segun_los_colores_que_usa(self):
        doc = pymupdf.open()
        w, h, dict_, data = stamp256(code_bits("qr", 2))
        stamp = doc.get_new_xref()
        doc.update_object(stamp, f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} {dict_} >>")
        doc.update_stream(stamp, data)
        self.assertTrue(li.is_protected(doc, stamp))
        # La misma paleta usando 17 colores: ya no.
        many = doc.get_new_xref()
        doc.update_object(many, f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} {dict_} >>")
        doc.update_stream(many, bytes(i % 17 for i in range(w * h)))
        self.assertFalse(li.is_protected(doc, many))

    def test_las_smask_no_se_sustituyen(self):
        doc = pymupdf.open()
        page = doc.new_page()
        rng = random.Random(5)
        # Con alfa: insert_image crea una /SMask (gris de 8 bits, con pocos niveles).
        pix = pymupdf.Pixmap(pymupdf.csRGB, 20, 20, bytes(rng.randrange(256) if i % 4 < 3 else 255 * (i % 8 < 4)
                                                          for i in range(1600)), 1)
        page.insert_image(pymupdf.Rect(0, 0, 50, 50), pixmap=pix)
        smasks = [int(doc.xref_get_key(x, "SMask")[1].split()[0]) for x in range(1, doc.xref_length())
                  if doc.xref_get_key(x, "SMask")[0] == "xref"]
        self.assertTrue(smasks)
        self.assertTrue(all(li.is_protected(doc, m) for m in smasks), "por sí sola, se protegería")
        protected = li.protect(doc)
        for m in smasks:
            self.assertNotIn(m, protected)


class ProtegerYRestaurar(unittest.TestCase):
    """Sin Ghostscript: protect, guardar y restore deben devolver los mismos bytes."""

    def test_ida_y_vuelta_sin_cambios(self):
        d = tmp_dir(self)
        src, marked = os.path.join(d, "in.pdf"), os.path.join(d, "marked.pdf")
        names = make_codes_pdf(src)
        doc = pymupdf.open(src)
        protected = li.protect(doc)
        self.assertEqual(len(protected), len(names) - 1)
        doc.save(marked)
        out = pymupdf.open(marked)
        self.assertEqual(li.restore(out, pymupdf.open(src)), len(protected))
        orig = pymupdf.open(src)
        for page_no in range(len(names)):
            self.assertEqual(image_pixels(out, page_no), image_pixels(orig, page_no), names[page_no][0])

    def test_marcador_que_queda_en_el_contenido_da_error(self):
        """Si Ghostscript metiese un marcador dentro del contenido, no puede salir así."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((0, 0), " ")
        data = li.marker_bytes(7)
        doc.update_stream(page.get_contents()[0],
                          b"q 10 0 0 10 0 0 cm BI /IM true /W 64 /H 2 /BPC 1 ID " + data + b" EI Q")
        with self.assertRaises(li.RestoreError):
            li.restore(doc, pymupdf.open())

    def test_marcador_invertido_tambien_se_reconoce(self):
        data = li.marker_bytes(1234)
        self.assertEqual(li.marker_id(data), 1234)
        self.assertEqual(li.marker_id(bytes(255 - b for b in data)), 1234)
        self.assertIsNone(li.marker_id(b"\x00" * 16))


def make_deep_pdf(path, depth):
    """Página con una imagen de 1 bit (protegida) cuyo diccionario apunta a una cadena de
    `depth` referencias: /Foo N 0 R -> [M 0 R] -> ... -> (fondo)."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(72, 72, 172, 172), pixmap=pymupdf.Pixmap(pymupdf.csGRAY, 8, 1, b"\x00" * 8, 0))
    img = next(x for x in range(1, doc.xref_length()) if doc.xref_get_key(x, "Subtype") == ("name", "/Image"))
    doc.update_object(img, "<</Type/XObject/Subtype/Image/Width 8/Height 1/ColorSpace/DeviceGray/BitsPerComponent 1>>")
    doc.update_stream(img, b"\x55", compress=False)
    last = doc.get_new_xref()
    doc.update_object(last, "(fondo)")
    for _ in range(depth - 1):
        x = doc.get_new_xref()
        doc.update_object(x, f"[{last} 0 R]")
        last = x
    doc.xref_set_key(img, "Foo", f"{last} 0 R")
    doc.save(path)
    doc.close()


class AnidamientoProfundo(unittest.TestCase):
    """restore copia la imagen original y todo lo que referencia: con una cadena de referencias
    más larga que MAX_NESTING falla con NestingTooDeep (jpeg_flate.py sale con EXIT_RESTORE_FAILED
    y server.js devuelve el original), nunca copia a medias en silencio. Antes, con unos 330
    niveles, RecursionError: jpeg_flate.py salía con 1 y /v1/compress respondía 500."""

    def _restore(self, depth):
        d = tmp_dir(self)
        src = os.path.join(d, "in.pdf")
        make_deep_pdf(src, depth)
        doc = pymupdf.open(src)
        self.assertEqual(len(li.protect(doc)), 1)
        return li.restore(doc, pymupdf.open(src))

    def test_mas_hondo_que_el_limite_falla(self):
        for depth in (li.MAX_NESTING + 50, 3000):
            with self.subTest(depth=depth):
                with self.assertRaises(li.NestingTooDeep):
                    self._restore(depth)

    def test_dentro_del_limite_se_restaura(self):
        self.assertEqual(self._restore(li.MAX_NESTING - 10), 1)


def make_marker_pdf(path, orig_xref, inline=False):
    """Salida de Ghostscript simulada con un marcador que apunta a `orig_xref` del original: como
    imagen (64x2) o, con inline, dentro del contenido de la página (imagen en línea)."""
    doc = pymupdf.open()
    page = doc.new_page()
    if inline:
        page.insert_text((0, 0), " ")
        doc.update_stream(page.get_contents()[0], b"q 10 0 0 10 0 0 cm BI /IM true /W 64 /H 2 /BPC 1 ID "
                          + li.marker_bytes(orig_xref) + b" EI Q")
    else:
        page.insert_image(pymupdf.Rect(72, 72, 136, 74), pixmap=pymupdf.Pixmap(pymupdf.csGRAY, 8, 1, b"\x00" * 8, 0))
        img = next(x for x in range(1, doc.xref_length()) if doc.xref_get_key(x, "Subtype") == ("name", "/Image"))
        doc.update_object(img, f"<</Type/XObject/Subtype/Image/Width {li.MARK_W}/Height {li.MARK_H}"
                               "/ImageMask true/BitsPerComponent 1>>")
        doc.update_stream(img, li.marker_bytes(orig_xref), compress=False)
    doc.save(path)
    doc.close()


class RestauracionFallida(unittest.TestCase):
    """Cualquier RestoreError al restaurar (profundidad, marcador sin restaurar, marcador cuya
    imagen original no existe): jpeg_flate.py sale con EXIT_RESTORE_FAILED, sin traza y sin
    escribir la salida, y server.js devuelve el original sin tocar. Antes, solo la profundidad;
    los otros dos salían con 1 y /v1/compress respondía 500."""

    def _run(self, marked, src):
        out = os.path.join(os.path.dirname(marked), "out.pdf")
        proc = subprocess.run([sys.executable, os.path.join(API_DIR, "jpeg_flate.py"), marked, out, "--originals", src],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, jpeg_flate.EXIT_RESTORE_FAILED, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn(os.path.dirname(marked), proc.stderr)  # sin rutas
        self.assertFalse(os.path.exists(out))

    def test_profundidad(self):
        d = tmp_dir(self)
        src, marked = os.path.join(d, "in.pdf"), os.path.join(d, "marked.pdf")
        make_deep_pdf(src, 3000)
        doc = pymupdf.open(src)
        li.protect(doc)
        doc.save(marked)
        self._run(marked, src)

    def test_marcador_sin_restaurar_en_el_contenido(self):
        d = tmp_dir(self)
        src, marked = os.path.join(d, "in.pdf"), os.path.join(d, "marked.pdf")
        make_deep_pdf(src, 1)
        make_marker_pdf(marked, 5, inline=True)
        self._run(marked, src)

    def test_marcador_cuya_imagen_original_no_existe(self):
        d = tmp_dir(self)
        src, marked = os.path.join(d, "in.pdf"), os.path.join(d, "marked.pdf")
        make_deep_pdf(src, 1)
        for orig in (1, 99999):  # el catálogo (no es una imagen) y un objeto que no existe
            with self.subTest(orig=orig):
                make_marker_pdf(marked, orig)
                self._run(marked, src)


@unittest.skipUnless(GS and NODE and HAS_SERVER,
                     "hace falta Ghostscript, node y server.js (en el directorio padre) para comprimir de verdad")
class ComprimirConGhostscript(unittest.TestCase):
    """Extremo a extremo con los argumentos reales de server.js, en los tres niveles."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(prefix="lossless-gs-")
        cls.src = os.path.join(cls.d, "codes.pdf")
        cls.names = make_codes_pdf(cls.src)
        cls.out = {level: compress(cls.src, level, cls.d) for level in ("recommended", "extreme", "low")}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, True)

    def test_imagenes_protegidas_identicas_y_nunca_jpeg(self):
        orig = pymupdf.open(self.src)
        for level, path in self.out.items():
            out = pymupdf.open(path)
            self.assertEqual(len(out), len(self.names))
            for page_no, (name, _) in enumerate(self.names):
                if name == "photo":
                    continue
                with self.subTest(level=level, image=name):
                    xref = out[page_no].get_images(full=True)[0][0]
                    self.assertNotIn("DCT", out.xref_get_key(xref, "Filter")[1])
                    self.assertNotIn("JPX", out.xref_get_key(xref, "Filter")[1])
                    self.assertEqual(image_pixels(out, page_no), image_pixels(orig, page_no))

    def test_sello_de_paleta_grande_sin_reducir_y_con_los_mismos_colores(self):
        page_no = [n for n, _ in self.names].index("qr-stamp256")
        size = pymupdf.open(self.src)[page_no].get_images(full=True)[0][2:4]
        for level, path in self.out.items():
            with self.subTest(level=level):
                out = pymupdf.open(path)
                im = out[page_no].get_images(full=True)[0]
                self.assertEqual(im[2:4], size)
                self.assertEqual(li._indexed_hival(out, im[0]), 255)
                _, _, samples = image_pixels(out, page_no)
                self.assertEqual({samples[i:i + 3] for i in range(0, len(samples), 3)}, STAMP_COLORS)

    def test_la_foto_si_se_comprime(self):
        page_no = [n for n, _ in self.names].index("photo")
        for level in ("recommended", "extreme"):
            out = pymupdf.open(self.out[level])
            im = out[page_no].get_images(full=True)[0]
            self.assertLess(im[2], 1200, level)  # reducida (estaba a 300 ppp)
            self.assertIn("DCT", out.xref_get_key(im[0], "Filter")[1])
        self.assertLess(os.path.getsize(self.out["extreme"]), os.path.getsize(self.src))

    def test_sin_marcadores_y_qpdf_check(self):
        qpdf = os.environ.get("QPDF") or shutil.which("qpdf")
        for level, path in self.out.items():
            doc = pymupdf.open(path)
            for x in range(1, doc.xref_length()):
                if doc.xref_is_stream(x) and doc.xref_get_key(x, "Subtype")[1] != "/Image":
                    self.assertNotIn(li.MAGIC, doc.xref_stream(x) or b"", level)
            if qpdf:
                subprocess.run([qpdf, "--check", path], check=True, capture_output=True)

    @unittest.skipIf(zxingcpp is None, "zxing-cpp o Pillow no están instalados (pip install -r requirements-test.txt)")
    def test_qr_y_codigos_de_barras_se_siguen_leyendo(self):
        for level, path in self.out.items():
            out = pymupdf.open(path)
            for page_no, (name, _) in enumerate(self.names):
                if name in ("photo", "stamp"):
                    continue
                with self.subTest(level=level, image=name):
                    self.assertIn(QR_TEXT if name.startswith("qr") else BAR_TEXT, decode_page(out, page_no))


class AnonimizarIndexed(unittest.TestCase):
    """recompress_redacted_images: un QR o sello (pocos colores) nunca pasa a JPEG, aunque
    se empareje con una foto JPEG del mismo tamaño."""

    def make(self, path, photo_first, draw_photo_first):
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        bits = code_bits("qr", 3)
        w, h = len(bits[0]), len(bits)
        qr = doc.get_new_xref()
        doc.update_object(qr, f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /BitsPerComponent 8 "
                              "/ColorSpace [/Indexed /DeviceRGB 1 <000000FFFFFF>] >>")
        doc.update_stream(qr, bytes(1 if v else 0 for row in bits for v in row))
        photo = doc.get_new_xref()
        doc.update_object(photo, f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                                 "/BitsPerComponent 8 /ColorSpace /DeviceRGB >>")
        doc.update_stream(photo, photo_jpeg(w, h), compress=False)
        doc.xref_set_key(photo, "Filter", "/DCTDecode")
        page.insert_text((0, 0), " ")
        # El orden de los recursos y del dibujo cambia el orden en que se emparejan.
        a, b = (photo, qr) if photo_first else (qr, photo)
        doc.xref_set_key(page.xref, "Resources", f"<< /XObject << /A {a} 0 R /B {b} 0 R >> >>")
        p, q = ("A", "B") if photo_first else ("B", "A")
        draw = (f"q 200 0 0 200 300 600 cm /{p} Do Q q 200 0 0 200 40 600 cm /{q} Do Q" if draw_photo_first
                else f"q 200 0 0 200 40 600 cm /{q} Do Q q 200 0 0 200 300 600 cm /{p} Do Q").encode()
        doc.update_stream(page.get_contents()[0], draw)
        doc.save(path)
        return w, h

    def test_qr_indexed_sigue_sin_perdida(self):
        # Con el orden de los recursos distinto del de dibujo, el QR salía en JPEG.
        for photo_first in (False, True):
            for draw_photo_first in (False, True):
                with self.subTest(photo_first=photo_first, draw_photo_first=draw_photo_first):
                    self.check(photo_first, draw_photo_first)

    def check(self, photo_first, draw_photo_first):
        d = tmp_dir(self)
        src, out = os.path.join(d, "in.pdf"), os.path.join(d, "out.pdf")
        self.make(src, photo_first, draw_photo_first)
        # Una zona en cada imagen (esquina inferior derecha del QR: fuera del código).
        items = [{"id": "a", "page": 0, "text": "Zona", "category": "Censura Manual", "rect": [236, 238, 239, 241]},
                 {"id": "b", "page": 0, "text": "Zona", "category": "Censura Manual", "rect": [350, 100, 380, 130]}]
        report = redact.apply(src, out, items)
        self.assertTrue(report["verified"], report)
        doc = pymupdf.open(out)
        for im in doc[0].get_images(full=True):
            x0 = doc[0].get_image_rects(im[0])[0].x0
            # La foto (x = 300) sigue en JPEG; el QR (x = 40), sin pérdida.
            self.assertEqual("DCT" in doc.xref_get_key(im[0], "Filter")[1], x0 > 200, x0)
        if zxingcpp is not None:
            clip = pymupdf.Rect(40, 42, 240, 242)
            pix = doc[0].get_pixmap(dpi=300, clip=clip, colorspace=pymupdf.csGRAY)
            texts = {r.text for r in zxingcpp.read_barcodes(Image.frombytes("L", (pix.width, pix.height), pix.samples))}
            self.assertIn(QR_TEXT, texts)


if __name__ == "__main__":
    unittest.main()
