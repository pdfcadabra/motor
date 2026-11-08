# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
#
# Tests del posprocesado, con el banco de escritos sintéticos de pruebas-pdf/pdf-a-word (sin esa
# carpeta, con el motor solo, se saltan): las tablas de verdad siguen siendo tablas, las tablas
# falsas vuelven a ser texto unido a su párrafo y los títulos centrados quedan sin sangrías;
# (escritos forenses, f01-f06) firma y encabezamiento con una línea por párrafo, el título del
# juzgado fuera de la cabecera, sangrías francesas con tabulador, sangría de primera línea, espacio
# antes de las tablas, alto mínimo de las filas y filete del pie; (PDF que imitan los de Google
# Docs, g01-g04) tabuladores con el margen nuevo, U+200B, listas, tablas falsas de nivel 1, fuente
# sin familia, interlineado y paginación, viñetas que pdf2docx junta en un párrafo, elementos de una
# línea, listas de Word todo o nada y la fuente de las viñetas.
# Convierte con pdf2docx los PDF del banco en una carpeta temporal (alrededor de 1 minuto), con el
# OpenCV vacío de sin_opencv/ (como pdf_a_word.py). Listas de Word activadas por defecto: los tests
# de las etiquetas escritas piden `listas=False` y los de las listas de Word usan la conversión por
# defecto (ver ListasDeWordPorDefecto).
# Uso: python3 -m unittest posproceso_test.py
import logging
import os
import re
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI / "sin_opencv"))

from pdf2docx import Converter  # noqa: E402

import posproceso as pp  # noqa: E402

# Todos los PDF en una carpeta: e01-e16 (rondas 2 y 3), f01-f06 (forenses), g01-g04 (Google Docs)
# y 03-tablas-honorarios-plazos (primera ronda).
BANCO = AQUI.parent / "pruebas-pdf" / "pdf-a-word"
ESCRITOS = FORENSES = GDOCS = BANCO
W = pp.W
logging.disable(logging.CRITICAL)


def tablas(docx):
    body = Document(str(docx)).element.body
    return [{"filas": len(t.findall(W + "tr")), "bordes": not pp.sin_bordes(t),
             "texto": " ".join(pp.texto_p(p) for p in t.iter(W + "p"))} for t in body.findall(W + "tbl")]


@unittest.skipUnless(BANCO.is_dir(), "sin pruebas-pdf/pdf-a-word (motor solo)")
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.cache = {}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def convertir(self, pdf, listas=None):
        """(salida de pdf2docx, salida del posprocesado, informe). `listas`: None, el valor por
        defecto de posprocesar (listas de Word); True o False, pedido explícitamente."""
        clave = (pdf, listas)
        if clave not in self.cache:
            d1 = self.dir / f"{pdf.stem}.docx"
            if not d1.exists():
                c = Converter(str(pdf))
                c.convert(str(d1))
                c.close()
            sufijo = {None: "", True: "-listas", False: "-sin-listas"}[listas]
            d2 = self.dir / f"{pdf.stem}-pos{sufijo}.docx"
            opciones = {} if listas is None else {"listas": listas}
            inf = pp.posprocesar(pdf, d1, d2, **opciones)
            self.cache[clave] = (d1, d2, inf)
        return self.cache[clave]


class TablasDeVerdad(Base):
    def test_las_tablas_de_verdad_siguen(self):
        # Minuta (e05, e16, que pdf2docx parte en dos al cruzar de página), plazos (e06) y las dos
        # tablas con celdas unidas de la primera ronda (03).
        casos = [ESCRITOS / "e05-minuta.pdf", ESCRITOS / "e06-plazos.pdf", ESCRITOS / "e16-titulos-falsa-sans.pdf",
                 BANCO / "03-tablas-honorarios-plazos.pdf"]
        for pdf in casos:
            with self.subTest(pdf=pdf.name):
                d1, d2, inf = self.convertir(pdf)
                antes = [t for t in tablas(d1) if t["filas"] > 1 or t["bordes"]]
                despues = [t for t in tablas(d2) if t["filas"] > 1 or t["bordes"]]
                self.assertTrue(antes)
                self.assertEqual([t["filas"] for t in antes], [t["filas"] for t in despues])
                self.assertEqual([pp.nz(t["texto"]) for t in antes], [pp.nz(t["texto"]) for t in despues])

    def test_firma_en_tabla_sin_bordes_no_se_toca(self):
        # Una tabla de una fila sin bordes que es una línea suelta (la firma a dos columnas) no es
        # una tabla falsa: no es una línea de un párrafo justificado.
        pdf = ESCRITOS / "e13-tabla-falsa-serif.pdf"
        d1, _, _ = self.convertir(pdf)
        doc = Document(str(d1))
        body = doc.element.body
        firma = next(p for p in body.iter(W + "p") if pp.texto_p(p).startswith("Ldo."))
        tbl = parse_xml(
            f'<w:tbl {nsdecls("w")}><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid>'
            '<w:gridCol w:w="4000"/><w:gridCol w:w="4000"/></w:tblGrid><w:tr>'
            '<w:tc><w:tcPr><w:tcBorders/></w:tcPr><w:p><w:r><w:t>Ldo. Pedro Modelo Inventado</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:tcPr><w:tcBorders/></w:tcPr><w:p><w:r><w:t>Proc. Ana Ejemplo Ficticia</w:t></w:r></w:p></w:tc>'
            '</w:tr></w:tbl>')
        firma.addprevious(tbl)
        firma.getparent().remove(firma)
        hecho = self.dir / "firma-tabla.docx"
        doc.save(str(hecho))
        inf = pp.posprocesar(pdf, hecho, self.dir / "firma-tabla-pos.docx")
        self.assertEqual(inf["tablas_falsas"], [p for p in inf["tablas_falsas"] if "TERCERO" in p])
        self.assertTrue(any("Ldo." in t["texto"] for t in tablas(self.dir / "firma-tabla-pos.docx")))


class TablasFalsas(Base):
    TERCERO = "TERCERO.- Que, con ello, queda subsanado csv:"

    def test_vuelven_a_texto_unidas_a_su_parrafo(self):
        for nombre, n in (("e13-tabla-falsa-serif", 1), ("e14-tabla-falsa-carlito", 2), ("e16-titulos-falsa-sans", 1)):
            with self.subTest(escrito=nombre):
                d1, d2, inf = self.convertir(ESCRITOS / f"{nombre}.pdf")
                # pdf2docx las crea (si deja de hacerlo, este test ya no prueba nada).
                self.assertEqual(sum(1 for t in tablas(d1) if t["filas"] == 1 and not t["bordes"]), n)
                self.assertEqual(len(inf["tablas_falsas"]), n)
                self.assertFalse([t for t in tablas(d2) if t["filas"] == 1 and not t["bordes"]])
                # El TERCERO entero (la línea que era tabla y el resto, en la página siguiente) es un
                # solo párrafo justificado.
                pars = [p for p in Document(str(d2)).paragraphs if p.text.startswith("TERCERO.- Que")]
                self.assertEqual(len(pars), 1)
                self.assertIn(pp.nz(self.TERCERO), pp.nz(pars[0].text))
                self.assertTrue(pars[0].text.rstrip().endswith("sin necesidad de nuevo requerimiento a esta parte."))
                self.assertEqual(pars[0].alignment, 3)  # justificado


class TitulosCentrados(Base):
    def test_sin_sangrias(self):
        casos = {"e13-tabla-falsa-serif": ["A LA SALA"], "e15-titulos-centrados": ["A LA SECCIÓN", "PROCEDIMIENTO"],
                 "e16-titulos-falsa-sans": ["A LA SECCIÓN", "PROCEDIMIENTO"]}
        for nombre, inicios in casos.items():
            _, d2, _ = self.convertir(ESCRITOS / f"{nombre}.pdf")
            for p in Document(str(d2)).paragraphs:
                if p.text.startswith(tuple(inicios + ["HECHOS", "FUNDAMENTOS"])):
                    with self.subTest(escrito=nombre, titulo=p.text[:30]):
                        pf = p.paragraph_format
                        self.assertEqual(p.alignment, 1)  # centrado
                        self.assertFalse(pf.left_indent)
                        self.assertFalse(pf.right_indent)
                        self.assertFalse(pf.first_line_indent)


class LineasCentradasSueltas(Base):
    FIRMA = ["Ldo. Supuesto Modelo de Prueba", "Col. n.º 00000", "Proc. Ficticia Ejemplo Inventada", "Col. n.º 0000"]

    def test_firma_una_linea_por_parrafo(self):
        for pdf in sorted(FORENSES.glob("f0*.pdf")):
            with self.subTest(escrito=pdf.stem):
                _, d2, _ = self.convertir(pdf)
                textos = [p.text.strip() for p in Document(str(d2)).paragraphs]
                for linea in self.FIRMA:
                    self.assertIn(linea, textos)

    def test_firma_junta_si_el_original_la_tiene_en_una_pagina(self):
        # f06: entera en la página 8 del original -> «conservar con el siguiente» en sus tres
        # primeras líneas. f01: partida 3 + 1 en el original -> sin tocar.
        for nombre, esperado in (("f06-varias-paginas-pie", [True, True, True, None]),
                                 ("f01-demanda-hechos-fundamentos", [None] * 4)):
            with self.subTest(escrito=nombre):
                _, d2, _ = self.convertir(FORENSES / f"{nombre}.pdf")
                pars = {p.text.strip(): p for p in Document(str(d2)).paragraphs}
                self.assertEqual([pars[l].paragraph_format.keep_with_next for l in self.FIRMA], esperado)

    def test_encabezamiento_una_linea_por_parrafo(self):
        _, d2, _ = self.convertir(FORENSES / "f05-encabezamiento-francesas.pdf")
        pars = Document(str(d2)).paragraphs
        for linea in ("Juicio Ordinario n.º 0000/2099", "NIG: 00000 00 1 2099 0000001", "Sección: CIVIL · Negociado: 0"):
            p = next((p for p in pars if p.text.strip() == linea), None)
            with self.subTest(linea=linea):
                self.assertIsNotNone(p)
                self.assertEqual(p.alignment, 1)  # centrado

    def test_titulo_de_dos_lineas_sigue_unido(self):
        # «A LA SECCIÓN CIVIL … PLAZA N.º 3,» / «SERVICIO COMÚN…»: la segunda continúa la frase.
        for nombre in ("e15-titulos-centrados", "e16-titulos-falsa-sans"):
            with self.subTest(escrito=nombre):
                _, d2, _ = self.convertir(ESCRITOS / f"{nombre}.pdf")
                pars = [p.text for p in Document(str(d2)).paragraphs if "SERVICIO COMÚN" in p.text]
                self.assertEqual(len(pars), 1)
                self.assertTrue(pars[0].startswith("A LA SECCIÓN"))

    def test_continua_frase(self):
        def l(t):
            return {"texto": t}
        self.assertTrue(pp.continua_frase(l("A LA SALA DE LO CIVIL DE LA AUDIENCIA PROVINCIAL DE"), l("VILLAFICTICIA")))
        self.assertTrue(pp.continua_frase(l("TRIBUNAL DE INSTANCIA, PLAZA N.º 3,"), l("SERVICIO COMÚN")))
        self.assertTrue(pp.continua_frase(l("Recurso de apelación contra la sentencia"), l("de uno de enero")))
        self.assertFalse(pp.continua_frase(l("Ldo. Supuesto Modelo de Prueba"), l("Col. n.º 00000")))
        self.assertFalse(pp.continua_frase(l("Juicio Ordinario n.º 0000/2099"), l("NIG: 00000 00 1 2099 0000001")))
        self.assertFalse(pp.continua_frase(l("AL JUZGADO DE PRIMERA INSTANCIA N.º 0"), l("Sección: CIVIL")))
        self.assertFalse(pp.continua_frase(l("HECHOS."), l("de la demanda")))


class CabeceraDeUnaPagina(Base):
    def test_el_juzgado_es_cuerpo_y_el_membrete_cabecera(self):
        for pdf, juzgado, membrete in ((FORENSES / "f03-tramite-breve.pdf", "AL JUZGADO DE PRIMERA INSTANCIA N.º 0",
                                        "MODELO & PRUEBA"),
                                       (ESCRITOS / "e04-personacion.pdf", "AL JUZGADO DE PRIMERA INSTANCIA N.º 3",
                                        "DESPACHO EJEMPLO")):
            with self.subTest(escrito=pdf.stem):
                _, d2, inf = self.convertir(pdf)
                doc = Document(str(d2))
                self.assertTrue(any(p.text.startswith(juzgado) for p in doc.paragraphs))
                cab = " ".join(p.text for p in doc.sections[0].first_page_header.paragraphs)
                self.assertIn(membrete, cab)
                self.assertNotIn(juzgado, cab)


class SangriasFrancesas(Base):
    def test_sangria_tabulador_y_justificado(self):
        _, d2, inf = self.convertir(FORENSES / "f05-encabezamiento-francesas.pdf")
        self.assertEqual(inf["sangria_francesa"], 8)
        doc = Document(str(d2))
        items = [p for p in doc.paragraphs if p.text.startswith(("1.º", "2.º", "3.º", "Documento n.º"))]
        self.assertEqual(len(items), 8)
        for p in items:
            with self.subTest(parrafo=p.text[:20]):
                pf = p.paragraph_format
                self.assertGreater(pf.left_indent, 0)
                self.assertAlmostEqual(pf.first_line_indent, -pf.left_indent, delta=2 * 635)  # hasta el margen (±2 twips)
                self.assertEqual(p.alignment, 3)  # justificado, nada centrado
                # Un tabulador entre la etiqueta y el texto, y ninguno más.
                self.assertEqual(len(list(p._p.iter(W + "tab"))) - len(p._p.findall(f"{W}pPr/{W}tabs/{W}tab")), 1)
                self.assertRegex(p.text, r"^(\d\.º|Documento n\.º \d\.–)\t\S")


class SangriaPrimeraLinea(Base):
    def test_parrafo_de_una_linea(self):
        _, d2, _ = self.convertir(FORENSES / "f01-demanda-hechos-fundamentos.pdf")
        p = next(p for p in Document(str(d2)).paragraphs if p.text.strip() == "En su virtud,")
        self.assertFalse(p.paragraph_format.left_indent)
        self.assertGreater(p.paragraph_format.first_line_indent, 0)

    def test_parrafo_de_dos_lineas(self):
        # pdf2docx la emulaba con un tabulador sin sangría (f02).
        _, d2, _ = self.convertir(FORENSES / "f02-recurso-notas.pdf")
        p = next(p for p in Document(str(d2)).paragraphs if p.text.startswith("Que, dentro del plazo legal"))
        self.assertFalse(p.paragraph_format.left_indent)
        self.assertGreater(p.paragraph_format.first_line_indent, 0)


class TablasYPie(Base):
    def test_espacio_antes_de_la_tabla(self):
        # El párrafo que la precede empezó en la página anterior: al unirlo se perdía su espacio
        # posterior, que es el que separa la tabla (f04).
        _, d2, _ = self.convertir(FORENSES / "f04-tabla-cuantias.pdf")
        tbl = Document(str(d2)).element.body.find(W + "tbl")
        antes = tbl.getprevious()
        sp = antes.find(f"{W}pPr/{W}spacing")
        self.assertGreaterEqual(int(sp.get(W + "after")), 200)

    def test_alto_minimo_de_las_filas(self):
        for pdf in (FORENSES / "f04-tabla-cuantias.pdf", ESCRITOS / "e05-minuta.pdf"):
            with self.subTest(escrito=pdf.stem):
                d1, d2, inf = self.convertir(pdf)
                self.assertTrue(list(Document(str(d1)).element.body.iter(W + "trHeight")))
                reglas = {th.get(W + "hRule") for th in Document(str(d2)).element.body.iter(W + "trHeight")}
                self.assertEqual(reglas, {"atLeast"})

    def test_filete_del_pie(self):
        _, d2, inf = self.convertir(FORENSES / "f06-varias-paginas-pie.pdf")
        self.assertIsNotNone(inf["filete_pie"])
        pie = Document(str(d2)).sections[0].footer._element
        self.assertIsNotNone(pie.find(f"{W}p/{W}pPr/{W}pBdr/{W}top"))
        # Sin filete en el original, sin borde.
        _, d2, inf = self.convertir(FORENSES / "f01-demanda-hechos-fundamentos.pdf")
        self.assertIsNone(inf["filete_pie"])


# --- Quinta ronda: PDF de Google Docs -----------------------------------------------------------

def parrafos_cuerpo(docx):
    """Párrafos del cuerpo con texto (sin tablas) del .docx."""
    body = Document(str(docx)).element.body
    return [p for p in body.findall(W + "p") if pp.texto_p(p).strip()]


def margen_izq(docx):
    return int(Document(str(docx)).element.body.find(f"{W}sectPr/{W}pgMar").get(W + "left"))


def texto_runs(p):
    """Texto de los runs (sin las tabulaciones definidas en pPr), con los tabuladores como \t."""
    return "".join((x.text or "") if x.tag == W + "t" else "\t" for x in p.iter(W + "t", W + "tab")
                   if x.getparent().tag == W + "r")


class TabuladoresConElMargen(Base):
    def test_el_texto_tras_el_tabulador_cae_donde_en_el_pdf(self):
        # g01: pdf2docx deja margen 0 y el tabulador en 2422 twips (121,1 pt desde el
        # borde); con el margen en 85,1 pt, sin recalcularlo el texto caía en 206,2.
        d1, d2, _ = self.convertir(GDOCS / "g01-gdocs-tabuladores.pdf")
        self.assertEqual(margen_izq(d1), 0)
        m = margen_izq(d2)
        con_tab = [p for p in parrafos_cuerpo(d2) if p.find(f"{W}pPr/{W}tabs") is not None]
        self.assertGreaterEqual(len(con_tab), 6)
        for p in con_tab:
            with self.subTest(parrafo=pp.texto_p(p)[:30]):
                for tab in p.find(f"{W}pPr/{W}tabs"):
                    self.assertAlmostEqual((m + int(tab.get(W + "pos"))) / 20, 121.1, delta=1.5)


class CaracteresDeFormato(Base):
    def test_el_u200b_no_cuenta_como_comienzo_de_linea(self):
        a = pp.analizar_pdf(GDOCS / "g01-gdocs-tabuladores.pdf")
        primeras = [l for _, _, l in a["cuerpo"] if l["texto"].startswith(("DOÑA FICTICIA", "Que, por medio"))]
        self.assertEqual(len(primeras), 2)
        for l in primeras:
            self.assertAlmostEqual(l["x0"], 121.1, delta=0.5)
            self.assertNotIn("\u200b", l["texto"])
        self.assertEqual(pp.nz("a\u200bb c"), "ab" + "c")

    def test_parrafos_con_tabulador_inicial(self):
        # Empiezan con U+200B + tabulador: en el .docx, con su tabulador (como el original) y sin
        # sangría de primera línea, y sin el U+200B.
        _, d2, _ = self.convertir(GDOCS / "g01-gdocs-tabuladores.pdf")
        pars = parrafos_cuerpo(d2)
        self.assertFalse([p for p in pars if "\u200b" in pp.texto_p(p)])
        inicios = ("DOÑA FICTICIA", "Que, por medio", "PRIMERA.-", "SEGUNDA.-", "3.- PRUEBA", "SOBRE LA IMPOSICIÓN",
                   "Es justicia", "En Villaficticia")
        con_tab = [p for p in pars if texto_runs(p).lstrip("\t").startswith(inicios)]
        self.assertEqual(len(con_tab), 8)
        for p in con_tab:
            with self.subTest(parrafo=pp.texto_p(p)[:30]):
                self.assertTrue(texto_runs(p).startswith("\t"))
                ind = p.find(f"{W}pPr/{W}ind")
                self.assertEqual(int(ind.get(W + "firstLine", "0")) if ind is not None else 0, 0)


class Listas(Base):
    def test_un_parrafo_por_elemento(self):
        # g03: 23 párrafos en el original; antes, elementos unidos (una viñeta pegada
        # a la línea llena anterior) y partidos (texto tras «N.» antes del tope del tabulador, líneas
        # de una lista sin justificar que no llegan al margen).
        a = pp.analizar_pdf(GDOCS / "g03-gdocs-listas.pdf")
        self.assertEqual(sum(pp.parrafos_pdf(a)), 23)
        # Por defecto, listas de Word: 15 elementos con numPr, sin la etiqueta escrita, con la
        # misma sangría francesa.
        _, d2, _ = self.convertir(GDOCS / "g03-gdocs-listas.pdf")
        pars = parrafos_cuerpo(d2)
        self.assertEqual(len(pars), 23)
        con_numpr = [p for p in pars if p.find(f"{W}pPr/{W}numPr") is not None]
        self.assertEqual(len(con_numpr), 15)
        for p in con_numpr:
            with self.subTest(listas=True, parrafo=pp.texto_p(p)[:30]):
                ind = p.find(f"{W}pPr/{W}ind")
                self.assertEqual(int(ind.get(W + "hanging", "0")), 360)
                self.assertIn(int(ind.get(W + "left")), (720, 1440))
                self.assertRegex(pp.sin_cf(texto_runs(p)), r"^[^\t●]\S")
        # Sin listas de Word: la etiqueta escrita y un tabulador.
        _, d2, _ = self.convertir(GDOCS / "g03-gdocs-listas.pdf", listas=False)
        pars = parrafos_cuerpo(d2)
        self.assertEqual(len(pars), 23)
        items = [p for p in pars if re.match(r"^\s*(●|\d\.)", pp.sin_cf(texto_runs(p)))]
        self.assertEqual(len(items), 15)
        for p in items:
            with self.subTest(parrafo=pp.texto_p(p)[:30]):
                t = pp.nz(pp.texto_p(p))
                # Una sola etiqueta por párrafo, sangría francesa de 18 pt y tabulador tras la etiqueta.
                self.assertEqual(t.count("●"), 1 if t.startswith("●") else 0)
                ind = p.find(f"{W}pPr/{W}ind")
                self.assertEqual(int(ind.get(W + "hanging", "0")), 360)
                self.assertIn(int(ind.get(W + "left")), (720, 1440))
                self.assertRegex(texto_runs(p), r"^(●|\d\.)\t\S")

    def test_corte_frances_tras_el_numero(self):
        # El texto tras «1.» empieza en 105,6 y la continuación en 108,0: es la misma sangría francesa.
        a = pp.analizar_pdf(GDOCS / "g03-gdocs-listas.pdf")
        cuerpo = [l for _, _, l in a["cuerpo"]]
        k = next(i for i, l in enumerate(cuerpo) if l["texto"].startswith("1.La sentencia"))
        self.assertEqual(pp.etiqueta_de(cuerpo[k])[0], "1.")
        self.assertAlmostEqual(pp.corte_frances(cuerpo[k], cuerpo[k + 1]), 105.6, delta=0.5)

    def test_sin_falsos_positivos_en_los_bancos_anteriores(self):
        for patron in ("e*.pdf", "f*.pdf"):
            for pdf in sorted(BANCO.glob(patron)):
                with self.subTest(escrito=pdf.stem):
                    a = pp.analizar_pdf(pdf)
                    self.assertFalse([l["texto"][:30] for _, _, l in a["cuerpo"] if pp.etiqueta_de(l)])


class TablasFalsasDeNivel1(Base):
    def test_lineas_estiradas_con_sangria(self):
        # Líneas de continuación de nivel 1 (x0 = 144 con el margen en 72) muy estiradas: pdf2docx
        # las convierte en tablas de una fila (o de dos, si son dos seguidas, también a los dos lados
        # de un salto de página). Todas vuelven a ser texto de su elemento.
        for listas, inicio in ((None, "El juzgador"), (False, "1.El juzgador")):
            with self.subTest(listas=listas):
                d1, d2, _ = self.convertir(GDOCS / "g03-gdocs-listas.pdf", listas=listas)
                self.assertGreaterEqual(len([t for t in tablas(d1) if not t["bordes"]]), 8)
                self.assertEqual(tablas(d2), [])
                p = next(p for p in parrafos_cuerpo(d2) if "B23456C" in pp.texto_p(p))
                self.assertTrue(pp.nz(pp.texto_p(p)).startswith(pp.nz(inicio + " omite valorar la prueba documental")))
                self.assertTrue(pp.texto_p(p).rstrip().endswith("por los documentos aportados."))
                # Con listas de Word, el número es de la numeración (numPr).
                self.assertEqual(p.find(f"{W}pPr/{W}numPr") is not None, listas is None)


class FuenteSinFamilia(Base):
    def test_rfonts_con_el_nombre_del_pdf(self):
        # g02: una fuente incrustada sin familia (name ID 1 vacío) va la primera y
        # pdf2docx deja rFonts vacío en todos los runs.
        d1, d2, _ = self.convertir(GDOCS / "g02-gdocs-fuente-vacia.pdf")
        antes = {rf.get(W + "ascii") for rf in Document(str(d1)).element.body.iter(W + "rFonts")}
        self.assertEqual(antes, {""})
        runs = [r for r in Document(str(d2)).element.body.iter(W + "r") if r.find(W + "t") is not None]
        fuente = {}
        for r in runs:
            rf = r.find(f"{W}rPr/{W}rFonts")
            fuente.setdefault(rf.get(W + "ascii") if rf is not None else None, []).append(r)
        self.assertEqual(set(fuente), {"Times New Roman", "Arial", "Calibri"})
        calibri = "".join(t.text for r in fuente["Calibri"] for t in r.iter(W + "t"))
        self.assertEqual(calibri.strip(), "Documento de referencia 0")

    def test_familia_pdf(self):
        for nombre, fam in (("BAAAAA+TimesNewRomanPSMT", "Times New Roman"), ("AAAAAA+TimesNewRomanPS-BoldMT",
                            "Times New Roman"), ("DAAAAA+ArialMT", "Arial"), ("CourierNewPSMT", "Courier New"),
                            ("CAAAAA+Calibri", "Calibri"), ("ABCDEF+BookmanOldStyle-Italic", "Bookman Old Style")):
            self.assertEqual(pp.familia_pdf(nombre), fam)


class InterlineadoYPaginacion(Base):
    def test_interlineado_del_pdf(self):
        # Google Docs a 1,5 líneas: pdf2docx daba de 218 a 346 «auto» por párrafo y el texto subía.
        for nombre in ("g01-gdocs-tabuladores", "g02-gdocs-fuente-vacia", "g03-gdocs-listas"):
            with self.subTest(escrito=nombre):
                _, d2, _ = self.convertir(GDOCS / f"{nombre}.pdf")
                reglas = {(sp.get(W + "line"), sp.get(W + "lineRule")) for p in parrafos_cuerpo(d2)
                          for sp in [p.find(f"{W}pPr/{W}spacing")] if sp is not None}
                self.assertEqual(reglas, {("360", "auto")})

    def test_sin_huecos_del_cambio_de_pagina(self):
        # Con margen superior 0, pdf2docx ponía como «antes» del primer párrafo de cada página la
        # distancia al borde (unos 80 pt), que acababa como «después» del anterior.
        for nombre in ("g01-gdocs-tabuladores", "g02-gdocs-fuente-vacia", "g03-gdocs-listas"):
            with self.subTest(escrito=nombre):
                _, d2, _ = self.convertir(GDOCS / f"{nombre}.pdf")
                grandes = [pp.texto_p(p)[:30] for p in parrafos_cuerpo(d2)
                           for sp in [p.find(f"{W}pPr/{W}spacing")] if sp is not None
                           and max(int(sp.get(W + "before", "0")), int(sp.get(W + "after", "0"))) > 400]
                self.assertEqual(grandes, [])

    def test_pagina_a4_normalizada(self):
        # Google Docs da A4 como 596 × 842 pt; LibreOffice lo ajusta a 595,3 y el texto justificado se
        # partía antes. El ancho del texto es el del PDF.
        _, d2, _ = self.convertir(GDOCS / "g02-gdocs-fuente-vacia.pdf")
        sect = Document(str(d2)).element.body.find(f"{W}sectPr")
        self.assertEqual(sect.find(W + "pgSz").get(W + "w"), "11906")
        mg = sect.find(W + "pgMar")
        a = pp.analizar_pdf(GDOCS / "g02-gdocs-fuente-vacia.pdf")
        ancho = (11906 - int(mg.get(W + "left")) - int(mg.get(W + "right"))) / 20
        self.assertAlmostEqual(ancho, a["der"] - a["izq"], delta=0.1)

    def test_vineta_sin_justificar_a_la_izquierda_y_sin_sangria_derecha(self):
        # pdf2docx centraba una viñeta de dos líneas cortas y le ponía 79 pt de sangría derecha.
        _, d2, _ = self.convertir(GDOCS / "g03-gdocs-listas.pdf")
        p = next(p for p in parrafos_cuerpo(d2) if "Justificantes bancarios" in pp.texto_p(p))
        jc = p.find(f"{W}pPr/{W}jc")
        self.assertIn(jc.get(W + "val") if jc is not None else "left", ("left", "start"))
        self.assertEqual(int(p.find(f"{W}pPr/{W}ind").get(W + "right", "0")), 0)


# --- Sexta ronda: viñetas unidas, elementos de una línea, listas de Word y fuente de la viñeta ---

G04 = GDOCS / "g04-gdocs-vinetas-unidas.pdf"
VINETAS_G04 = ("\u2022", "\u25cf")


def items(docx):
    """Párrafos del cuerpo que empiezan con viñeta (escrita)."""
    return [p for p in parrafos_cuerpo(docx) if pp.sin_cf(texto_runs(p)).lstrip().startswith(VINETAS_G04)]


def items_numpr(docx):
    """Párrafos del cuerpo de una lista de Word (numPr)."""
    return [p for p in parrafos_cuerpo(docx) if p.find(f"{W}pPr/{W}numPr") is not None]


def fuente_de_la_etiqueta(docx, p):
    """Fuente de la viñeta o el número de un elemento de una lista de Word (la del nivel)."""
    numeracion = Document(str(docx)).part.numbering_part.element
    npr = p.find(f"{W}pPr/{W}numPr")
    num_id, ilvl = npr.find(W + "numId").get(W + "val"), npr.find(W + "ilvl").get(W + "val")
    num = next(n for n in numeracion.findall(W + "num") if n.get(W + "numId") == num_id)
    abs_id = num.find(W + "abstractNumId").get(W + "val")
    an = next(a for a in numeracion.findall(W + "abstractNum") if a.get(W + "abstractNumId") == abs_id)
    lvl = next(lv for lv in an.findall(W + "lvl") if lv.get(W + "ilvl") == ilvl)
    return lvl.find(f"{W}rPr/{W}rFonts").get(W + "ascii"), lvl.find(W + "lvlText").get(W + "val")


class VinetasUnidas(Base):
    def test_un_parrafo_por_vineta(self):
        # Un elemento que acaba casi en el margen y el siguiente con el paso normal: pdf2docx los
        # junta en un párrafo sin salto de línea (seis pares en g04).
        d1, d2, inf = self.convertir(G04)
        juntos = [p for p in parrafos_cuerpo(d1) if sum(pp.texto_p(p).count(v) for v in VINETAS_G04) > 1]
        self.assertEqual(len(juntos), 6)
        # Por defecto, listas de Word: 15 elementos, ninguno con una viñeta escrita.
        self.assertEqual(len(items_numpr(d2)), 15)
        self.assertEqual(items(d2), [])
        p = next(p for p in parrafos_cuerpo(d2) if "Copia del contrato de obra." in pp.texto_p(p))
        self.assertEqual(pp.nz(pp.texto_p(p)), pp.nz("Copia del contrato de obra."))
        self.assertIsNotNone(p.find(f"{W}pPr/{W}numPr"))
        # Sin listas de Word: una viñeta escrita por párrafo.
        _, d2, _ = self.convertir(G04, listas=False)
        self.assertEqual(len(items(d2)), 15)
        for p in items(d2):
            with self.subTest(parrafo=pp.texto_p(p)[:30]):
                self.assertEqual(sum(pp.texto_p(p).count(v) for v in VINETAS_G04), 1)
        p = next(p for p in parrafos_cuerpo(d2) if "Copia del contrato de obra." in pp.texto_p(p))
        self.assertEqual(pp.nz(pp.texto_p(p)), pp.nz("\u2022 Copia del contrato de obra."))

    def test_elementos_de_una_linea(self):
        # Sangría francesa con tabulador como los de varias líneas: left = x del texto − margen,
        # francesa = x del texto − x de la viñeta.
        _, d2, _ = self.convertir(G04)
        unalinea = [p for p in items_numpr(d2) if len(pp.texto_p(p)) < 40]
        self.assertEqual(len(unalinea), 8)
        for p in unalinea:
            with self.subTest(listas=True, parrafo=pp.texto_p(p)[:30]):
                ind = p.find(f"{W}pPr/{W}ind")
                self.assertEqual((int(ind.get(W + "left")), int(ind.get(W + "hanging", "0"))), (720, 360))
                self.assertRegex(pp.sin_cf(texto_runs(p)), r"^[^\t\u2022\u25cf]")
        _, d2, _ = self.convertir(G04, listas=False)
        unalinea = [p for p in items(d2) if len(pp.texto_p(p)) < 40]
        self.assertEqual(len(unalinea), 8)
        for p in unalinea:
            with self.subTest(listas=False, parrafo=pp.texto_p(p)[:30]):
                ind = p.find(f"{W}pPr/{W}ind")
                self.assertEqual((int(ind.get(W + "left")), int(ind.get(W + "hanging", "0"))), (720, 360))
                self.assertRegex(texto_runs(p), r"^(\u2022|\u25cf)\t\S")

    def test_fuente_de_la_vineta(self):
        # Con la Calibri sin familia, las viñetas que el PDF dibuja en Times salían en Calibri.
        # Por defecto, la de cada nivel de la lista de Word.
        _, d2, _ = self.convertir(G04)
        self.assertEqual([fuente_de_la_etiqueta(d2, p) for p in items_numpr(d2)],
                         [("Calibri", "\u2022")] * 5 + [("Times New Roman", "\u2022")] * 5 + [("Arial", "\u25cf")] * 5)
        # Sin listas de Word, la del run de la viñeta escrita.
        _, d2, _ = self.convertir(G04, listas=False)
        fuentes = []
        for p in items(d2):
            r = next(r for r in p.iter(W + "r") if pp.sin_cf("".join(t.text or "" for t in r.iter(W + "t"))).strip())
            fuentes.append(r.find(f"{W}rPr/{W}rFonts").get(W + "ascii"))
        self.assertEqual(fuentes, ["Calibri"] * 5 + ["Times New Roman"] * 5 + ["Arial"] * 5)


class ListasDeWordTodoONada(Base):
    def test_todos_los_elementos(self):
        # Con la conversión por defecto (listas de Word activadas).
        for pdf, n in ((G04, 15), (GDOCS / "g03-gdocs-listas.pdf", 15)):
            with self.subTest(escrito=pdf.stem):
                _, d3, inf = self.convertir(pdf)
                self.assertEqual(inf["listas_word"]["elementos"], n)
                self.assertEqual(inf["listas_word"]["sin_convertir"], [])
                con_numpr = [p for p in parrafos_cuerpo(d3) if p.find(f"{W}pPr/{W}numPr") is not None]
                self.assertEqual(len(con_numpr), n)
                # Ninguna etiqueta escrita queda: todas son de la numeración.
                self.assertFalse([p for p in parrafos_cuerpo(d3)
                                  if re.match(r"^\s*(\u2022|\u25cf|\d\.)", pp.sin_cf(texto_runs(p)))])

    def test_nada_de_listas_a_medias(self):
        # Si un elemento de una lista no se puede convertir, no se convierte ninguno de esa lista.
        # Sobre la salida sin listas de Word (las etiquetas escritas), como hace posprocesar.
        _, d2, _ = self.convertir(G04, listas=False)
        doc = Document(str(d2))
        body = doc.element.body
        els = items(d2)
        reg = []
        for p in body.findall(W + "p"):
            if p in els or any(pp.texto_p(p) == pp.texto_p(e) for e in els):
                t = pp.sin_cf(texto_runs(p)).lstrip()
                reg.append({"el": p, "etiqueta": t[0], "left": 720, "hanging": 360, "fuente": "Arial", "tam": 12})
        self.assertEqual(len(reg), 15)
        reconocidos = {r["el"] for r in reg}
        sin_uno = reg[:7] + reg[8:]  # falta el tercero de la segunda lista
        inf = {}
        pp.listas_de_word(doc, body, sin_uno, reconocidos, inf)
        self.assertEqual(inf["listas_word"]["elementos"], 10)
        self.assertEqual(inf["listas_word"]["sin_convertir"],
                         [{"elementos": 5, "motivo": "elemento sin sangría francesa reconocida"}])
        segunda = [r["el"] for r in reg[5:10]]
        self.assertFalse([p for p in segunda if p.find(f"{W}pPr/{W}numPr") is not None])


class ListasDeWordPorDefecto(Base):
    """Decisión del 2026-11-07: listas de Word activadas por defecto, con opción para
    desactivarlas (posprocesar(listas=False), --sin-listas-word)."""

    def test_por_defecto_igual_que_pedirlas(self):
        for pdf in (G04, GDOCS / "g03-gdocs-listas.pdf"):
            with self.subTest(escrito=pdf.stem):
                _, por_defecto, inf = self.convertir(pdf)
                _, pedidas, inf_pedidas = self.convertir(pdf, listas=True)
                self.assertEqual(parte(por_defecto, "word/document.xml"), parte(pedidas, "word/document.xml"))
                self.assertEqual(parte(por_defecto, "word/numbering.xml"), parte(pedidas, "word/numbering.xml"))
                self.assertEqual(inf["listas_word"], inf_pedidas["listas_word"])

    def test_desactivadas(self):
        for pdf in (G04, GDOCS / "g03-gdocs-listas.pdf"):
            with self.subTest(escrito=pdf.stem):
                _, d2, inf = self.convertir(pdf, listas=False)
                self.assertNotIn("listas_word", inf)
                self.assertEqual(items_numpr(d2), [])

    def test_sin_listas_en_el_pdf_no_cambia_nada(self):
        # Ni los escritos ni los forenses tienen etiquetas de lista (test de falsos positivos): el
        # .docx es el mismo con y sin listas de Word.
        for pdf in (ESCRITOS / "e05-minuta.pdf", FORENSES / "f05-encabezamiento-francesas.pdf",
                    GDOCS / "g01-gdocs-tabuladores.pdf"):
            with self.subTest(escrito=pdf.stem):
                _, con, inf = self.convertir(pdf)
                _, sin, _ = self.convertir(pdf, listas=False)
                self.assertEqual(parte(con, "word/document.xml"), parte(sin, "word/document.xml"))
                # Sin elementos registrados, posprocesar no llega a listas_de_word ni lo informa.
                self.assertEqual(inf.get("listas_word", {}).get("elementos", 0), 0)

    def test_linea_de_comandos(self):
        import subprocess
        d1, _, _ = self.convertir(G04)
        for opcion, numpr in (([], 15), (["--sin-listas-word"], 0), (["--listas-word"], 15)):
            with self.subTest(opcion=opcion):
                salida = self.dir / f"cli{len(opcion)}{''.join(opcion)}.docx"
                subprocess.run([sys.executable, str(AQUI / "posproceso.py"), str(G04), str(d1), str(salida),
                                f"--informe={salida}.json", *opcion], check=True, cwd=AQUI,
                               env={**os.environ, "PYTHONPATH": str(AQUI / "sin_opencv")})
                self.assertEqual(len(items_numpr(salida)), numpr)


def parte(docx, nombre):
    import zipfile
    with zipfile.ZipFile(docx) as z:
        return z.read(nombre) if nombre in z.namelist() else None


if __name__ == "__main__":
    unittest.main()
