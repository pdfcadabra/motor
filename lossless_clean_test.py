# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Tests de lossless_clean.py (nivel baja de Comprimir, sin pérdida) y de perfil.py, con PDFs
sintéticos y PyMuPDF real: python -m unittest lossless_clean_test.py

- Ninguna imagen cambia (píxeles idénticos y, en los JPEG, los mismos bytes), tampoco las
  protegidas, y los QR y códigos de barras se siguen leyendo (con zxing-cpp).
- El contenido de las páginas sale byte a byte (con ' y ").
- Fuentes: las TrueType completas se recortan con el mismo texto y el mismo render; las Type1,
  CFF, Type3 y las del formulario (/DR) salen como estaban; si el texto cambiase, se descarta
  el recorte.
- PDF/A: sin recorte y, en PDF/A-1, sin flujos de objetos (un PDF/A-1b de verdad, hecho con
  Ghostscript, si está instalado).
"""
import glob
import json
import os
import random
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

import pymupdf

import lossless_clean
import lossless_images as li
import perfil
import pdf_check
from lossless_images_test import GS, QR_TEXT, BAR_TEXT, decode_page, make_codes_pdf, tmp_dir, zxingcpp

try:
    from PIL import Image
except ImportError:  # Pillow está en requirements.txt
    Image = None

HERE = os.path.dirname(os.path.abspath(__file__))
pymupdf.TOOLS.mupdf_display_errors(False)

TEXT = "Juzgado de Primera Instancia n.º 3 de Málaga. Procedimiento ordinario 123/2026. «Ñandú»"


def type1_file():
    """Una fuente Type1 (.t1/.pfb) del sistema (las URW de Ghostscript), o None."""
    for pattern in ("/usr/share/fonts/type1/urw-base35/*.t1", "/usr/share/fonts/type1/*/*.pfb",
                    "/usr/share/ghostscript/fonts/*.pfb"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


def text_pdf(path, fontbuffer=None, fontfile=None, simple=False, pages=3):
    """Texto con una fuente incrustada (completa: sin recortar) en cada página."""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_font(fontname="F1", fontbuffer=fontbuffer, fontfile=fontfile, set_simple=simple)
        page.insert_text((50, 100), TEXT, fontname="F1", fontsize=11)
        page.insert_text((50, 130), f"Página {i + 1}: ABCxyz 0123456789", fontname="F1", fontsize=11)
    doc.save(path)
    return path


def type3_pdf(path):
    """Fuente Type3 a mano: un cuadrado (a) y un triángulo (b), con ToUnicode."""
    doc = pymupdf.open()
    page = doc.new_page()

    def stream(data, dict_="<<>>"):
        x = doc.get_new_xref()
        doc.update_object(x, dict_)
        doc.update_stream(x, data)
        return x

    sq = stream(b"1000 0 0 0 750 750 d1 0 0 750 750 re f")
    tr = stream(b"1000 0 0 0 750 750 d1 0 0 m 750 0 l 375 750 l f")
    tu = stream(b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /T3 def "
                b"1 begincodespacerange <00> <FF> endcodespacerange 2 beginbfchar <61> <0061> "
                b"<62> <0062> endbfchar endcmap CMapName currentdict /CMap defineresource pop end end")
    font = doc.get_new_xref()
    doc.update_object(font, f"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 750 750] "
                            f"/FontMatrix [0.001 0 0 0.001 0 0] /CharProcs << /sq {sq} 0 R /tr {tr} 0 R >> "
                            f"/Encoding << /Type /Encoding /Differences [97 /sq /tr] >> /FirstChar 97 "
                            f"/LastChar 98 /Widths [1000 1000] /ToUnicode {tu} 0 R /Resources << >> >>")
    content = stream(b"BT /T3 24 Tf 72 700 Td (abba ab) Tj ET")
    doc.xref_set_key(page.xref, "Contents", f"{content} 0 R")
    doc.xref_set_key(page.xref, "Resources", f"<< /Font << /T3 {font} 0 R >> >>")
    doc.save(path)
    return path


def fonts(doc):
    """{xref del descriptor: (FontName, clave del programa, bytes del programa)}."""
    out = {}
    for x in range(1, doc.xref_length()):
        if doc.xref_get_key(x, "Type")[1] != "/FontDescriptor":
            continue
        for k in lossless_clean.FONT_FILES:
            t, v = doc.xref_get_key(x, k)
            if t == "xref":
                out[x] = (doc.xref_get_key(x, "FontName")[1], k, doc.xref_stream(int(v.split()[0])))
    return out


def font_objects(doc):
    """Diccionarios de fuente y descriptores, como texto, sin las referencias (los números de
    objeto cambian al guardar con garbage=4)."""
    objs = []
    for x in range(1, doc.xref_length()):
        if doc.xref_get_key(x, "Type")[1] in ("/Font", "/FontDescriptor") and not doc.xref_is_stream(x):
            text = re.sub(r"\d+ 0 R", "R", doc.xref_object(x, compressed=True))
            objs.append(" ".join(sorted(re.findall(r"/\w+(?:\[[^\]]*\]|\([^)]*\)|<[^<>]*>|[^/]*)", text))))
    return sorted(objs)


def renders(doc, dpi=100):
    return [p.get_pixmap(dpi=dpi).samples for p in doc]


def all_images(doc):
    """[(página, bbox redondeado, ancho, alto, n, muestras, flujo decodificado sin DCT)] de cada
    imagen colocada en cada página, en orden: así se comparan aunque cambie el xref."""
    out = []
    for pno, page in enumerate(doc):
        for info in sorted(page.get_image_info(xrefs=True), key=lambda i: tuple(i["bbox"])):
            x = info["xref"]
            pix = pymupdf.Pixmap(doc, x)
            out.append((pno, tuple(round(v, 1) for v in info["bbox"]), pix.width, pix.height, pix.n,
                        pix.samples, doc.xref_stream(x) if "DCT" in doc.xref_get_key(x, "Filter")[1] else None))
    return out


def page_contents(doc):
    return [b"".join(doc.xref_stream(c) for c in page.get_contents()) for page in doc]


def images_pdf(path):
    """Una página por tipo de imagen que no puede cambiar: JPEG gris, RGB y CMYK (sin capa
    Flate), foto sin pérdida (Flate RGB), Indexed de 16 colores, imagen con SMask y JPEG 2000,
    más contenido sin comprimir con los operadores ' y "."""
    doc = pymupdf.open()
    rng = random.Random(7)
    noise = bytes(rng.randrange(256) for _ in range(400 * 300 * 3))
    rgb = Image.frombytes("RGB", (400, 300), noise)

    def encoded(img, fmt, **kw):
        import io
        b = io.BytesIO()
        img.save(b, fmt, **kw)
        return b.getvalue()

    def add_image(dict_, data, compress):
        # update_stream(compress=False) quita /Filter: el filtro va después.
        filt = re.search(r"/Filter (/\w+)", dict_)
        x = doc.get_new_xref()
        rest = re.sub(r"/Filter /\w+", "", dict_)
        doc.update_object(x, f"<< /Type /XObject /Subtype /Image {rest} >>")
        doc.update_stream(x, data, compress=compress)
        if filt:
            doc.xref_set_key(x, "Filter", filt.group(1))
        return x

    imgs = [
        ("/Width 400 /Height 300 /BitsPerComponent 8 /ColorSpace /DeviceGray /Filter /DCTDecode",
         encoded(rgb.convert("L"), "JPEG", quality=90), False),
        ("/Width 400 /Height 300 /BitsPerComponent 8 /ColorSpace /DeviceRGB /Filter /DCTDecode",
         encoded(rgb, "JPEG", quality=85), False),
        ("/Width 400 /Height 300 /BitsPerComponent 8 /ColorSpace /DeviceCMYK /Filter /DCTDecode",
         encoded(rgb.convert("CMYK"), "JPEG", quality=85), False),
        ("/Width 400 /Height 300 /BitsPerComponent 8 /ColorSpace /DeviceRGB", noise, True),
        ("/Width 400 /Height 300 /BitsPerComponent 8 /ColorSpace /DeviceRGB /Filter /JPXDecode",
         encoded(rgb, "JPEG2000", quality_mode="rates", quality_layers=[20]), False),
    ]
    xrefs = [add_image(*i) for i in imgs]
    palette = bytes(rng.randrange(256) for _ in range(48)).hex()
    xrefs.append(add_image(f"/Width 64 /Height 64 /BitsPerComponent 8 /ColorSpace [/Indexed /DeviceRGB 15 <{palette}>]",
                           bytes(rng.randrange(16) for _ in range(64 * 64)), True))
    smask = add_image("/Width 400 /Height 300 /BitsPerComponent 8 /ColorSpace /DeviceGray",
                      bytes(rng.randrange(256) for _ in range(400 * 300)), True)
    xrefs.append(add_image(f"/Width 400 /Height 300 /BitsPerComponent 8 /ColorSpace /DeviceRGB /SMask {smask} 0 R",
                           noise, True))
    for x in xrefs:
        page = doc.new_page(width=595, height=842)
        page.insert_text((0, 0), " ")
        doc.xref_set_key(page.xref, "Resources", f"<< /XObject << /Im {x} 0 R >> /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>")
        doc.update_stream(page.get_contents()[0],
                          b"q 400 0 0 300 50 400 cm /Im Do Q BT /F1 12 Tf 14 TL 50 300 Td (uno) ' 2 1 (dos) \" ET",
                          compress=False)
    doc.save(path)
    return path


class SinPerdida(unittest.TestCase):
    def test_ninguna_imagen_cambia_y_los_jpeg_conservan_sus_bytes(self):
        d = tmp_dir(self)
        src, out = images_pdf(os.path.join(d, "in.pdf")), os.path.join(d, "out.pdf")
        lossless_clean.clean(src, out)
        a, b = pymupdf.open(src), pymupdf.open(out)
        ia, ib = all_images(a), all_images(b)
        self.assertEqual(len(ia), 7)
        self.assertEqual(sum(1 for i in ia if i[6] is not None), 3, "tres JPEG")
        self.assertEqual(ia, ib)
        self.assertEqual(renders(a), renders(b))
        self.assertIsNone(pdf_check.verify(src, out))
        # La capa Flate de los JPEG sí cambia el flujo crudo (eso es lo que ahorra).
        filters = {b.xref_get_key(x, "Filter")[1] for x in range(1, b.xref_length())
                   if b.xref_get_key(x, "Subtype")[1] == "/Image"}
        filters = {f.replace(" ", "") for f in filters}
        self.assertIn("[/FlateDecode/DCTDecode]", filters)
        self.assertIn("/JPXDecode", filters)

    def test_el_contenido_de_las_paginas_sale_byte_a_byte(self):
        d = tmp_dir(self)
        src, out = images_pdf(os.path.join(d, "in.pdf")), os.path.join(d, "out.pdf")
        lossless_clean.clean(src, out)
        self.assertEqual(page_contents(pymupdf.open(src)), page_contents(pymupdf.open(out)))
        # Y ahora va comprimido (estaba sin comprimir).
        b = pymupdf.open(out)
        self.assertNotEqual(b.xref_get_key(b[0].get_contents()[0], "Filter")[1], "null")

    def test_imagenes_protegidas_identicas(self):
        d = tmp_dir(self)
        src, out = os.path.join(d, "codes.pdf"), os.path.join(d, "out.pdf")
        names = make_codes_pdf(src)
        lossless_clean.clean(src, out)
        a, b = pymupdf.open(src), pymupdf.open(out)
        protected = [x for _, x in names if li.is_protected(a, x)]
        self.assertGreaterEqual(len(protected), 10)
        self.assertEqual(all_images(a), all_images(b))
        self.assertEqual(renders(a, 150), renders(b, 150))

    @unittest.skipIf(zxingcpp is None, "zxing-cpp no está instalado (pip install -r requirements-test.txt)")
    def test_qr_y_codigos_de_barras_se_siguen_leyendo(self):
        d = tmp_dir(self)
        src, out = os.path.join(d, "codes.pdf"), os.path.join(d, "out.pdf")
        names = make_codes_pdf(src)
        lossless_clean.clean(src, out)
        b = pymupdf.open(out)
        for page_no, (name, _) in enumerate(names):
            if name in ("photo", "stamp"):
                continue
            with self.subTest(image=name):
                self.assertIn(QR_TEXT if name.startswith("qr") else BAR_TEXT, decode_page(b, page_no))

    def test_ahorra_en_lo_estructural(self):
        # La misma fuente completa en dos documentos unidos (expediente) y contenido sin
        # comprimir: garbage=4 une las copias y el recorte las deja en lo que se usa.
        d = tmp_dir(self)
        one = text_pdf(os.path.join(d, "uno.pdf"), fontbuffer=pymupdf.Font("cjk").buffer)
        doc = pymupdf.open(one)
        doc.insert_pdf(pymupdf.open(one))
        src, out = os.path.join(d, "in.pdf"), os.path.join(d, "out.pdf")
        doc.save(src)
        lossless_clean.clean(src, out)
        self.assertLess(os.path.getsize(out), os.path.getsize(src) * 0.1)
        self.assertEqual(lossless_clean.page_texts(src), lossless_clean.page_texts(out))


class Fuentes(unittest.TestCase):
    def check(self, src, subset):
        out = src[:-4] + "-out.pdf"
        result = lossless_clean.clean(src, out)
        a, b = pymupdf.open(src), pymupdf.open(out)
        self.assertEqual(lossless_clean.page_texts(src), lossless_clean.page_texts(out))
        self.assertEqual(renders(a, 150), renders(b, 150))
        self.assertFalse(result["subset_reverted"])
        fa, fb = fonts(a), fonts(b)
        if subset:
            self.assertGreater(result["fonts_subset"], 0)
            for name, key, data in fb.values():
                self.assertRegex(name, r"^/[A-Z]{6}\+")
            self.assertLess(sum(len(v[2]) for v in fb.values()), sum(len(v[2]) for v in fa.values()) / 4)
        else:
            self.assertEqual(result["fonts_subset"], 0)
            self.assertEqual(sorted(fa.values()), sorted(fb.values()), "programas de fuente intactos")
            self.assertEqual(font_objects(a), font_objects(b), "diccionarios de fuente intactos")
        return a, b

    def test_truetype_completa_cid_se_recorta(self):
        d = tmp_dir(self)
        self.check(text_pdf(os.path.join(d, "tt.pdf"), fontbuffer=pymupdf.Font("cjk").buffer), subset=True)

    def test_truetype_completa_simple_se_recorta(self):
        d = tmp_dir(self)
        self.check(text_pdf(os.path.join(d, "tt.pdf"), fontbuffer=pymupdf.Font("cjk").buffer, simple=True), subset=True)

    def test_truetype_recortada_sin_cidset(self):
        # MuPDF no actualiza /CIDSet al recortar: diría que hay glifos que ya no están.
        d = tmp_dir(self)
        src = text_pdf(os.path.join(d, "tt.pdf"), fontbuffer=pymupdf.Font("cjk").buffer)
        doc = pymupdf.open(src)
        for x in fonts(doc):
            c = doc.get_new_xref()
            doc.update_object(c, "<<>>")
            doc.update_stream(c, b"\xff" * 64)
            doc.xref_set_key(x, "CIDSet", f"{c} 0 R")
        doc.save(src, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
        _, b = self.check(src, subset=True)
        for x in fonts(b):
            self.assertEqual(b.xref_get_key(x, "CIDSet")[0], "null")

    def test_cff_simple_y_cid_no_se_tocan(self):
        d = tmp_dir(self)
        for simple in (True, False):
            with self.subTest(simple=simple):
                src = text_pdf(os.path.join(d, f"cff{simple}.pdf"), fontbuffer=pymupdf.Font("tiro").buffer, simple=simple)
                a, _ = self.check(src, subset=False)
                self.assertEqual({v[1] for v in fonts(a).values()}, {"FontFile3"})

    @unittest.skipUnless(type1_file(), "no hay ninguna fuente Type1 (.t1/.pfb) en el sistema")
    def test_type1_simple_y_cid_no_se_tocan(self):
        # MuPDF 1.28.2 no recorta su programa, pero en la simple ponía a 0 los /Widths de los
        # caracteres sin usar y le añadía un prefijo de subconjunto falso.
        d = tmp_dir(self)
        for simple in (True, False):
            with self.subTest(simple=simple):
                src = text_pdf(os.path.join(d, f"t1{simple}.pdf"), fontfile=type1_file(), simple=simple)
                a, _ = self.check(src, subset=False)
                self.assertEqual({v[1] for v in fonts(a).values()}, {"FontFile"})

    def test_type3_no_se_toca(self):
        d = tmp_dir(self)
        src = type3_pdf(os.path.join(d, "t3.pdf"))
        a, b = self.check(src, subset=False)
        self.assertEqual(lossless_clean.page_texts(src), ["abba ab"])

    def test_truetype_y_cff_juntas_solo_se_recorta_la_truetype(self):
        d = tmp_dir(self)
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_font(fontname="TT", fontbuffer=pymupdf.Font("cjk").buffer)
        page.insert_font(fontname="CF", fontbuffer=pymupdf.Font("tiro").buffer)
        page.insert_text((50, 100), TEXT, fontname="TT")
        page.insert_text((50, 130), TEXT, fontname="CF")
        src, out = os.path.join(d, "in.pdf"), os.path.join(d, "out.pdf")
        doc.save(src)
        result = lossless_clean.clean(src, out)
        self.assertEqual(result["fonts_subset"], 1)
        fb = {v[1]: v for v in fonts(pymupdf.open(out)).values()}
        fa = {v[1]: v for v in fonts(pymupdf.open(src)).values()}
        self.assertEqual(fb["FontFile3"], fa["FontFile3"])
        self.assertLess(len(fb["FontFile2"][2]), len(fa["FontFile2"][2]))
        self.assertEqual(renders(pymupdf.open(src)), renders(pymupdf.open(out)))

    def test_la_fuente_del_formulario_no_se_recorta(self):
        # Una fuente de /AcroForm /DR: un campo que se rellene después necesita la entera.
        d = tmp_dir(self)
        src = text_pdf(os.path.join(d, "form.pdf"), fontbuffer=pymupdf.Font("cjk").buffer, pages=1)
        doc = pymupdf.open(src)
        font = doc[0].get_fonts()[0][0]
        doc.xref_set_key(doc.pdf_catalog(), "AcroForm", f"<< /Fields [] /DR << /Font << /F1 {font} 0 R >> >> /DA (/F1 0 Tf 0 g) >>")
        doc.save(src, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
        self.check(src, subset=False)

    def test_programa_compartido_con_la_del_formulario_no_se_recorta(self):
        # Dos descriptores con el mismo flujo de programa (p. ej. tras unir documentos): si uno
        # es del formulario, recortar el otro recortaría también el suyo.
        d = tmp_dir(self)
        src = text_pdf(os.path.join(d, "form.pdf"), fontbuffer=pymupdf.Font("cjk").buffer, pages=1)
        doc = pymupdf.open(src)
        font = doc[0].get_fonts()[0][0]
        desc = lossless_clean._descriptor(doc, font)
        copy_desc = doc.get_new_xref()
        doc.update_object(copy_desc, doc.xref_object(desc, compressed=True))
        copy_font = doc.get_new_xref()
        obj = doc.xref_object(font, compressed=True)
        if doc.xref_get_key(font, "DescendantFonts")[0] == "array":
            cid = lossless_clean._refs(doc.xref_get_key(font, "DescendantFonts")[1])[0]
            copy_cid = doc.get_new_xref()
            doc.update_object(copy_cid, doc.xref_object(cid, compressed=True).replace(f"{desc} 0 R", f"{copy_desc} 0 R"))
            obj = obj.replace(f"{cid} 0 R", f"{copy_cid} 0 R")
        else:
            obj = obj.replace(f"{desc} 0 R", f"{copy_desc} 0 R")
        doc.update_object(copy_font, obj)
        doc.xref_set_key(doc.pdf_catalog(), "AcroForm", f"<< /Fields [] /DR << /Font << /F9 {copy_font} 0 R >> >> >>")
        doc.save(src, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
        out = os.path.join(d, "out.pdf")
        result = lossless_clean.clean(src, out)
        self.assertEqual(result["fonts_subset"], 0)
        self.assertEqual(sorted({v[2] for v in fonts(pymupdf.open(src)).values()}),
                         sorted({v[2] for v in fonts(pymupdf.open(out)).values()}))

    def test_si_el_texto_cambia_se_descarta_el_recorte(self):
        d = tmp_dir(self)
        src = text_pdf(os.path.join(d, "tt.pdf"), fontbuffer=pymupdf.Font("cjk").buffer)
        out = os.path.join(d, "out.pdf")
        real = lossless_clean.page_texts
        calls = []

        def texts(path):
            calls.append(path)
            return real(path) if path == src else ["otro texto"]

        with mock.patch.object(lossless_clean, "page_texts", texts):
            result = lossless_clean.clean(src, out)
        self.assertTrue(result["subset_reverted"])
        self.assertEqual(result["fonts_subset"], 0)
        self.assertEqual(sorted(fonts(pymupdf.open(src)).values()), sorted(fonts(pymupdf.open(out)).values()))

    def test_si_subset_fonts_falla_se_guarda_sin_recortar(self):
        d = tmp_dir(self)
        src = text_pdf(os.path.join(d, "tt.pdf"), fontbuffer=pymupdf.Font("cjk").buffer)
        out = os.path.join(d, "out.pdf")
        with mock.patch.object(pymupdf.Document, "subset_fonts", side_effect=RuntimeError("fallo")):
            result = lossless_clean.clean(src, out)
        self.assertTrue(result["subset_reverted"])
        self.assertEqual(sorted(fonts(pymupdf.open(src)).values()), sorted(fonts(pymupdf.open(out)).values()))
        self.assertIsNone(pdf_check.verify(src, out))


def xmp_pdfa(part, conformance="B", style="attr", prefix="pdfaid"):
    ns = 'xmlns:%s="http://www.aiim.org/pdfa/ns/id/"' % prefix
    if style == "attr":
        desc = f'<rdf:Description rdf:about="" {ns} {prefix}:part="{part}" {prefix}:conformance="{conformance}"/>'
    else:
        desc = (f'<rdf:Description rdf:about="" {ns}><{prefix}:part>{part}</{prefix}:part>'
                f'<{prefix}:conformance>{conformance}</{prefix}:conformance></rdf:Description>')
    return ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">' + desc +
            '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>')


def has_object_streams(path):
    doc = pymupdf.open(path)
    return any(doc.xref_get_key(x, "Type")[1] in ("/ObjStm", "/XRef") for x in range(1, doc.xref_length()))


def srgb_icc():
    found = sorted(glob.glob("/usr/share/ghostscript/*/iccprofiles/srgb.icc")
                   + glob.glob("/usr/share/color/icc/ghostscript/srgb.icc"))
    return found[0] if found else None


class PdfA(unittest.TestCase):
    def test_declarado_por_xmp(self):
        d = tmp_dir(self)
        cases = [(1, "B", "attr", "pdfaid"), (2, "U", "elem", "pdfaid"), (3, "A", "attr", "id")]
        for part, conf, style, prefix in cases:
            with self.subTest(part=part, style=style, prefix=prefix):
                src = text_pdf(os.path.join(d, f"a{part}.pdf"), fontbuffer=pymupdf.Font("cjk").buffer)
                doc = pymupdf.open(src)
                doc.set_xml_metadata(xmp_pdfa(part, conf, style, prefix))
                doc.save(src, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
                self.assertEqual(pdf_check.pdfa(pymupdf.open(src)), {"part": part, "conformance": conf})
                out = os.path.join(d, f"o{part}.pdf")
                result = lossless_clean.clean(src, out)
                self.assertEqual(result["pdfa"], f"{part}{conf}")
                self.assertEqual(result["fonts_subset"], 0)
                self.assertEqual(sorted(fonts(pymupdf.open(src)).values()), sorted(fonts(pymupdf.open(out)).values()))
                self.assertEqual(has_object_streams(out), part != 1)
                b = pymupdf.open(out)
                self.assertEqual(pdf_check.pdfa(b), {"part": part, "conformance": conf}, "el XMP se conserva")
                meta = int(b.xref_get_key(b.pdf_catalog(), "Metadata")[1].split()[0])
                self.assertEqual(b.xref_get_key(meta, "Filter")[0], "null", "XMP sin comprimir")

    def test_sin_pdfa(self):
        d = tmp_dir(self)
        src = text_pdf(os.path.join(d, "x.pdf"), fontbuffer=pymupdf.Font("tiro").buffer)
        self.assertIsNone(pdf_check.pdfa(pymupdf.open(src)))
        doc = pymupdf.open(src)
        doc.set_xml_metadata('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                             '<rdf:Description rdf:about="" xmlns:pdf="http://ns.adobe.com/pdf/1.3/" pdf:Producer="x"/></rdf:RDF></x:xmpmeta>')
        doc.save(src, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
        self.assertIsNone(pdf_check.pdfa(pymupdf.open(src)))

    @unittest.skipUnless(GS and srgb_icc(), "hace falta Ghostscript (con su srgb.icc) para generar un PDF/A-1b")
    def test_pdfa_1b_de_ghostscript(self):
        # Un PDF/A-1b de verdad (Ghostscript con -dPDFA=1, fuentes TrueType completas). veraPDF
        # da por buena la salida (comprobado el 2026-10-24; no va en los tests: es Java).
        d = tmp_dir(self)
        base = text_pdf(os.path.join(d, "base.pdf"), fontbuffer=pymupdf.Font("cjk").buffer, simple=True)
        defs = os.path.join(d, "pdfa_def.ps")
        with open(defs, "w") as f:
            f.write(f"""%!
[ /Title (Prueba PDF/A) /DOCINFO pdfmark
[/_objdef {{icc_PDFA}} /type /stream /OBJ pdfmark
[{{icc_PDFA}} << /N 3 >> /PUT pdfmark
[{{icc_PDFA}} ({srgb_icc()}) (r) file /PUT pdfmark
[/_objdef {{OutputIntent_PDFA}} /type /dict /OBJ pdfmark
[{{OutputIntent_PDFA}} << /Type /OutputIntent /S /GTS_PDFA1 /DestOutputProfile {{icc_PDFA}} /OutputConditionIdentifier (sRGB) >> /PUT pdfmark
[{{Catalog}} << /OutputIntents [ {{OutputIntent_PDFA}} ] >> /PUT pdfmark
""")
        src, out = os.path.join(d, "pdfa1b.pdf"), os.path.join(d, "out.pdf")
        subprocess.run([GS, "-q", "-dPDFA=1", "-dBATCH", "-dNOPAUSE", "-dNOSAFER", "-dPDFACompatibilityPolicy=1",
                        "-sColorConversionStrategy=RGB", "-sDEVICE=pdfwrite", "-dSubsetFonts=false",
                        f"-sOutputFile={src}", defs, base], check=True, capture_output=True)
        a = pymupdf.open(src)
        self.assertEqual(pdf_check.pdfa(a), {"part": 1, "conformance": "B"})
        result = lossless_clean.clean(src, out)
        self.assertEqual(result, {"pdfa": "1B", "fonts_subset": 0, "object_streams": False, "subset_reverted": False})
        self.assertFalse(has_object_streams(out))
        with open(out, "rb") as f:
            self.assertTrue(f.read(8).startswith(b"%PDF-1."))
        b = pymupdf.open(out)
        self.assertEqual(sorted(fonts(a).values()), sorted(fonts(b).values()))
        self.assertNotEqual(b.xref_get_key(b.pdf_catalog(), "OutputIntents")[0], "null")
        self.assertEqual(lossless_clean.page_texts(src), lossless_clean.page_texts(out))
        self.assertEqual(renders(a), renders(b))
        self.assertIsNone(pdf_check.verify(src, out))


class LineaDeComandos(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "lossless_clean.py"), *args],
                              capture_output=True, text=True)

    def test_uso_y_salida(self):
        self.assertEqual(self.run_cli().returncode, 2)
        d = tmp_dir(self)
        src, out = images_pdf(os.path.join(d, "in.pdf")), os.path.join(d, "out.pdf")
        r = self.run_cli(src, out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertTrue(os.path.exists(out))

    def test_contrasena_de_apertura(self):
        d = tmp_dir(self)
        src, out = os.path.join(d, "a.pdf"), os.path.join(d, "out.pdf")
        doc = pymupdf.open()
        doc.new_page().insert_text((50, 50), "manzana1")
        doc.save(src, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="qa1234", owner_pw="qa1234-owner")
        r = self.run_cli(src, out)
        self.assertEqual(r.returncode, pdf_check.EXIT_ENCRYPTED)
        self.assertFalse(os.path.exists(out))

    def test_solo_propietario_se_limpia_y_sale_sin_cifrar(self):
        d = tmp_dir(self)
        src, out = os.path.join(d, "b.pdf"), os.path.join(d, "out.pdf")
        doc = pymupdf.open()
        doc.new_page().insert_text((50, 50), "manzana1")
        doc.save(src, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="", owner_pw="qa1234-owner",
                 permissions=int(pymupdf.PDF_PERM_PRINT))
        self.assertEqual(self.run_cli(src, out).returncode, 0)
        self.assertFalse(pymupdf.open(out).is_encrypted)


class Perfil(unittest.TestCase):
    """Motivos de perfil.py (docs/estudio-comprimir-nivel-bajo.md, punto 4)."""

    def hint(self, path):
        with pymupdf.open(path) as doc:
            return perfil.hint(perfil.profile(doc, os.path.getsize(path)))

    def jpeg_pdf(self, path, quality, size_px, place_pt, pages=2):
        import io
        rng = random.Random(3)
        img = Image.frombytes("RGB", (size_px, size_px), bytes(rng.randrange(256) for _ in range(size_px * size_px * 3)))
        b = io.BytesIO()
        img.save(b, "JPEG", quality=quality)
        doc = pymupdf.open()
        for _ in range(pages):
            doc.new_page().insert_image(pymupdf.Rect(0, 0, place_pt, place_pt), stream=b.getvalue())
        doc.save(path)
        return path

    def test_vectorial(self):
        d = tmp_dir(self)
        self.assertEqual(self.hint(text_pdf(os.path.join(d, "v.pdf"), fontbuffer=pymupdf.Font("tiro").buffer)), "vectorial")

    def test_escaneo_en_blanco_y_negro(self):
        d = tmp_dir(self)
        doc = pymupdf.open()
        rng = random.Random(5)
        w, h = 2480, 3508
        x = doc.get_new_xref()
        doc.update_object(x, f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /BitsPerComponent 1 /ColorSpace /DeviceGray >>")
        doc.update_stream(x, bytes(rng.randrange(256) for _ in range(w * h // 8)))
        page = doc.new_page()
        page.insert_text((0, 0), " ")
        doc.xref_set_key(page.xref, "Resources", f"<< /XObject << /Im {x} 0 R >> >>")
        doc.update_stream(page.get_contents()[0], b"q 595 0 0 842 0 0 cm /Im Do Q")
        src = os.path.join(d, "bn.pdf")
        doc.save(src)
        self.assertEqual(self.hint(src), "bn_sin_perdida")

    def test_jpeg(self):
        d = tmp_dir(self)
        # 900 px en 216 pt = 300 ppp, calidad 90: escaneo; 300 px en 216 pt = 100 ppp, calidad 50: ya optimizado.
        self.assertEqual(self.hint(self.jpeg_pdf(os.path.join(d, "s.pdf"), 90, 900, 216)), "escaneo_o_fotos_jpeg")
        self.assertEqual(self.hint(self.jpeg_pdf(os.path.join(d, "o.pdf"), 50, 300, 216)), "jpeg_ya_optimizado")

    def test_otro(self):
        d = tmp_dir(self)
        rng = random.Random(9)
        doc = pymupdf.open()
        pix = pymupdf.Pixmap(pymupdf.csRGB, 300, 300, bytes(rng.randrange(256) for _ in range(300 * 300 * 3)), 0)
        doc.new_page().insert_image(pymupdf.Rect(0, 0, 300, 300), pixmap=pix)
        src = os.path.join(d, "f.pdf")
        doc.save(src, deflate=True)
        self.assertEqual(self.hint(src), "otro")

    def test_linea_de_comandos(self):
        d = tmp_dir(self)
        src, report = text_pdf(os.path.join(d, "v.pdf"), fontbuffer=pymupdf.Font("tiro").buffer), os.path.join(d, "p.json")
        r = subprocess.run([sys.executable, os.path.join(HERE, "perfil.py"), src, report], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(report, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["hint"], "vectorial")
        self.assertIn(data["hint"], perfil.HINTS)
        self.assertEqual(set(data["shares"]), {"prot", "jpeg", "sinperd", "fuentes", "jpeg_q", "jpeg_ppp"})
        bad = subprocess.run([sys.executable, os.path.join(HERE, "perfil.py")], capture_output=True)
        self.assertEqual(bad.returncode, 2)


if __name__ == "__main__":
    unittest.main()
