# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Regresión de la auditoría de Anonimizar (2026-10-07): caminos por los que el término
censurado podía seguir en el PDF entregado.

Cada caso genera su PDF sintético al vuelo (sin PDFs en el repo), lo censura con
redact.apply y comprueba lo que haría /v1/redact/apply con el informe:
- LeakTests: caminos por los que el término seguía recuperable. En la fase 1 no se
  entregaban (422); desde la fase 2 se limpian y se entregan (200) sin el término en
  ningún objeto descomprimido y con el texto de control intacto. Siguen en 422 el flujo
  de contenido no analizable, los rectángulos no válidos y las censuras fallidas.
- CorrectTests: casos que la auditoría dio por correctos -> se entregan (200), sin el
  término en ningún objeto descomprimido y sin perder el texto de control.
- VectorTests: trazos que toca una zona (REMOVE_IF_TOUCHED).

Uso: python -m unittest redact_leaks_test.py   (necesita PyMuPDF, como redact.py)
"""
import os
import shutil
import tempfile
import unittest

import pymupdf as fitz

import redact

TERM = "SECRETO7731"
CONTROL = "control"


def server_delivers(report):
    """Lo que decide server.js en /v1/redact/apply con el informe de redact.py: True si
    responde 200 con el documento. Mantener igual que server.js."""
    if not report or report.get("verified") is not True:
        return False  # 422 REDACT_NOT_VERIFIED
    if report.get("failed"):
        return False  # 422 REDACT_ITEMS_FAILED: falló alguna censura
    return True


def term_forms(term):
    t = term.encode()
    return [t, term.encode("utf-16-be"), t.hex().encode(), term.encode("utf-16-be").hex().encode()]


def full_dump(path):
    """Todos los objetos del PDF descomprimidos (también los de object streams) y el
    trailer: lo mismo que mostraría qpdf --qdf."""
    doc = fitz.open(path)
    parts = [doc.pdf_trailer().encode("latin-1", "replace")]
    for x in range(1, doc.xref_length()):
        parts.append(doc.xref_object(x, compressed=False).encode("latin-1", "replace"))
        if doc.xref_is_stream(x):
            parts.append(doc.xref_stream(x) or b"")
    doc.close()
    return b"\n".join(parts).lower()


def term_anywhere(path, term=TERM):
    raw = open(path, "rb").read().lower()
    dump = full_dump(path)
    return any(f.lower() in raw or f.lower() in dump for f in term_forms(term))


# ==========================================
# CONSTRUCCIÓN DE LOS PDF SINTÉTICOS
# ==========================================
def base_doc(pages=1):
    """Página con el término visible en (72, 100) y un texto de control que debe quedar."""
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"Nombre: {TERM} fin", fontsize=12)
        page.insert_text((72, 200), f"Texto de {CONTROL} que debe quedar", fontsize=12)
    return doc


def font_name(page):
    return page.get_fonts()[0][4]


def new_object(doc, source, stream=None):
    xref = doc.get_new_xref()
    doc.update_object(xref, source)
    if stream is not None:
        doc.update_stream(xref, stream)
    return xref


def append_contents(doc, page, *streams):
    """Añade flujos al final de /Contents de la página."""
    xrefs = [new_object(doc, "<<>>", s) for s in streams]
    refs = " ".join(f"{x} 0 R" for x in page.get_contents() + xrefs)
    doc.xref_set_key(page.xref, "Contents", f"[{refs}]")


def set_resource(doc, page, key, value):
    res = doc.xref_get_key(page.xref, "Resources")
    if res[0] == "xref":
        doc.xref_set_key(int(res[1].split()[0]), key, value)
    else:
        doc.xref_set_key(page.xref, f"Resources/{key}", value)


def term_pixmap(gray=False):
    """El término renderizado como imagen (600x120 px)."""
    src = fitz.open()
    page = src.new_page(width=300, height=60)
    page.insert_text((10, 38), TERM, fontsize=28)
    pix = page.get_pixmap(dpi=144, colorspace=fitz.csGRAY if gray else fitz.csRGB)
    src.close()
    return pix


IMG = fitz.Rect(72, 80, 372, 140)  # donde se dibuja la imagen
ZONE = fitz.Rect(120, 85, 330, 135)  # zona censurada, dentro de la imagen


def item(page, rect, text=TERM, category="Personalizado", id_="i"):
    return {"id": id_, "page": page, "text": text, "category": category, "rect": list(rect)}


def manual(page, rect):
    return item(page, rect, text="Zona manual", category=redact.MANUAL_CATEGORY, id_="m")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="redact-leaks-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def save(self, doc, name="in.pdf", **kwargs):
        path = os.path.join(self.dir, name)
        doc.save(path, **kwargs)
        doc.close()
        return path

    def search_items(self, path, pages=None, term=TERM):
        """Hallazgos como los que envía el frontend tras /v1/redact/search."""
        doc = fitz.open(path)
        redact.flatten_document(doc)
        items = [item(p.number, r, term, id_=f"{p.number}-{i}")
                 for p in doc if pages is None or p.number in pages
                 for i, r in enumerate(p.search_for(term))]
        doc.close()
        return items

    def apply(self, path, items=None):
        if items is None:
            items = self.search_items(path)
        out = os.path.join(self.dir, "out.pdf")
        if os.path.exists(out):
            os.remove(out)
        report = redact.apply(path, out, items)
        return report, out

    def assertBlocked(self, report, out):
        detail = "" if not os.path.exists(out) else (
            f" (término en la salida: {term_anywhere(out)})")
        self.assertFalse(server_delivers(report),
                         f"se entregaría (200): {report}{detail}")

    def assertDeliveredClean(self, report, out, control=True):
        self.assertTrue(server_delivers(report), f"no se entrega: {report}")
        self.assertFalse(term_anywhere(out), "el término sigue en la salida")
        self.assertEqual(report.get("text_loss_pages"), [])
        if control:
            doc = fitz.open(out)
            self.assertIn(CONTROL, "".join(p.get_text() for p in doc))
            doc.close()


# ==========================================
# FUGAS: SE ENTREGAN LIMPIAS (SALVO LAS QUE NO SE PUEDEN ARREGLAR)
# ==========================================
class LeakTests(Base):
    # --- 1. ' con el operando en otro flujo de /Contents, o flujo no analizable ---
    def _quote(self, *streams):
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 400), CONTROL, fontsize=12)
        append_contents(doc, page, *(s.replace(b"FN", font_name(page).encode()) for s in streams))
        return self.save(doc)

    def test_1_quote_operando_en_otro_flujo(self):
        path = self._quote(f"BT /FN 12 Tf 14 TL 72 742 Td (Nombre) Tj ({TERM}) ".encode(),
                           b"' (otra linea) ' ET")
        self.assertDeliveredClean(*self.apply(path))

    def test_1_quote_flujo_no_analizable(self):
        """Sigue sin entregarse: no se sabe dónde dejaría MuPDF el texto."""
        path = self._quote(f"BT /FN 12 Tf 14 TL 72 742 Td (Nombre) Tj ({TERM}) ' (otra linea) ' ET }}".encode())
        report, out = self.apply(path)
        self.assertBlocked(report, out)
        self.assertEqual(report["leaks"], ["content_syntax"])
        self.assertFalse(os.path.exists(out))

    # --- 2. Texto fuera del MediaBox o del CropBox ---
    def test_2_fuera_del_mediabox(self):
        doc = base_doc()
        append_contents(doc, doc[0], f"BT /{font_name(doc[0])} 12 Tf 72 900 Td ({TERM}) Tj ET".encode())
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_2_fuera_del_cropbox(self):
        doc = base_doc()
        append_contents(doc, doc[0], f"BT /{font_name(doc[0])} 12 Tf 72 30 Td ({TERM}) Tj ET".encode())
        doc[0].set_cropbox(fitz.Rect(0, 0, 595, 700))
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    # --- 3. Adjuntos por /AF ---
    def test_3_facturx_af_y_embeddedfiles_misma_filespec(self):
        doc = base_doc()
        ef = new_object(doc, "<</Type/EmbeddedFile/Subtype/text#2Fxml>>",
                        f"<Invoice><Buyer>{TERM}</Buyer></Invoice>".encode())
        fs = new_object(doc, f"<</Type/Filespec/F(factur-x.xml)/UF(factur-x.xml)"
                             f"/AFRelationship/Data/EF<</F {ef} 0 R/UF {ef} 0 R>>>>")
        doc.xref_set_key(doc.pdf_catalog(), "Names", f"<</EmbeddedFiles<</Names[(factur-x.xml) {fs} 0 R]>>>>")
        doc.xref_set_key(doc.pdf_catalog(), "AF", f"[{fs} 0 R]")
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_3_af_solo_en_el_catalogo(self):
        doc = base_doc()
        ef = new_object(doc, "<<>>", f"af {TERM}".encode())
        fs = new_object(doc, f"<</Type/Filespec/F(datos.bin)/EF<</F {ef} 0 R>>>>")
        doc.xref_set_key(doc.pdf_catalog(), "AF", f"[{fs} 0 R]")
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_3_af_en_la_pagina(self):
        doc = base_doc()
        ef = new_object(doc, "<<>>", f"pagina {TERM}".encode())
        fs = new_object(doc, f"<</Type/Filespec/F({TERM}.xml)/EF<</F {ef} 0 R>>>>")
        doc.xref_set_key(doc[0].xref, "AF", f"[{fs} 0 R]")
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    # --- 4. Capas OCG ---
    def test_4_texto_en_capa_off(self):
        doc = base_doc()
        off = doc.add_ocg("oculta", on=False)
        doc[0].insert_text((72, 650), f"Capa off {TERM}", fontsize=12, oc=off)
        report, out = self.apply(self.save(doc))
        self.assertDeliveredClean(report, out)
        # Sin relleno: nada nuevo se ve donde estaba el texto de la capa apagada, y el
        # resto de la capa sigue en ella.
        doc = fitz.open(out)
        self.assertEqual(doc[0].get_pixmap(clip=fitz.Rect(72, 630, 300, 660)).color_count(), 1)
        doc.close()
        self.assertIn("Capa off", self._all_layers_text(out))

    def _all_layers_text(self, path):
        doc, copy = redact.open_all_layers(path)
        try:
            return doc[0].get_text()
        finally:
            doc.close()
            if copy:
                os.remove(copy)

    def test_4_nombre_de_capa(self):
        doc = base_doc()
        doc.add_ocg(f"Capa {TERM}", on=True)
        doc.add_ocg("Otra", on=False)
        report, out = self.apply(self.save(doc))
        self.assertDeliveredClean(report, out)
        doc = fitz.open(out)
        self.assertEqual(sorted(o["name"] for o in doc.get_ocgs().values()), ["Capa 1", "Otra"])
        doc.close()

    # --- 5. Estructuras con texto libre ---
    def _structure(self, build):
        doc = base_doc()
        build(doc)
        return self.apply(self.save(doc))

    def test_5_names_dests(self):
        def build(doc):
            dest = new_object(doc, f"[{doc[0].xref} 0 R /XYZ 0 0 0]")
            doc.xref_set_key(doc.pdf_catalog(), "Names", f"<</Dests<</Names[({TERM}) {dest} 0 R]>>>>")
        self.assertDeliveredClean(*self._structure(build))

    def test_5_dests_conserva_los_neutros_y_el_marcador_su_pagina(self):
        doc = base_doc(pages=2)
        d0 = new_object(doc, f"[{doc[0].xref} 0 R /XYZ 0 0 0]")
        d1 = new_object(doc, f"[{doc[1].xref} 0 R /XYZ 0 0 0]")
        kids = [new_object(doc, f"<</Names[(a-neutro) {d0} 0 R]/Limits[(a-neutro)(a-neutro)]>>"),
                new_object(doc, f"<</Names[({TERM}) {d1} 0 R]/Limits[({TERM})({TERM})]>>")]
        doc.xref_set_key(doc.pdf_catalog(), "Names", f"<</Dests<</Kids[{kids[0]} 0 R {kids[1]} 0 R]>>>>")
        doc.set_toc([[1, "Neutro", 2]])
        outline = doc.get_toc(simple=False)[0][3]["xref"]
        doc.xref_set_key(outline, "Dest", f"({TERM})")
        report, out = self.apply(self.save(doc))
        self.assertDeliveredClean(report, out)
        doc = fitz.open(out)
        self.assertEqual(list(doc.resolve_names()), ["a-neutro"])
        self.assertEqual([e[:3] for e in doc.get_toc()], [[1, "Neutro", 2]])
        doc.close()

    def test_5_aa_quita_solo_las_entradas_con_el_termino(self):
        doc = base_doc()
        bad = new_object(doc, f"<</S/URI/URI(https://x.es/{TERM})>>")
        ok = new_object(doc, "<</S/URI/URI(https://x.es/neutro)>>")
        doc.xref_set_key(doc[0].xref, "AA", f"<</O {bad} 0 R/C {ok} 0 R>>")
        report, out = self.apply(self.save(doc))
        self.assertDeliveredClean(report, out)
        doc = fitz.open(out)
        aa = doc.xref_get_key(doc[0].xref, "AA")
        self.assertNotIn("/O", aa[1])
        self.assertIn("/C", aa[1])
        doc.close()

    def test_5_aa_de_pagina_uri(self):
        def build(doc):
            uri = new_object(doc, f"<</S/URI/URI(https://x.es/{TERM})>>")
            doc.xref_set_key(doc[0].xref, "AA", f"<</O {uri} 0 R>>")
        self.assertDeliveredClean(*self._structure(build))

    def test_5_openaction_uri(self):
        def build(doc):
            uri = new_object(doc, f"<</S/URI/URI(https://x.es/{TERM})>>")
            doc.xref_set_key(doc.pdf_catalog(), "OpenAction", f"{uri} 0 R")
        self.assertDeliveredClean(*self._structure(build))

    def test_5_pieceinfo(self):
        def build(doc):
            doc.xref_set_key(doc[0].xref, "PieceInfo", f"<</App<</Private({TERM})>>>>")
        self.assertDeliveredClean(*self._structure(build))

    def test_5_marcador_neutro_con_uri(self):
        def build(doc):
            doc.set_toc([[1, "Neutro", 1]])
            doc.set_toc_item(0, kind=fitz.LINK_URI, uri=f"https://x.es/{TERM}")
        report, out = self._structure(build)
        self.assertDeliveredClean(report, out)
        doc = fitz.open(out)
        self.assertEqual([e[1] for e in doc.get_toc()], ["Neutro"])  # el marcador se queda
        doc.close()

    def test_5_structtree_alt_actualtext(self):
        def build(doc):
            se = new_object(doc, f"<</Type/StructElem/S/Figure/Alt({TERM})/ActualText({TERM})>>")
            st = new_object(doc, f"<</Type/StructTreeRoot/K {se} 0 R>>")
            doc.xref_set_key(doc.pdf_catalog(), "StructTreeRoot", f"{st} 0 R")
        self.assertDeliveredClean(*self._structure(build))

    def test_5_structtree_en_utf16_hex(self):
        def build(doc):
            alt = "FEFF" + TERM.encode("utf-16-be").hex().upper()
            se = new_object(doc, f"<</Type/StructElem/S/Figure/Alt<{alt}>>>")
            st = new_object(doc, f"<</Type/StructTreeRoot/K {se} 0 R>>")
            doc.xref_set_key(doc.pdf_catalog(), "StructTreeRoot", f"{st} 0 R")
        self.assertDeliveredClean(*self._structure(build))

    def test_5_variante_con_guiones_y_espacios(self):
        def build(doc):
            doc.xref_set_key(doc[0].xref, "PieceInfo", "<</App<</Private(SECRETO 77-31)>>>>")
        self.assertDeliveredClean(*self._structure(build))

    def test_5_pagelabels(self):
        def build(doc):
            doc.set_page_labels([{"startpage": 0, "prefix": f"{TERM}-", "style": "D"}])
        self.assertDeliveredClean(*self._structure(build))

    def test_5_threads(self):
        def build(doc):
            bead = new_object(doc, "<<>>")
            thread = new_object(doc, f"<</Type/Thread/I<</Title({TERM})>>/F {bead} 0 R>>")
            doc.update_object(bead, f"<</T {thread} 0 R/P {doc[0].xref} 0 R/R[0 0 10 10]/N {bead} 0 R/V {bead} 0 R>>")
            doc.xref_set_key(doc.pdf_catalog(), "Threads", f"[{thread} 0 R]")
        self.assertDeliveredClean(*self._structure(build))

    # --- 7. Rectángulos inválidos ---
    def _bad_rect(self, transform):
        path = self.save(base_doc())
        items = self.search_items(path)
        r = items[0]["rect"]
        items[0]["rect"] = transform(r)
        return self.apply(path, items)

    def test_7_rect_invertido(self):
        self.assertBlocked(*self._bad_rect(lambda r: [r[2], r[3], r[0], r[1]]))

    def test_7_rect_altura_cero(self):
        self.assertBlocked(*self._bad_rect(lambda r: [r[0], r[1], r[2], r[1]]))

    def test_7_rect_fuera_de_la_pagina(self):
        self.assertBlocked(*self._bad_rect(lambda r: [700, 900, 750, 950]))

    # --- 8. Censuras fallidas ---
    def test_8_una_fallida_y_una_buena(self):
        path = self.save(base_doc())
        items = self.search_items(path) + [item(9, [72, 90, 200, 110], id_="mala")]
        self.assertBlocked(*self.apply(path, items))

    def test_8_pagina_negativa(self):
        path = self.save(base_doc(pages=2))
        items = self.search_items(path, pages={0})
        items.append(dict(items[0], id="neg", page=-1))  # -1 no es "la última página"
        self.assertBlocked(*self.apply(path, items))

    # --- Decisiones de diseño: una aparición visible desmarcada + una oculta ---
    def test_desmarcada_visible_mas_oculta(self):
        """La visible desmarcada se queda; la oculta se censura sola."""
        doc = base_doc(pages=2)
        append_contents(doc, doc[1], f"BT /{font_name(doc[1])} 12 Tf 72 30 Td ({TERM}) Tj ET".encode())
        doc[1].set_cropbox(fitz.Rect(0, 0, 595, 700))
        path = self.save(doc)
        # Se censura la página 0 y se desmarca la aparición visible de la 1.
        report, out = self.apply(path, self.search_items(path, pages={0}))
        self.assertTrue(server_delivers(report), report)
        self.assertEqual(report.get("text_loss_pages"), [])
        doc = fitz.open(out)
        self.assertNotIn(TERM, doc[0].get_text())
        self.assertIn(TERM, doc[1].get_text(clip=doc[1].rect))
        self.assertNotIn(TERM, redact.hidden_page_text(doc[1], doc[1]))
        doc.close()


# ==========================================
# CORRECTOS: SE ENTREGAN CON EL RESULTADO DE SIEMPRE
# ==========================================
class CorrectTests(Base):
    def test_tj_basico(self):
        self.assertDeliveredClean(*self.apply(self.save(base_doc())))

    def test_pagina_girada(self):
        """Los hallazgos llegan sin girar (search_for): no se dan por fuera de la página."""
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((400, 800), f"{TERM} abajo", fontsize=12)
        page.insert_text((72, 200), CONTROL, fontsize=12)
        page.set_rotation(90)
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_cropbox_desplazado(self):
        doc = base_doc()
        doc[0].set_cropbox(fitz.Rect(50, 50, 545, 792))
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_quote_y_dobles_comillas_en_un_flujo(self):
        doc = base_doc()
        append_contents(doc, doc[0], f"BT /{font_name(doc[0])} 12 Tf 14 TL 72 300 Td (linea) ' 1 0 ({TERM} q1) \" ET".encode())
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_texto_partido_y_tj(self):
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 200), CONTROL, fontsize=12)
        append_contents(doc, page, f"BT /{font_name(page)} 12 Tf 72 742 Td (SECR) Tj (ETO) Tj [(77) -20 (31)] TJ ET".encode())
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_capa_ocr_invisible(self):
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), f"Nombre {TERM}", fontsize=12, render_mode=3)
        page.insert_text((72, 200), CONTROL, fontsize=12)
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_actualtext(self):
        doc = base_doc()
        append_contents(doc, doc[0], f"/Span <</ActualText ({TERM})>> BDC BT /{font_name(doc[0])} 12 Tf 72 300 Td (XXXX) Tj ET EMC".encode())
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_type3_sin_tounicode(self):
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 300), CONTROL, fontsize=12)
        chars = sorted(set(TERM))
        procs = {c: new_object(doc, "<<>>", b"600 0 0 0 500 700 d1 0 0 500 700 re f") for c in chars}
        first, last = ord(chars[0]), ord(chars[-1])
        font = new_object(doc, (
            "<</Type/Font/Subtype/Type3/FontBBox[0 0 600 700]/FontMatrix[0.001 0 0 0.001 0 0]"
            f"/CharProcs<<{' '.join(f'/g{ord(c)} {x} 0 R' for c, x in procs.items())}>>"
            f"/Encoding<</Type/Encoding/Differences[{' '.join(f'{ord(c)} /g{ord(c)}' for c in chars)}]>>"
            f"/FirstChar {first}/LastChar {last}/Widths[{' '.join('600' for _ in range(first, last + 1))}]"
            "/Resources<<>>>>"))
        set_resource(doc, page, "Font", f"<</F3 {font} 0 R /{font_name(page)} {page.get_fonts()[0][0]} 0 R>>")
        append_contents(doc, page, f"BT /F3 20 Tf 72 720 Td ({TERM}) Tj ET".encode())
        path = self.save(doc)
        self.assertDeliveredClean(*self.apply(path, [manual(0, fitz.Rect(60, 105, 400, 130))]))

    def test_xobject_en_dos_paginas_ambas(self):
        doc = fitz.open()
        src = fitz.open()
        src.new_page(width=300, height=50).insert_text((10, 30), f"Ref {TERM}", fontsize=12)
        for _ in range(2):
            page = doc.new_page()
            page.show_pdf_page(fitz.Rect(72, 80, 372, 130), src, 0)
            page.insert_text((72, 300), CONTROL, fontsize=12)
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_desmarcada_visible_sola(self):
        """Decisión de diseño: una aparición visible que el usuario desmarca se entrega."""
        path = self.save(base_doc(pages=2))
        report, out = self.apply(path, self.search_items(path, pages={0}))
        self.assertTrue(server_delivers(report), report)
        doc = fitz.open(out)
        self.assertNotIn(TERM, doc[0].get_text())
        self.assertIn(TERM, doc[1].get_text())
        doc.close()

    def test_pagina_girada_desmarcada_abajo(self):
        """Página apaisada (girada 90): una aparición visible abajo que el usuario desmarca
        no es texto oculto (antes, con clip=page.rect sin girar, lo parecía: 422)."""
        doc = fitz.open()
        for _ in range(2):
            page = doc.new_page()
            page.insert_text((400, 800), f"{TERM} abajo", fontsize=12)
            page.insert_text((72, 200), CONTROL, fontsize=12)
            page.set_rotation(90)
        path = self.save(doc)
        report, out = self.apply(path, self.search_items(path, pages={0}))
        self.assertTrue(server_delivers(report), report)
        doc = fitz.open(out)
        self.assertNotIn(TERM, doc[0].get_text())
        self.assertIn(TERM, doc[1].get_text())
        doc.close()

    def test_xobject_en_dos_paginas_solo_la_primera(self):
        doc = fitz.open()
        src = fitz.open()
        src.new_page(width=300, height=50).insert_text((10, 30), f"Ref {TERM}", fontsize=12)
        for _ in range(2):
            page = doc.new_page()
            page.show_pdf_page(fitz.Rect(72, 80, 372, 130), src, 0)
        path = self.save(doc)
        report, out = self.apply(path, self.search_items(path, pages={0}))
        self.assertTrue(server_delivers(report), report)
        doc = fitz.open(out)
        self.assertNotIn(TERM, doc[0].get_text())
        self.assertIn(TERM, doc[1].get_text())
        doc.close()

    def test_anotaciones_freetext_nota_oculta(self):
        doc = base_doc()
        page = doc[0]
        page.add_freetext_annot(fitz.Rect(72, 400, 300, 430), f"Nota {TERM}")
        page.add_text_annot((400, 400), f"comentario {TERM}")
        hidden = page.add_freetext_annot(fitz.Rect(72, 450, 300, 480), f"Oculta {TERM}")
        hidden.set_flags(fitz.PDF_ANNOT_IS_HIDDEN)
        hidden.update()
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_formulario_v_dv_nombre_xfa(self):
        doc = base_doc()
        page = doc[0]
        for name, value, y in (("dni", TERM, 500), (f"campo_{TERM}", "neutro", 540)):
            w = fitz.Widget()
            w.field_name, w.field_type = name, fitz.PDF_WIDGET_TYPE_TEXT
            w.rect, w.field_value = fitz.Rect(72, y, 300, y + 20), value
            page.add_widget(w)
        for w in page.widgets():
            if w.field_name == "dni":
                doc.xref_set_key(w.xref, "DV", f"({TERM})")
        xfa = new_object(doc, "<<>>", f"<xfa><dni>{TERM}</dni></xfa>".encode())
        doc.xref_set_key(doc.pdf_catalog(), "AcroForm/XFA", f"{xfa} 0 R")
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_javascript_y_enlaces_de_pagina(self):
        doc = base_doc()
        js = new_object(doc, f"<</S/JavaScript/JS(app.alert('{TERM}'))>>")
        doc.xref_set_key(doc.pdf_catalog(), "OpenAction", f"{js} 0 R")
        js2 = new_object(doc, f"<</S/JavaScript/JS(var a='{TERM}';)>>")
        doc.xref_set_key(doc.pdf_catalog(), "Names", f"<</JavaScript<</Names[(doc) {js2} 0 R]>>>>")
        doc[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 600, 200, 620), "uri": f"https://x.es/?dni={TERM}"})
        doc[0].insert_link({"kind": fitz.LINK_LAUNCH, "from": fitz.Rect(72, 630, 200, 650), "file": f"{TERM}.pdf"})
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_info_xmp_metadata_de_pagina(self):
        doc = base_doc()
        doc.set_metadata({"author": TERM, "title": f"Exp {TERM}"})
        info = int(doc.xref_get_key(-1, "Info")[1].split()[0])
        doc.xref_set_key(info, "Empresa", f"({TERM})")
        doc.set_xml_metadata(f"<x:xmpmeta xmlns:x='adobe:ns:meta/'><dc>{TERM}</dc></x:xmpmeta>")
        meta = new_object(doc, "<</Type/Metadata/Subtype/XML>>", f"<x>{TERM}</x>".encode())
        doc.xref_set_key(doc[0].xref, "Metadata", f"{meta} 0 R")
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_marcador_con_el_termino(self):
        doc = base_doc()
        doc.set_toc([[1, f"Parte {TERM}", 1], [2, "hijo", 1]])
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_adjuntos_y_fileattachment(self):
        doc = base_doc()
        doc.embfile_add(f"{TERM}.txt", f"dato {TERM}".encode(), filename=f"{TERM}.txt", desc=TERM)
        doc[0].add_file_annot((400, 300), f"adj {TERM}".encode(), f"f_{TERM}.txt", desc=TERM)
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_miniatura(self):
        doc = base_doc()
        thumb = new_object(doc, "<</Type/XObject/Subtype/Image/Width 11/Height 1/ColorSpace/DeviceGray/BitsPerComponent 8>>",
                           TERM.encode())
        doc.xref_set_key(doc[0].xref, "Thumb", f"{thumb} 0 R")
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_redact_pendiente_de_acrobat(self):
        doc = base_doc()
        doc[0].add_redact_annot(doc[0].search_for(TERM)[0], text=TERM)
        self.assertDeliveredClean(*self.apply(self.save(doc), []))

    # --- Imágenes: bajo la zona queda un único valor de píxel ---
    def assertZoneWiped(self, out, page_no=0, zone=ZONE):
        doc = fitz.open(out)
        page = doc[page_no]
        checked = 0
        for info in page.get_image_info(xrefs=True):
            box = fitz.Rect(info["bbox"])
            inter = box & zone
            if inter.is_empty:
                continue
            xrefs = [info["xref"]]
            smask = doc.xref_get_key(info["xref"], "SMask")[1]
            if smask.endswith(" 0 R"):
                xrefs.append(int(smask.split()[0]))
            for x in xrefs:
                pix = fitz.Pixmap(doc, x)
                sx, sy = pix.width / box.width, pix.height / box.height
                values = {pix.pixel(px, py)
                          for py in range(int((inter.y0 - box.y0) * sy) + 2, int((inter.y1 - box.y0) * sy) - 2, 2)
                          for px in range(int((inter.x0 - box.x0) * sx) + 2, int((inter.x1 - box.x0) * sx) - 2, 2)}
                self.assertEqual(len(values), 1, f"imagen {x}: {len(values)} valores bajo la zona")
                checked += 1
        doc.close()
        return checked

    def _image_page(self):
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 300), CONTROL, fontsize=12)
        return doc, page

    def test_imagen_jpeg(self):
        doc, page = self._image_page()
        page.insert_image(IMG, stream=term_pixmap().tobytes("jpg"))
        report, out = self.apply(self.save(doc), [manual(0, ZONE)])
        self.assertDeliveredClean(report, out)
        self.assertEqual(self.assertZoneWiped(out), 1)

    def test_imagen_smask(self):
        doc, page = self._image_page()
        gray = term_pixmap(gray=True)
        black = fitz.Pixmap(fitz.csRGB, gray.irect, False)
        black.clear_with(0)
        img = fitz.Pixmap(black, 1)
        img.set_alpha(bytes(255 - b for b in gray.samples))
        page.insert_image(IMG, pixmap=img)
        report, out = self.apply(self.save(doc), [manual(0, ZONE)])
        self.assertDeliveredClean(report, out)
        self.assertEqual(self.assertZoneWiped(out), 2)  # color y SMask

    def test_imagen_imagemask_1bit(self):
        doc, page = self._image_page()
        gray = term_pixmap(gray=True)
        w, h = gray.width, gray.height
        stride = (w + 7) // 8
        bits = bytearray(stride * h)
        for y in range(h):
            for x in range(w):
                if gray.pixel(x, y)[0] < 128:
                    bits[y * stride + x // 8] |= 0x80 >> (x % 8)
        im = new_object(doc, f"<</Type/XObject/Subtype/Image/Width {w}/Height {h}/ImageMask true/BitsPerComponent 1/Decode[1 0]>>",
                        bytes(bits))
        set_resource(doc, page, "XObject", f"<</Im9 {im} 0 R>>")
        append_contents(doc, page, b"q 0 g 300 0 0 60 72 702 cm /Im9 Do Q")
        report, out = self.apply(self.save(doc), [manual(0, ZONE)])
        self.assertDeliveredClean(report, out)
        self.assertEqual(self.assertZoneWiped(out), 1)

    def test_imagen_en_linea(self):
        doc, page = self._image_page()
        gray = term_pixmap(gray=True)
        append_contents(doc, page, (f"q 300 0 0 60 72 702 cm BI /W {gray.width} /H {gray.height} /CS /G /BPC 8 /F /AHx ID\n").encode()
                        + gray.samples.hex().upper().encode() + b">\nEI Q")
        report, out = self.apply(self.save(doc), [manual(0, ZONE)])
        self.assertDeliveredClean(report, out)
        out_doc = fitz.open(out)
        blocks = [b for b in out_doc[0].get_text("dict")["blocks"] if b["type"] == 1]
        self.assertEqual(len(blocks), 1)
        pix, box = fitz.Pixmap(blocks[0]["image"]), fitz.Rect(blocks[0]["bbox"])
        sx, sy = pix.width / box.width, pix.height / box.height
        values = {pix.pixel(x, y)
                  for y in range(int((ZONE.y0 - box.y0) * sy) + 2, int((ZONE.y1 - box.y0) * sy) - 2, 2)
                  for x in range(int((ZONE.x0 - box.x0) * sx) + 2, int((ZONE.x1 - box.x0) * sx) - 2, 2)}
        out_doc.close()
        self.assertEqual(len(values), 1)

    def test_imagen_en_xobject_compartido(self):
        doc = fitz.open()
        src = fitz.open()
        sp = src.new_page(width=300, height=60)
        sp.insert_image(sp.rect, stream=term_pixmap().tobytes("png"))
        for _ in range(2):
            page = doc.new_page()
            page.show_pdf_page(IMG, src, 0)
            page.insert_text((72, 300), CONTROL, fontsize=12)
        report, out = self.apply(self.save(doc), [manual(0, ZONE)])
        self.assertTrue(server_delivers(report), report)
        self.assertEqual(self.assertZoneWiped(out, 0), 1)
        # La página 1 no se censuró: conserva su imagen.
        out_doc = fitz.open(out)
        self.assertEqual(len(out_doc[1].get_images()), 1)
        out_doc.close()

    # --- Patrón con texto ---
    def _pattern_doc(self):
        doc, page = self._image_page()
        helv = new_object(doc, "<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
        pat = new_object(doc, f"<</Type/Pattern/PatternType 1/PaintType 1/TilingType 1/BBox[0 0 300 40]"
                              f"/XStep 300/YStep 40/Resources<</Font<</H {helv} 0 R>>>>>>",
                         f"BT /H 14 Tf 5 15 Td ({TERM}) Tj ET".encode())
        set_resource(doc, page, "Pattern", f"<</P1 {pat} 0 R>>")
        append_contents(doc, page, b"q /Pattern cs /P1 scn 72 702 300 40 re f Q")
        return self.save(doc)

    def test_patron_cubierto_entero(self):
        self.assertDeliveredClean(*self.apply(self._pattern_doc(), [manual(0, fitz.Rect(72, 100, 372, 140))]))

    def test_patron_cubierto_en_parte_se_quita_entero(self):
        """El relleno con patrón es un trazo: con REMOVE_IF_TOUCHED (fase 2) se quita entero
        si la zona lo roza, también la parte de fuera, que se avisa como texto perdido.
        Antes quedaba con el término dentro del patrón y no se entregaba (422)."""
        report, out = self.apply(self._pattern_doc(), [manual(0, fitz.Rect(72, 100, 250, 140))])
        self.assertTrue(server_delivers(report), report)
        self.assertFalse(term_anywhere(out))
        self.assertEqual(report.get("text_loss_pages"), [1])
        doc = fitz.open(out)
        self.assertIn(CONTROL, doc[0].get_text())
        doc.close()

    # --- Revisiones incrementales, huérfanos y object streams ---
    def test_revision_incremental(self):
        path = os.path.join(self.dir, "in.pdf")
        doc = base_doc()
        doc.set_metadata({"author": TERM})
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        doc[0].add_redact_annot(doc[0].search_for(TERM)[0])
        doc[0].apply_redactions()
        doc.set_metadata({"author": "otro"})
        doc.save(path, incremental=True, encryption=0)
        doc.close()
        self.assertIn(TERM.encode(), open(path, "rb").read())  # la revisión antigua está en la entrada
        self.assertDeliveredClean(*self.apply(path, []))

    def test_huerfano_y_object_streams(self):
        doc = base_doc()
        new_object(doc, f"<</Huerfano ({TERM})>>")
        self.assertDeliveredClean(*self.apply(self.save(doc, use_objstms=1, garbage=0)))

    def test_busqueda_cruza_saltos_de_linea(self):
        import json
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "Demandante: Juan", fontsize=12)
        page.insert_text((72, 115), "Perez Gomez", fontsize=12)
        path = self.save(doc)
        patterns = os.path.join(self.dir, "p.json")
        results = os.path.join(self.dir, "r.json")
        with open(patterns, "w", encoding="utf-8") as f:
            json.dump({"Nombre": r"Juan\s+Perez"}, f)
        redact.search(path, patterns, results)
        with open(results, encoding="utf-8") as f:
            rects = [fitz.Rect(r["rect"]) for r in json.load(f)["results"]]
        self.assertEqual(len(rects), 2)  # "Juan" en una línea y "Perez" en la siguiente
        self.assertEqual(sorted(round(r.y0) // 10 for r in rects), [8, 10])  # líneas en y=100 y y=115


# ==========================================
# TRAZOS VECTORIALES
# ==========================================
class VectorTests(Base):
    def test_6_firma_vectorial_cubierta_en_parte(self):
        """Hallazgo 6: un trazo que entra y sale de la zona quedaba entero bajo el negro.
        Desde la fase 2 (REMOVE_IF_TOUCHED) se quita entero."""
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 300), CONTROL, fontsize=12)
        shape = page.new_shape()
        shape.draw_polyline([fitz.Point(60 + i * 8, 110 + (15 if i % 2 else -15)) for i in range(40)])
        shape.finish(color=(0, 0, 1), width=1.5)
        shape.commit()
        zone = fitz.Rect(100, 90, 300, 130)
        report, out = self.apply(self.save(doc), [manual(0, zone)])
        out_doc = fitz.open(out) if os.path.exists(out) else None
        inside = 0 if out_doc is None else sum(
            1 for d in out_doc[0].get_drawings() if d.get("color") == (0.0, 0.0, 1.0)
            for it in d["items"] for p in it[1:] if isinstance(p, fitz.Point) and zone.contains(p))
        self.assertFalse(server_delivers(report) and inside > 0, f"{inside} puntos del trazo bajo la zona")

    # --- Rellenos lisos que toca una zona: se repintan debajo de todo ---
    def _rect(self, page, rect, fill, opacity=1):
        sh = page.new_shape()
        sh.draw_rect(rect)
        sh.finish(fill=fill, color=None, fill_opacity=opacity)
        sh.commit()

    def _pixel(self, out, x, y, page_no=0):
        """Color renderizado (RGB, 72 ppp) en un punto de la página."""
        doc = fitz.open(out)
        pix = doc[page_no].get_pixmap(dpi=72)
        doc.close()
        return pix.pixel(int(x), int(y))

    def assertColor(self, actual, expected):
        self.assertTrue(all(abs(a - e) <= 2 for a, e in zip(actual, expected)), f"{actual} != {expected}")

    def _redact_term(self, doc):
        report, out = self.apply(self.save(doc))
        self.assertDeliveredClean(report, out)
        return out

    def test_fondo_de_pagina_completa_se_conserva(self):
        doc = base_doc()
        page = doc[0]
        # El fondo, dibujado antes que el texto (como en un PDF real).
        bg = new_object(doc, "<<>>", b"0.85 0.9 1 rg 0 0 595 842 re f")
        refs = " ".join(f"{x} 0 R" for x in [bg] + page.get_contents())
        doc.xref_set_key(page.xref, "Contents", f"[{refs}]")
        out = self._redact_term(doc)
        self.assertColor(self._pixel(out, 400, 600), (217, 230, 255))
        self.assertEqual(self._pixel(out, 150, 96), (0, 0, 0))  # el negro de la censura, encima

    def test_celda_sombreada_se_conserva(self):
        doc = fitz.open()
        page = doc.new_page()
        self._rect(page, fitz.Rect(60, 85, 300, 110), (0.8, 0.8, 0.8))  # celda que toca la zona
        page.insert_text((72, 100), f"Nombre: {TERM}", fontsize=12)
        page.insert_text((72, 200), f"Texto de {CONTROL}", fontsize=12)
        out = self._redact_term(doc)
        self.assertColor(self._pixel(out, 280, 98), (204, 204, 204))

    def test_texto_blanco_sobre_franja_oscura_sigue_visible(self):
        doc = fitz.open()
        page = doc.new_page()
        self._rect(page, fitz.Rect(0, 80, 595, 110), (0.1, 0.1, 0.3))  # franja que toca la zona
        page.insert_text((72, 100), f"Nombre: {TERM}", fontsize=12, color=(1, 1, 1))
        page.insert_text((300, 100), "Blanco visible", fontsize=12, color=(1, 1, 1))
        page.insert_text((72, 200), f"Texto de {CONTROL}", fontsize=12)
        out = self._redact_term(doc)
        doc = fitz.open(out)
        pix = doc[0].get_pixmap(dpi=144, clip=fitz.Rect(300, 88, 380, 104))
        doc.close()
        colors = {pix.pixel(x, y) for x in range(pix.width) for y in range(pix.height)}
        self.assertIn((255, 255, 255), colors)  # el texto blanco
        self.assertIn((26, 26, 77), colors)  # sobre la franja oscura

    def test_firma_vectorial_sobre_fondo(self):
        """Con fondo, la firma tocada se sigue quitando y el fondo se queda."""
        doc = fitz.open()
        page = doc.new_page()
        self._rect(page, page.rect, (0.95, 0.95, 0.8))
        page.insert_text((72, 300), CONTROL, fontsize=12)
        sh = page.new_shape()
        sh.draw_polyline([fitz.Point(60 + i * 8, 110 + (15 if i % 2 else -15)) for i in range(40)])
        sh.finish(color=(0, 0, 1), width=1.5)
        sh.commit()
        report, out = self.apply(self.save(doc), [manual(0, fitz.Rect(100, 90, 300, 130))])
        self.assertTrue(server_delivers(report), report)
        out_doc = fitz.open(out)
        self.assertFalse([d for d in out_doc[0].get_drawings() if d.get("color") == (0.0, 0.0, 1.0)])
        out_doc.close()
        self.assertColor(self._pixel(out, 500, 600), (242, 242, 204))

    def test_relleno_recortado_solo_dentro_del_recorte(self):
        doc = base_doc()
        # Antes que el texto. Recorte en coordenadas PDF (origen abajo): y 732-757 = 85-110.
        page = doc[0]
        bg = new_object(doc, "<<>>", b"q 60 732 300 25 re W n 0 0.5 0 rg 0 0 595 842 re f Q")
        refs = " ".join(f"{x} 0 R" for x in [bg] + page.get_contents())
        doc.xref_set_key(page.xref, "Contents", f"[{refs}]")
        out = self._redact_term(doc)
        self.assertColor(self._pixel(out, 340, 100), (0, 128, 0))  # dentro del recorte
        self.assertEqual(self._pixel(out, 400, 600), (255, 255, 255))  # fuera: sin pintar

    def test_pagina_girada_con_fondo(self):
        doc = fitz.open()
        page = doc.new_page()
        self._rect(page, fitz.Rect(0, 0, 595, 842), (0.85, 0.9, 1))
        page.insert_text((400, 800), f"{TERM} abajo", fontsize=12)
        page.insert_text((72, 200), CONTROL, fontsize=12)
        page.set_rotation(90)
        out = self._redact_term(doc)
        self.assertColor(self._pixel(out, 100, 100), (217, 230, 255))
        self.assertColor(self._pixel(out, 700, 500), (217, 230, 255))

    # --- Censura falsa: un relleno que tapaba algo se repinta encima ---
    def _render(self, path, clip):
        doc = fitz.open(path)
        pix = doc[0].get_pixmap(dpi=144, clip=clip)
        doc.close()
        return pix

    def test_censura_falsa_rozada_sigue_tapando(self):
        """Un texto tapado con un rectángulo negro que la zona roza sigue sin verse."""
        doc = base_doc()
        page = doc[0]
        page.insert_text((250, 100), "DNI 12345678Z", fontsize=12)
        self._rect(page, fitz.Rect(240, 85, 360, 110), (0, 0, 0))
        path = self.save(doc)
        report, out = self.apply(path, [manual(0, fitz.Rect(70, 85, 245, 110))])
        self.assertTrue(server_delivers(report), report)
        box = fitz.Rect(240, 85, 360, 110)
        pix = self._render(out, box)
        self.assertEqual({pix.pixel(x, y) for x in range(pix.width) for y in range(pix.height)}, {(0, 0, 0)})
        self.assertEqual(self._render(path, box).samples, pix.samples)

    def test_censura_falsa_con_opacidad_igual_que_el_original(self):
        """Con su color y su opacidad: la parte fuera de la zona se ve como en el original."""
        doc = base_doc()
        page = doc[0]
        page.insert_text((250, 100), "dato", fontsize=12)
        self._rect(page, fitz.Rect(240, 85, 320, 110), (1, 1, 0), opacity=0.6)
        path = self.save(doc)
        report, out = self.apply(path, [manual(0, fitz.Rect(70, 85, 245, 110))])
        self.assertTrue(server_delivers(report), report)
        outside = fitz.Rect(246, 85, 320, 110)
        self.assertEqual(self._render(path, outside).samples, self._render(out, outside).samples)
        self.assertEqual(self._pixel(out, 242, 97), (0, 0, 0))  # el negro de la zona, encima

    def test_censura_falsa_con_algo_dibujado_despues(self):
        """Lo que se le dibujaba encima queda tapado: el documento nunca queda menos tapado."""
        doc = base_doc()
        page = doc[0]
        page.insert_text((250, 100), "DNI 12345678Z", fontsize=12)
        self._rect(page, fitz.Rect(240, 85, 360, 110), (0, 0, 0))
        page.insert_text((300, 100), "etiqueta", fontsize=8, color=(1, 1, 1))  # encima del negro
        report, out = self.apply(self.save(doc), [manual(0, fitz.Rect(70, 85, 245, 110))])
        self.assertTrue(server_delivers(report), report)
        pix = self._render(out, fitz.Rect(240, 85, 360, 110))
        self.assertEqual({pix.pixel(x, y) for x in range(pix.width) for y in range(pix.height)}, {(0, 0, 0)})


class AnidamientoProfundo(Base):
    """Cadenas de referencias más largas que MAX_NESTING (p. ej. /PieceInfo -> [1 0 R] ->
    [2 0 R] -> ... -> (término)): _walk_strings no las recorre enteras, así que no puede
    limpiarlas. La censura no se da por verificada (422) y no hay salida; nunca se salta en
    silencio. Con el recorrido recursivo, unos 1.000 niveles daban RecursionError (500)."""

    def _chain(self, depth, term=TERM):
        doc = base_doc()
        last = new_object(doc, f"[({term})]")
        for _ in range(depth - 1):
            last = new_object(doc, f"[{last} 0 R]")
        doc.xref_set_key(doc[0].xref, "PieceInfo", f"<</App {last} 0 R>>")
        return self.apply(self.save(doc))

    def test_termino_mas_hondo_que_el_limite_no_se_entrega(self):
        for depth in (redact.MAX_NESTING + 50,):
            with self.subTest(depth=depth):
                report, out = self._chain(depth)
                self.assertBlocked(report, out)
                self.assertEqual(report["leaks"], ["nesting_depth"])
                self.assertFalse(os.path.exists(out))

    def test_sin_termino_mas_hondo_que_el_limite_tampoco(self):
        """No se sabe qué hay más abajo sin recorrerlo: falla igual, aunque no haya término."""
        report, out = self._chain(redact.MAX_NESTING + 50, term="nada que ver")
        self.assertBlocked(report, out)
        self.assertEqual(report["leaks"], ["nesting_depth"])

    def test_dentro_del_limite_se_limpia_y_se_entrega(self):
        self.assertDeliveredClean(*self._chain(redact.MAX_NESTING - 10))

    def test_mas_hondo_que_la_pila_de_python_se_limpia_y_se_entrega(self):
        """3.000 niveles: con el recorrido recursivo, RecursionError (500); con el de pila
        propia, dentro del tope: se limpia y se entrega."""
        self.assertDeliveredClean(*self._chain(3000))


class EstructurasLargasNormales(Base):
    """Listas enlazadas y árboles grandes que tiene un expediente normal (índices de cientos de
    marcadores, artículos, PDF etiquetados, árboles de nombres): nunca un 422 por anidamiento.

    2a-2c no pasan por _walk_strings (los títulos los mira remove_outline_terms con get_toc y
    /Alt y /ActualText se leen objeto a objeto): pasaban ya con MAX_NESTING = 200. Las que sí
    lo recorren, y fallaban con 200: un marcador que abre un artículo (/S /Thread: hilo -> primera
    cuenta -> /N -> /N...) de 300 cuentas (422 nesting_depth), y un árbol de /Names /Dests
    degenerado de 1.500 niveles (RecursionError en _clean_named_dests: 500)."""

    def test_2a_mil_marcadores_planos_con_el_termino_en_el_ultimo(self):
        doc = base_doc()
        doc.set_toc([[1, f"Documento {i}", 1] for i in range(999)] + [[1, f"Anexo {TERM}", 1]])
        report, out = self.apply(self.save(doc))
        self.assertDeliveredClean(report, out)
        self.assertEqual(len(fitz.open(out).get_toc()), 999)

    def test_2b_quinientos_marcadores_anidados_a_diez_niveles(self):
        doc = base_doc()
        doc.set_toc([[i % 10 + 1, f"Sección {i}" if i < 499 else f"Sección {TERM}", 1] for i in range(500)])
        report, out = self.apply(self.save(doc))
        self.assertDeliveredClean(report, out)
        self.assertEqual(len(fitz.open(out).get_toc()), 499)

    def test_2c_etiquetado_de_300_paginas_con_el_termino_en_alt_y_actualtext(self):
        doc = base_doc(pages=300)
        root, docel = new_object(doc, "<<>>"), new_object(doc, "<<>>")
        kids = []
        for page in doc:
            text = f"/Alt(Foto de {TERM})/ActualText({TERM})" if page.number == 299 else f"/Alt(Figura {page.number})"
            kids.append(new_object(doc, f"<</Type/StructElem/S/Figure/P {docel} 0 R/Pg {page.xref} 0 R{text}>>"))
        doc.update_object(docel, f"<</Type/StructElem/S/Document/P {root} 0 R/K[{' '.join(f'{k} 0 R' for k in kids)}]>>")
        doc.update_object(root, f"<</Type/StructTreeRoot/K {docel} 0 R>>")
        doc.xref_set_key(doc.pdf_catalog(), "StructTreeRoot", f"{root} 0 R")
        doc.xref_set_key(doc.pdf_catalog(), "MarkInfo", "<</Marked true>>")
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_marcador_que_abre_un_articulo_de_300_cuentas(self):
        doc = base_doc(pages=300)
        doc.set_toc([[1, "Leer el artículo", 1], [1, "Anexo", 1]])
        thread = new_object(doc, "<<>>")
        beads = [new_object(doc, "<<>>") for _ in range(300)]
        for i, bead in enumerate(beads):
            doc.update_object(bead, f"<</T {thread} 0 R/P {doc[i].xref} 0 R/R[50 50 500 700]"
                                    f"/N {beads[(i + 1) % 300]} 0 R/V {beads[i - 1]} 0 R>>")
            doc.xref_set_key(doc[i].xref, "B", f"[{bead} 0 R]")
        doc.update_object(thread, f"<</Type/Thread/I<</Title(Crónica)>>/F {beads[0]} 0 R>>")
        doc.xref_set_key(doc.pdf_catalog(), "Threads", f"[{thread} 0 R]")
        item_xref = doc.get_toc(simple=False)[0][3]["xref"]
        doc.xref_set_key(item_xref, "A", f"<</S/Thread/D {thread} 0 R/B {beads[0]} 0 R>>")
        doc.xref_set_key(item_xref, "Dest", "null")
        self.assertDeliveredClean(*self.apply(self.save(doc)))

    def test_arbol_de_nombres_degenerado_de_1500_niveles(self):
        doc = base_doc()
        dest = new_object(doc, f"[{doc[0].xref} 0 R /XYZ 0 0 0]")
        node = new_object(doc, f"<</Limits[(a)(z)]/Names[(Expediente {TERM}) {dest} 0 R (otro) {dest} 0 R]>>")
        for _ in range(1499):
            node = new_object(doc, f"<</Limits[(a)(z)]/Kids[{node} 0 R]>>")
        doc.xref_set_key(doc.pdf_catalog(), "Names", f"<</Dests {node} 0 R>>")
        report, out = self.apply(self.save(doc))
        self.assertDeliveredClean(report, out)
        self.assertIn("otro", fitz.open(out).resolve_names())


class TerminoCortadoPorElBorde(Base):
    """Página 65 de dos PDF del corpus del lote 2d (2026-10-16): «procurador» se salía por el
    borde derecho y solo se veía «procurad». La parte oculta completa el término, así que la
    palabra entera se censura como texto oculto (decisión de diseño, 2026-10-07), también su parte
    visible: si no, quedarían 8 de sus 10 letras. Esa zona no es del usuario, así que la página
    sale en text_loss_pages (X-Redact-Text-Loss): lo perdido es un trozo del término, no otro
    texto."""

    def test_se_censura_entero_y_se_avisa(self):
        doc = base_doc()  # con el término visible en (72, 100)
        doc[0].insert_text((480, 400), f"del {TERM}", fontsize=14)  # sale por la derecha
        src = self.save(doc)
        visible = [w[4] for w in fitz.open(src)[0].get_text("words")]
        self.assertEqual(visible.count(TERM), 1)  # la de base_doc; la del borde, cortada:
        self.assertTrue(any(TERM.startswith(w) and 4 <= len(w) < len(TERM) for w in visible), visible)

        items = self.search_items(src)  # solo encuentra la aparición visible
        self.assertEqual(len(items), 1)
        report, out = self.apply(src, items)
        self.assertTrue(server_delivers(report), report)
        self.assertFalse(term_anywhere(out))
        self.assertEqual(report["text_loss_pages"], [1])
        left = [w[4] for w in fitz.open(out)[0].get_text("words", clip=fitz.INFINITE_RECT())]
        self.assertFalse([w for w in left if w[:4] == TERM[:4]], left)  # ni el trozo visible
        for word in ("Nombre:", "fin", "del", CONTROL):
            self.assertIn(word, left)


if __name__ == "__main__":
    unittest.main()
