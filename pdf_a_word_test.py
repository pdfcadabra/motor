# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Tests de pdf_a_word.py (PDF a Word).

- BancoDelEstudio: los 26 escritos sintéticos del banco (e01-e16, forenses f01-f06 y que imitan a
  Google Docs g01-g04, en pruebas-pdf/pdf-a-word, ver su README) dan el mismo .docx, parte a parte
  salvo docProps (fechas), que pdf2docx + este posprocesado con la rueda de OpenCV
  (esperado.json): la misma calidad, medida con ellos (97,3, 96,7 y 98,6 de media sobre 100).
  esperado.json se hizo sin listas de Word: los que no tienen listas dan lo mismo con la opción
  por defecto, y g03 y g04, con --sin-listas-word; por defecto, sus listas son de Word
  (ListasDeWord). Sin esa carpeta (motor solo), se saltan.
- Rechazos, OCR, memoria y datos: PDF sintéticos hechos aquí, siempre.

    python3 -m unittest pdf_a_word_test.py
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

import pymupdf

import pdf_a_word

AQUI = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(AQUI, "pdf_a_word.py")
BANCO = os.path.join(AQUI, "..", "pruebas-pdf", "pdf-a-word")
# Los del banco con listas: esperado.json es sin listas de Word.
CON_LISTAS = ("g03-gdocs-listas", "g04-gdocs-vinetas-unidas")
MARCA = "Zarzaparrilla Ficticia 7Q"  # texto que nunca puede salir por stderr ni en el informe
PARRAFO = ("En su virtud, la parte actora interesa que se tenga por presentado este escrito, con "
           "sus copias, y se acuerde de conformidad con lo solicitado en el cuerpo del mismo. ")


def partes(docx):
    with zipfile.ZipFile(docx) as z:
        return {n: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist() if not n.startswith("docProps/")}


def texto_docx(docx):
    with zipfile.ZipFile(docx) as z:
        return z.read("word/document.xml").decode("utf-8")


def escrito(path, paginas=2, **save):
    """Escrito de texto sencillo: título y párrafos justificados en cada página, con la marca."""
    doc = pymupdf.open()
    for n in range(paginas):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 90), f"AL JUZGADO DE PRIMERA INSTANCIA - {MARCA}", fontsize=12)
        page.insert_textbox(pymupdf.Rect(72, 120, 523, 700), PARRAFO * 6, fontsize=12, align=3)
        page.insert_text((280, 800), f"Página {n + 1} de {paginas}", fontsize=9)
    doc.save(path, **save)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def ruta(self, nombre):
        return os.path.join(self.dir, nombre)

    def convertir(self, pdf, *opciones, env=None):
        """(código de salida, informe o None, stdout + stderr, archivos que quedan en la carpeta)."""
        docx, informe = self.ruta("salida.docx"), self.ruta("informe.json")
        entorno = {**os.environ, **(env or {})}
        p = subprocess.run([sys.executable, SCRIPT, pdf, docx, informe, *opciones], cwd=AQUI,
                           capture_output=True, text=True, env=entorno, timeout=600)
        rep = None
        if os.path.exists(informe):
            with open(informe, encoding="utf-8") as f:
                rep = json.load(f)
        quedan = sorted(f for f in os.listdir(self.dir) if f != os.path.basename(pdf))
        return p.returncode, rep, p.stdout + p.stderr, quedan


@unittest.skipUnless(os.path.isdir(BANCO), "sin pruebas-pdf/pdf-a-word (motor solo)")
class BancoDelEstudio(Base):
    def test_mismo_docx_que_el_estudio(self):
        with open(os.path.join(BANCO, "esperado.json"), encoding="utf-8") as f:
            esperado = json.load(f)
        self.assertEqual(len(esperado), 26)
        for nombre, partes_estudio in sorted(esperado.items()):
            with self.subTest(nombre):
                opciones = ["--sin-listas-word"] if nombre in CON_LISTAS else []
                codigo, rep, salida, quedan = self.convertir(os.path.join(BANCO, nombre + ".pdf"), *opciones)
                self.assertEqual(codigo, 0, salida)
                self.assertNotIn("refused", rep)
                self.assertEqual(salida, "")
                self.assertEqual(quedan, ["informe.json", "salida.docx"])
                self.assertEqual(partes(self.ruta("salida.docx")), partes_estudio)
                # Lo que el estudio da por bueno en todos: cabecera o pie reales (los de Google Docs
                # no tienen) y notas al pie reales.
                if not nombre.startswith("g"):
                    self.assertTrue(rep["header"] or rep["first_page_header"] or rep["footer"])
                self.assertEqual(rep["notes"], rep["notes_pdf"])
                self.assertEqual((rep["lists"], rep["list_items"], rep["lists_not_converted"]), (0, 0, 0))
        self.assertEqual(sorted(n for n in esperado if n.startswith("g")), ["g01-gdocs-tabuladores",
                         "g02-gdocs-fuente-vacia", *CON_LISTAS])


@unittest.skipUnless(os.path.isdir(BANCO), "sin pruebas-pdf/pdf-a-word (motor solo)")
class ListasDeWord(Base):
    """Por defecto (decisión del 2026-11-07), las viñetas y los números de lista pasan a listas
    de Word; con --sin-listas-word, se quedan escritos con un tabulador."""

    def numpr(self):
        with zipfile.ZipFile(self.ruta("salida.docx")) as z:
            return z.read("word/document.xml").count(b"<w:numPr>"), "word/numbering.xml" in z.namelist()

    def test_por_defecto_activadas(self):
        for nombre, listas in (("g03-gdocs-listas", 3), ("g04-gdocs-vinetas-unidas", 3)):
            with self.subTest(nombre):
                codigo, rep, salida, quedan = self.convertir(os.path.join(BANCO, nombre + ".pdf"))
                self.assertEqual((codigo, salida), (0, ""))
                self.assertEqual(quedan, ["informe.json", "salida.docx"])
                self.assertEqual((rep["lists"], rep["list_items"], rep["lists_not_converted"]), (listas, 15, 0))
                self.assertEqual(self.numpr(), (15, True))

    def test_desactivadas(self):
        for nombre in ("g03-gdocs-listas", "g04-gdocs-vinetas-unidas"):
            with self.subTest(nombre):
                codigo, rep, _, _ = self.convertir(os.path.join(BANCO, nombre + ".pdf"), "--sin-listas-word")
                self.assertEqual(codigo, 0)
                self.assertEqual((rep["lists"], rep["list_items"], rep["lists_not_converted"]), (0, 0, 0))
                self.assertEqual(self.numpr()[0], 0)


class Conversion(Base):
    def test_escrito_sencillo(self):
        pdf = self.ruta("e.pdf")
        escrito(pdf, paginas=3)
        codigo, rep, salida, quedan = self.convertir(pdf)
        self.assertEqual((codigo, salida), (0, ""))
        self.assertEqual(quedan, ["informe.json", "salida.docx"])
        self.assertEqual(rep["pages"], 3)
        self.assertFalse(rep["ocr"])
        self.assertTrue(rep["footer"])  # «Página X de Y» en las tres
        self.assertGreaterEqual(rep["page_fields"], 1)
        self.assertIn("la parte actora interesa", texto_docx(self.ruta("salida.docx")))

    def test_argumentos(self):
        pdf = self.ruta("e.pdf")
        escrito(pdf, paginas=1)
        for opciones in (["--listas-word"], ["--max-paginas=0"], ["--otra"]):
            with self.subTest(opciones=opciones):
                codigo, rep, salida, _ = self.convertir(pdf, *opciones)
                self.assertEqual((codigo, rep), (2, None))
                self.assertIn("Uso:", salida)

    def test_el_informe_no_lleva_texto_del_documento(self):
        pdf = self.ruta("e.pdf")
        escrito(pdf)
        _, rep, salida, _ = self.convertir(pdf)
        self.assertNotIn("Zarzaparrilla", json.dumps(rep, ensure_ascii=False))
        self.assertNotIn("Zarzaparrilla", salida)

    def test_contrasena_de_propietario_se_convierte(self):
        pdf = self.ruta("b.pdf")
        escrito(pdf, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="propietario", user_pw="")
        codigo, rep, _, _ = self.convertir(pdf)
        self.assertEqual(codigo, 0)
        self.assertNotIn("refused", rep)


class Rechazos(Base):
    def rechazo(self, pdf, *opciones, env=None):
        codigo, rep, salida, quedan = self.convertir(pdf, *opciones, env=env)
        self.assertEqual(codigo, 0, salida)
        self.assertEqual(quedan, ["informe.json"])  # ni salida ni temporales
        self.assertNotIn("Zarzaparrilla", salida)
        return rep

    def test_contrasena_de_apertura(self):
        pdf = self.ruta("a.pdf")
        escrito(pdf, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="propietario", user_pw="usuario")
        codigo, rep, salida, quedan = self.convertir(pdf)
        self.assertEqual(codigo, pdf_a_word.EXIT_ENCRYPTED)
        self.assertIsNone(rep)
        self.assertEqual(quedan, [])

    def test_demasiadas_paginas(self):
        pdf = self.ruta("p.pdf")
        escrito(pdf, paginas=3)
        rep = self.rechazo(pdf, "--max-paginas=2")
        self.assertEqual(rep, {"refused": "PDF_TOO_LARGE", "reason": "pages", "pages": 3, "max_pages": 2})

    def test_escaneo_sin_texto(self):
        doc = pymupdf.open()
        page = doc.new_page()
        pix = pymupdf.Pixmap(pymupdf.csGRAY, 400, 400, bytes(range(256)) * 625, False)
        page.insert_image(page.rect, pixmap=pix)
        pdf = self.ruta("s.pdf")
        doc.save(pdf)
        self.assertEqual(self.rechazo(pdf), {"refused": "PDF_NO_TEXT", "reason": "scanned"})

    def test_formulario(self):
        pdf = self.ruta("f.pdf")
        escrito(pdf, paginas=1)
        doc = pymupdf.open(pdf)
        w = pymupdf.Widget()
        w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        w.field_name = "nombre"
        w.rect = pymupdf.Rect(72, 720, 300, 740)
        doc[0].add_widget(w)
        doc.saveIncr()
        self.assertEqual(self.rechazo(pdf), {"refused": "PDF_UNSUPPORTED", "reason": "form"})

    def test_pagina_girada(self):
        pdf = self.ruta("g.pdf")
        escrito(pdf)
        doc = pymupdf.open(pdf)
        doc[1].set_rotation(90)
        doc.saveIncr()
        self.assertEqual(self.rechazo(pdf), {"refused": "PDF_UNSUPPORTED", "reason": "rotated"})

    def test_texto_girado(self):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 90), "Anexo", fontsize=12)
        for i in range(6):
            page.insert_text((100 + 40 * i, 700), PARRAFO[:90], fontsize=10, rotate=90)
        pdf = self.ruta("t.pdf")
        doc.save(pdf)
        self.assertEqual(self.rechazo(pdf), {"refused": "PDF_UNSUPPORTED", "reason": "rotated"})

    def test_dibujo_vectorial_que_necesita_opencv(self):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Texto del escrito con un dibujo debajo.", fontsize=12)
        for i in range(30):
            page.draw_circle((200 + 3 * i, 300 + 2 * i), 40 + i, color=(0, 0, 0))
        pdf = self.ruta("d.pdf")
        doc.save(pdf)
        self.assertEqual(self.rechazo(pdf), {"refused": "PDF_UNSUPPORTED", "reason": "images"})

    def test_dos_columnas(self):
        doc = pymupdf.open()
        for _ in range(2):
            page = doc.new_page()
            texto = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor. " * 12
            page.insert_textbox(pymupdf.Rect(50, 60, 290, 780), texto, fontsize=10)
            page.insert_textbox(pymupdf.Rect(310, 60, 550, 780), texto, fontsize=10)
        pdf = self.ruta("c.pdf")
        doc.save(pdf)
        self.assertEqual(self.rechazo(pdf), {"refused": "PDF_UNSUPPORTED", "reason": "layout"})

    def test_memoria_agotada(self):
        # Con tantos trabajos «a la vez», cada uno tiene unos 120 MB de memoria virtual: ni carga pdf2docx.
        memoria = pdf_a_word.container_memory()
        if memoria is None:
            self.skipTest("sin la memoria de la máquina")
        trabajos = int(pdf_a_word.MEMORY_SHARE * memoria / (120 * 2 ** 20))
        pdf = self.ruta("m.pdf")
        escrito(pdf)
        rep = self.rechazo(pdf, env={pdf_a_word.MAX_JOBS_ENV: str(trabajos)})
        self.assertEqual((rep["refused"], rep["reason"]), ("PDF_TOO_LARGE", "memory"))
        self.assertEqual(rep["max_memory"], int(pdf_a_word.MEMORY_SHARE * memoria / trabajos))
        self.assertLess(rep["max_memory"], 130 * 2 ** 20)

    def test_pdf_roto_error_sin_datos(self):
        pdf = self.ruta("roto.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.7\n" + MARCA.encode() + b"\n%%EOF")
        codigo, rep, salida, quedan = self.convertir(pdf)
        self.assertEqual(codigo, 1)
        self.assertIsNone(rep)
        self.assertEqual(quedan, [])
        self.assertNotIn("Zarzaparrilla", salida)
        self.assertNotIn("Traceback", salida)


def pagina_escaneada(doc, page, *, visible_extra=None, dpi=100):
    """Añade a `doc` la página `page` «escaneada»: su imagen en gris y, encima, su texto invisible
    (modo 3), como lo deja un OCR. visible_extra: (punto, texto) en modo normal, como el sello de
    una sede electrónica sobre el escaneo."""
    nueva = doc.new_page(width=page.rect.width, height=page.rect.height)
    nueva.insert_image(nueva.rect, pixmap=page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY))
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for s in line["spans"]:
                nueva.insert_text(s["origin"], s["text"], fontsize=s["size"], render_mode=3)
    if visible_extra:
        nueva.insert_text(visible_extra[0], visible_extra[1], fontsize=8)
    return nueva


OCR_FRASE = "Ocrfrase Quimerica Septentrional"


def mixto(path, origen, *, sello=False):
    """PDF de 3 páginas: la 1 y la 3 de texto (las de `origen`) y la 2 escaneada con OCR, con una
    frase que solo está en su capa invisible."""
    src = pymupdf.open(origen)
    pag = pymupdf.open()
    p = pag.new_page(width=595, height=842)
    p.insert_textbox(pymupdf.Rect(72, 90, 523, 760), (OCR_FRASE + ". " + PARRAFO) * 5, fontsize=12, align=3)
    doc = pymupdf.open()
    doc.insert_pdf(src, from_page=0, to_page=0)
    pagina_escaneada(doc, pag[0], visible_extra=((72, 820), "Documento firmado electrónicamente") if sello else None)
    doc.insert_pdf(src, from_page=1, to_page=1)
    doc.save(path)


class PdfMixto(Base):
    """Páginas de texto y páginas escaneadas con OCR en el mismo PDF: el texto del OCR no puede
    perderse en silencio. Antes, con alguna página de texto visible, el de las escaneadas se perdía
    (pdf2docx no lee el texto invisible) y el .docx salía sin X-Word-OCR."""

    def origen(self):
        path = os.path.join(self.dir, "..", os.path.basename(self.dir) + "-origen.pdf")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        escrito(path, paginas=2)
        return path

    def test_convierte_el_texto_de_las_escaneadas(self):
        for sello in (False, True):
            with self.subTest(sello=sello):
                pdf = self.ruta("mixto.pdf")
                mixto(pdf, self.origen(), sello=sello)
                codigo, rep, salida, quedan = self.convertir(pdf)
                self.assertEqual((codigo, salida), (0, ""))
                self.assertEqual(quedan, ["informe.json", "salida.docx"])
                self.assertNotIn("refused", rep)
                self.assertTrue(rep["ocr"])
                self.assertEqual(rep["ocr_pages"], 1)
                self.assertGreater(rep["ocr_lines"], 10)
                self.assertEqual((rep["ocr_words_missing"], rep["ocr_lines_missing"]), (0, 0))
                xml = texto_docx(self.ruta("salida.docx"))
                self.assertIn(OCR_FRASE, xml)
                self.assertIn("la parte actora interesa", xml)
                if sello:
                    self.assertIn("firmado electrónicamente", xml)
                os.unlink(self.ruta("salida.docx"))
                os.unlink(self.ruta("informe.json"))

    def test_texto_oculto_sin_escaneo_se_rechaza(self):
        # Texto invisible en una página de texto normal (sin escaneo debajo): no se sabe qué es ni si
        # pdf2docx lo daría entero junto al visible. 422 propio en vez de perderlo.
        pdf = self.ruta("oculto.pdf")
        escrito(pdf, paginas=2)
        doc = pymupdf.open(pdf)
        doc[1].insert_text((72, 760), OCR_FRASE, fontsize=11, render_mode=3)
        doc.saveIncr()
        codigo, rep, salida, quedan = self.convertir(pdf)
        self.assertEqual(codigo, 0, salida)
        self.assertEqual(quedan, ["informe.json"])
        self.assertEqual(rep, {"refused": "PDF_MIXED_SCAN", "reason": "hidden_text"})

    @unittest.skipUnless(os.path.isdir(BANCO), "sin pruebas-pdf/pdf-a-word (motor solo)")
    def test_fixture_mixto(self):
        # x01-mixto-ocr: e03 con las páginas 4 a 6 escaneadas con OCR y un sello visible en la 5.
        codigo, rep, salida, _ = self.convertir(os.path.join(BANCO, "x01-mixto-ocr.pdf"))
        self.assertEqual((codigo, salida), (0, ""))
        self.assertEqual((rep["pages"], rep["ocr"], rep["ocr_pages"]), (10, True, 3))
        self.assertEqual((rep["ocr_words_missing"], rep["ocr_lines_missing"]), (0, 0))
        # Todo el texto de las tres páginas escaneadas (el del e03 original), y el sello.
        e03 = os.path.join(BANCO, "e03-apelacion-10p.pdf")
        self.assertEqual(pdf_a_word.missing_text(e03, [3, 4, 5], self.ruta("salida.docx"))[1::2], (0, 0))
        self.assertIn("Código seguro de verificación", texto_docx(self.ruta("salida.docx")))

    def test_sin_ocr_no_cambia(self):
        # Un PDF sin texto invisible no pasa por la copia del OCR.
        pdf = self.ruta("e.pdf")
        escrito(pdf, paginas=2)
        _, rep, _, _ = self.convertir(pdf)
        self.assertEqual((rep["ocr"], rep["ocr_pages"]), (False, 0))


class TextoCompleto(Base):
    """missing_text: el texto de las páginas con OCR tiene que estar entero en el .docx."""

    def docx(self, *parrafos):
        from docx import Document
        d = Document()
        for t in parrafos:
            d.add_paragraph(t)
        path = self.ruta("t.docx")
        d.save(path)
        return path

    def pdf(self, *lineas):
        doc = pymupdf.open()
        page = doc.new_page()
        for i, t in enumerate(lineas):
            page.insert_text((72, 100 + 20 * i), t, fontsize=11)
        path = self.ruta("t.pdf")
        doc.save(path)
        return path

    def test_todo_esta(self):
        pdf = self.pdf("Primera línea del escrito,", "y la segunda, partida.", "Página 4 de 10")
        # Unidas en un párrafo y con otros espacios; el pie, con otro número de página (las cifras
        # no cuentan en las líneas).
        docx = self.docx("Primera  línea del escrito, y la segunda, partida.", "Página 1 de 10")
        self.assertEqual(pdf_a_word.missing_text(pdf, [0], docx), (10, 0, 3, 0))

    def test_una_linea_de_palabras_repetidas(self):
        # Todas sus palabras salen en otra línea: por palabras no falta nada, pero falta la línea.
        pdf = self.pdf("la parte actora interesa", "interesa la parte actora")
        docx = self.docx("la parte actora interesa")
        self.assertEqual(pdf_a_word.missing_text(pdf, [0], docx), (4, 0, 2, 1))

    def test_si_falta_texto_no_se_entrega(self):
        # Si a la conversión le faltara texto del OCR, PDF_MIXED_SCAN (o PDF_UNSUPPORTED si todo es
        # escaneo) sin salida.
        origen = os.path.join(self.dir, "..", os.path.basename(self.dir) + "-origen.pdf")
        self.addCleanup(lambda: os.path.exists(origen) and os.unlink(origen))
        escrito(origen, paginas=2)
        pdf = self.ruta("mixto.pdf")
        mixto(pdf, origen)
        real = pdf_a_word.missing_text
        pdf_a_word.missing_text = lambda *a: (100, 1, 40, 0)
        self.addCleanup(setattr, pdf_a_word, "missing_text", real)
        salida = self.ruta("s.docx")
        with self.assertRaises(pdf_a_word.Refused) as r:
            pdf_a_word.convert(pdf, salida, 300)
        self.assertEqual(r.exception.report, {"refused": "PDF_MIXED_SCAN", "reason": "ocr_text", "ocr_words": 100,
                                              "ocr_words_missing": 1, "ocr_lines": 40, "ocr_lines_missing": 0})
        self.assertEqual(sorted(os.listdir(self.dir)), ["mixto.pdf"])


class TextoDeOcr(Base):
    """Escaneo con la capa de texto invisible de un OCR (imagen de la página + texto en modo 3)."""

    def test_se_convierte_el_texto_sin_la_imagen(self):
        origen = os.path.join(self.dir, "..", os.path.basename(self.dir) + "-origen.pdf")
        self.addCleanup(lambda: os.path.exists(origen) and os.unlink(origen))
        escrito(origen, paginas=2)
        src = pymupdf.open(origen)
        doc = pymupdf.open()
        for page in src:
            nueva = doc.new_page(width=page.rect.width, height=page.rect.height)
            nueva.insert_image(nueva.rect, pixmap=page.get_pixmap(dpi=100, colorspace=pymupdf.csGRAY))
            for b in page.get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    for s in line["spans"]:
                        nueva.insert_text(s["origin"], s["text"], fontsize=s["size"], render_mode=3)
        pdf = self.ruta("ocr.pdf")
        doc.save(pdf)
        codigo, rep, salida, quedan = self.convertir(pdf)
        self.assertEqual((codigo, salida), (0, ""))
        self.assertEqual(quedan, ["informe.json", "salida.docx"])  # la copia .ocr.pdf, borrada
        self.assertTrue(rep["ocr"])
        self.assertIn("la parte actora interesa", texto_docx(self.ruta("salida.docx")))
        with zipfile.ZipFile(self.ruta("salida.docx")) as z:
            self.assertFalse([n for n in z.namelist() if n.startswith("word/media/")])


if __name__ == "__main__":
    unittest.main()
