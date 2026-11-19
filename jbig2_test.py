# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Tests de jbig2.py y de `lossless_images.py protect --jbig2` (desde la 1.5.0).

Uso: python -m unittest jbig2_test.py

Necesitan el programa `jbig2` (jbig2enc); sin él se saltan, salvo el de que entonces no se
convierte nada. Con Ghostscript (`gs`, o la variable GS), también se comprueba que otro
decodificador de JBIG2 ve los mismos píxeles.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pymupdf

import jbig2
import jpeg_flate
import lossless_images as li

HERE = os.path.dirname(os.path.abspath(__file__))
GS = os.environ.get("GS") or shutil.which("gs") or shutil.which("gswin64c")
HAS_JBIG2 = jbig2.available()


def tmp_dir(test):
    d = tempfile.mkdtemp(prefix="jbig2-test-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


def text_bits(width=1001, height=700, size=11, seed=0):
    """Bitmap de 1 bit (1 = blanco, filas empaquetadas) con texto, como un escaneo a 300 ppp.
    Ancho impar: hay bits de relleno al final de cada fila."""
    doc = pymupdf.open()
    page = doc.new_page(width=width * 72 / 300, height=height * 72 / 300)
    y = 14
    n = seed
    while y < page.rect.height - 4:
        page.insert_text((6, y), f"Linea {n} de prueba 1234,56 EUR expediente {n * 7}/2026 abcdefghijk", fontsize=size * 0.6)
        y += size
        n += 1
    page.draw_rect(pymupdf.Rect(4, 4, page.rect.width - 4, page.rect.height - 4), width=0.6)
    pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csGRAY)
    assert (pix.width, pix.height) == (width, height), (pix.width, pix.height)
    from PIL import Image
    img = Image.frombytes("L", (width, height), pix.samples).point(lambda v: 255 if v >= 128 else 0)
    out = bytearray(img.convert("1", dither=Image.Dither.NONE).tobytes())
    stride, pad = (width + 7) // 8, (8 - width % 8) % 8
    for i in range(stride - 1, len(out), stride):  # relleno a 1 (blanco), como lo decodifica MuPDF
        out[i] |= (1 << pad) - 1
    return bytes(out)


def g4(raw, width, height):
    stride = (width + 7) // 8
    return pymupdf.mupdf.fz_compress_ccitt_fax_g4(pymupdf.mupdf.python_buffer_data(raw), width, height, stride).fz_buffer_extract()


def add_image(doc, width, height, dict_, raw, filt="g4"):
    """Imagen de 1 bit en una página nueva, a 300 ppp. Devuelve su xref."""
    x = doc.get_new_xref()
    doc.update_object(x, "<<>>")
    if filt == "g4":
        doc.update_stream(x, g4(raw, width, height), compress=False)
        tail = f"/Filter /CCITTFaxDecode /DecodeParms << /K -1 /Columns {width} /Rows {height} >>"
    else:
        doc.update_stream(x, raw, compress=True)
        tail = "/Filter /FlateDecode"
    doc.update_object(x, f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} {dict_} {tail} "
                      f"/Length {len(doc.xref_stream_raw(x))} >>")
    pw, ph = width * 72 / 300, height * 72 / 300
    page = doc.new_page(width=pw + 40, height=ph + 40)
    page.insert_text((0, 0), " ")
    doc.xref_set_key(page.xref, "Resources", f"<< /XObject << /Im {x} 0 R >> >>")
    doc.update_stream(page.get_contents()[0], f"q 0 g {pw:.4f} 0 0 {ph:.4f} 20 20 cm /Im Do Q".encode())
    return x


CONVENTIONS = {
    "gris": "/BitsPerComponent 1 /ColorSpace /DeviceGray",
    "gris-decode-invertido": "/BitsPerComponent 1 /ColorSpace /DeviceGray /Decode [1 0]",
    "indexed-2": "/BitsPerComponent 1 /ColorSpace [/Indexed /DeviceRGB 1 <102030F0E0D0>]",
    "mascara": "/ImageMask true /BitsPerComponent 1",
}


def render(doc, page_no):
    return doc[page_no].get_pixmap(dpi=150, colorspace=pymupdf.csRGB).samples


@unittest.skipUnless(HAS_JBIG2, "falta el programa jbig2 (jbig2enc)")
class Conversion(unittest.TestCase):
    def test_identica_y_menor_que_g4_en_cada_convencion(self):
        raw = text_bits()
        for name, dict_ in CONVENTIONS.items():
            with self.subTest(name):
                doc = pymupdf.open()
                x = add_image(doc, 1001, 700, dict_, raw)
                before_doc = pymupdf.open("pdf", doc.tobytes())
                before = render(before_doc, 0)
                old = len(doc.xref_stream_raw(x))
                self.assertTrue(jbig2.convert(doc, x))
                self.assertEqual(doc.xref_get_key(x, "Filter")[1], "/JBIG2Decode")
                self.assertEqual(doc.xref_get_key(x, "DecodeParms")[0], "null")
                self.assertLess(len(doc.xref_stream_raw(x)), old)
                self.assertTrue(jbig2.same_pixels(doc, x, before_doc, x))
                after = pymupdf.open("pdf", doc.tobytes())
                self.assertEqual(render(after, 0), before)

    def test_desde_flate_solo_si_ocupa_menos(self):
        # Texto limpio (sin ruido de escáner): Flate gana a JBIG2 genérico y se queda en Flate.
        raw = text_bits(width=640, height=300)
        doc = pymupdf.open()
        x = add_image(doc, 640, 300, CONVENTIONS["gris"], raw, filt="flate")
        smaller = len(jbig2.encode(raw, 640, 300)) < len(doc.xref_stream_raw(x))
        self.assertEqual(jbig2.convert(doc, x), smaller)
        self.assertEqual(doc.xref_get_key(x, "Filter")[1], "/JBIG2Decode" if smaller else "/FlateDecode")
        self.assertEqual(doc.xref_stream(x), raw)

    @unittest.skipUnless(GS, "falta Ghostscript")
    def test_ghostscript_ve_los_mismos_pixeles(self):
        d = tmp_dir(self)
        raw = text_bits()
        doc = pymupdf.open()
        for dict_ in CONVENTIONS.values():
            add_image(doc, 1001, 700, dict_, raw)
        doc.save(os.path.join(d, "g4.pdf"))
        for x in range(1, doc.xref_length()):
            if jbig2.is_candidate(doc, x):
                self.assertTrue(jbig2.convert(doc, x))
        doc.save(os.path.join(d, "jbig2.pdf"))

        def gs_render(name):
            out = os.path.join(d, name + "-%d.pgm")
            subprocess.run([GS, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pgmraw", "-r150",
                            "-dGraphicsAlphaBits=1", f"-sOutputFile={out}", os.path.join(d, name + ".pdf")],
                           check=True, capture_output=True)
            return [open(out % (i + 1), "rb").read() for i in range(len(CONVENTIONS))]

        self.assertEqual(gs_render("jbig2"), gs_render("g4"))

    def test_si_no_se_decodifica_igual_se_deja_la_original(self):
        raw = text_bits()
        other = bytearray(raw)
        other[5000] ^= 0x10  # un píxel distinto
        real = jbig2.encode
        doc = pymupdf.open()
        x = add_image(doc, 1001, 700, CONVENTIONS["gris"], raw)
        old = doc.xref_stream_raw(x)
        with mock.patch.object(jbig2, "encode", lambda r, w, h, t=None: real(bytes(other), w, h, t)):
            self.assertFalse(jbig2.convert(doc, x))
        self.assertEqual(doc.xref_stream_raw(x), old)
        self.assertEqual(doc.xref_get_key(x, "Filter")[1], "/CCITTFaxDecode")
        self.assertEqual(doc.xref_stream(x), raw)

    def test_si_ocupa_mas_se_deja_la_original(self):
        doc = pymupdf.open()
        x = add_image(doc, 1001, 700, CONVENTIONS["gris"], text_bits())
        old = doc.xref_stream_raw(x)
        with mock.patch.object(jbig2, "encode", lambda r, w, h, t=None: b"\0" * (len(old) + 1)):
            self.assertFalse(jbig2.convert(doc, x))
        self.assertEqual(doc.xref_stream_raw(x), old)

    def test_no_toca_lo_que_no_es_de_1_bit_ni_lo_que_ya_es_jbig2(self):
        doc = pymupdf.open()
        x = add_image(doc, 1001, 700, CONVENTIONS["gris"], text_bits())
        self.assertTrue(jbig2.convert(doc, x))
        self.assertFalse(jbig2.is_candidate(doc, x))
        gray = doc.get_new_xref()
        doc.update_object(gray, "<< /Type /XObject /Subtype /Image /Width 2 /Height 1 /BitsPerComponent 8 /ColorSpace /DeviceGray >>")
        doc.update_stream(gray, b"\x00\xff")
        self.assertFalse(jbig2.is_candidate(doc, gray))

    def test_sin_temporales_y_jbig2_atado_a_este_proceso(self):
        d = tmp_dir(self)
        doc = pymupdf.open()
        xs = [add_image(doc, 1001, 700, CONVENTIONS["gris"], text_bits(seed=i)) for i in range(2)]
        seen = []
        real_run = subprocess.run

        def spy(cmd, *a, **k):
            seen.append(cmd)
            return real_run(cmd, *a, **k)

        with mock.patch.object(jbig2.subprocess, "run", spy), mock.patch.dict(os.environ, {"TMPDIR": d}):
            tempfile.tempdir = None
            try:
                self.assertEqual(jbig2.convert_all(doc, xs), 2)
            finally:
                tempfile.tempdir = None
        self.assertEqual(os.listdir(d), [])
        for cmd in seen:
            if jbig2._SETPRIV:
                self.assertEqual(cmd[:4], [jbig2._SETPRIV, "--pdeathsig", "KILL", "--"])
            self.assertEqual(cmd[-3:-1], ["jbig2", "-p"])
            self.assertNotIn("-s", cmd)


class SinJbig2(unittest.TestCase):
    def test_sin_el_programa_no_convierte_nada(self):
        doc = pymupdf.open()
        x = add_image(doc, 1001, 700, CONVENTIONS["gris"], text_bits())
        old = doc.xref_stream_raw(x)
        with mock.patch.object(jbig2.shutil, "which", lambda name: None):
            self.assertEqual(jbig2.convert_all(doc, [x]), 0)
        self.assertEqual(doc.xref_stream_raw(x), old)


@unittest.skipUnless(HAS_JBIG2, "falta el programa jbig2 (jbig2enc)")
class ProtegerConJbig2(unittest.TestCase):
    """protect --jbig2: la copia con las mismas xref y jpeg_flate.py restaurando desde ella."""

    def test_copia_y_restauracion(self):
        d = tmp_dir(self)
        src = os.path.join(d, "in.pdf")
        doc = pymupdf.open()
        scans = [add_image(doc, 1001, 700, CONVENTIONS["gris"], text_bits(seed=i)) for i in range(2)]
        gray8 = doc.get_new_xref()  # protegida (2 colores) pero de 8 bits: no pasa a JBIG2
        doc.update_object(gray8, "<< /Type /XObject /Subtype /Image /Width 4 /Height 1 /BitsPerComponent 8 /ColorSpace /DeviceGray >>")
        doc.update_stream(gray8, b"\x00\xff\x00\xff")
        page = doc.new_page()
        page.insert_text((0, 0), " ")
        doc.xref_set_key(page.xref, "Resources", f"<< /XObject << /G {gray8} 0 R >> >>")
        doc.update_stream(page.get_contents()[0], b"q 40 0 0 10 20 20 cm /G Do Q")
        doc.save(src)
        marked, jb, post = (os.path.join(d, n) for n in ("marked.pdf", "jb.pdf", "post.pdf"))
        r = subprocess.run([sys.executable, os.path.join(HERE, "lossless_images.py"), "protect", src, marked, "--jbig2", jb],
                           capture_output=True, text=True, check=True)
        self.assertEqual(r.stdout.split(), ["3", "2"])
        copy, orig = pymupdf.open(jb), pymupdf.open(src)
        self.assertEqual(copy.xref_length(), orig.xref_length())
        for x in scans:
            self.assertEqual(copy.xref_get_key(x, "Filter")[1], "/JBIG2Decode")
            self.assertTrue(jbig2.same_pixels(copy, x, orig, x))
        self.assertEqual(copy.xref_stream_raw(gray8), orig.xref_stream_raw(gray8))
        # Sin Ghostscript de por medio: la salida con marcadores hace de salida del compresor.
        jpeg_flate.main(marked, post, jb)
        out = pymupdf.open(post)
        for page_no in range(len(out)):
            x = out[page_no].get_images(full=True)[0][0]
            ox = orig[page_no].get_images(full=True)[0][0]
            self.assertEqual(pymupdf.Pixmap(out, x).samples, pymupdf.Pixmap(orig, ox).samples)
        self.assertEqual(sum(1 for p in out for im in p.get_images(full=True)
                             if out.xref_get_key(im[0], "Filter")[1] == "/JBIG2Decode"), 2)

    def test_sin_protegidas_ni_de_1_bit(self):
        d = tmp_dir(self)
        src = os.path.join(d, "in.pdf")
        doc = pymupdf.open()
        doc.new_page().insert_text((50, 50), "Solo texto")
        doc.save(src)
        jb = os.path.join(d, "jb.pdf")
        r = subprocess.run([sys.executable, os.path.join(HERE, "lossless_images.py"), "protect", src,
                            os.path.join(d, "m.pdf"), "--jbig2", jb], capture_output=True, text=True, check=True)
        self.assertEqual(r.stdout.split(), ["0", "0"])
        self.assertFalse(os.path.exists(jb))

    def test_sin_la_opcion_la_salida_de_siempre(self):
        d = tmp_dir(self)
        src = os.path.join(d, "in.pdf")
        doc = pymupdf.open()
        add_image(doc, 1001, 700, CONVENTIONS["gris"], text_bits())
        doc.save(src)
        r = subprocess.run([sys.executable, os.path.join(HERE, "lossless_images.py"), "protect", src,
                            os.path.join(d, "m.pdf")], capture_output=True, text=True, check=True)
        self.assertEqual(r.stdout, "1\n")


if __name__ == "__main__":
    unittest.main()
