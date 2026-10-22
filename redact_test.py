# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Tests de redact.py con PDFs sintéticos creados al vuelo (sin datos reales).

Uso: python -m unittest redact_test.py   (necesita PyMuPDF, como redact.py)
"""
import os
import struct
import tempfile
import unittest
import zlib

import pymupdf as fitz

import redact

TERM = "manzana1"


def tmp_path(test, name):
    d = tempfile.mkdtemp(prefix="redact-test-")
    test.addCleanup(lambda: [os.remove(os.path.join(d, f)) for f in os.listdir(d)] and None or os.rmdir(d))
    return os.path.join(d, name)


def search_items(path, word):
    """Hallazgos como los que envía el frontend, usando la búsqueda de redact.py."""
    doc = fitz.open(path)
    redact.flatten_document(doc)
    items = []
    for page in doc:
        for r in page.search_for(word):
            items.append({"id": f"{page.number}-{len(items)}", "page": page.number, "text": word,
                          "category": "Personalizado", "rect": [r.x0, r.y0, r.x1, r.y1]})
    doc.close()
    return items


def all_text(path):
    """Todo lo que podría delatar un término: texto, campos, metadatos, XMP, marcadores,
    adjuntos y los flujos descomprimidos."""
    doc = fitz.open(path)
    parts = [p.get_text() for p in doc]
    parts += [str(w.field_value) for p in doc for w in p.widgets()]
    parts += [str(v) for v in doc.metadata.values()]
    parts.append(doc.get_xml_metadata())
    parts += [e[1] for e in doc.get_toc()]
    parts += [doc.embfile_get(n).decode("latin-1") for n in doc.embfile_names()]
    for x in range(1, doc.xref_length()):
        parts.append(doc.xref_object(x))
        if doc.xref_is_stream(x):
            parts.append((doc.xref_stream(x) or b"").decode("latin-1"))
    doc.close()
    return "\n".join(parts)


def make_form_pdf(path):
    """Como formulario.pdf de scripts/qa/generar-muestras.mjs (archivo 6 de la QA): el
    término está en el texto de la página y en el valor de un campo de texto. Además,
    en un marcador, en los metadatos, en el XMP y en un adjunto."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 60), f"Referencia {TERM} del juicio ordinario 123/2026", fontsize=12)
    page.insert_text((60, 270), "Expediente:", fontsize=12)
    page.insert_text((60, 700), "Resolución del índice, España, pingüino", fontsize=12)
    w = fitz.Widget()
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.field_name = "expediente"
    w.field_value = TERM
    w.rect = fitz.Rect(200, 255, 500, 280)
    page.add_widget(w)
    doc.set_toc([[1, f"Expediente {TERM}", 1], [2, "Hijo del marcador", 1], [1, "Anexos", 1]])
    doc.set_metadata({"title": f"Caso {TERM}", "author": "Autor ficticio"})
    doc.set_xml_metadata(f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><dc>{TERM}</dc></x:xmpmeta>')
    doc.embfile_add("nota.txt", f"nota con {TERM}".encode(), filename="nota.txt")
    doc.save(path)
    doc.close()


QUOTE_LINES = [
    "Documento de prueba con el operador comilla",
    "Demandante: Persona Ficticia Uno, con DNI 12345678Z.",
    "Correo de contacto: persona.ficticia@example.com",
    "Vistos por el magistrado los presentes autos",
]


def make_quote_pdf(path, content=None):
    """Texto escrito con el operador ' (como texto.pdf de scripts/test-migracion, el
    archivo 7 de la QA): "14 TL 60 780 Td (línea) ' (línea) ' ..."."""
    if content is None:
        ops = ["BT /F1 11 Tf 14 TL 60 780 Td"]
        ops += [f"({line}) '" for line in QUOTE_LINES]
        ops.append('2 0.5 (Linea con comilla doble) "')
        ops.append("ET")
        content = "\n".join(ops)
    data = zlib.compress(content.encode("latin-1"))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    with open(path, "wb") as f:
        f.write(out)


def words(path, page=0):
    doc = fitz.open(path)
    try:
        return [w[4] for w in doc[page].get_text("words")]
    finally:
        doc.close()


class RewriteQuoteOperators(unittest.TestCase):
    def test_comilla_simple_y_doble(self):
        self.assertEqual(redact.rewrite_quote_operators(b"BT 14 TL (a) ' ET"), b"BT 14 TL T* (a) Tj ET")
        self.assertEqual(
            redact.rewrite_quote_operators(b'BT 1 0.5 <41> " ET'),
            b"BT 1 Tw 0.5 Tc T* <41> Tj ET",
        )

    def test_sin_operadores_no_cambia(self):
        self.assertIsNone(redact.rewrite_quote_operators(b"BT (it's \\(a\\) \"x\") Tj ET"))
        self.assertIsNone(redact.rewrite_quote_operators(b"BT [(a ' b) -20 (c)] TJ ET % ' en comentario"))

    def test_imagen_en_linea_con_comillas_en_los_datos(self):
        data = b"q BI /W 2 /H 1 /BPC 8 /CS /G ID '\" EI Q BT (x) ' ET"
        self.assertEqual(
            redact.rewrite_quote_operators(data),
            b"q BI /W 2 /H 1 /BPC 8 /CS /G ID '\" EI Q BT T* (x) Tj ET",
        )

    def test_flujo_roto_se_detecta(self):
        with self.assertRaises(redact._ContentSyntaxError):
            redact.rewrite_quote_operators(b"BT (sin cerrar ' ET")


class Fallo1FormularioArchivo6(unittest.TestCase):
    """El valor de un campo con el término se conservaba y se veía tras censurar."""

    def test_campo_marcador_metadatos_y_adjunto_sin_el_termino(self):
        src, out = tmp_path(self, "in.pdf"), tmp_path(self, "out.pdf")
        make_form_pdf(src)
        items = search_items(src, TERM)
        self.assertEqual(len(items), 2)  # el de la página y el del campo (aplanado)
        report = redact.apply(src, out, items)
        self.assertTrue(report["verified"], report)
        self.assertEqual(report["text_loss_pages"], [])
        self.assertNotIn(TERM, all_text(out).casefold())
        doc = fitz.open(out)
        self.assertFalse(doc.is_form_pdf)
        self.assertEqual([e[1] for e in doc.get_toc()], ["Hijo del marcador", "Anexos"])
        self.assertEqual(doc.embfile_count(), 0)
        doc.close()
        # El resto del texto sigue ahí y se puede extraer.
        self.assertIn("pingüino", words(out))
        self.assertIn("Expediente:", words(out))

    def test_busqueda_encuentra_el_valor_del_campo(self):
        src, res, pat = tmp_path(self, "in.pdf"), tmp_path(self, "r.json"), tmp_path(self, "p.json")
        make_form_pdf(src)
        with open(pat, "w", encoding="utf-8") as f:
            f.write('{"Personalizado": "(?i)manzana1"}')
        redact.search(src, pat, res)
        import json
        with open(res, encoding="utf-8") as f:
            rects = [r["rect"] for r in json.load(f)["results"]]
        self.assertEqual(len(rects), 2)
        self.assertTrue(any(fitz.Rect(r).intersects(fitz.Rect(200, 255, 500, 280)) for r in rects))


class VerificacionFinal(unittest.TestCase):
    def test_si_queda_el_termino_no_se_entrega(self):
        """Sin aplanar (el fallo 1), el campo conserva el término: verified=False y no hay salida."""
        src, out = tmp_path(self, "in.pdf"), tmp_path(self, "out.pdf")
        make_form_pdf(src)
        items = search_items(src, TERM)
        real_flatten, real_scrub = redact.flatten_document, fitz.Document.scrub
        redact.flatten_document = lambda doc: []
        fitz.Document.scrub = lambda doc, **kw: real_scrub(doc, **{**kw, "reset_fields": False})
        try:
            report = redact.apply(src, out, items)
        finally:
            redact.flatten_document, fitz.Document.scrub = real_flatten, real_scrub
        self.assertFalse(report["verified"])
        self.assertIn("form_fields", report["leaks"])
        self.assertFalse(os.path.exists(out))
        self.assertNotIn(TERM, repr(report))  # el informe (que va al log) no lleva términos

    def test_detecta_cada_sitio(self):
        path = tmp_path(self, "f.pdf")
        make_form_pdf(path)
        doc = fitz.open(path)
        zones = {0: doc[0].search_for(TERM)}
        doc.close()
        leaks = redact.verify_redaction(path, zones, {TERM})
        self.assertEqual(
            leaks,
            # pdf_objects: el barrido de objetos también ve el término en esos mismos sitios.
            ["attachments", "form_fields", "metadata_info", "metadata_xmp", "outline", "page_text", "pdf_objects"],
        )

    def test_aparicion_no_seleccionada_no_es_fuga(self):
        """El usuario puede desmarcar una aparición: si queda fuera de las zonas, no es fuga."""
        path = tmp_path(self, "f.pdf")
        doc = fitz.open()
        doc.new_page().insert_text((60, 60), f"{TERM} y otra vez {TERM}")
        doc.save(path)
        doc.close()
        self.assertEqual(redact.verify_redaction(path, {0: [fitz.Rect(0, 0, 1, 1)]}, {TERM}), [])


class CapaOcr(unittest.TestCase):
    def test_bajo_la_zona_desaparece_y_el_resto_sigue_buscable(self):
        """Escaneo con capa OCR invisible (render mode 3): el término bajo la zona se borra
        también de la capa, y el resto de la capa se conserva y se puede buscar."""
        src, out = tmp_path(self, "in.pdf"), tmp_path(self, "out.pdf")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((60, 60), f"Expediente {TERM} del juicio ordinario 123/2026", fontsize=12, render_mode=3)
        page.insert_text((60, 90), "Resolución del índice, España, pingüino", fontsize=12, render_mode=3)
        doc.save(src)
        doc.close()
        items = search_items(src, TERM)
        self.assertEqual(len(items), 1)
        report = redact.apply(src, out, items)
        self.assertTrue(report["verified"], report)
        self.assertEqual(report["text_loss_pages"], [])
        self.assertNotIn(TERM, all_text(out).casefold())
        doc = fitz.open(out)
        for word in ("Expediente", "123/2026", "pingüino"):
            self.assertTrue(doc[0].search_for(word), f"{word} debe seguir en la capa OCR")
        doc.close()


class Fallo2OperadorComillaArchivo7(unittest.TestCase):
    """Con texto escrito con ', censurar un DNI borraba todo el texto de la página."""

    def _apply_dni(self, src, out):
        items = search_items(src, "12345678Z")
        self.assertEqual(len(items), 1)
        return redact.apply(src, out, items)

    def test_solo_desaparece_el_dni(self):
        src, out = tmp_path(self, "in.pdf"), tmp_path(self, "out.pdf")
        make_quote_pdf(src)
        before = words(src)
        report = self._apply_dni(src, out)
        self.assertTrue(report["verified"], report)
        self.assertEqual(report["text_loss_pages"], [])
        after = words(out)
        self.assertNotIn("12345678Z.", after)
        # El punto que sigue al DNI queda fuera de la zona y se conserva.
        self.assertEqual([w if w != "12345678Z." else "." for w in before], after)
        # Y en su sitio: la primera y la última línea no se han movido.
        doc_in, doc_out = fitz.open(src), fitz.open(out)
        for word in ("Documento", "comilla", "doble"):
            self.assertEqual(doc_in[0].search_for(word), doc_out[0].search_for(word))

    def test_sin_reescritura_no_se_entrega(self):
        """Sin la reescritura de ' (el fallo 2), MuPDF saca el texto de la página sin borrar
        el DNI (sigue en el flujo, fuera de la página): la verificación no lo entrega.
        Antes se entregaba con X-Redact-Text-Loss (auditoría 2026-10-07, hallazgo 1)."""
        src, out = tmp_path(self, "in.pdf"), tmp_path(self, "out.pdf")
        make_quote_pdf(src)
        real = redact.normalize_quote_operators
        redact.normalize_quote_operators = lambda doc: (0, 0)
        try:
            report = self._apply_dni(src, out)
        finally:
            redact.normalize_quote_operators = real
        self.assertFalse(report["verified"])
        self.assertIn("hidden_text", report["leaks"])
        self.assertFalse(os.path.exists(out))


def make_xref_gap_pdf(path):
    """xref en flujo con /Index en varios bloques ([0 5 8 2]: faltan los objetos 5 a 7), como la
    que escribe pdf-lib (p. ej. la salida de Unir). Es válido, pero doc.scrub() aborta."""
    content = f"BT /F1 11 Tf 60 780 Td (Expediente {TERM} de prueba) Tj ET".encode()
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 8 0 R >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        8: b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    }
    out = bytearray(b"%PDF-1.5\n")
    off = {}
    for n, body in objs.items():
        off[n] = len(out)
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    off[9] = len(out)
    rows = b"".join(
        struct.pack(">BIH", 0 if n == 0 else 1, off.get(n, 0), 65535 if n == 0 else 0)
        for n in (0, 1, 2, 3, 4, 8, 9)
    )
    out += b"9 0 obj\n<< /Type /XRef /Size 10 /Root 1 0 R /W [1 4 2] /Index [0 5 8 2] /Length %d >>\n" % len(rows)
    out += b"stream\n" + rows + b"\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n" % off[9]
    with open(path, "wb") as f:
        f.write(out)


class XrefConHuecos(unittest.TestCase):
    """doc.scrub() abortaba con "cannot find object in xref" en PDFs salidos de Unir."""

    def test_se_censura_igual(self):
        src, out = tmp_path(self, "in.pdf"), tmp_path(self, "out.pdf")
        make_xref_gap_pdf(src)
        with self.assertRaises(RuntimeError):  # el fallo de PyMuPDF que se esquiva
            fitz.open(src).scrub()
        report = redact.apply(src, out, search_items(src, TERM))
        self.assertTrue(report["verified"], report)
        self.assertEqual(words(out), ["Expediente", "de", "prueba"])


def make_scan_pdf(path, kind):
    """Escaneo sintético de 2 páginas sin texto: JPEG en gris con ruido ("jpeg") o 1 bit
    ("1bit", Flate) a pantalla completa."""
    import random
    rng = random.Random(7)
    w, h = 1240, 1754
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page(width=595, height=842)
        if kind == "jpeg":
            samples = bytes(200 + rng.randrange(40) for _ in range(w * h))
            pix = fitz.Pixmap(fitz.csGRAY, w, h, samples, 0)
            page.insert_image(page.rect, stream=pix.tobytes("jpg", jpg_quality=60))
        else:
            stride = (w + 7) // 8
            rows = bytes(0xFF if (x // 40 + y // 40) % 2 else 0xF0 for y in range(h) for x in range(stride))
            xref = doc.get_new_xref()
            doc.update_object(xref, f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                                    "/ColorSpace /DeviceGray /BitsPerComponent 1 >>")
            doc.update_stream(xref, rows)
            page.insert_text((0, 0), " ")  # crea /Contents y /Resources
            doc.xref_set_key(page.xref, "Resources", f"<< /XObject << /Scan {xref} 0 R >> >>")
            doc.update_stream(page.get_contents()[0], b"q 595 0 0 842 0 0 cm /Scan Do Q")
    doc.save(path)
    doc.close()


ZONE = [120, 140, 300, 165]


def zone_items(n_pages):
    return [{"id": str(i), "page": i, "text": "Zona manual", "category": "Censura Manual", "rect": ZONE}
            for i in range(n_pages)]


def image_info(path):
    doc = fitz.open(path)
    try:
        return [(doc.xref_get_key(im[0], "Filter")[1], im[4]) for im in doc[0].get_images(full=True)]
    finally:
        doc.close()


def zone_is_black(path):
    doc = fitz.open(path)
    try:
        pix = doc[0].get_pixmap(dpi=72, clip=fitz.Rect(ZONE) + (4, 4, -4, -4), colorspace=fitz.csGRAY)
        return max(pix.samples) < 30
    finally:
        doc.close()


class TamanoDeSalida(unittest.TestCase):
    """apply_redactions dejaba las imágenes tocadas sin comprimir: un escaneo JPEG se
    multiplicaba por 3-5 y uno en CCITT pasaba a Flate."""

    def test_escaneo_jpeg_sigue_en_jpeg_y_no_crece(self):
        src, out = tmp_path(self, "in.pdf"), tmp_path(self, "out.pdf")
        make_scan_pdf(src, "jpeg")
        report = redact.apply(src, out, zone_items(2))
        self.assertTrue(report["verified"])
        self.assertLessEqual(os.path.getsize(out), os.path.getsize(src) * 1.1)
        self.assertIn("DCTDecode", image_info(out)[0][0])
        self.assertTrue(zone_is_black(out))

    def test_escaneo_1bit_vuelve_a_ccitt_sin_perdida(self):
        src, out = tmp_path(self, "in.pdf"), tmp_path(self, "out.pdf")
        make_scan_pdf(src, "1bit")
        report = redact.apply(src, out, zone_items(2))
        self.assertTrue(report["verified"])
        self.assertEqual(image_info(out)[0], ("/CCITTFaxDecode", 1))
        self.assertTrue(zone_is_black(out))
        # Fuera de la zona, los píxeles son los mismos (G4 no tiene pérdida).
        a = fitz.open(src)[0].get_pixmap(dpi=72, clip=fitz.Rect(300, 400, 500, 600))
        b = fitz.open(out)[0].get_pixmap(dpi=72, clip=fitz.Rect(300, 400, 500, 600))
        self.assertEqual(a.samples, b.samples)

    def test_calidad_jpeg_estimada(self):
        pix = fitz.Pixmap(fitz.csGRAY, 64, 64, bytes(range(256)) * 16, 0)
        for q in (40, 60, 85):
            self.assertAlmostEqual(redact.jpeg_quality(pix.tobytes("jpg", jpg_quality=q)), q, delta=2)
        self.assertIsNone(redact.jpeg_quality(b"no es un jpeg"))


# ==========================================
# Lote 2d: la salida no pesa más que la entrada
# ==========================================
SCAN_W, SCAN_H = 1240, 1754  # A4 a 150 ppp, a página completa


def scan_image(mode, seed=3):
    """Escaneo en color sintético: papel con ruido suave, franjas de color, renglones de texto
    y un sello rojo (con croma de detalle fino, donde el submuestreo 4:2:0 ahorra)."""
    import random
    from PIL import Image, ImageDraw, ImageFilter
    rng = random.Random(seed)
    img = Image.new("RGB", (SCAN_W, SCAN_H), (244, 240, 229))
    img = Image.blend(img, Image.effect_noise((SCAN_W, SCAN_H), 12).convert("RGB"), 0.05)
    d = ImageDraw.Draw(img)
    d.rectangle([90, 70, 1150, 150], fill=(206, 219, 236))
    for i in range(45):
        y = 220 + 32 * i
        color = (20, 40, 140) if i % 3 else (150, 30, 30)
        for x in range(100, 100 + rng.randrange(600, 1000), 14):
            d.rectangle([x, y, x + rng.randrange(4, 11), y + 14], fill=color)
    d.ellipse([850, 1350, 1100, 1600], outline=(176, 42, 48), width=10)
    img = img.filter(ImageFilter.GaussianBlur(0.7))
    return img.convert(mode)


def add_image(doc, page, data, filt, colorspace, name="Im", rect=None):
    """Imagen ya codificada (data con su filtro) dibujada en `rect` (por defecto, la página)."""
    xref = doc.get_new_xref()
    doc.update_object(xref, f"<< /Type /XObject /Subtype /Image /Width {SCAN_W} /Height {SCAN_H} "
                            f"/ColorSpace {colorspace} /BitsPerComponent 8 >>")
    doc.update_stream(xref, data, compress=False)
    doc.xref_set_key(xref, "Filter", filt)
    r = rect or page.rect
    if not page.get_contents():
        page.insert_text((0, 0), " ")  # crea /Contents y /Resources
    res = int(doc.xref_get_key(page.xref, "Resources")[1].split()[0])  # PyMuPDF lo crea indirecto
    xobjs = doc.xref_get_key(res, "XObject")
    entries = (xobjs[1][2:-2] if xobjs[0] == "dict" else "") + f" /{name} {xref} 0 R"
    doc.xref_set_key(res, "XObject", f"<<{entries}>>")
    c = page.get_contents()[0]
    doc.update_stream(c, doc.xref_stream(c) + f"\nq {r.width} 0 0 {r.height} {r.x0} {842 - r.y1} cm /{name} Do Q".encode())
    return xref


def encode(img, fmt, **kw):
    import io
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def make_image_pdf(path, pages):
    """pages: [[(datos, filtro, espacio de color, rect o None), ...], ...]."""
    doc = fitz.open()
    for images in pages:
        page = doc.new_page(width=595, height=842)
        for n, (data, filt, cs, rect) in enumerate(images):
            add_image(doc, page, data, filt, cs, name=f"Im{n}", rect=rect)
    doc.save(path)
    doc.close()


def first_image(path, page=0):
    """(filtro, componentes, bytes del flujo, Pixmap) de la primera imagen de la página."""
    doc = fitz.open(path)
    x = doc[page].get_images(full=True)[0][0]
    return doc.xref_get_key(x, "Filter")[1], fitz.Pixmap(doc, x).n, len(doc.xref_stream_raw(x)), fitz.Pixmap(doc, x)


def jpeg_sampling(path):
    """Factores de muestreo (h, v) de la luminancia de la primera JPEG de la página 1."""
    import io
    from PIL import Image
    doc = fitz.open(path)
    x = doc[0].get_images(full=True)[0][0]
    data = doc.xref_stream_raw(x)
    if "Flate" in doc.xref_get_key(x, "Filter")[1]:
        data = zlib.decompress(data)
    return Image.open(io.BytesIO(data)).layer[0][1:3]


# Zona en la imagen a página completa (en píxeles de la imagen: 150/72 por punto, con margen).
IMG_ZONE = (int(ZONE[0] * SCAN_W / 595) - 4, int(ZONE[1] * SCAN_H / 842) - 4,
            int(ZONE[2] * SCAN_W / 595) + 5, int(ZONE[3] * SCAN_H / 842) + 5)


def pixels_outside_zone(pix):
    """Píxeles de la imagen con la zona censurada (IMG_ZONE) pintada de 0 en los dos lados."""
    from PIL import Image
    mode = {1: "L", 3: "RGB", 4: "CMYK"}[pix.n]
    img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    img.paste(0, IMG_ZONE)
    return img


def mean_abs_diff(a, b):
    from PIL import ImageChops, ImageStat
    return max(ImageStat.Stat(ImageChops.difference(a, b)).mean)


class TamanoDeSalidaLote2d(unittest.TestCase):
    """Lote 2d (2026-10-15): un escaneo JPEG 4:2:0 crecía un 11 % (MuPDF lo recodificaba en
    4:4:4), uno en JPEG CMYK se quedaba en Flate (×3,3) y uno en JPEG 2000 pasaba a JPEG q85."""

    def apply_zone(self, src):
        out = tmp_path(self, "out.pdf")
        report = redact.apply(src, out, zone_items(1))
        self.assertTrue(report["verified"], report)
        self.assertTrue(zone_is_black(out))
        return out

    def test_jpeg_rgb_420_conserva_su_submuestreo_y_no_crece(self):
        src = tmp_path(self, "in.pdf")
        make_image_pdf(src, [[(encode(scan_image("RGB"), "JPEG", quality=85, subsampling=2),
                               "/DCTDecode", "/DeviceRGB", None)]])
        out = self.apply_zone(src)
        self.assertIn("DCTDecode", first_image(out)[0])
        self.assertEqual(jpeg_sampling(out), (2, 2))  # 4:2:0, como el original
        self.assertLessEqual(os.path.getsize(out), os.path.getsize(src))

    def test_jpeg_con_submuestreo_conocido_no_codifica_tambien_con_mupdf(self):
        """Con las tablas y el submuestreo del original basta Pillow: la copia de MuPDF (4:4:4)
        no ganaba en los escaneos del lote 2d y costaba entre un 22 y un 37 % del tiempo de apply."""
        from unittest import mock
        for mode, cs, kw in (("RGB", "/DeviceRGB", {"subsampling": 2}), ("L", "/DeviceGray", {})):
            with self.subTest(mode=mode):
                src = tmp_path(self, "in.pdf")
                make_image_pdf(src, [[(encode(scan_image(mode), "JPEG", quality=85, **kw), "/DCTDecode", cs, None)]])
                tobytes = fitz.Pixmap.tobytes
                with mock.patch.object(fitz.Pixmap, "tobytes", autospec=True, side_effect=tobytes) as spy:
                    out = self.apply_zone(src)
                self.assertFalse([c for c in spy.call_args_list if "jpg" in c.args], spy.call_args_list)
                self.assertIn("DCTDecode", first_image(out)[0])
                self.assertLessEqual(os.path.getsize(out), os.path.getsize(src))

    def test_submuestreo_que_pillow_no_lee_tambien_con_mupdf(self):
        import io
        from PIL import Image
        img = scan_image("RGB")
        src = Image.open(io.BytesIO(encode(img, "JPEG", quality=85)))
        self.assertTrue(redact._same_layout(img, src))
        src.layer = [(1, 1, 2, 0), (2, 1, 1, 1), (3, 1, 1, 1)]  # 4:4:0: Pillow no lo reproduce
        self.assertFalse(redact._same_layout(img, src))

    def test_jpeg_cmyk_vuelve_a_jpeg_cmyk_con_sus_colores(self):
        cmyk = scan_image("CMYK")
        for decode in ("", "/Decode [1 0 1 0 1 0 1 0]"):  # Adobe invertido, con /Decode o sin él
            with self.subTest(decode=decode):
                src = tmp_path(self, "in.pdf")
                make_image_pdf(src, [[(encode(cmyk, "JPEG", quality=90), "/DCTDecode", "/DeviceCMYK", None)]])
                if decode:
                    doc = fitz.open(src)
                    x = doc[0].get_images(full=True)[0][0]
                    doc.xref_set_key(x, "Decode", "[1 0 1 0 1 0 1 0]")
                    doc.saveIncr()
                    doc.close()
                out = self.apply_zone(src)
                filt, n, _, pix = first_image(out)
                self.assertIn("DCTDecode", filt)
                self.assertEqual(n, 4)
                self.assertLessEqual(os.path.getsize(out), os.path.getsize(src) * 1.1)
                # Mismos colores que el original fuera de la zona (decodificados por MuPDF).
                before = pixels_outside_zone(first_image(src)[3])
                self.assertLess(mean_abs_diff(before, pixels_outside_zone(pix)), 3)

    def test_jpeg2000_sin_perdida_sale_sin_perdida(self):
        src = tmp_path(self, "in.pdf")
        make_image_pdf(src, [[(encode(scan_image("RGB"), "JPEG2000", irreversible=False, no_jp2=True),
                               "/JPXDecode", "/DeviceRGB", None)]])
        self.assertIs(redact.jpx_reversible(fitz.open(src).xref_stream_raw(fitz.open(src)[0].get_images()[0][0])), True)
        out = self.apply_zone(src)
        self.assertNotIn("DCT", first_image(out)[0])
        # Fuera de la zona, píxeles idénticos.
        self.assertEqual(pixels_outside_zone(first_image(src)[3]).tobytes(),
                         pixels_outside_zone(first_image(out)[3]).tobytes())

    def test_jpeg2000_con_perdida_no_crece(self):
        src = tmp_path(self, "in.pdf")
        img = scan_image("RGB")
        data = encode(img, "JPEG2000", quality_mode="rates", quality_layers=[20], irreversible=True, no_jp2=True)
        make_image_pdf(src, [[(data, "/JPXDecode", "/DeviceRGB", None)]])
        self.assertIs(redact.jpx_reversible(data), False)
        out = self.apply_zone(src)
        self.assertLessEqual(os.path.getsize(out), os.path.getsize(src) * 1.1)

    def test_jpeg2000_5_3_truncado_no_crece(self):
        """5/3 truncada por tasa (Pillow con quality_layers y su irreversible=False por defecto):
        la cabecera dice reversible, pero tiene pérdida. Se recodificaba sin pérdida: ×2 en el
        corpus del lote 2d."""
        from PIL import Image, ImageDraw
        src = tmp_path(self, "in.pdf")
        # Texto en color sobre blanco (como el del corpus): truncado a tasa 20, recodificarlo sin
        # pérdida ocupa unas 2,5 veces el original. (Una foto a tasa 20, solo un 9 % más.)
        img = Image.new("RGB", (SCAN_W, SCAN_H), "white")
        d = ImageDraw.Draw(img)
        for i in range(50):
            d.text((80, 60 + 32 * i), "juicio ordinario 123/2026 procurador letrado " * 2, fill=(20, 30, 120))
        data = encode(img, "JPEG2000", quality_mode="rates", quality_layers=[20], no_jp2=True)
        make_image_pdf(src, [[(data, "/JPXDecode", "/DeviceRGB", None)]])
        self.assertIs(redact.jpx_reversible(data), True)
        out = self.apply_zone(src)
        self.assertIn("JPXDecode", first_image(out)[0])
        self.assertLessEqual(os.path.getsize(out), os.path.getsize(src) * 1.1)

    def test_transformada_jpeg2000(self):
        from PIL import Image
        img = Image.new("RGB", (64, 64), (10, 120, 200))
        self.assertIs(redact.jpx_reversible(encode(img, "JPEG2000", irreversible=False, no_jp2=True)), True)
        self.assertIs(redact.jpx_reversible(encode(img, "JPEG2000", irreversible=True, no_jp2=True)), False)
        # En un contenedor JP2, igual.
        self.assertIs(redact.jpx_reversible(encode(img, "JPEG2000", irreversible=True)), False)
        # Ilegible: en duda (se trata como sin pérdida).
        self.assertIsNone(redact.jpx_reversible(b"no es un jpeg 2000"))
        self.assertIsNone(redact.jpx_reversible(encode(img, "JPEG2000", irreversible=True, no_jp2=True)[:20]))

    def test_imagenes_no_censuradas_quedan_identicas(self):
        """Imágenes que la zona no toca (en la página censurada y en otra sin censurar): mismos
        píxeles y mismo flujo comprimido que en la entrada."""
        from PIL import Image
        rgb, gray, cmyk = scan_image("RGB"), scan_image("L"), scan_image("CMYK")
        bits = Image.new("1", (SCAN_W, SCAN_H), 1)
        bits.paste(0, (100, 100, 600, 900))
        aside = fitz.Rect(300, 500, 560, 800)  # lejos de la zona
        kinds = [
            (encode(rgb, "JPEG", quality=85), "/DCTDecode", "/DeviceRGB"),
            (encode(gray, "JPEG", quality=75), "/DCTDecode", "/DeviceGray"),
            (encode(cmyk, "JPEG", quality=90), "/DCTDecode", "/DeviceCMYK"),
            (encode(rgb, "JPEG2000", irreversible=False, no_jp2=True), "/JPXDecode", "/DeviceRGB"),
            (encode(rgb, "JPEG2000", quality_mode="rates", quality_layers=[20], no_jp2=True), "/JPXDecode", "/DeviceRGB"),
        ]
        src = tmp_path(self, "in.pdf")
        # Página 1: la censurada (imagen tocada + una de cada tipo apartada). Página 2: sin censurar.
        page1 = [(encode(rgb, "JPEG", quality=85), "/DCTDecode", "/DeviceRGB", None)]
        page1 += [(d, f, cs, aside) for d, f, cs in kinds]
        page2 = [(d, f, cs, None) for d, f, cs in kinds]
        make_image_pdf(src, [page1, page2])
        out = tmp_path(self, "out.pdf")
        report = redact.apply(src, out, zone_items(1))
        self.assertTrue(report["verified"], report)

        def images(path, skip=None):
            """[(página, flujo comprimido, píxeles)]. MuPDF renombra los recursos al censurar:
            las imágenes se emparejan por su flujo, no por su nombre."""
            doc = fitz.open(path)
            return [(p.number, doc.xref_stream_raw(im[0]), fitz.Pixmap(doc, im[0]).samples)
                    for p in doc for im in p.get_images(full=True) if (p.number, im[7]) != skip]

        untouched = images(src, skip=(0, "Im0"))  # Im0: la que toca la zona
        self.assertEqual(len(untouched), 2 * len(kinds))
        out_images = images(out)
        for n, (page, stream, samples) in enumerate(untouched):
            with self.subTest(page=page, image=n):
                same = [s for p, st, s in out_images if p == page and st == stream]
                self.assertTrue(same, "el flujo comprimido ha cambiado")
                self.assertEqual(same[0], samples)


class BusquedaConRepeticiones(unittest.TestCase):
    """Un valor que se repite en la página se busca con search_for una sola vez (devuelve todas
    sus apariciones). Antes, una vez por repetición: k llamadas y k² hallazgos que luego se
    deduplicaban (30 páginas con 80 repeticiones cada una: 17,9 s en vez de 0,3 s)."""

    def test_una_llamada_por_valor_y_cada_aparicion_con_su_contexto(self):
        import json
        from unittest import mock
        src, res, pat = tmp_path(self, "in.pdf"), tmp_path(self, "r.json"), tmp_path(self, "p.json")
        doc = fitz.open()
        page = doc.new_page()
        for i, who in enumerate(("Ana", "Marta Prueba", "Eva")):
            page.insert_text((72, 72 + 20 * i), f"DNI 12345678Z de {who}")
        page.insert_text((72, 140), "y otro 87654321X")
        doc.save(src)
        doc.close()
        with open(pat, "w", encoding="utf-8") as f:
            json.dump({"dni": r"\b\d{8}[A-Z]\b", "Personalizado": "(?i)12345678z"}, f)
        real = fitz.Page.search_for
        with mock.patch.object(fitz.Page, "search_for", autospec=True, side_effect=real) as spy:
            redact.search(src, pat, res)
        values = sorted(c.args[1] for c in spy.call_args_list)
        spy.reset_mock()  # suelta las páginas (y el PDF abierto: en Windows no se podría borrar)
        self.assertEqual(values, ["12345678Z", "87654321X"])
        with open(res, encoding="utf-8") as f:
            results = json.load(f)["results"]
        self.assertEqual([(r["text"], r["category"]) for r in results], [("12345678Z", "dni")] * 3 + [("87654321X", "dni")])
        for r, who in zip(results, ("Ana", "Marta Prueba", "Eva")):
            self.assertIn(f"12345678Z de {who}", r["context"])
        self.assertEqual(len({tuple(r["rect"]) for r in results}), 4)


class DocumentosCerrados(unittest.TestCase):
    """search() y main() cierran los documentos que abren (antes no: en Windows, el PDF de
    entrada no se podía borrar mientras quedase una referencia)."""

    def test_search_y_main_cierran_lo_que_abren(self):
        import json
        from unittest import mock
        src, res, pat = tmp_path(self, "in.pdf"), tmp_path(self, "r.json"), tmp_path(self, "p.json")
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), f"Expediente {TERM}")
        doc.save(src)
        doc.close()
        with open(pat, "w", encoding="utf-8") as f:
            json.dump({"Personalizado": TERM}, f)
        opened = []
        real_open = fitz.open

        def tracking_open(*args, **kwargs):
            d = real_open(*args, **kwargs)
            opened.append(d)
            return d

        with mock.patch.object(redact.fitz, "open", side_effect=tracking_open):
            redact.main(["redact.py", "search", src, pat, res])
        self.assertGreaterEqual(len(opened), 2)  # la comprobación de contraseña y la búsqueda
        self.assertTrue(all(d.is_closed for d in opened), [d.is_closed for d in opened])
        with open(res, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["results"]), 1)


class PatronesNoValidos(unittest.TestCase):
    """search: patterns tiene que ser un objeto y cada patrón una cadena no vacía. Si no, sale
    con EXIT_BAD_PATTERNS (server.js responde 400), nunca con el 1 de un error inesperado (500).
    Un patrón vacío casaba en cada posición del texto de cada página sin encontrar nada."""

    def run_search(self, patterns_json):
        import subprocess
        import sys
        src, res, pat = tmp_path(self, "in.pdf"), tmp_path(self, "r.json"), tmp_path(self, "p.json")
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), f"Expediente {TERM}")
        doc.save(src)
        doc.close()
        with open(pat, "w", encoding="utf-8") as f:
            f.write(patterns_json)
        here = os.path.dirname(os.path.abspath(__file__))
        return subprocess.run([sys.executable, os.path.join(here, "redact.py"), "search", src, pat, res],
                              capture_output=True, text=True)

    def test_patrones_no_validos_salen_con_su_codigo(self):
        for bad in ('[]', '["manzana1"]', '"manzana1"', 'null', '3',
                    '{"a": 5}', '{"a": null}', '{"a": ["x"]}', '{"a": {}}', '{"a": ""}',
                    '{"a": "x", "b": ""}'):
            with self.subTest(patterns=bad):
                proc = self.run_search(bad)
                self.assertEqual(proc.returncode, redact.EXIT_BAD_PATTERNS, proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

    def test_patrones_validos_siguen_funcionando(self):
        for good in ('{}', '{"Personalizado": "(?i)manzana1"}', '{"roto": "(", "ok": "manzana1"}'):
            with self.subTest(patterns=good):
                proc = self.run_search(good)
                self.assertEqual(proc.returncode, 0, proc.stderr)


# Páginas con /Rotate y CropBox desplazada a la vez (fallo encontrado en la QA de la web,
# 2026-10-16): el texto y los píxeles se borraban en su sitio, pero la caja negra salía
# desplazada por el origen de la CropBox o fuera de la página. (MediaBox, CropBox) en espacio
# de usuario; la CropBox que se sale de la MediaBox se recorta con ella, como en pdf.js.
GEOMETRIES = {
    "cropbox en el origen": ("[0 0 600 800]", "[0 0 600 800]"),
    "desplazada": ("[0 0 600 800]", "[50 100 450 700]"),
    "negativa": ("[-100 -150 500 650]", "[-80 -120 400 600]"),
    "se sale por arriba": ("[0 0 600 800]", "[300 400 900 1100]"),
    "se sale por abajo": ("[0 0 600 800]", "[-50 -60 400 600]"),
}
TERM_SIZE = 20
DATA_GRAY = 60  # el «texto» del escaneo, en la imagen; el papel es 200


def _box(s):
    return fitz.Rect([float(v) for v in s.strip("[]").split()])


def make_rotated_pdf(path, mediabox, cropbox, rotation, scan):
    """Página con el término en espacio de usuario, a 60 pt del borde izquierdo y 80 del
    superior de lo que se ve. scan: imagen gris de la MediaBox (1 px por punto) con un bloque
    oscuro bajo el término y el término como capa OCR invisible. Devuelve el rectángulo del
    término en espacio de usuario (x0, y0, x1, y1 con la y hacia arriba)."""
    media, crop = _box(mediabox), _box(cropbox)
    seen = media & crop  # lo que se ve: CropBox recortada a la MediaBox (page.view en pdf.js)
    ux, uy = seen.x0 + 60, seen.y1 - 80
    width = fitz.get_text_length(TERM, "helv", TERM_SIZE)
    term = (ux, uy - 5, ux + width, uy + 15)
    doc = fitz.open()
    page = doc.new_page()
    doc.xref_set_key(page.xref, "MediaBox", mediabox)
    doc.xref_set_key(page.xref, "CropBox", cropbox)
    font = doc.get_new_xref()
    doc.update_object(font, "<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>")
    resources = f"/Font<</F1 {font} 0 R>>"
    content = f"BT {3 if scan else 0} Tr /F1 {TERM_SIZE} Tf {ux} {uy} Td ({TERM}) Tj ET"
    if scan:
        from PIL import Image, ImageDraw
        w, h = int(media.width), int(media.height)
        img = Image.new("L", (w, h), 200)
        # Píxel (columna, fila) = usuario (media.x0 + columna, media.y1 - fila).
        ImageDraw.Draw(img).rectangle([term[0] - media.x0, media.y1 - term[3],
                                       term[2] - media.x0, media.y1 - term[1]], fill=DATA_GRAY)
        im = doc.get_new_xref()
        doc.update_object(im, f"<</Type/XObject/Subtype/Image/Width {w}/Height {h}"
                              "/ColorSpace/DeviceGray/BitsPerComponent 8>>")
        doc.update_stream(im, img.tobytes())
        resources += f"/XObject<</Im0 {im} 0 R>>"
        content = f"q {w} 0 0 {h} {media.x0} {media.y0} cm /Im0 Do Q " + content
    c = doc.get_new_xref()
    doc.update_object(c, "<<>>")
    doc.update_stream(c, content.encode())
    doc.xref_set_key(page.xref, "Contents", f"{c} 0 R")
    doc.xref_set_key(page.xref, "Resources", f"<<{resources}>>")
    doc.xref_set_key(page.xref, "Rotate", str(rotation))
    doc.save(path)
    doc.close()
    return term, seen


def manual_items(term, seen, margin=2):
    """Censura manual como la manda la web (viewportRectToBackend de pdf-coords.ts): página sin
    girar, origen arriba a la izquierda de lo que se ve y la y hacia abajo."""
    x0, y0, x1, y1 = term
    rect = [x0 - margin - seen.x0, seen.y1 - y1 - margin, x1 + margin - seen.x0, seen.y1 - y0 + margin]
    return [{"id": "m0", "page": 0, "text": "Zona manual (Pág. 1)", "category": redact.MANUAL_CATEGORY,
             "rect": rect}]


def rendered(path):
    """La página como se ve (con /Rotate y la CropBox), en gris a 72 ppp."""
    from PIL import Image
    pix = fitz.open(path)[0].get_pixmap(dpi=72, colorspace=fitz.csGRAY)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def dark_bbox(img, below):
    return img.point(lambda v: 255 if v < below else 0).getbbox()


class CensuraConRotateYCropBox(unittest.TestCase):
    """La caja negra tapa el término donde se ve, y en un escaneo los píxeles de la imagen bajo
    el término quedan destruidos, en las 4 rotaciones con la CropBox desplazada."""

    def check(self, scan, manual):
        for name, (mediabox, cropbox) in GEOMETRIES.items():
            for rotation in (0, 90, 180, 270):
                with self.subTest(geometria=name, rotate=rotation):
                    src, out = tmp_path(self, "in.pdf"), tmp_path(self, "out.pdf")
                    term, seen = make_rotated_pdf(src, mediabox, cropbox, rotation, scan)
                    # Dónde se ve el término (o el bloque oscuro del escaneo) antes de censurar.
                    where = dark_bbox(rendered(src), 128)
                    self.assertIsNotNone(where)
                    items = manual_items(term, seen) if manual else search_items(src, TERM)
                    self.assertEqual(len(items), 1)
                    report = redact.apply(src, out, items)
                    self.assertTrue(report["verified"], report)
                    self.assertEqual(report["failed"], [])
                    after = rendered(out)
                    # a) Todo lo que se veía del término queda negro...
                    covered = after.crop(where).point(lambda v: 255 if v < 40 else 0)
                    self.assertGreater(sum(covered.histogram()[255:]) / (covered.width * covered.height), 0.98,
                                       f"el término se ve en {where}; negro en {dark_bbox(after, 40)}")
                    # ...y el negro está en su sitio: no se sale más de 12 px (la zona de la
                    # búsqueda es la línea entera, algo más alta que las letras).
                    black = dark_bbox(after, 40)
                    self.assertTrue((fitz.Rect(where) + (-12, -12, 12, 12)).contains(fitz.Rect(black)), (where, black))
                    if scan:
                        # b) Ningún píxel del dato queda en la imagen de la salida.
                        doc = fitz.open(out)
                        images = doc[0].get_images()
                        self.assertEqual(len(images), 1)
                        samples = fitz.Pixmap(doc, images[0][0]).samples
                        doc.close()
                        self.assertEqual(samples.count(DATA_GRAY), 0)

    def test_texto_con_la_busqueda(self):
        self.check(scan=False, manual=False)

    def test_texto_con_censura_manual(self):
        self.check(scan=False, manual=True)

    def test_escaneo_con_la_busqueda(self):
        self.check(scan=True, manual=False)

    def test_escaneo_con_censura_manual(self):
        self.check(scan=True, manual=True)

    def test_relleno_repintado_en_su_sitio(self):
        """Una celda de color bajo el término: REMOVE_IF_TOUCHED la quita y restore_fills la
        repinta (también con PyMuPDF): debe volver a su sitio."""
        for name, (mediabox, cropbox) in GEOMETRIES.items():
            for rotation in (0, 90, 180, 270):
                with self.subTest(geometria=name, rotate=rotation):
                    src, out = tmp_path(self, "in.pdf"), tmp_path(self, "out.pdf")
                    term, seen = make_rotated_pdf(src, mediabox, cropbox, rotation, scan=False)
                    doc = fitz.open(src)
                    page = doc[0]
                    x0, y0, x1, y1 = term
                    cell = f"q 0.6 0.8 1 rg {x0 - 30} {y0 - 20} {x1 - x0 + 60} {y1 - y0 + 40} re f Q\n".encode()
                    c = page.get_contents()[0]
                    doc.update_stream(c, cell + doc.xref_stream(c))
                    doc.saveIncr()
                    doc.close()
                    before = rendered(src)
                    report = redact.apply(src, out, search_items(src, TERM))
                    self.assertTrue(report["verified"], report)
                    after = rendered(out)
                    black = fitz.Rect(dark_bbox(after, 40))
                    # Fuera del negro (y de su borde), la página se ve igual que antes.
                    from PIL import ImageChops
                    diff = ImageChops.difference(before, after)
                    diff.paste(0, tuple(int(v) for v in black + (-2, -2, 2, 2)))
                    self.assertIsNone(diff.point(lambda v: 255 if v > 30 else 0).getbbox(), (name, rotation))


class VerificacionPorRender(unittest.TestCase):
    """Una zona que toca una imagen tiene que verse negra en la salida: si no (como cuando el
    negro salía desplazado con /Rotate y la CropBox desplazada), 422 y nada descargado. La
    verificación de texto no lo ve: los píxeles del escaneo no se pueden buscar."""

    def test_zona_sobre_imagen_sin_negro_no_se_entrega(self):
        from unittest import mock
        for name, rotation in (("cropbox en el origen", 0), ("desplazada", 90), ("se sale por arriba", 270)):
            for manual in (False, True):
                with self.subTest(geometria=name, rotate=rotation, manual=manual):
                    src, out = tmp_path(self, "in.pdf"), tmp_path(self, "out.pdf")
                    term, seen = make_rotated_pdf(src, *GEOMETRIES[name], rotation, scan=True)
                    items = manual_items(term, seen) if manual else search_items(src, TERM)
                    with mock.patch.object(redact, "paint_zones"):  # el negro no se pinta
                        report = redact.apply(src, out, items)
                    self.assertFalse(report["verified"], report)
                    self.assertEqual(report["leaks"], ["render_not_black"])
                    self.assertFalse(os.path.exists(out))

    def test_render_es_una_condicion_aparte_de_page_text(self):
        """render_not_black se suma a page_text, no lo sustituye: una zona sobre una imagen con
        el texto visible y sin negro da las dos fugas; con el texto sobre blanco, solo page_text."""
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("L", (100, 100), 200).save(buf, "PNG")
        zone = fitz.Rect(60, 60, 200, 90)
        for with_image, expected in ((True, ["page_text", "render_not_black"]), (False, ["page_text"])):
            with self.subTest(imagen=with_image):
                path = tmp_path(self, "out.pdf")
                doc = fitz.open()
                page = doc.new_page()
                if with_image:
                    page.insert_image(fitz.Rect(0, 0, 300, 300), stream=buf.getvalue())
                page.insert_text((70, 80), TERM, fontsize=14)
                doc.save(path)
                doc.close()
                self.assertEqual(redact.verify_redaction(path, {0: [zone]}, {TERM}), expected)

    def test_zona_pintada_pasa(self):
        src, out = tmp_path(self, "in.pdf"), tmp_path(self, "out.pdf")
        make_rotated_pdf(src, *GEOMETRIES["desplazada"], 90, scan=True)
        report = redact.apply(src, out, search_items(src, TERM))
        self.assertTrue(report["verified"], report)

    def test_zona_demasiado_pequena_no_se_comprueba(self):
        """Menos de unos 4 pt: a 36 ppp no le queda ningún píxel entero tras reducirla 1 px."""
        doc = fitz.open()
        page = doc.new_page()
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("L", (100, 100), 255).save(buf, "PNG")
        page.insert_image(fitz.Rect(0, 0, 300, 300), stream=buf.getvalue())
        self.assertTrue(redact.zones_not_black(page, [fitz.Rect(50, 50, 100, 100)]))
        self.assertFalse(redact.zones_not_black(page, [fitz.Rect(50, 50, 53, 53)]))
        self.assertFalse(redact.zones_not_black(page, [fitz.Rect(400, 400, 500, 500)]))  # sin imagen
        doc.close()


if __name__ == "__main__":
    unittest.main()
