# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Tests de reduce_bn.py (escaneos en blanco y negro a menos resolución, desde la 1.5.0).

Uso: python -m unittest reduce_bn_test.py

Los que buscan y leen códigos necesitan zxing-cpp (requirements-test.txt); sin él se saltan,
salvo el que comprueba que entonces no se reduce nada.
"""
import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pymupdf
from PIL import Image

import jbig2
import pdf_check
import reduce_bn
from jbig2_test import g4, text_bits

try:
    import zxingcpp
except ImportError:
    zxingcpp = None

HERE = os.path.dirname(os.path.abspath(__file__))
QR_TEXT = "CSV:INV1234567890K https://sede.example/csv"
BAR_TEXT = "EXP0042123456"
W, H = 1667, 1167  # 400 x 280 pt a 300 ppp


def tmp_dir(test):
    d = tempfile.mkdtemp(prefix="reduce-bn-test-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


def to_raw(img):
    raw = bytearray(img.convert("1", dither=Image.Dither.NONE).tobytes())
    stride, pad = (img.width + 7) // 8, (8 - img.width % 8) % 8
    for i in range(stride - 1, len(raw), stride):
        raw[i] |= (1 << pad) - 1
    return bytes(raw)


def scan_raw(codes=True, seed=0):
    """Página escaneada a 1 bit: texto y, abajo a la derecha, un QR (módulos de 6 px) y un
    Code128 (módulos de 3 px) en la misma imagen."""
    img = Image.frombytes("1", (W, H), text_bits(W, H, seed=seed)).convert("L")
    if codes and zxingcpp is not None:
        for text, fmt, scale, box in ((QR_TEXT, zxingcpp.BarcodeFormat.QRCode, 6, (W - 420, H - 420)),
                                      (BAR_TEXT, zxingcpp.BarcodeFormat.Code128, 3, (W - 1200, H - 300))):
            arr = zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(text, fmt), scale=scale, add_quiet_zones=True)
            code = Image.frombytes("L", (arr.shape[1], arr.shape[0]), bytes(arr))
            if fmt == zxingcpp.BarcodeFormat.Code128:
                code = code.crop((0, 0, code.width, 1)).resize((code.width, 150))
            img.paste(code, box)
    return to_raw(img)


def image(doc, width, height, dict_, data, filt):
    x = doc.get_new_xref()
    doc.update_object(x, "<<>>")
    doc.update_stream(x, data, compress=filt is None)
    tail = filt or ""
    doc.update_object(x, f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} {dict_} {tail} >>")
    if filt is None:
        doc.xref_set_key(x, "Filter", "/FlateDecode")
    return x


def g4_image(doc, raw, width=W, height=H, dict_="/BitsPerComponent 1 /ColorSpace /DeviceGray"):
    x = doc.get_new_xref()
    doc.update_object(x, "<<>>")
    doc.update_stream(x, g4(raw, width, height), compress=False)
    doc.update_object(x, f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} {dict_} "
                      f"/Filter /CCITTFaxDecode /DecodeParms << /K -1 /Columns {width} /Rows {height} >> >>")
    return x


def page_with(doc, draws, size=(400, 280)):
    """Página con los dibujos [(xref, x0, y0, ancho_pt, alto_pt)] (origen abajo a la izquierda)."""
    page = doc.new_page(width=size[0], height=size[1])
    page.insert_text((0, 0), " ")
    names = " ".join(f"/I{i} {x} 0 R" for i, (x, *_) in enumerate(draws))
    doc.xref_set_key(page.xref, "Resources", f"<< /XObject << {names} >> >>")
    ops = " ".join(f"q 0 g {w:.4f} 0 0 {h:.4f} {x0:.4f} {y0:.4f} cm /I{i} Do Q" for i, (_, x0, y0, w, h) in enumerate(draws))
    doc.update_stream(page.get_contents()[0], ops.encode())
    return page


def build(path, codes=True):
    """Página 1: escaneo a toda la página (300 ppp) con códigos; un código de barras de 1 bit
    aparte y una foto en color pequeños encima. Página 2: otro escaneo sin códigos.
    Devuelve los xref {scan, scan2, bar, photo}."""
    doc = pymupdf.open()
    scan = g4_image(doc, scan_raw(codes))
    bw, bh = 300, 60
    bar = image(doc, bw, bh, "/BitsPerComponent 1 /ColorSpace /DeviceGray",
                to_raw(Image.frombytes("L", (bw, bh), bytes((0 if (x // 3) % 3 == 0 else 255) for _ in range(bh) for x in range(bw)))), None)
    photo = image(doc, 60, 40, "/BitsPerComponent 8 /ColorSpace /DeviceRGB",
                  bytes(v for y in range(40) for x in range(60) for v in (x * 4 % 256, y * 6 % 256, 128)), None)
    page_with(doc, [(scan, 0, 0, 400, 280), (bar, 20, 20, 72, 14.4), (photo, 300, 200, 30, 20)])
    scan2 = g4_image(doc, scan_raw(False, seed=9))
    page_with(doc, [(scan2, 0, 0, 400, 280)])
    doc.save(path)
    return {"scan": scan, "scan2": scan2, "bar": bar, "photo": photo}


def drawn(doc, page_no, i):
    """xref del dibujo /I<i> de la página (la salida se guarda con garbage: otros números)."""
    return int(doc.xref_get_key(doc[page_no].xref, f"Resources/XObject/I{i}")[1].split()[0])


def codes_on(doc, page_no):
    pix = doc[page_no].get_pixmap(dpi=300, colorspace=pymupdf.csGRAY)
    return {r.text for r in zxingcpp.read_barcodes(Image.frombytes("L", (pix.width, pix.height), pix.samples))}


class Eleccion(unittest.TestCase):
    def test_escaneos_de_pagina_si_y_lo_demas_no(self):
        d = tmp_dir(self)
        src = os.path.join(d, "in.pdf")
        xs = build(src, codes=False)
        found = reduce_bn.scans(pymupdf.open(src), 200)
        self.assertEqual(set(found), {xs["scan"], xs["scan2"]})
        self.assertEqual(found[xs["scan"]], (1112, 778))  # 1667 x 1167 a 2/3, hacia arriba

    def test_poca_resolucion_mascaras_y_lo_que_no_tapa_la_pagina(self):
        doc = pymupdf.open()
        raw = scan_raw(False)
        at210 = g4_image(doc, raw)
        page_with(doc, [(at210, 0, 0, W * 72 / 210, H * 72 / 210)], size=(W * 72 / 210, H * 72 / 210))
        half = g4_image(doc, raw)
        page_with(doc, [(half, 0, 0, 200, 140)])  # un cuarto de la página
        mask = g4_image(doc, raw, dict_="/ImageMask true /BitsPerComponent 1")
        page_with(doc, [(mask, 0, 0, 400, 280)])
        smask = doc.get_new_xref()
        doc.update_object(smask, f"<< /Type /XObject /Subtype /Image /Width 2 /Height 1 /BitsPerComponent 8 /ColorSpace /DeviceGray >>")
        doc.update_stream(smask, b"\xff\xff")
        with_smask = g4_image(doc, raw, dict_=f"/BitsPerComponent 1 /ColorSpace /DeviceGray /SMask {smask} 0 R")
        page_with(doc, [(with_smask, 0, 0, 400, 280)])
        ok = g4_image(doc, raw)
        page_with(doc, [(ok, 0, 0, 400, 280)])
        page_with(doc, [(ok, 0, 0, 100, 70)])  # la misma, pequeña en otra página: ya no es un escaneo de página
        self.assertEqual(reduce_bn.scans(doc, 200), {})
        self.assertEqual(set(reduce_bn.scans(doc, 150)), {at210})

    def test_resolucion_minima_150(self):
        d = tmp_dir(self)
        src = os.path.join(d, "in.pdf")
        build(src, codes=False)
        for ppp in ("149", "1201", "abc"):
            r = subprocess.run([sys.executable, os.path.join(HERE, "reduce_bn.py"), "reduce", src,
                                os.path.join(d, "o.pdf"), os.path.join(d, "r.json"), "--ppp", ppp], capture_output=True)
            self.assertEqual(r.returncode, 2, ppp)
        r = subprocess.run([sys.executable, os.path.join(HERE, "reduce_bn.py"), "estimate", src,
                            os.path.join(d, "e.json"), "--ppp", "150"], capture_output=True)
        self.assertEqual(r.returncode, 0)

    def test_contrasena(self):
        d = tmp_dir(self)
        src = os.path.join(d, "c.pdf")
        doc = pymupdf.open()
        doc.new_page().insert_text((50, 50), "x")
        doc.save(src, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
        r = subprocess.run([sys.executable, os.path.join(HERE, "reduce_bn.py"), "estimate", src, os.path.join(d, "e.json")],
                           capture_output=True)
        self.assertEqual(r.returncode, pdf_check.EXIT_ENCRYPTED)


class SinZxing(unittest.TestCase):
    def test_sin_zxing_no_se_reduce_nada(self):
        d = tmp_dir(self)
        src, out = os.path.join(d, "in.pdf"), os.path.join(d, "out.pdf")
        build(src, codes=False)
        with mock.patch.dict(sys.modules, {"zxingcpp": None}):
            report = reduce_bn.reduce(src, out, 200)
            est = reduce_bn.estimate(src, 200)
        self.assertEqual((report["images"], report["codes_unavailable"]), (0, 2))
        self.assertFalse(os.path.exists(out))
        self.assertEqual((est["images"], est["estimate"]), (0, None))


@unittest.skipIf(zxingcpp is None, "zxing-cpp no está instalado (pip install -r requirements-test.txt)")
class Reduccion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(prefix="reduce-bn-test-")
        cls.src = os.path.join(cls.d, "in.pdf")
        cls.out = os.path.join(cls.d, "out.pdf")
        cls.xs = build(cls.src)
        cls.report = reduce_bn.reduce(cls.src, cls.out, 200)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, True)

    def test_informe_y_salida_valida(self):
        self.assertEqual(self.report, {"ppp": 200, "images": 2, "code_patches": 2, "codes_unavailable": 0,
                                       "codes_lost": 0, "not_identical": 0, "untouched_changed": False})
        r = subprocess.run([sys.executable, os.path.join(HERE, "pdf_check.py"), "verify", self.src, self.out])
        self.assertEqual(r.returncode, 0)
        self.assertLess(os.path.getsize(self.out), os.path.getsize(self.src))

    def test_los_codigos_se_leen_y_son_los_pixeles_originales(self):
        orig, out = pymupdf.open(self.src), pymupdf.open(self.out)
        self.assertEqual(codes_on(orig, 0), {QR_TEXT, BAR_TEXT})
        self.assertEqual(codes_on(out, 0), {QR_TEXT, BAR_TEXT})
        # La imagen de la página 1 es ahora un Form con la reducida y los recortes sin pérdida.
        form = drawn(out, 0, 0)
        self.assertEqual(out.xref_get_key(form, "Subtype")[1], "/Form")
        raw = orig.xref_stream(self.xs["scan"])
        full = Image.frombytes("1", (W, H), raw)
        content = out.xref_stream(form).decode()
        patches = 0
        for line in content.splitlines():
            parts = line.split()
            if parts[-3:-1] != ["/R", "Do"] and parts[-2] == "Do":
                name = parts[-3]
                a, dd, e, f = (float(parts[i]) for i in (1, 4, 5, 6))
                box = (round(e * W), round((1 - f - dd) * H), round((e + a) * W), round((1 - f) * H))
                xref = int(out.xref_get_key(form, f"Resources/XObject/{name[1:]}")[1].split()[0])
                pw, ph = box[2] - box[0], box[3] - box[1]
                self.assertEqual((out.xref_get_key(xref, "Width")[1], out.xref_get_key(xref, "Height")[1]), (str(pw), str(ph)))
                self.assertTrue(jbig2._rows_equal(out.xref_stream(xref), to_raw(full.crop(box).convert("L")), pw, ph))
                patches += 1
        self.assertEqual(patches, 2)

    def test_resolucion_y_aspecto(self):
        orig, out = pymupdf.open(self.src), pymupdf.open(self.out)
        x2 = drawn(out, 1, 0)
        self.assertEqual((out.xref_get_key(x2, "Width")[1], out.xref_get_key(x2, "Height")[1]), ("1112", "778"))
        self.assertEqual(out.xref_get_key(x2, "Subtype")[1], "/Image")
        a = orig[1].get_pixmap(dpi=72, colorspace=pymupdf.csGRAY).samples
        b = out[1].get_pixmap(dpi=72, colorspace=pymupdf.csGRAY).samples
        self.assertLess(sum(abs(p - q) for p, q in zip(a, b)) / len(a), 10)

    def test_lo_demas_no_cambia(self):
        orig, out = pymupdf.open(self.src), pymupdf.open(self.out)
        for i, name in ((1, "bar"), (2, "photo")):
            self.assertEqual(out.xref_stream(drawn(out, 0, i)), orig.xref_stream(self.xs[name]), name)
            self.assertEqual(out.xref_stream_raw(drawn(out, 0, i)), orig.xref_stream_raw(self.xs[name]), name)

    def test_estimacion_cerca_del_resultado(self):
        est = reduce_bn.estimate(self.src, 200)
        self.assertEqual((est["images"], est["sampled"]), (2, 2))
        self.assertLess(abs(est["estimate"] - os.path.getsize(self.out)) / os.path.getsize(self.out), 0.15)

    def test_si_la_reducida_no_se_decodifica_igual_no_se_reduce(self):
        d = tmp_dir(self)
        out = os.path.join(d, "o.pdf")
        real = reduce_bn.encodings

        def wrong(raw, w, h, tmpdir, jb=False):
            other = bytearray(raw)
            other[len(other) // 2] ^= 0x40
            return real(bytes(other), w, h, tmpdir)

        with mock.patch.object(reduce_bn, "encodings", wrong):
            report = reduce_bn.reduce(self.src, out, 200)
        self.assertEqual((report["images"], report["not_identical"]), (0, 2))
        self.assertFalse(os.path.exists(out))

    def test_si_un_codigo_dejaria_de_leerse_esa_pagina_no_se_reduce(self):
        # En la comprobación (la reducida ampliada con los recortes encima) zxing no lee nada: la
        # página 1, con códigos, se queda como estaba; la 2, sin códigos, se reduce.
        d = tmp_dir(self)
        out = os.path.join(d, "o.pdf")
        with mock.patch.object(reduce_bn, "_read_codes", lambda img: reduce_bn.collections.Counter()):
            report = reduce_bn.reduce(self.src, out, 200)
        self.assertEqual((report["images"], report["codes_lost"], report["code_patches"]), (1, 1, 0))
        orig, res = pymupdf.open(self.src), pymupdf.open(out)
        self.assertEqual(res.xref_stream_raw(drawn(res, 0, 0)), orig.xref_stream_raw(self.xs["scan"]))
        self.assertEqual(codes_on(res, 0), {QR_TEXT, BAR_TEXT})

    def test_la_comprobacion_de_codigos_de_verdad(self):
        # Sin recortes, con la reducida sola, el Code128 de módulos de 3 px no da lo mismo: la
        # comprobación tiene que mirar lo que de verdad se entrega.
        doc = pymupdf.open(self.src)
        raw = doc.xref_stream(self.xs["scan"])
        boxes, codes = reduce_bn.find_codes(raw, W, H)
        self.assertEqual({t for _, t in codes}, {QR_TEXT, BAR_TEXT})
        small = reduce_bn.downsample(raw, W, H, 1112, 778)
        self.assertTrue(reduce_bn.codes_still_read(small, 1112, 778, raw, W, H, boxes, codes))
        tiny = reduce_bn.downsample(raw, W, H, 300, 210)
        self.assertFalse(reduce_bn.codes_still_read(tiny, 300, 210, raw, W, H, [], codes))

    def test_las_demas_imagenes_byte_a_byte_y_si_no_sin_salida(self):
        reduced = set(reduce_bn.scans(pymupdf.open(self.src), 200))
        self.assertTrue(reduce_bn.untouched(self.src, self.out, reduced))
        # Una salida con la foto cambiada: la comprobación lo ve.
        d = tmp_dir(self)
        bad = os.path.join(d, "mal.pdf")
        doc = pymupdf.open(self.out)
        foto = drawn(doc, 0, 2)
        doc.update_stream(foto, bytes(60 * 40 * 3))
        doc.save(bad)
        self.assertFalse(reduce_bn.untouched(self.src, bad, reduced))
        # Y si falla, reduce no deja salida.
        out = os.path.join(d, "o.pdf")
        with mock.patch.object(reduce_bn, "untouched", lambda *a: False):
            report = reduce_bn.reduce(self.src, out, 200)
        self.assertEqual((report["images"], report["untouched_changed"]), (0, True))
        self.assertFalse(os.path.exists(out))

    def test_cli(self):
        d = tmp_dir(self)
        out, rep = os.path.join(d, "o.pdf"), os.path.join(d, "r.json")
        r = subprocess.run([sys.executable, os.path.join(HERE, "reduce_bn.py"), "reduce", self.src, out, rep], capture_output=True)
        self.assertEqual(r.returncode, 0)
        with open(rep, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["images"], 2)
        self.assertEqual(r.stdout, b"")


class ImagenSinFiltro(unittest.TestCase):
    def test_una_imagen_sin_filtro_sale_en_flate_con_los_mismos_bytes(self):
        # El guardado con deflate comprime las imágenes que venían sin filtro: la comprobación de
        # que lo demás está intacto lo admite solo con los mismos bytes decodificados.
        d = tmp_dir(self)
        src, out = os.path.join(d, "in.pdf"), os.path.join(d, "out.pdf")
        doc = pymupdf.open()
        scan = g4_image(doc, scan_raw(False))
        datos = bytes(v for y in range(40) for x in range(60) for v in (x * 4 % 256, y * 6 % 256, 7))
        foto = doc.get_new_xref()
        doc.update_object(foto, "<< /Type /XObject /Subtype /Image /Width 60 /Height 40 /BitsPerComponent 8 /ColorSpace /DeviceRGB >>")
        doc.update_stream(foto, datos, compress=False)
        page_with(doc, [(scan, 0, 0, 400, 280), (foto, 300, 200, 30, 20)])
        doc.save(src)
        self.assertEqual(pymupdf.open(src).xref_get_key(foto, "Filter")[0], "null")
        report = reduce_bn.reduce(src, out, 200)
        self.assertEqual((report["images"], report["untouched_changed"]), (1, False))
        res = pymupdf.open(out)
        self.assertEqual(res.xref_stream(drawn(res, 0, 1)), datos)
        # Con otros bytes decodificados, no vale.
        res.update_stream(drawn(res, 0, 1), bytes(len(datos)))
        mal = os.path.join(d, "mal.pdf")
        res.save(mal)
        self.assertFalse(reduce_bn.untouched(src, mal, set(reduce_bn.scans(pymupdf.open(src), 200))))

TESSERACT = shutil.which("tesseract")
OCR_TEXTO = ("Juzgado procedimiento ordinario liquidación intereses costas embargo cuantía "
             "fundamento 12.345,67 euros 03/11/2025 R-123/2024 folio 864 testifical traslado")


def ocr_words(img):
    """Palabras que lee Tesseract (español) en una imagen Pillow a 300 ppp."""
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        img.save(f.name, dpi=(300, 300))
        out = subprocess.run([TESSERACT, f.name, "-", "-l", "spa", "--dpi", "300"], capture_output=True, text=True,
                             env={**os.environ, "OMP_THREAD_LIMIT": "1"}, check=True).stdout
    return collections.Counter(w.strip(".,()").lower() for w in out.split() if any(c.isalnum() for c in w))


@unittest.skipUnless(TESSERACT, "falta tesseract (con el idioma spa): la legibilidad no se mide")
class LegibilidadOCR(unittest.TestCase):
    """Mide (no bloquea nada en el motor): palabras que el OCR lee en el original a 300 ppp y deja
    de leer a 200 ppp, con texto de 8, 10 y 12 pt. Con el sintético grande, alrededor del 1 %;
    aquí se exige que no pase del 5 % (si sube, algo ha empeorado la reducción)."""

    def test_perdida_de_palabras_a_200_ppp(self):
        doc = pymupdf.open()
        page = doc.new_page(width=W * 72 / 300, height=H * 72 / 300)
        y = 20
        real = collections.Counter()
        for size in (8, 10, 12) * 4:
            page.insert_text((10, y), OCR_TEXTO[:int(95 * 8 / size)], fontname="tiro" if size == 10 else "helv",
                             fontsize=size)
            real.update(w.strip(".,()").lower() for w in OCR_TEXTO[:int(95 * 8 / size)].split())
            y += size * 1.6
        pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples).crop((0, 0, W, H)).point(lambda v: 255 if v >= 150 else 0)
        raw = to_raw(img)
        small = reduce_bn.downsample(raw, W, H, 1112, 778)
        big = Image.frombytes("1", (1112, 778), small).convert("L").resize((W, H), Image.BILINEAR)
        antes = real & ocr_words(img)
        despues = real & ocr_words(big)
        perdidas = sum((antes - despues).values())
        print(f"\nOCR a 200 ppp: {perdidas} de {sum(antes.values())} palabras leídas se pierden")
        self.assertLessEqual(perdidas, 0.05 * sum(antes.values()))


if __name__ == "__main__":
    unittest.main()
