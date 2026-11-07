# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Tests de pdf_check.py (verify, info, firma y PDF/A) y de la detección de PDF con contraseña
y de firma en lossless_images.py y redact.py, con PDFs sintéticos (PyMuPDF real):
python -m unittest pdf_check_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import pymupdf

import lossless_images
import pdf_check

HERE = os.path.dirname(os.path.abspath(__file__))


def make_pdf(path, pages=2, user_pw=None, owner_pw=None, blank=()):
    """PDF con texto («manzana1») en cada página salvo en las de `blank`, cifrado AES-256 si
    se da alguna contraseña (solo owner_pw = solo contraseña de propietario)."""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=300, height=200)
        if i not in blank:
            page.insert_text((20, 100), f"manzana1 página {i + 1}", fontsize=14)
    kwargs = {}
    if user_pw is not None or owner_pw is not None:
        kwargs = {
            "encryption": pymupdf.PDF_ENCRYPT_AES_256,
            "user_pw": user_pw or "",
            "owner_pw": owner_pw or "",
            "permissions": int(pymupdf.PDF_PERM_PRINT),
        }
    doc.save(path, **kwargs)
    return path


class PdfCheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = self.dir.name
        self.plain = make_pdf(os.path.join(d, "plain.pdf"))
        self.open_pw = make_pdf(os.path.join(d, "a.pdf"), user_pw="qa1234", owner_pw="qa1234-owner")
        self.owner_only = make_pdf(os.path.join(d, "b.pdf"), owner_pw="qa1234-owner")

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def test_needs_password_solo_con_contrasena_de_apertura(self):
        self.assertTrue(pdf_check.needs_password(pymupdf.open(self.open_pw)))
        self.assertFalse(pdf_check.needs_password(pymupdf.open(self.owner_only)))
        self.assertFalse(pdf_check.needs_password(pymupdf.open(self.plain)))

    def test_verify_acepta_una_salida_completa(self):
        self.assertEqual(pdf_check.main(["", "verify", self.plain, self.plain]), 0)
        # Solo propietario: también se puede verificar (la salida de gs ya va sin cifrar).
        self.assertEqual(pdf_check.main(["", "verify", self.owner_only, self.plain]), 0)

    def test_verify_rechaza_menos_paginas(self):
        one = make_pdf(self.path("una.pdf"), pages=1)
        self.assertEqual(pdf_check.main(["", "verify", self.plain, one]), pdf_check.EXIT_INVALID)

    def test_verify_rechaza_una_pagina_que_ha_quedado_en_blanco(self):
        blank = make_pdf(self.path("blanco.pdf"), blank=(1,))
        self.assertEqual(pdf_check.main(["", "verify", self.plain, blank]), pdf_check.EXIT_INVALID)

    def test_verify_acepta_paginas_que_ya_estaban_en_blanco(self):
        src = make_pdf(self.path("src.pdf"), blank=(0,))
        self.assertEqual(pdf_check.main(["", "verify", src, src]), 0)

    def test_verify_rechaza_una_salida_que_no_es_pdf(self):
        bad = self.path("roto.pdf")
        with open(bad, "wb") as f:
            f.write(b"esto no es un PDF")
        self.assertEqual(pdf_check.main(["", "verify", self.plain, bad]), pdf_check.EXIT_INVALID)

    def test_verify_con_entrada_con_contrasena(self):
        # Lo que hacía Ghostscript: una página en blanco a partir de un PDF que no pudo abrir.
        blank = make_pdf(self.path("gs.pdf"), pages=1, blank=(0,))
        self.assertEqual(
            pdf_check.main(["", "verify", self.open_pw, blank]), pdf_check.EXIT_ENCRYPTED
        )

    def test_lossless_images_no_procesa_un_pdf_con_contrasena(self):
        out = self.path("protegido.pdf")
        self.assertEqual(
            lossless_images.main(["", "protect", self.open_pw, out]), pdf_check.EXIT_ENCRYPTED
        )
        self.assertFalse(os.path.exists(out))
        # Solo propietario: sigue como cualquier otro (sin imágenes que proteger, 0).
        self.assertEqual(lossless_images.main(["", "protect", self.owner_only, out]), 0)

    def test_redact_no_procesa_un_pdf_con_contrasena(self):
        patterns = self.path("patrones.json")
        with open(patterns, "w", encoding="utf-8") as f:
            json.dump({"custom_0": "manzana1"}, f)
        for pdf, code in ((self.open_pw, pdf_check.EXIT_ENCRYPTED), (self.owner_only, 0)):
            results = self.path("resultados.json")
            r = subprocess.run(
                [sys.executable, os.path.join(HERE, "redact.py"), "search", pdf, patterns, results],
                cwd=HERE,
                capture_output=True,
                text=True,
            )
            self.assertEqual(r.returncode, code, r.stderr)
            if code == 0:
                with open(results, encoding="utf-8") as f:
                    self.assertTrue(json.load(f)["results"])


def signed_pdf(path, kind="field", sign=True):
    """Firma con la estructura de PAdES (el contenido CMS es de relleno: solo se detecta, no se
    valida). kind: "field" (campo /FT /Sig con /V indirecto), "direct" (con el /V directo en el
    campo), "timestamp" (sello de tiempo de documento, /DocTimeStamp) o "empty" (campo de firma
    sin firmar)."""
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((20, 100), "Resolución firmada", fontsize=14)
    sig = (f"<< /Type /{'DocTimeStamp' if kind == 'timestamp' else 'Sig'} /Filter /Adobe.PPKLite "
           f"/SubFilter /{'ETSI.RFC3161' if kind == 'timestamp' else 'ETSI.CAdES.detached'} "
           f"/ByteRange [0 1000 9000 500] /Contents <{'00' * 4000}> /M (D:20261024120000+02'00') >>")
    field = doc.get_new_xref()
    value = ""
    if kind in ("field", "timestamp"):
        sx = doc.get_new_xref()
        doc.update_object(sx, sig)
        value = f"/V {sx} 0 R"
    elif kind == "direct":
        value = f"/V {sig}"
    doc.update_object(field, f"<< /FT /Sig /T (Firma1) /Type /Annot /Subtype /Widget /Rect [0 0 0 0] /F 132 "
                             f"/P {page.xref} 0 R {value} >>")
    doc.xref_set_key(page.xref, "Annots", f"[{field} 0 R]")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", f"<< /Fields [{field} 0 R] /SigFlags 3 >>")
    doc.save(path)
    return path


class FirmaYPdfA(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def test_firma_detectada(self):
        for kind in ("field", "direct", "timestamp"):
            with self.subTest(kind=kind):
                self.assertTrue(pdf_check.signed(pymupdf.open(signed_pdf(self.path(f"{kind}.pdf"), kind))))

    def test_sin_firma(self):
        self.assertFalse(pdf_check.signed(pymupdf.open(signed_pdf(self.path("empty.pdf"), "empty"))))
        self.assertFalse(pdf_check.signed(pymupdf.open(make_pdf(self.path("plain.pdf")))))

    def test_info(self):
        report = self.path("info.json")
        cases = ((signed_pdf(self.path("s.pdf")), {"signed": True, "pdfa": None}),
                 (make_pdf(self.path("p.pdf")), {"signed": False, "pdfa": None}))
        for pdf, expected in cases:
            self.assertEqual(pdf_check.main(["", "info", pdf, report]), 0)
            with open(report, encoding="utf-8") as f:
                self.assertEqual(json.load(f), expected)
        doc = pymupdf.open(make_pdf(self.path("a.pdf")))
        doc.set_xml_metadata('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                             '<rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/" '
                             'pdfaid:part="2" pdfaid:conformance="b"/></rdf:RDF></x:xmpmeta>')
        doc.save(self.path("a2.pdf"))
        self.assertEqual(pdf_check.main(["", "info", self.path("a2.pdf"), report]), 0)
        with open(report, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"signed": False, "pdfa": "2B"})

    def test_info_con_contrasena_de_apertura(self):
        open_pw = make_pdf(self.path("a.pdf"), user_pw="qa1234", owner_pw="qa1234-owner")
        report = self.path("info.json")
        self.assertEqual(pdf_check.main(["", "info", open_pw, report]), pdf_check.EXIT_ENCRYPTED)
        self.assertFalse(os.path.exists(report))
        self.assertEqual(pdf_check.main(["", "info", open_pw]), 2)

    def test_redact_informa_de_la_firma_en_search_y_apply(self):
        patterns, items = self.path("patrones.json"), self.path("items.json")
        with open(patterns, "w", encoding="utf-8") as f:
            json.dump({"custom_0": "firmada"}, f)
        for pdf, signed in ((signed_pdf(self.path("s.pdf")), True), (make_pdf(self.path("p.pdf")), False)):
            with self.subTest(signed=signed):
                results, report = self.path("r.json"), self.path("informe.json")
                run = lambda *a: subprocess.run([sys.executable, os.path.join(HERE, "redact.py"), *a],
                                                cwd=HERE, capture_output=True, text=True)
                r = run("search", pdf, patterns, results)
                self.assertEqual(r.returncode, 0, r.stderr)
                with open(results, encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data.get("signed"), True if signed else None)
                with open(items, "w", encoding="utf-8") as f:
                    json.dump([{"page": 0, "rect": [20, 85, 120, 105], "category": "Censura Manual", "text": "x"}], f)
                r = run("apply", pdf, self.path("out.pdf"), items, report)
                self.assertEqual(r.returncode, 0, r.stderr)
                with open(report, encoding="utf-8") as f:
                    rep = json.load(f)
                self.assertTrue(rep["verified"])
                self.assertEqual(rep.get("signed"), True if signed else None)


if __name__ == "__main__":
    unittest.main()
