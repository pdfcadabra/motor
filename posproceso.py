# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
#
# Posprocesado del .docx de pdf2docx para escritos de texto, ajustado con un banco de escritos
# sintéticos (de LibreOffice y que imitan los PDF de Google Docs). Las listas de Word (numPr) van
# activadas por defecto (`posprocesar(..., listas=True)`; en la línea de comandos,
# `--sin-listas-word` las desactiva y `--listas-word` se sigue aceptando; decisión del 2026-11-07).
# Lo usa pdf_a_word.py; su línea de comandos de abajo es para pruebas y no forma parte de la
# interfaz del motor (README.md).
# Usa la geometría del PDF (PyMuPDF) para saber qué es cabecera, pie, nota y párrafo, y cambia
# el .docx (python-docx y XML directo):
#   1. Una sola sección: pdf2docx pone una por página (salto de página forzado y un párrafo
#      vacío de relleno arriba); al editar, el texto no pasaba a la página siguiente. Márgenes,
#      distancia de cabecera y pie y sangrías se recalculan con la geometría del PDF.
#   2. Cabecera y pie reales: las líneas que se repiten arriba o abajo en las páginas (con
#      tolerancia de posición; los números cuentan como comodín) salen del cuerpo y van a la
#      cabecera y el pie de la sección. El membrete que solo está en la primera página va a una
#      cabecera de primera página distinta. «Página X de Y» -> campos PAGE y NUMPAGES.
#   3. Párrafos que fluyen: se unen los trozos de un mismo párrafo (sobre todo los partidos
#      entre páginas), los saltos de línea dentro de un párrafo justificado pasan a espacios y
#      los centrados que pdf2docx emula con sangrías vuelven a ser centrados.
#   4. Notas al pie (opcional): llamada pequeña en el cuerpo + texto bajo el filete de la misma
#      página -> nota al pie de Word. Solo si cuadra todo (números 1..N, una llamada por nota y
#      todas localizadas en el .docx); si no, no se toca nada y el informe dice por qué.
#
# Uso: python posproceso.py <original.pdf> <pdf2docx.docx> <salida.docx> [--sin-notas]
#                           [--sin-listas-word] [--informe=<informe.json>]   (si no, imprime el informe)
# Listas de Word (por defecto): las viñetas y los números de lista del PDF pasan a listas de Word
# (numPr). Con --sin-listas-word quedan como texto con un tabulador.
import json
import re
import statistics
import sys
import unicodedata
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pymupdf
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import XmlPart
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = "{%s}" % W_NS
TW = 20  # twips por punto
MARGEN_SUP = 2.4 / 2.54 * 72  # pt
RE_PAGINA = re.compile(r"^\s*(p[áa]g(ina|\.)?\s*)?\d{1,4}(\s*(de|/)\s*\d{1,4})?\s*$", re.I)


def sin_cf(s):
    """Sin los caracteres de formato (categoría Cf: U+200B, U+200C, U+FEFF, U+00AD…). Google Docs
    dibuja un U+200B tras cada viñeta y número de lista y al principio de los párrafos que empiezan
    con tabulador; no se ve, pero str.strip() no lo quita y contaba como primera letra."""
    if s.isascii():
        return s
    return RE_CF.sub("", s)


def visible(c):
    """Un carácter que se ve: ni blanco ni de formato (Cf)."""
    return bool(c.strip()) and c not in CF


# Los caracteres Cf (unos 170: el plano básico y los pocos bloques de fuera que los tienen),
# calculados una vez: unicodedata.category carácter a carácter era lo que más tardaba.
CF = frozenset(chr(c) for rango in (range(0x10000), range(0x110B0, 0x110D0), range(0x13430, 0x13460),
                                    range(0x1BCA0, 0x1BCA4), range(0x1D173, 0x1D17B), range(0xE0000, 0xE0080))
               for c in rango if unicodedata.category(chr(c)) == "Cf")
RE_CF = re.compile("[" + "".join(re.escape(c) for c in sorted(CF)) + "]")


def nz(s):
    return re.sub(r"\s+", "", sin_cf(s)).lower()


def clave(s):
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", sin_cf(s))).strip().lower()


# --- PDF ----------------------------------------------------------------------------------

def lineas_pdf(doc):
    """Por página, las líneas con su texto, caja, tamaño y fragmentos."""
    paginas = []
    for page in doc:
        ls = []
        sueltos = []
        # rawdict: cajas por carácter. La del fragmento incluye el espacio final (3 pt en cuerpo 12) y el
        # ancho del texto salía de más: LibreOffice partía las líneas en otro sitio.
        for b in page.get_text("rawdict")["blocks"]:
            for l in b.get("lines", []):
                for s in l["spans"]:
                    s["text"] = "".join(c["c"] for c in s["chars"])
                # Los caracteres Cf (el U+200B de Google Docs) no cuentan para el comienzo de la línea.
                spans = [s for s in l["spans"] if any(visible(c) for c in s["text"])]
                if abs(l["dir"][1]) > 0.01:
                    continue
                if not spans:
                    # Solo un U+200B (PyMuPDF lo da a veces como línea aparte): se guarda para la línea
                    # de su renglón.
                    cs = [c for s in l["spans"] for c in s["chars"] if unicodedata.category(c["c"]) == "Cf"]
                    if cs:
                        sueltos.append({"base": max(s["origin"][1] for s in l["spans"]), "chars": cs})
                    continue
                letras = [c for s in spans for c in s["chars"] if visible(c["c"])]
                x0 = min(c["bbox"][0] for c in letras)
                x1 = max(c["bbox"][2] for c in letras)
                y0 = min(s["bbox"][1] for s in spans)
                y1 = max(s["bbox"][3] for s in spans)
                ls.append({"texto": sin_cf("".join(s["text"] for s in l["spans"])).strip(), "x0": x0, "x1": x1,
                           "y0": y0, "y1": y1, "base": max(s["origin"][1] for s in spans),
                           "tam": max(s["size"] for s in spans), "spans": spans,
                           # Todos los caracteres, también los Cf (para reconocer «N.» + U+200B).
                           "crudos": [c for s in l["spans"] for c in s["chars"]]})
        # Los trozos de un mismo renglón que PyMuPDF da como líneas separadas (una línea justificada
        # muy estirada sale palabra a palabra; también «Ldo.» y «Proc.» en la firma) son una línea.
        ls.sort(key=lambda l: (l["base"], l["x0"]))
        juntas = []
        for l in ls:
            prev = juntas[-1] if juntas else None
            if prev and abs(l["base"] - prev["base"]) <= 1.5 and l["x0"] >= prev["x1"] - 1:
                prev.update(texto=prev["texto"] + " " + l["texto"], x1=l["x1"], y0=min(prev["y0"], l["y0"]),
                            y1=max(prev["y1"], l["y1"]), tam=max(prev["tam"], l["tam"]),
                            spans=prev["spans"] + l["spans"], crudos=prev["crudos"] + l["crudos"],
                            trozos=prev.get("trozos", 1) + 1)
            else:
                juntas.append(l)
        juntas.sort(key=lambda l: (round(l["base"]), l["x0"]))
        for su in sueltos:
            x = su["chars"][0]["bbox"][0]
            l = min((l for l in juntas if abs(l["base"] - su["base"]) <= 1.5 and l["x0"] >= x - 1),
                    key=lambda l: l["x0"], default=None)
            if l is not None:
                l["crudos"] = sorted(su["chars"] + l["crudos"], key=lambda c: c["bbox"][0])
        paginas.append(juntas)
    return paginas


def filetes(page):
    """Líneas horizontales cortas en la mitad inferior (el filete de las notas). Un subrayado no
    lo es: tiene texto justo encima, en la misma franja horizontal."""
    cajas = [l["bbox"] for b in page.get_text("dict")["blocks"] for l in b.get("lines", [])]
    out = []
    for d in page.get_drawings():
        r = d["rect"]
        if r.height <= 1.5 and 30 <= r.width <= page.rect.width * 0.5 and r.y0 > page.rect.height * 0.4:
            if any(c[0] < r.x1 and c[2] > r.x0 and -2 <= r.y0 - c[3] <= 4 or c[1] < r.y0 < c[3]
                   and c[0] < r.x1 and c[2] > r.x0 for c in cajas):
                continue
            out.append(r)
    return out


def solo_cambia_la_pagina(occ, n):
    """En las apariciones (página, texto) de una línea repetida, las cifras que cambian son el
    número de página (o el total)."""
    trozos = [(p, re.findall(r"\d+", t)) for p, t in occ]
    largos = {len(x) for _, x in trozos}
    if len(largos) != 1:
        return False
    for k in range(largos.pop()):
        valores = {x[k] for _, x in trozos}
        if len(valores) > 1 and not all(int(x[k]) in (p + 1, n) for p, x in trozos):
            return False
    return True


def analizar_pdf(ruta):
    doc = pymupdf.open(ruta)
    pags = lineas_pdf(doc)
    n = len(pags)
    h = doc[0].rect.height
    w = doc[0].rect.width
    # Repetidas: misma clave (cifras como comodín) en al menos el 60 % de las páginas (y 2), a la
    # misma altura ±6 pt, arriba o abajo. Con la mitad, una frase del cuerpo que caía arriba en dos
    # de cuatro páginas pasaba a cabecera.
    con_filete = [min((f.y0 for f in filetes(doc[p])), default=None) for p in range(n)]
    vistas = {}
    for p, ls in enumerate(pags):
        for i, l in enumerate(ls):
            vistas.setdefault(clave(l["texto"]), []).append((p, i, l["y0"]))
    rep = set()
    if n >= 2:
        minimo = max(2, -(-n * 3 // 5))
        for k, occ in vistas.items():
            ym = statistics.median(o[2] for o in occ)
            buenas = [o for o in occ if abs(o[2] - ym) <= 6]
            if len({o[0] for o in buenas}) < minimo or not (ym < h * 0.2 or ym > h * 0.8):
                continue
            # Abajo, unas notas con el mismo molde («N Sentencia del Tribunal Supremo… de N de
            # mayo») también se repiten: un pie de verdad sale también en alguna página sin notas
            # o, si todas tienen notas, solo cambia en el número de página.
            if ym > h / 2:
                fuera = any(con_filete[o[0]] is None or o[2] < con_filete[o[0]] for o in buenas)
                if not fuera and not (all(f is not None for f in con_filete) and
                                      solo_cambia_la_pagina([(o[0], pags[o[0]][o[1]]["texto"]) for o in buenas], n)):
                    continue
            rep |= {(o[0], o[1]) for o in buenas}
    zona = {}  # (p, i) -> "cab" | "pie" | "cab1" | "nota"
    for p, ls in enumerate(pags):
        for i, l in enumerate(ls):
            if n >= 2 and (p, i) in rep:
                zona[(p, i)] = "cab" if l["y0"] < h / 2 else "pie"
            # Con una sola página no hay repetición que lo confirme: solo es cabecera lo que empieza
            # dentro del margen superior (2,4 cm; el de un escrito es de 2,5 cm o más). «AL
            # JUZGADO…» en la primera línea del cuerpo (a 2,5 cm, f03) caía en el 10 % superior.
            elif n == 1 and l["y1"] < h * 0.10 and l["y0"] < MARGEN_SUP:
                zona[(p, i)] = "cab1"
            elif n == 1 and l["y0"] > h * 0.90:
                zona[(p, i)] = "pie"
    # Notas: lo que queda bajo el filete (sin el pie).
    lineas_nota = []
    for p, ls in enumerate(pags):
        yf = con_filete[p]
        mias = sorted((i for i, l in enumerate(ls) if yf is not None and l["y0"] >= yf and (p, i) not in zona),
                      key=lambda i: ls[i]["y0"])
        for i in mias:
            zona[(p, i)] = "nota"
        lineas_nota.append(mias)
    # Membrete solo en la primera página: por encima del cuerpo de las demás.
    if n >= 2:
        tops = [min(l["y0"] for i, l in enumerate(ls) if (p, i) not in zona)
                for p, ls in enumerate(pags[1:], 1) if any((p, i) not in zona for i in range(len(ls)))]
        if tops:
            techo = min(tops) - 2
            for i, l in enumerate(pags[0]):
                if (0, i) not in zona and l["y1"] < techo:
                    zona[(0, i)] = "cab1"
    # Cada nota empieza en una línea con su número; las demás líneas la continúan.
    notas_pag = []
    for p, mias in enumerate(lineas_nota):
        notas, actual = [], None
        for i in mias:
            l = pags[p][i]
            m = re.match(r"^(\d{1,3})\s*(.*)$", l["texto"])
            if m and (actual is None or int(m.group(1)) == actual["num"] + 1):
                actual = {"num": int(m.group(1)), "texto": m.group(2), "lineas": [i]}
                notas.append(actual)
            elif actual:
                actual["texto"] += " " + l["texto"]
                actual["lineas"].append(i)
        notas_pag.append(notas)
    cuerpo = [(p, i, l) for p, ls in enumerate(pags) for i, l in enumerate(ls) if (p, i) not in zona]
    tam_cuerpo = statistics.median(l["tam"] for _, _, l in cuerpo) if cuerpo else 12
    # Llamadas: fragmentos de cifras más pequeños que el cuerpo dentro de una línea del cuerpo.
    llamadas = []
    for p, i, l in cuerpo:
        for s in l["spans"]:
            t = s["text"].strip()
            if t.isdigit() and s["size"] < tam_cuerpo * 0.8 and (s["origin"][1] < l["base"] - 1 or s["flags"] & 1):
                llamadas.append((p, int(t)))
    # Interlineado y separación de las notas, para reproducirlas a su tamaño.
    paso, sep, tams = [], [], []
    for p, notas in enumerate(notas_pag):
        ls = pags[p]
        for k, nt in enumerate(notas):
            bs = [ls[i]["base"] for i in nt["lineas"]]
            paso += [b - a for a, b in zip(bs, bs[1:])]
            tams += [s["size"] for i in nt["lineas"] for s in ls[i]["spans"] if not s["text"].strip().isdigit()]
            if k + 1 < len(notas):
                sep.append(ls[notas[k + 1]["lineas"][0]]["base"] - bs[-1])
    paso_nota = statistics.median(paso) if paso else None
    tam_nota = statistics.median(tams) if tams else None
    extra_nota = max(0, statistics.median(sep) - paso_nota) if sep and paso_nota else 0
    saltos = [cuerpo[k][2]["base"] - cuerpo[k - 1][2]["base"] for k in range(1, len(cuerpo))
              if cuerpo[k][0] == cuerpo[k - 1][0]]
    paso_cuerpo = statistics.median([x for x in saltos if x > 0]) if saltos else None
    izq = min((l["x0"] for _, _, l in cuerpo), default=72)
    der = max((l["x1"] for _, _, l in cuerpo), default=w - 72)
    return {"doc": doc, "pags": pags, "zona": zona, "notas": notas_pag, "llamadas": llamadas,
            "cuerpo": cuerpo, "tam_cuerpo": tam_cuerpo, "paso_nota": paso_nota, "tam_nota": tam_nota,
            "extra_nota": extra_nota, "paso_cuerpo": paso_cuerpo, "w": w, "h": h, "n": n, "izq": izq, "der": der}


# Conectores con los que una línea centrada deja la frase a medias (o con los que empieza la que la
# continúa): preposiciones, artículos y conjunciones.
CONECTORES = {"a", "al", "ante", "con", "contra", "de", "del", "desde", "e", "el", "en", "entre", "hacia", "la",
              "las", "lo", "los", "ni", "o", "para", "por", "que", "sobre", "su", "sus", "u", "un", "una", "y"}
# Líneas cortas que son un dato por sí mismas (firma, datos del procedimiento): nunca continúan la
# línea centrada anterior.
RE_DATO_SUELTO = re.compile(r"^(ldo|lda|ldo\(a\)|letrad[oa]|col|colegiad[oa]|proc|procurador[a]?|fdo|firmado|"
                            r"nig|n\.?\s*i\.?\s*g|secci[oó]n|negociado|procedimiento|juicio|autos|rollo|"
                            r"ejecuci[oó]n|recurso|asunto|materia|parte)\b", re.I)


def continua_frase(prev, sig):
    """La línea centrada `sig` continúa la frase de `prev` (un título partido en dos líneas): `prev`
    no acaba en punto, dos puntos ni punto y coma, y `sig` empieza en minúscula o con un conector, o
    `prev` acaba en coma o en un conector. Un dato suelto («Ldo. …», «Col. …», «NIG …») nunca."""
    a, b = prev["texto"].strip(), sig["texto"].strip()
    if not a or not b or a.endswith((".", ":", ";")) or RE_DATO_SUELTO.match(b):
        return False
    ult = re.sub(r"[^\w]", "", a.split()[-1]).lower()
    pri = re.sub(r"[^\w]", "", b.split()[0]).lower()
    return b[0].islower() or pri in CONECTORES or a.endswith(",") or ult in CONECTORES


VINETAS = "\u25cf\u25cb\u25a0\u25a1\u25c6\u25c7\u2022\u25aa\u25ab\u25e6\u2023\u2043\uf0b7\uf0a7"
RE_NUMERO_LISTA = re.compile(r"^(\d{1,3}|[a-zA-Z]|[ivx]{1,5}|[IVX]{1,5})[.)]$")


def etiqueta_de(l):
    """Si la línea empieza con una viñeta o un número de lista, (etiqueta, x del texto que la
    sigue); si no, None. Viñeta: un carácter de VINETAS seguido de un hueco (≥ 4 pt) o de un
    carácter Cf. Número («1.», «a)», «iv.»): seguido de un Cf (Google Docs pone un U+200B tras el
    número y el texto justo detrás, sin hueco) o de un hueco de tabulador (≥ 4 pt). Un número
    seguido de un espacio normal no cuenta: puede ser texto («3. Que…» a mitad de párrafo)."""
    cs = l.get("crudos") or [c for s in l["spans"] for c in s["chars"]]
    vis = [k for k, c in enumerate(cs) if visible(c["c"])]
    if not vis:
        return None

    def tras(k):
        """(hay un Cf justo detrás, hueco hasta el siguiente visible, x del siguiente visible)."""
        sig = next((m for m in vis if m > k), None)
        if sig is None:
            return False, 0, None
        cf = any(unicodedata.category(cs[m]["c"]) == "Cf" for m in range(k + 1, sig))
        return cf, cs[sig]["bbox"][0] - cs[k]["bbox"][2], cs[sig]["bbox"][0]

    k0 = vis[0]
    if cs[k0]["c"] in VINETAS:
        cf, hueco, x = tras(k0)
        if x is not None and (cf or hueco >= 4):
            return cs[k0]["c"], x
        return None
    # Número: los visibles seguidos desde el primero hasta un blanco o un Cf.
    k = k0
    texto = ""
    for m in vis:
        if m != k:
            break
        texto += cs[m]["c"]
        k = m + 1
        if len(texto) > 7:
            return None
    ult = k - 1
    if not RE_NUMERO_LISTA.match(texto):
        return None
    cf, _, x = tras(ult)
    if x is not None and cf:
        return texto, x
    return None


def corte_frances(prev, l):
    """Si `prev` es la primera línea de un párrafo con sangría francesa y `l` su continuación, la x
    donde empieza el texto tras la etiqueta en `prev` (la de `l`, con un hueco de tabulador antes);
    si no, None. Con una etiqueta de lista (viñeta o número), el texto de la primera línea puede
    quedar antes del tope del tabulador: Google Docs lo pone justo detrás de «N.» (105,3 con la
    continuación en 108,0); vale si `l` empieza entre el final de la etiqueta y 6 pt más allá del
    texto que la sigue."""
    if l["x0"] <= prev["x0"] + 3:
        return None
    et = etiqueta_de(prev)
    if et is not None and et[1] - 1 <= l["x0"] <= et[1] + 6:
        return et[1]
    letras = [c for s in prev["spans"] for c in s["chars"]]
    for j, c in enumerate(letras):
        if not visible(c["c"]) or j == 0:
            continue
        if abs(c["bbox"][0] - l["x0"]) <= 2:
            anterior = next((x for x in reversed(letras[:j]) if visible(x["c"])), None)
            if anterior is not None and c["bbox"][0] - anterior["bbox"][2] >= 4:
                return c["bbox"][0]
            return None
    return None


def no_cabia(prev, l, der):
    """La primera palabra de `l` no cabía al final de `prev` (con un espacio) antes de `der`."""
    palabra = []
    for c in [c for s in l["spans"] for c in s["chars"]]:
        if not c["c"].strip() and palabra:
            break
        if visible(c["c"]):
            palabra.append(c)
    if not palabra:
        return False
    esp = [c["bbox"][2] - c["bbox"][0] for s in prev["spans"] for c in s["chars"] if c["c"] == " "]
    hueco = statistics.median(esp) if esp else 0.25 * prev["tam"]
    # A medio punto del margen no se sabe: cuenta como que no cabía (las demás condiciones de
    # parrafos_pdf ya limitan cuándo se usa).
    return prev["x1"] + hueco + palabra[-1]["bbox"][2] - palabra[0]["bbox"][0] > der - 0.5


def parrafos_pdf(a):
    """Marca qué líneas del cuerpo empiezan párrafo. Una línea continúa la anterior si la
    anterior llega al margen derecho de su bloque (texto justificado), la nueva no está más
    sangrada que la anterior (salvo la segunda línea de una sangría francesa, alineada con el texto
    tras la etiqueta de la primera) y el interlineado es el normal (o es el cambio de página). Dos
    líneas centradas seguidas con el interlineado normal son un mismo párrafo solo si la segunda
    continúa la frase de la primera (un título de dos líneas); la firma y los datos del
    procedimiento, una línea por párrafo.
    Listas: una línea que empieza con viñeta o número de lista siempre empieza párrafo; dentro de
    un elemento, las líneas alineadas con su continuación lo siguen aunque no lleguen al margen (una
    lista sin justificar). Una línea que sale hacia la izquierda respecto a la anterior, cuando la
    anterior no es la primera de su párrafo, empieza otro (una viñeta en 90 tras la continuación del
    elemento anterior en 108)."""
    cuerpo = a["cuerpo"]
    saltos = [cuerpo[k][2]["base"] - cuerpo[k - 1][2]["base"] for k in range(1, len(cuerpo))
              if cuerpo[k][0] == cuerpo[k - 1][0]]
    normal = statistics.median([s for s in saltos if s > 0]) if saltos else 14
    medio = (a["izq"] + a["der"]) / 2

    def centrada(l):
        return abs((l["x0"] + l["x1"]) / 2 - medio) <= 4 and l["x0"] > a["izq"] + 4 and l["x1"] < a["der"] - 4

    empieza = []
    item = None  # elemento de lista en curso: {"x_cont": x de su continuación (None hasta verla)}
    for k, (p, i, l) in enumerate(cuerpo):
        et = etiqueta_de(l)
        if k == 0:
            empieza.append(True)
            item = {"x_cont": None} if et else None
            continue
        pp_, _, prev = cuerpo[k - 1]
        llena = prev["x1"] >= a["der"] - 30 and prev["x1"] - prev["x0"] > (a["der"] - a["izq"]) * 0.6
        if pp_ == p:
            salto = l["base"] - prev["base"]
            normal_ok = salto <= normal * 1.25
        else:
            normal_ok = True
        if et:
            sigue = False
        elif item is not None and normal_ok and (
                (item["x_cont"] is None and empieza[k - 1] and corte_frances(prev, l) is not None)
                or (item["x_cont"] is not None and abs(l["x0"] - item["x_cont"]) <= 3)):
            sigue = True
        elif not empieza[k - 1] and l["x0"] < prev["x0"] - 3 and not (centrada(prev) and centrada(l)):
            sigue = False
        else:
            sangrada = l["x0"] > prev["x0"] + 3 and not (llena and corte_frances(prev, l))
            # Sin justificar: si la primera palabra de esta línea no cabía al final de la anterior, el
            # corte lo puso el margen y es la misma frase («En Villaficticia, a uno de enero de dos
            # mil / noventa y nueve.»), aunque la anterior no llegue a 30 pt del margen. Solo en la
            # misma página, con el interlineado normal, si la anterior no acaba la frase y esta empieza
            # en minúscula o cifra (un título o un párrafo nuevo tras una línea corta no lo cumplen), y
            # con las dos líneas de un solo trozo (no las filas de una tabla, con huecos entre columnas).
            if not llena and not sangrada and pp_ == p and normal_ok and not centrada(prev) \
                    and not prev["texto"].rstrip().endswith((".", ":", ";")) \
                    and prev.get("trozos", 1) == 1 and l.get("trozos", 1) == 1 \
                    and re.match(r"[a-záéíóúüñ0-9]", l["texto"]) and no_cabia(prev, l, a["der"]):
                llena = True
            if pp_ == p:
                sigue = (llena and not sangrada or centrada(prev) and centrada(l) and continua_frase(prev, l)) \
                    and normal_ok
            else:
                sigue = llena and not sangrada
        empieza.append(not sigue)
        if not sigue:
            item = {"x_cont": None} if et else None
        elif item is not None and item["x_cont"] is None:
            item["x_cont"] = l["x0"]
    return empieza


# --- DOCX ---------------------------------------------------------------------------------

def texto_p(p):
    out = []
    for el in p.iter(W + "t", W + "tab", W + "br"):
        out.append(el.text or "" if el.tag == W + "t" else " ")
    return "".join(out)


def segmentos(body):
    """Elementos del cuerpo por página de pdf2docx (cada página acaba en un párrafo con sectPr)."""
    segs, actual = [], []
    for el in list(body):
        if el.tag == W + "sectPr":
            continue
        actual.append(el)
        if el.tag == W + "p" and el.find(f"{W}pPr/{W}sectPr") is not None:
            segs.append(actual)
            actual = []
    if actual:
        segs.append(actual)
    return segs


def margenes(sect):
    m = sect.find(W + "pgMar")
    return {k: int(m.get(W + k, "0")) for k in ("top", "right", "bottom", "left", "header", "footer")}


def poner_ind(p, **kw):
    ppr = p.find(W + "pPr")
    if ppr is None:
        ppr = OxmlElement("w:pPr")
        p.insert(0, ppr)
    ind = ppr.find(W + "ind")
    if ind is None:
        ind = OxmlElement("w:ind")
        ppr.append(ind)
    for k, v in kw.items():
        if v is None:
            ind.attrib.pop(W + k, None)
        else:
            ind.set(W + k, str(int(v)))
    return ind


def poner_jc(p, val):
    ppr = p.find(W + "pPr")
    jc = ppr.find(W + "jc")
    if jc is None:
        jc = OxmlElement("w:jc")
        ppr.append(jc)
    jc.set(W + "val", val)


def run_con(texto, rpr):
    r = OxmlElement("w:r")
    if rpr is not None:
        r.append(deepcopy(rpr))
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = texto
    r.append(t)
    return r


def campo(instr, rpr, ejemplo):
    out = []
    for tipo in ("begin", "instr", "separate", "res", "end"):
        if tipo == "res":
            out.append(run_con(ejemplo, rpr))
            continue
        r = OxmlElement("w:r")
        if rpr is not None:
            r.append(deepcopy(rpr))
        if tipo == "instr":
            e = OxmlElement("w:instrText")
            e.set(qn("xml:space"), "preserve")
            e.text = f" {instr} "
        else:
            e = OxmlElement("w:fldChar")
            e.set(qn("w:fldCharType"), tipo)
        r.append(e)
        out.append(r)
    return out


def primer_rpr(p):
    for r in p.iter(W + "r"):
        if r.find(W + "t") is not None:
            return r.find(W + "rPr")
    return None


def parrafo_zona(linea, a, n_pags, valores):
    """Párrafo de cabecera o pie para una línea del PDF: alineación por su posición, fragmentos con
    su formato (fuente, tamaño, negrita, cursiva) y los números de página como campos."""
    p = OxmlElement("w:p")
    ppr = OxmlElement("w:pPr")
    sp = OxmlElement("w:spacing")
    sp.set(W + "before", "0")
    sp.set(W + "after", "0")
    ppr.append(sp)
    jc = OxmlElement("w:jc")
    centro = (linea["x0"] + linea["x1"]) / 2
    medio = (a["izq"] + a["der"]) / 2
    if abs(centro - medio) < 8:
        jc.set(W + "val", "center")
    elif linea["x1"] > a["der"] - 8 and linea["x0"] > medio:
        jc.set(W + "val", "right")
    else:
        jc.set(W + "val", "left")
    ppr.append(jc)
    p.append(ppr)
    texto = linea["texto"]
    rpr = OxmlElement("w:rPr")
    s0 = linea["spans"][0]
    fuente = familia_pdf(s0["font"])
    rf = OxmlElement("w:rFonts")
    rf.set(W + "ascii", fuente)
    rf.set(W + "hAnsi", fuente)
    rpr.append(rf)
    if "bold" in s0["font"].lower() or s0["flags"] & 16:
        rpr.append(OxmlElement("w:b"))
    if "italic" in s0["font"].lower() or s0["flags"] & 2:
        rpr.append(OxmlElement("w:i"))
    sz = OxmlElement("w:sz")
    sz.set(W + "val", str(round(s0["size"] * 2)))
    rpr.append(sz)
    if valores == "campos":
        # «Página 3 de 10» -> «Página » PAGE « de » NUMPAGES (el segundo número, si lo hay).
        trozos = re.split(r"(\d+)", texto)
        n_nums = sum(t.isdigit() for t in trozos)
        visto = 0
        for t in trozos:
            if not t:
                continue
            if t.isdigit():
                visto += 1
                es_total = n_nums == 2 and visto == 2
                p.extend(campo("NUMPAGES" if es_total else "PAGE", rpr, str(n_pags) if es_total else "1"))
            else:
                p.append(run_con(t, rpr))
    else:
        p.append(run_con(texto, rpr))
    return p


def crear_notas(doc):
    part = None
    for rel in doc.part.rels.values():
        if rel.reltype == RT.FOOTNOTES:
            part = rel.target_part
    if part is None:
        xml = (f'<w:footnotes {nsdecls("w")}>'
               '<w:footnote w:type="separator" w:id="-1"><w:p><w:pPr><w:spacing w:after="0" w:line="240" '
               'w:lineRule="auto"/></w:pPr><w:r><w:separator/></w:r></w:p></w:footnote>'
               '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:pPr><w:spacing w:after="0" '
               'w:line="240" w:lineRule="auto"/></w:pPr><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
               '</w:footnotes>')
        part = XmlPart(PackURI("/word/footnotes.xml"),
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
                       parse_xml(xml), doc.part.package)
        doc.part.relate_to(part, RT.FOOTNOTES)
    return part.element


# --- Fuentes ---------------------------------------------------------------------------------

# Nombre PostScript del PDF (sin prefijo de subconjunto ni estilo) -> familia para w:rFonts.
FAMILIAS = {"TimesNewRomanPSMT": "Times New Roman", "TimesNewRomanPS": "Times New Roman",
            "TimesNewRoman": "Times New Roman", "ArialMT": "Arial", "Arial": "Arial",
            "ArialNarrow": "Arial Narrow", "CourierNewPSMT": "Courier New", "CourierNewPS": "Courier New",
            "CourierNew": "Courier New", "Calibri": "Calibri", "CalibriLight": "Calibri Light",
            "Cambria": "Cambria", "Georgia": "Georgia", "Verdana": "Verdana", "Tahoma": "Tahoma",
            "Garamond": "Garamond", "BookAntiqua": "Book Antiqua", "CenturyGothic": "Century Gothic",
            "MSGothic": "MS Gothic", "MS": "MS Gothic", "SymbolMT": "Symbol", "Symbol": "Symbol",
            "LiberationSerif": "Liberation Serif", "LiberationSans": "Liberation Sans",
            "LiberationMono": "Liberation Mono", "Carlito": "Carlito", "Caladea": "Caladea"}


def familia_pdf(nombre):
    """Familia de una fuente del PDF a partir de su nombre: sin el prefijo de subconjunto
    (AAAAAA+), sin el estilo (-Bold, ,Italic…) y con la tabla de equivalencias (TimesNewRomanPSMT →
    Times New Roman, ArialMT → Arial, CourierNewPSMT → Courier New). Si no está en la tabla, sin
    PSMT/MT/PS final y con espacios entre las palabras en mayúscula («BookmanOldStyle» → «Bookman Old
    Style»)."""
    n = re.sub(r"^[A-Z]{6}\+", "", nombre or "")
    n = re.split(r"[-,]", n)[0]
    if n in FAMILIAS:
        return FAMILIAS[n]
    n = re.sub(r"(PSMT|PS|MT)$", "", n)
    if n in FAMILIAS:
        return FAMILIAS[n]
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", n) or nombre


def fuentes_vacias(a, body, inf):
    """pdf2docx 0.5.13 toma la familia (name ID 1) de cada fuente incrustada; si una no la tiene
    (una Calibri en un PDF de Google Docs) su nombre es '' y, como '' está contenido en cualquier nombre, todos los
    fragmentos que no casan exactamente con otra fuente se quedan con ella: rFonts vacío en todo el
    documento. Cada run con rFonts vacío toma la fuente de su sitio en el PDF: su texto, con el de
    los runs de alrededor en el mismo párrafo como contexto, se busca en la secuencia de caracteres
    del PDF (en orden, desde donde se encontró el anterior) y se toma la fuente de esos caracteres.
    Así una viñeta «•» toma la fuente con que el PDF la dibuja (Times en una lista, Calibri en otra);
    votando por palabras salía siempre la de la fuente que más viñetas tiene. Si no se encuentra, la
    del fragmento que contiene el texto entero del run; si no, la que más llevan sus palabras; y si
    tampoco, la del cuerpo."""
    por_palabra = {}
    todas = Counter()
    fragmentos = []  # (texto sin blancos, familia)
    secuencia, fam_de = [], []  # caracteres visibles del PDF, en orden de lectura, y su familia
    for ls in a["pags"]:
        for l in ls:
            for sp in l["spans"]:
                fam = familia_pdf(sp["font"])
                fragmentos.append((nz(sp["text"]), fam))
                for c in sp["chars"]:
                    if visible(c["c"]):
                        for x in c["c"].lower():
                            secuencia.append(x)
                            fam_de.append(fam)
                for w in sp["text"].split():
                    w = nz(w)
                    if w:
                        por_palabra.setdefault(w, Counter())[fam] += 1
                        todas[fam] += len(w)
    if not todas:
        return
    secuencia = "".join(secuencia)
    cuerpo = todas.most_common(1)[0][0]
    inf["fuentes_vacias"] = 0
    cursor = 0

    def en_su_sitio(r, aguja):
        """Familia de los caracteres del PDF donde está el run (con contexto), o None."""
        nonlocal cursor
        p = r.getparent()
        runs = [x for x in p if x.tag == W + "r"] if p is not None else [r]
        i = runs.index(r) if r in runs else 0
        antes = "".join(nz("".join(t.text or "" for t in x.iter(W + "t"))) for x in runs[:i])[-12:]
        despues = "".join(nz("".join(t.text or "" for t in x.iter(W + "t"))) for x in runs[i + 1:])[:16]
        for pre, suf in ((antes, despues), ("", despues), (antes, "")):
            ctx = pre + aguja + suf
            if len(ctx) < 6:
                continue
            k = secuencia.find(ctx, cursor)
            if k < 0:
                k = secuencia.find(ctx)
            if k >= 0:
                k += len(pre)
                cursor = k + len(aguja)
                return Counter(fam_de[k:k + len(aguja)]).most_common(1)[0][0]
        return None

    for rf in body.iter(W + "rFonts"):
        if any(rf.get(W + k) for k in ("ascii", "hAnsi")):
            continue
        r = rf.getparent().getparent()
        texto = "".join(t.text or "" for t in r.iter(W + "t"))
        aguja = nz(texto)
        sitio = en_su_sitio(r, aguja) if aguja else None
        if sitio:
            for k in ("ascii", "hAnsi", "eastAsia", "cs"):
                rf.set(W + k, sitio)
            inf["fuentes_vacias"] += 1
            continue
        # pdf2docx hace un run por fragmento del PDF: primero, los fragmentos que contienen el texto entero
        # del run (si es lo bastante largo para no ser ambiguo); si no, la fuente de sus palabras.
        votos = Counter()
        if len(aguja) >= 8:
            votos.update(fam for t, fam in fragmentos if aguja in t)
        if not votos:
            for w in texto.split():
                votos.update(por_palabra.get(nz(w), {}))
        fam = votos.most_common(1)[0][0] if votos else cuerpo
        for k in ("ascii", "hAnsi", "eastAsia", "cs"):
            rf.set(W + k, fam)
        inf["fuentes_vacias"] += 1


# --- Tablas falsas ---------------------------------------------------------------------------

def sin_bordes(tbl):
    """Ningún borde visible ni sombreado en la tabla ni en sus celdas."""
    for b in tbl.iter(W + "tblBorders", W + "tcBorders"):
        for borde in b:
            if borde.get(W + "val") not in (None, "nil", "none"):
                return False
    for shd in tbl.iter(W + "shd"):
        if shd.get(W + "fill") not in (None, "auto", "FFFFFF", "ffffff"):
            return False
    return True


def convertir_tablas_falsas(a, body, segs, mg, inf, interlineado=None):
    """Una tabla de pdf2docx sin bordes cuyas filas, cada una con las celdas juntas, son líneas
    seguidas del PDF que llegan al margen derecho y no son la última de su párrafo (líneas de
    texto justificado muy estiradas), se sustituye por un párrafo con esos runs. La línea tiene que
    empezar en la sangría de su propio párrafo (la x de la línea siguiente, ±40 pt por la sangría
    de primera línea o la francesa de una viñeta), no en el margen: las de nivel 1 de una lista
    empiezan en 144 con el margen en 72. Las tablas de verdad (con bordes, o filas que no son líneas
    de un párrafo, como una firma a dos columnas) no se tocan."""
    empieza = parrafos_pdf(a)
    cuerpo = a["cuerpo"]
    befores = [int(sp.get(W + "before", "0")) for sp in body.iter(W + "spacing")
               if sp.getparent().getparent().tag == W + "p" and int(sp.get(W + "before", "0") or 0) > 0]
    antes = statistics.median(befores) if befores else 120

    def estirada(k):
        """La línea k es una línea de párrafo justificado: no es la última y llega al margen
        derecho desde la sangría de su párrafo."""
        l = cuerpo[k][2]
        if k + 1 >= len(cuerpo) or empieza[k + 1]:
            return False
        sigue = cuerpo[k + 1][2]
        return l["x1"] >= a["der"] - 3 and abs(l["x0"] - sigue["x0"]) <= 40

    for p, elementos in enumerate(segs):
        for idx, tbl in enumerate(list(elementos)):
            if tbl.tag != W + "tbl" or tbl.getparent() is not body:
                continue
            filas = tbl.findall(W + "tr")
            if not filas or not sin_bordes(tbl) or tbl.find(f".//{W}tbl") is not None:
                continue
            celdas_f = [f.findall(W + "tc") for f in filas]
            textos_f = [[" ".join(texto_p(x) for x in c.findall(W + "p")).strip() for c in cs] for cs in celdas_f]
            juntos = [nz("".join(ts)) for ts in textos_f]
            if any(len(cs) < 2 for cs in celdas_f) or not all(juntos):
                continue
            # Las líneas seguidas del PDF que forman las filas: en esta página o en una vecina (pdf2docx
            # pasa a veces el final de una página a la sección siguiente y junta en una tabla dos
            # líneas a los dos lados del salto de página).
            for k, (pp, _, l) in enumerate(cuerpo):
                if abs(pp - p) > 1 or nz(l["texto"]) != juntos[0]:
                    continue
                ks = list(range(k, k + len(filas)))
                if ks[-1] >= len(cuerpo) or any(nz(cuerpo[m][2]["texto"]) != juntos[r] or not estirada(m)
                                                or (r and empieza[m]) for r, m in enumerate(ks)):
                    continue
                sigue = cuerpo[ks[-1] + 1][2]
                par = OxmlElement("w:p")
                ppr = OxmlElement("w:pPr")
                sp = OxmlElement("w:spacing")
                sp.set(W + "before", str(int(antes)) if empieza[k] else "0")
                sp.set(W + "after", "0")
                if interlineado:
                    sp.set(W + "line", str(interlineado[1]))
                    sp.set(W + "lineRule", interlineado[0])
                elif a["paso_cuerpo"]:
                    sp.set(W + "line", str(round(a["paso_cuerpo"] * TW)))
                    sp.set(W + "lineRule", "exact")
                ppr.append(sp)
                ind = OxmlElement("w:ind")
                # Relativas a los márgenes de la sección de esta página, como las de pdf2docx (luego
                # se recalculan con los márgenes nuevos).
                ind.set(W + "left", str(round(sigue["x0"] * TW) - mg[p]["left"]))
                ind.set(W + "right", str(round((a["w"] - a["der"]) * TW) - mg[p]["right"]))
                primera = round((l["x0"] - sigue["x0"]) * TW) if empieza[k] else 0
                # Francesa (la primera línea de una viñeta, más afuera que las demás) si es negativa.
                ind.set(W + ("firstLine" if primera >= 0 else "hanging"), str(abs(primera)))
                ppr.append(ind)
                jc = OxmlElement("w:jc")
                jc.set(W + "val", "both")
                ppr.append(jc)
                par.append(ppr)
                for celdas in celdas_f:
                    for c in celdas:
                        runs = [r for x in c.findall(W + "p") for r in x.findall(W + "r")]
                        # Un espacio entre celdas, salvo al principio (la primera celda puede ser solo el
                        # U+200B de Google Docs).
                        hay = sin_cf(texto_p(par)).strip()
                        if runs and hay and not texto_p(par).endswith(" "):
                            par.append(run_con(" ", runs[0].find(W + "rPr")))
                        for r in runs:
                            par.append(deepcopy(r))
                body.replace(tbl, par)
                elementos[idx] = par
                inf["tablas_falsas"].append(" / ".join(" ".join(ts) for ts in textos_f)[:80])
                break


# --- Párrafos: saltos de línea y sangrías -----------------------------------------------------

def partir_en_saltos(body, clase, pag_de, cadena, inicios):
    """pdf2docx junta en un párrafo con saltos de línea las líneas centradas seguidas (la firma
    «Ldo. … / Col. … / Proc. …», los datos del procedimiento). Si tras un salto empieza otro párrafo
    del PDF, el párrafo se parte ahí: el primer trozo conserva el espacio de antes y el último el de
    después, como en el original."""
    partidos = 0
    pos = 0
    for el in [e for e in body.iter(W + "p") if clase.get(e) == "cuerpo" and e.getparent() is body]:
        t = nz(texto_p(el))
        k = cadena.find(t, pos, pos + len(t) + 400) if t else -1
        if k < 0:
            continue
        pos = k + len(t)
        brs = [br for br in el.iter(W + "br") if br.get(W + "type") in (None, "textWrapping")]
        if not brs:
            continue
        # Posición en la cadena de lo que sigue a cada salto.
        cortes, cuenta = [], 0
        for x in el.iter(W + "t", W + "br"):
            if x.tag == W + "t":
                cuenta += len(nz(x.text or ""))
            elif x in brs and k + cuenta in inicios:
                cortes.append(x)
        actual = el
        for br in cortes:
            r = br.getparent()
            if r.getparent() is not actual:
                continue
            nuevo = OxmlElement("w:p")
            ppr = actual.find(W + "pPr")
            if ppr is not None:
                nuevo.append(deepcopy(ppr))
            # Lo que sigue al salto dentro de su run, en un run nuevo con el mismo formato.
            despues = list(r)[list(r).index(br) + 1:]
            r.remove(br)
            hermanos = list(actual)[list(actual).index(r) + 1:]
            if despues:
                r2 = OxmlElement("w:r")
                if r.find(W + "rPr") is not None:
                    r2.append(deepcopy(r.find(W + "rPr")))
                for x in despues:
                    r2.append(x)
                nuevo.append(r2)
            for x in hermanos:
                nuevo.append(x)
            if not any(x.tag in (W + "t", W + "tab") for x in r.iter()):
                actual.remove(r)
            sp_a, sp_n = actual.find(f"{W}pPr/{W}spacing"), nuevo.find(f"{W}pPr/{W}spacing")
            if sp_a is not None and sp_n is not None:
                sp_n.set(W + "before", "0")
                sp_a.set(W + "after", "0")
            actual.addnext(nuevo)
            clase[nuevo] = "cuerpo"
            pag_de[nuevo] = pag_de.get(el)
            actual = nuevo
            partidos += 1
    return partidos


def _antes_blanco(el, tab):
    """El texto del párrafo justo antes de `tab` acaba en blanco (o no hay texto antes)."""
    antes = ""
    for x in el.iter(W + "t", W + "tab"):
        if x is tab:
            break
        if x.getparent().tag == W + "r":
            antes += x.text or "" if x.tag == W + "t" else "\t"
    return not antes or antes[-1].isspace()


def partir_en_etiquetas(body, clase, pag_de, cadena, con_etiqueta):
    """pdf2docx junta a veces en un párrafo, sin salto de línea, un elemento de lista cuya última
    línea llega casi al margen y el elemento siguiente (visto en PDF de Google Docs con viñetas).
    Si dentro de un párrafo del .docx empieza un párrafo del PDF con viñeta o número de lista, el
    párrafo se parte ahí, en mitad del run si hace falta; el primer trozo pierde el espacio de
    después y el segundo el de antes (son elementos seguidos)."""
    partidos = 0
    pos = 0
    for el in [e for e in body.iter(W + "p") if clase.get(e) == "cuerpo" and e.getparent() is body]:
        t = nz(texto_p(el))
        k = cadena.find(t, pos, pos + len(t) + 400) if t else -1
        if k < 0:
            continue
        pos = k + len(t)
        dentro = sorted(i - k for i in con_etiqueta if k < i < k + len(t))
        actual = el
        hecho = 0
        for corte in dentro:
            nuevo = partir_parrafo(actual, corte - hecho)
            if nuevo is None:
                break
            clase[nuevo] = "cuerpo"
            pag_de[nuevo] = pag_de.get(el)
            sp_a, sp_n = actual.find(f"{W}pPr/{W}spacing"), nuevo.find(f"{W}pPr/{W}spacing")
            if sp_a is not None and sp_n is not None:
                sp_n.set(W + "before", "0")
                sp_a.set(W + "after", "0")
            hecho = corte
            actual = nuevo
            partidos += 1
    return partidos


def partir_parrafo(el, n):
    """Parte el párrafo tras sus `n` primeros caracteres visibles (los que cuenta nz) y devuelve el
    párrafo nuevo con el resto, a continuación; None si no hay tanto texto."""
    vistos = 0
    runs = [r for r in el if r.tag == W + "r"]
    for i, r in enumerate(runs):
        for t in [x for x in r if x.tag == W + "t"]:
            texto = t.text or ""
            m = len(nz(texto))
            if vistos + m <= n:
                vistos += m
                continue
            # El corte cae dentro de este texto: tras el (n − vistos)-ésimo carácter visible.
            falta, corte = n - vistos, 0
            for j, ch in enumerate(texto):
                if falta == 0:
                    corte = j
                    break
                if nz(ch):
                    falta -= 1
            antes, despues = texto[:corte], texto[corte:]
            nuevo = OxmlElement("w:p")
            ppr = el.find(W + "pPr")
            if ppr is not None:
                nuevo.append(deepcopy(ppr))
            # El resto de este run (desde este w:t) va a un run nuevo con el mismo formato.
            r2 = OxmlElement("w:r")
            if r.find(W + "rPr") is not None:
                r2.append(deepcopy(r.find(W + "rPr")))
            hijos = list(r)
            for x in hijos[hijos.index(t) + 1:]:
                r2.append(x)
            if despues.strip():
                r2.insert(1 if r2.find(W + "rPr") is not None else 0, run_con(despues.lstrip(), None).find(W + "t"))
            t.text = antes.rstrip()
            if not t.text:
                r.remove(t)
            if any(x.tag != W + "rPr" for x in r2):
                nuevo.append(r2)
            for x in runs[i + 1:]:
                nuevo.append(x)
            if not any(x.tag not in (W + "rPr",) for x in r):
                el.remove(r)
            el.addnext(nuevo)
            return nuevo
    return None


def etiqueta_con_tabulador(el, etiqueta):
    """Sangría francesa: los tabuladores que pdf2docx pone donde se partía la línea pasan a
    espacios y entre la etiqueta («1.º», «Documento n.º 1.–») y el texto va un tabulador (pdf2docx
    los dejaba pegados: «1.ºSe condene»)."""
    for tab in list(el.iter(W + "tab")):
        if tab.getparent().tag == W + "r":
            t = OxmlElement("w:t")
            t.set(qn("xml:space"), "preserve")
            t.text = "" if _antes_blanco(el, tab) else " "
            tab.getparent().replace(tab, t)
    falta = len(etiqueta)
    for t in list(el.iter(W + "t")):
        texto = t.text or ""
        n = len(nz(texto))
        if n < falta:
            falta -= n
            continue
        # El corte va tras el carácter `falta`-ésimo no blanco de este texto.
        vistos, corte = 0, len(texto)
        for j, ch in enumerate(texto):
            if not ch.isspace():
                vistos += 1
                if vistos == falta:
                    corte = j + 1
                    break
        r = t.getparent()
        antes, resto = texto[:corte], texto[corte:].lstrip()
        t.text = antes
        rpr = r.find(W + "rPr")
        tab_r = OxmlElement("w:r")
        if rpr is not None:
            tab_r.append(deepcopy(rpr))
        tab_r.append(OxmlElement("w:tab"))
        r.addnext(tab_r)
        if resto:
            tab_r.addnext(run_con(resto, rpr))
        else:
            # El texto sigue en el run siguiente: sin el espacio con que empezaba.
            sig = tab_r.getnext()
            while sig is not None and sig.find(W + "t") is None:
                sig = sig.getnext()
            if sig is not None:
                st = sig.find(W + "t")
                st.text = (st.text or "").lstrip()
        return True
    return False


def tab_inicial(l):
    """x del carácter Cf con que empieza la línea si tras él hay un hueco de tabulador (≥ 20 pt)
    hasta el texto: el U+200B que Google Docs dibuja al principio de un párrafo que empieza con
    tabulador. Si no, None."""
    cs = l.get("crudos") or []
    if not cs or unicodedata.category(cs[0]["c"]) != "Cf":
        return None
    x = cs[0]["bbox"][0]
    return x if l["x0"] - x >= 20 else None


def empezar_con_tabulador(el):
    """Que el primer contenido del párrafo sea un w:tab (si no, se añade un run con él)."""
    for r in el.iter(W + "r"):
        hijos = [x for x in r if x.tag in (W + "t", W + "tab")]
        if not hijos or (hijos[0].tag == W + "t" and not (hijos[0].text or "").strip()):
            continue
        if hijos[0].tag == W + "tab":
            return False
        nuevo = OxmlElement("w:r")
        if r.find(W + "rPr") is not None:
            nuevo.append(deepcopy(r.find(W + "rPr")))
        nuevo.append(OxmlElement("w:tab"))
        r.addprevious(nuevo)
        t = hijos[0]
        t.text = t.text.lstrip()
        return True
    return False


def sangrias_del_pdf(a, comienzo, bloques, starts, m, inf, listas=None):
    """Sangrías de cada párrafo con la geometría del PDF, donde pdf2docx las pierde:
      - primera línea más adentro que las demás: sangría de primera línea (pdf2docx ponía a veces
        un tabulador o nada, f02);
      - primera línea más afuera, con un hueco tras la etiqueta donde empiezan las demás: sangría
        francesa con tabulador (pdf2docx: sin sangría y la etiqueta pegada al texto, f05);
      - una sola línea a la distancia de la sangría de primera línea habitual («En su virtud,»):
        sangría de primera línea, no izquierda."""
    import bisect
    inf["sangria_primera"] = inf["sangria_francesa"] = 0
    medio = (a["izq"] + a["der"]) / 2

    def centrado(ls):
        return all(abs((l["x0"] + l["x1"]) / 2 - medio) <= 6 and l["x0"] > a["izq"] + 4 for l in ls)

    def rel(x):
        v = round(x * TW) - m["left"]
        return v if v > 60 else 0

    primeras = [ls[0]["x0"] - ls[1]["x0"] for _, ls in bloques if len(ls) >= 2 and not centrado(ls)
                and ls[0]["x0"] > ls[1]["x0"] + 3 and abs(ls[1]["x0"] - a["izq"]) <= 1.5]
    habitual = statistics.median(primeras) if primeras else None
    inf["tabulador_inicial"] = 0
    def francesa(el, l0, a0, b, corte):
        """Sangría francesa con tabulador: la etiqueta en a0 y el texto (y las demás líneas) en b."""
        etiqueta = nz("".join(c["c"] for s in l0["spans"] for c in s["chars"] if c["bbox"][0] < corte - 1))
        poner_ind(el, left=rel(b), hanging=round((b - a0) * TW), firstLine=None)
        ppr = el.find(W + "pPr")
        tabs = ppr.find(W + "tabs")
        if tabs is not None:
            ppr.remove(tabs)
        tabs = OxmlElement("w:tabs")
        tab = OxmlElement("w:tab")
        tab.set(W + "val", "left")
        tab.set(W + "pos", str(rel(b)))
        tabs.append(tab)
        # En el orden del esquema de pPr: tras widowControl, numPr, pBdr y shd.
        antes_de = {W + x for x in ("suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct",
                                    "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
                                    "snapToGrid", "spacing", "ind", "contextualSpacing", "jc")}
        sig = next((x for x in ppr if x.tag in antes_de), None)
        if sig is not None:
            sig.addprevious(tabs)
        else:
            ppr.append(tabs)
        if etiqueta:
            etiqueta_con_tabulador(el, etiqueta)
        inf["sangria_francesa"] += 1
        et = etiqueta_de(l0)
        if listas is not None and et is not None:
            sp = next(sp for sp in l0["spans"] if any(visible(c["c"]) for c in sp["chars"]))
            listas.append({"el": el, "etiqueta": et[0], "left": rel(b), "hanging": round((b - a0) * TW),
                           "fuente": familia_pdf(sp["font"]), "tam": sp["size"]})

    for el, k in comienzo.items():
        if el.getparent() is None:
            continue
        b0, ls = bloques[bisect.bisect_right(starts, k) - 1]
        if b0 != k or centrado(ls):
            continue
        # Párrafo que empieza con tabulador (Google Docs: U+200B en la sangría y el texto en el tope
        # siguiente): el original llevaba un w:tab, no una sangría de primera línea. Se conserva (o se
        # pone, si pdf2docx lo convirtió en tabla falsa) y la sangría es la de las demás líneas.
        xt = tab_inicial(ls[0])
        if xt is not None:
            izq_p = statistics.median([l["x0"] for l in ls[1:]]) if len(ls) >= 2 else xt
            if abs(xt - izq_p) <= 3:
                poner_ind(el, left=rel(izq_p), firstLine=None, hanging=None)
                empezar_con_tabulador(el)
                inf["tabulador_inicial"] += 1
                continue
        if len(ls) >= 2:
            resto = [l["x0"] for l in ls[1:]]
            b = statistics.median(resto)
            if any(abs(x - b) > 1.5 for x in resto):
                continue
            a0 = ls[0]["x0"]
            if a0 > b + 3:
                poner_ind(el, left=rel(b), firstLine=round((a0 - b) * TW), hanging=None)
                inf["sangria_primera"] += 1
            elif a0 < b - 3:
                corte = corte_frances(ls[0], ls[1]) if ls[1]["x0"] > ls[0]["x0"] + 3 else None
                if corte is None:
                    continue
                francesa(el, ls[0], a0, b, corte)
        elif etiqueta_de(ls[0]):
            # Elemento de lista de una sola línea: sin segunda línea, la sangría es la x del texto tras
            # la etiqueta (left = x del texto − margen; francesa = x del texto − x de la etiqueta).
            # Sin esto quedaba sin sangría francesa ni tabulador, y --listas-word no lo convertía.
            et = etiqueta_de(ls[0])
            if et[1] > ls[0]["x0"] + 3:
                francesa(el, ls[0], ls[0]["x0"], et[1], et[1])
        elif habitual is not None and abs(ls[0]["x0"] - a["izq"] - habitual) <= 2:
            ind = el.find(f"{W}pPr/{W}ind")
            if ind is not None and int(ind.get(W + "firstLine", "0")) == 0 and int(ind.get(W + "left", "0")) > 60:
                poner_ind(el, left=0, firstLine=round(habitual * TW), hanging=None)
                inf["sangria_primera"] += 1


def metricas_fuentes(doc):
    """Por nombre de fuente (sin prefijo de subconjunto), (altura de línea, ascendente) por unidad de
    tamaño, de la tabla hhea de la fuente incrustada: (ascent − descent + lineGap) / unitsPerEm, que
    es la altura de una línea sencilla en LibreOffice y en Word (con estas fuentes, la de Windows,
    winAscent + winDescent + interlineado externo, da lo mismo). Las que no se pueden leer no están."""
    from io import BytesIO

    from fontTools.ttLib import TTFont
    out = {}
    vistos = set()
    for page in doc:
        for f in page.get_fonts():
            if f[0] in vistos:
                continue
            vistos.add(f[0])
            try:
                _, ext, _, buf = doc.extract_font(f[0])
                if ext in ("n/a", "cff") or not buf:
                    continue
                tt = TTFont(BytesIO(buf))
                h, upm = tt["hhea"], tt["head"].unitsPerEm
                out[re.sub(r"^[A-Z]{6}\+", "", f[3])] = ((h.ascent - h.descent + h.lineGap) / upm, h.ascent / upm)
            except Exception:  # noqa: BLE001 — una fuente que no se lee no tiene métricas
                continue
    return out


def fuente_de(ls, met):
    """(tamaño, altura de línea, ascendente) en pt de la fuente que más texto lleva en las líneas."""
    c = Counter()
    for l in ls:
        for sp in l["spans"]:
            c[(sp["font"], round(sp["size"], 1))] += len(sp["text"].strip())
    (nombre, tam), _ = c.most_common(1)[0]
    alto, asc = met.get(nombre, (None, None))
    if alto is None:
        sp = next(sp for l in ls for sp in l["spans"] if sp["font"] == nombre)
        alto, asc = sp["ascender"] - sp["descender"], sp["ascender"]
    return tam, alto * tam, asc * tam


def alto_linea(l, met):
    """(altura de línea sencilla, ascendente) en pt de una línea: los máximos de sus fuentes, como
    los calcula LibreOffice (la viñeta en Arial sube la línea de un texto en Courier New)."""
    hs = []
    for sp in l["spans"]:
        if not sp["text"].strip():
            continue
        alto, asc = met.get(sp["font"], (sp["ascender"] - sp["descender"], sp["ascender"]))
        hs.append((alto * sp["size"], asc * sp["size"]))
    return max(h for h, _ in hs), max(x for _, x in hs)


def interlineado_auto(a):
    """Múltiplo de interlineado del documento, medido en el PDF: paso entre las líneas de un mismo
    párrafo (en la misma página) dividido por la altura de línea sencilla de su fuente. Google Docs
    a 1,5 líneas: Courier New 13, 22,09 / 14,73 = 1,50; Times 12, 20,70 / 13,80 = 1,50. pdf2docx
    lo estimaba por párrafo con la caja del bloque (310 a 346 en vez de 360) y el texto subía."""
    met = metricas_fuentes(a["doc"])
    empieza = parrafos_pdf(a)
    bloques, actual = [], None
    for k, (p, _, l) in enumerate(a["cuerpo"]):
        if empieza[k] or actual is None:
            actual = []
            bloques.append(actual)
        actual.append((p, l))
    medidas = []
    for b in bloques:
        pasos = [l["base"] - q["base"] for (p0, q), (p1, l) in zip(b, b[1:]) if p0 == p1]
        if pasos:
            _, h, _ = fuente_de([l for _, l in b], met)
            medidas += [statistics.median(pasos) / h] * len(pasos)
    doc = round(statistics.median(medidas), 2) if medidas else 1.0
    return {"doc": doc, "met": met}


def limite_inferior(a, multiplo):
    """Con interlineado automático, dónde acaba el texto de la página (pt desde arriba) para que
    LibreOffice corte las páginas donde el PDF. Google Docs mete una línea si cabe entera, con el
    espacio de más del interlineado; LibreOffice, si cabe su texto (la altura de línea sencilla) y
    deja que ese espacio se salga del margen. Con el margen a 2 pt de la letra más baja, en las
    páginas que el PDF acababa a 0,3 pt de no caber otra línea LibreOffice metía una más.
    La línea más baja del PDF (su parte de arriba) da el mínimo; la que Google Docs no metió en cada
    página (la última más un paso) da el máximo; el límite va en medio, más la altura sencilla."""
    met = multiplo["met"]
    ultimas = {}
    for p, _, l in a["cuerpo"]:
        if p not in ultimas or l["base"] > ultimas[p]["base"]:
            ultimas[p] = l
    if len(ultimas) < 2:
        return None
    alto, _ = alto_linea(max(ultimas.values(), key=lambda l: l["y0"]), met)
    paso = multiplo["doc"] * alto
    abajo = max(l["y0"] for l in ultimas.values())
    paginas = sorted(ultimas)
    techos = [ultimas[p]["y0"] + paso for p in paginas[:-1] if ultimas[p]["y0"] + paso > abajo + 1]
    arriba = min(techos) if techos else abajo + paso
    return (abajo + arriba) / 2 + alto


def titulos_con_siguiente(a, comienzo, bloques, starts, multiplo, limite, inf):
    """Google Docs pasa a la página siguiente un título con «conservar con el siguiente» que no
    cabe con la primera línea de lo que le sigue, y pdf2docx no lo conserva: LibreOffice lo dejaba
    al pie. Si una página del PDF acaba con sitio para otra línea y la siguiente empieza con un
    párrafo de una sola línea seguido de otro párrafo, ese párrafo conserva con el siguiente."""
    import bisect
    inf["titulos_con_siguiente"] = 0
    if not limite:
        return
    met = multiplo["met"]
    cuerpo = a["cuerpo"]
    el_de = {k: el for el, k in comienzo.items() if el.getparent() is not None}
    for k in range(1, len(cuerpo)):
        p, _, l = cuerpo[k]
        q, _, prev = cuerpo[k - 1]
        if p == q:
            continue
        alto, _ = alto_linea(prev, met)
        if prev["y0"] + multiplo["doc"] * alto + alto > limite:
            continue  # la página estaba llena
        i = bisect.bisect_right(starts, sum(len(nz(x[2]["texto"])) for x in cuerpo[:k])) - 1
        b0, ls = bloques[i]
        if len(ls) != 1 or ls[0] is not l or i + 1 >= len(bloques) or b0 not in el_de:
            continue
        ppr = el_de[b0].find(W + "pPr")
        if ppr is not None and ppr.find(W + "keepNext") is None:
            ppr.insert(1 if ppr.find(W + "pStyle") is not None else 0, OxmlElement("w:keepNext"))
            inf["titulos_con_siguiente"] += 1


def espacios_del_pdf(a, body, clase, comienzo, bloques, starts, multiplo, inf):
    """Interlineado y espacio entre párrafos tomados del PDF cuando pdf2docx usa interlineado
    automático (los PDF de Google Docs). Con margen superior 0 (Google Docs pinta el fondo de la
    página entera), pdf2docx ponía como «antes» del primer párrafo de cada página toda la distancia
    al borde (unos 80 pt) y el posprocesado la pasaba a «después» del párrafo anterior: un hueco en
    medio del texto. Ahora:
      - interlineado: el múltiplo medido en el PDF (`interlineado_auto`), automático, para que el
        texto siga fluyendo al editarlo (sin secciones por página ni interlineado exacto);
      - entre dos párrafos de la misma página: lo que queda de la distancia entre la última línea
        de uno y la primera del otro tras la altura de la línea (LibreOffice deja el espacio de más
        del interlineado debajo del texto), como «después» del primero;
      - a un cambio de página: el espacio habitual entre párrafos."""
    import bisect
    L = round(240 * multiplo["doc"])
    met = multiplo["met"]
    pars = [el for el in body if el.tag == W + "p" and clase.get(el) == "cuerpo"]
    for el in pars:
        sp = el.find(f"{W}pPr/{W}spacing")
        if sp is not None and sp.get(W + "lineRule", "auto") == "auto":
            sp.set(W + "line", str(L))
    inf["interlineado"] = L

    def lineas(el):
        """Primera y última línea del PDF del párrafo, por su posición en la cadena."""
        k = comienzo.get(el)
        if k is None:
            return None
        n = len(nz(texto_p(el)))
        i0 = bisect.bisect_right(starts, k) - 1
        i1 = bisect.bisect_right(starts, k + max(0, n - 1)) - 1
        return bloques[i0][1], bloques[i1][1]

    def pag(l):
        return next(p for p, _, x in a["cuerpo"] if x is l)

    pares = []
    hijos = list(body)
    for x, y in zip(hijos, hijos[1:]):
        if x in comienzo and y in comienzo and clase.get(x) == clase.get(y) == "cuerpo":
            lx, ly = lineas(x), lineas(y)
            if lx and ly:
                pares.append((x, y, lx[1], ly[0]))
    medidos = []
    for x, y, bx, by in pares:
        ultima, primera = bx[-1], by[0]
        if pag(ultima) == pag(primera):
            hx, ax = alto_linea(ultima, met)
            _, ay = alto_linea(primera, met)
            medidos.append((x, y, primera["base"] - ultima["base"] - multiplo["doc"] * hx - (ay - ax)))
    habitual = statistics.median([e for _, _, e in medidos if e > 1]) if any(e > 1 for _, _, e in medidos) else 0
    hechos = {(x, y): e for x, y, e in medidos}
    inf["espacios_del_pdf"] = 0
    for x, y, _, _ in pares:
        e = hechos.get((x, y), habitual)
        sx, sy = x.find(f"{W}pPr/{W}spacing"), y.find(f"{W}pPr/{W}spacing")
        if sx is None or sy is None:
            continue
        sx.set(W + "after", str(max(0, round(e * TW))))
        sy.set(W + "before", "0")
        inf["espacios_del_pdf"] += 1


def sangria_derecha(a, comienzo, bloques, starts, inf):
    """pdf2docx pone a un párrafo sin justificar una sangría derecha hasta su línea más larga (79 pt
    en una viñeta de un PDF de Google Docs) y, al editar, las líneas se parten antes. Si en cada corte de línea
    del PDF la primera palabra de la línea siguiente no cabía antes del margen derecho, es el margen
    el que corta: sangría derecha 0. Si en algún corte sí cabía, hay una sangría de verdad (una cita)
    y se deja. Solo en bloques sin justificar o elementos de lista."""
    import bisect
    inf["sangria_derecha_quitada"] = 0
    for el, k in comienzo.items():
        if el.getparent() is None:
            continue
        b0, ls = bloques[bisect.bisect_right(starts, k) - 1]
        ind = el.find(f"{W}pPr/{W}ind")
        if b0 != k or len(ls) < 2 or ind is None or int(ind.get(W + "right", "0")) <= 0:
            continue
        # Solo en un bloque sin justificar (los finales de las líneas que no son la última varían más
        # de 3 pt) o en un elemento de lista: una cita justificada con sangría a los dos lados acaba
        # todas sus líneas en el mismo sitio y, con 25 pt de sangría, la palabra siguiente tampoco
        # cabía nunca (e12).
        finales = [l["x1"] for l in ls[:-1]]
        if not (len(finales) >= 2 and max(finales) - min(finales) > 3 or etiqueta_de(ls[0])):
            continue
        cabia = False
        for l, sig in zip(ls, ls[1:]):
            letras = [c for s in sig["spans"] for c in s["chars"]]
            palabra = []
            for c in letras:
                if not c["c"].strip() and palabra:
                    break
                if visible(c["c"]):
                    palabra.append(c)
            esp = [c["bbox"][2] - c["bbox"][0] for s in l["spans"] for c in s["chars"] if c["c"] == " "]
            hueco = statistics.median(esp) if esp else 0.25 * l["tam"]
            if palabra and l["x1"] + hueco + (palabra[-1]["bbox"][2] - palabra[0]["bbox"][0]) <= a["der"] + 0.5:
                cabia = True
                break
        if not cabia:
            ind.set(W + "right", "0")
            inf["sangria_derecha_quitada"] += 1


RE_NUM = re.compile(r"^(?:(\d{1,3})|([ivx]{1,5})|([IVX]{1,5})|([a-z])|([A-Z]))([.)])$")


def listas_de_word(doc, body, registros, reconocidos, inf):
    """Viñetas y números de lista como listas de Word (numPr): sin la etiqueta escrita ni el
    tabulador que la sigue, con una definición por lista (un nivel por sangría; las que distan menos
    de 4 pt son el mismo nivel) que empieza donde el PDF y lleva la fuente y el tamaño de la
    etiqueta del PDF. Una lista son los elementos reconocidos (`reconocidos`: los párrafos que en el
    PDF empiezan con viñeta o número) seguidos; acaba en el primer párrafo que no lo es o donde
    cambia la etiqueta del primer nivel (una lista de viñetas seguida de una numerada). Todo o nada:
    si algún elemento de una lista no se puede convertir (sin sangría francesa registrada, etiqueta
    que no se sabe representar, niveles que mezclan etiquetas), no se convierte ninguno de esa lista
    y el informe dice por qué."""
    numeracion = doc.part.numbering_part.element
    ids = [int(x.get(W + "abstractNumId")) for x in numeracion.findall(W + "abstractNum")] + [0]
    nums = [int(x.get(W + "numId")) for x in numeracion.findall(W + "num")] + [0]
    sig_abs, sig_num = max(ids) + 1, max(nums) + 1
    de_el = {r["el"]: r for r in registros}
    seguidos, actual = [], []
    for el in body:
        if el in reconocidos:
            actual.append(el)
        elif el.tag == W + "p" and texto_p(el).strip() or el.tag == W + "tbl":
            if actual:
                seguidos.append(actual)
            actual = []
    if actual:
        seguidos.append(actual)
    inf["listas_word"] = {"listas": 0, "elementos": 0, "sin_convertir": []}

    def formato(et):
        if et in VINETAS:
            return "bullet", et, None
        m = RE_NUM.match(et)
        if not m:
            return None
        n, rl, ru, ll, lu, suf = m.groups()
        if n:
            return "decimal", suf, int(n)
        if rl or ru:
            r = rl or ru
            val = sum({"i": 1, "v": 5, "x": 10}[c] for c in r.lower())  # aproximado: i, ii, iii, iv…
            val = {"iv": 4, "ix": 9}.get(r.lower(), val)
            return ("lowerRoman" if rl else "upperRoman"), suf, val
        return ("lowerLetter" if ll else "upperLetter"), suf, ord((ll or lu).lower()) - 96

    def clase_et(et):
        """Lo que tiene que ser igual en todos los elementos de un nivel: la viñeta, o el tipo de
        número y su signo."""
        f = formato(et)
        return None if f is None else (f[0], f[1])

    grupos = []
    for els in seguidos:
        g = []
        for el in els:
            r = de_el.get(el)
            if r is None:
                g.append({"el": el, "etiqueta": None, "left": None})
                continue
            # Una viñeta tras una lista numerada (o al revés) en el primer nivel empieza otra lista.
            if g and g[0]["etiqueta"] and abs(r["left"] - g[0]["left"]) < 80 and \
                    clase_et(r["etiqueta"]) != clase_et(g[0]["etiqueta"]):
                grupos.append(g)
                g = []
            g.append(r)
        grupos.append(g)

    for g in grupos:
        motivo = None
        if any(r["etiqueta"] is None for r in g):
            motivo = "elemento sin sangría francesa reconocida"
        elif any(formato(r["etiqueta"]) is None for r in g):
            motivo = "etiqueta que no se sabe representar"
        if not motivo:
            # Niveles: sangrías a menos de 4 pt (80 twips) son el mismo; cada nivel, en la mayor de
            # las suyas (un número de una línea tiene el texto antes del tope: 105,6 frente a 108).
            grupos_l = []
            for left in sorted({r["left"] for r in g}):
                if grupos_l and left - grupos_l[-1][-1] < 80:
                    grupos_l[-1].append(left)
                else:
                    grupos_l.append([left])
            nivel_de = {x: i for i, gl in enumerate(grupos_l) for x in gl}
            lefts = [max(gl) for gl in grupos_l]
            niveles = {}
            for r in g:
                r["nivel"] = nivel_de[r["left"]]
                etiqueta_x = r["left"] - r["hanging"]
                r["left"] = lefts[r["nivel"]]
                r["hanging"] = r["left"] - etiqueta_x
                f = formato(r["etiqueta"])
                if r["nivel"] in niveles and clase_et(niveles[r["nivel"]][1]["etiqueta"]) != clase_et(r["etiqueta"]):
                    motivo = "un nivel mezcla etiquetas distintas"
                    break
                niveles.setdefault(r["nivel"], (f, r))
        if motivo:
            inf["listas_word"]["sin_convertir"].append({"elementos": len(g), "motivo": motivo})
            continue
        an = OxmlElement("w:abstractNum")
        an.set(W + "abstractNumId", str(sig_abs))
        mlt = OxmlElement("w:multiLevelType")
        mlt.set(W + "val", "hybridMultilevel")
        an.append(mlt)
        for lv in range(9):
            (fmt, suf, inicio), r = niveles.get(lv, (("decimal", ".", 1), None))
            ref = r or g[0]
            left = ref["left"] if r else lefts[-1] + 720 * (lv - len(lefts) + 1)
            texto = suf if fmt == "bullet" else f"%{lv + 1}{suf}"
            rpr = (f'<w:rPr><w:rFonts w:ascii="{ref["fuente"]}" w:hAnsi="{ref["fuente"]}" w:cs="{ref["fuente"]}"/>'
                   f'<w:sz w:val="{round(ref["tam"] * 2)}"/></w:rPr>')
            an.append(parse_xml(
                f'<w:lvl {nsdecls("w")} w:ilvl="{lv}"><w:start w:val="{inicio or 1}"/><w:numFmt w:val="{fmt}"/>'
                f'<w:suff w:val="tab"/><w:lvlText w:val="{escapar(texto)}"/><w:lvlJc w:val="left"/>'
                f'<w:pPr><w:ind w:left="{left}" w:hanging="{ref["hanging"]}"/></w:pPr>{rpr}</w:lvl>'))
        # abstractNum va antes de todos los w:num en el esquema.
        primero_num = numeracion.find(W + "num")
        if primero_num is not None:
            primero_num.addprevious(an)
        else:
            numeracion.append(an)
        num = parse_xml(f'<w:num {nsdecls("w")} w:numId="{sig_num}"><w:abstractNumId w:val="{sig_abs}"/></w:num>')
        numeracion.append(num)
        for r in g:
            quitar_etiqueta(r["el"])
            poner_ind(r["el"], left=r["left"], hanging=r["hanging"], firstLine=None)
            ppr = r["el"].find(W + "pPr")
            tabs = ppr.find(W + "tabs")
            if tabs is not None:
                ppr.remove(tabs)
            npr = parse_xml(f'<w:numPr {nsdecls("w")}><w:ilvl w:val="{r["nivel"]}"/>'
                            f'<w:numId w:val="{sig_num}"/></w:numPr>')
            # Tras pStyle, keepNext, keepLines, pageBreakBefore, framePr y widowControl.
            previos = [x for x in ppr if x.tag in {W + t for t in ("pStyle", "keepNext", "keepLines",
                                                                      "pageBreakBefore", "framePr",
                                                                      "widowControl")}]
            if previos:
                previos[-1].addnext(npr)
            else:
                ppr.insert(0, npr)
        sig_abs += 1
        sig_num += 1
        inf["listas_word"]["listas"] += 1
        inf["listas_word"]["elementos"] += len(g)


def quitar_etiqueta(el):
    """Quita del párrafo todo lo que hay hasta el primer tabulador incluido (la etiqueta)."""
    for r in [r for r in el.iter(W + "r")]:
        hijos = [x for x in r if x.tag in (W + "t", W + "tab")]
        for x in hijos:
            r.remove(x)
            if x.tag == W + "tab":
                if not any(y.tag != W + "rPr" for y in r):
                    r.getparent().remove(r)
                return
        if not any(y.tag != W + "rPr" for y in r):
            r.getparent().remove(r)


def bloques_juntos(a, comienzo, bloques, pagina_de_linea):
    """Líneas centradas sueltas seguidas, con el interlineado normal entre ellas (la firma «Ldo. … /
    Col. … / Proc. … / Col. …», ya una por párrafo): si en el PDF están todas en la misma página,
    «conservar con el siguiente» en todas menos la última, para que sigan juntas. En el original de
    f06 la firma empieza la página 8; en el .docx, sin el párrafo vacío que la separaba de la fecha
    (al pasar de página pdf2docx deja ese espacio a 0), sus dos primeras líneas cabían al pie de la
    7. Si en el PDF el bloque está partido entre páginas, no se toca."""
    medio = (a["izq"] + a["der"]) / 2
    normal = a["paso_cuerpo"] or 14
    el_de = {k: el for el, k in comienzo.items() if el.getparent() is not None}

    def suelta(ls):
        l = ls[0]
        return len(ls) == 1 and abs((l["x0"] + l["x1"]) / 2 - medio) <= 6 and l["x0"] > a["izq"] + 4

    # Al cambiar de página el bloque sigue solo si su última línea estaba al pie (no cabía otra); si
    # la página acabó antes (f06: un párrafo vacío entre la fecha y la firma), es otro bloque.
    fondo = max(l["y1"] for _, _, l in a["cuerpo"])

    def sigue(prev, l):
        if pagina_de_linea[id(l)] != pagina_de_linea[id(prev)]:
            return prev["y1"] >= fondo - normal * 1.25
        return l["base"] - prev["base"] <= normal * 1.25

    grupos, actual = [], []
    for k0, ls in bloques:
        if suelta(ls) and k0 in el_de and actual and sigue(actual[-1][1], ls[0]):
            actual.append((k0, ls[0]))
            continue
        grupos.append(actual)
        actual = [(k0, ls[0])] if suelta(ls) and k0 in el_de else []
    grupos.append(actual)
    # Solo los bloques que el PDF tiene enteros en una página (en f01 la firma está partida 3 + 1:
    # juntarla la pasaba entera a la página siguiente).
    grupos = [g for g in grupos if len(g) >= 2 and len({pagina_de_linea[id(l)] for _, l in g}) == 1]
    n = 0
    for g in grupos:
        for k0, _ in g[:-1]:
            ppr = el_de[k0].find(W + "pPr")
            if ppr is not None and ppr.find(W + "keepNext") is None:
                # keepNext va al principio de pPr (solo pStyle delante) en el orden del esquema.
                ppr.insert(1 if ppr.find(W + "pStyle") is not None else 0, OxmlElement("w:keepNext"))
                n += 1
    return n


# --- Posprocesado ---------------------------------------------------------------------------

def posprocesar(pdf, docx_in, docx_out, notas=True, listas=True):
    a = analizar_pdf(pdf)
    doc = Document(str(docx_in))
    body = doc.element.body
    segs = segmentos(body)
    inf = {"paginas_pdf": a["n"], "secciones_docx": len(segs), "cabecera": [], "cabecera_primera": [],
           "pie": [], "campos_pagina": 0, "parrafos_unidos": 0, "saltos_de_linea": 0, "centrados": 0,
           "notas": {"pdf": sum(len(x) for x in a["notas"]), "convertidas": 0, "motivo": None}}
    if len(segs) != a["n"]:
        inf["error"] = f"pdf2docx dio {len(segs)} secciones para {a['n']} páginas: sin posprocesar"
        doc.save(str(docx_out))
        return inf
    sects = [s[-1].find(f"{W}pPr/{W}sectPr") if s[-1].tag == W + "p" else None for s in segs]
    sects[-1] = body.find(W + "sectPr")
    mg = [margenes(s) if s is not None else margenes(sects[-1]) for s in sects]

    # 0. Tablas falsas: pdf2docx convierte en tabla de una fila una línea justificada muy estirada
    # (una palabra larga, p. ej. un código seguro de verificación, no cabe y la línea se estira; si
    # es la última de la página, también). Vuelven a ser texto y luego se unen a su párrafo.
    # Interlineado del cuerpo según pdf2docx: «exacto» cuando no lee las métricas de las fuentes
    # (los PDF de LibreOffice del banco) y «automático» (múltiplo) cuando sí (los de Google Docs).
    reglas = Counter(sp.get(W + "lineRule", "auto") for el in body.iter(W + "p")
                     for sp in [el.find(f"{W}pPr/{W}spacing")] if sp is not None and sp.get(W + "line"))
    auto = bool(reglas) and reglas["auto"] > reglas["exact"]
    inf["interlineado_pdf2docx"] = "auto" if auto else "exact"
    multiplo = interlineado_auto(a) if auto else None

    # 0. Fuentes sin nombre (rFonts vacío), antes de copiar runs a ningún sitio.
    fuentes_vacias(a, body, inf)
    inf["tablas_falsas"] = []
    convertir_tablas_falsas(a, body, segs, mg, inf,
                            ("auto", round(240 * multiplo["doc"])) if multiplo else None)

    # 1. Qué párrafo del .docx es qué, por página.
    clase = {}
    pag_de = {}
    for p, elementos in enumerate(segs):
        zonas = {}
        for i, l in enumerate(a["pags"][p]):
            z = a["zona"].get((p, i))
            if z:
                zonas.setdefault("nota" if z == "nota" else "cabpie", []).append(nz(l["texto"]))
        for el in elementos:
            pag_de[el] = p
            if el.tag != W + "p":
                continue
            t = nz(texto_p(el))
            if not t:
                clase[el] = "vacio"
                continue
            for z, ts in zonas.items():
                junto = "".join(ts)
                if (len(t) >= 3 and t in junto) or t in ts:
                    clase[el] = z
                    break
            else:
                clase[el] = "cuerpo"

    primeros_de_pagina = set()
    for p, elementos in enumerate(segs[1:], 1):
        for el in elementos:
            if clase.get(el) == "cuerpo":
                primeros_de_pagina.add(el)
                break

    # 2. Notas al pie: localizar todo antes de tocar nada.
    plan_notas = None
    if notas and inf["notas"]["pdf"]:
        nums = [x["num"] for ns in a["notas"] for x in ns]
        marcas = sorted(m[1] for m in a["llamadas"])
        if nums != list(range(1, len(nums) + 1)):
            inf["notas"]["motivo"] = f"numeración de las notas no consecutiva: {nums}"
        elif marcas != nums:
            inf["notas"]["motivo"] = f"llamadas {marcas} no cuadran con notas {nums}"
        else:
            runs = []
            siguiente = 1
            sz_cuerpo = round(a["tam_cuerpo"] * 2)
            for el in body.iter(W + "p"):
                if clase.get(el) != "cuerpo":
                    continue
                for r in el.findall(W + "r"):
                    t = "".join(x.text or "" for x in r.findall(W + "t")).strip()
                    sz = r.find(f"{W}rPr/{W}sz")
                    if t == str(siguiente) and sz is not None and int(sz.get(W + "val")) < sz_cuerpo * 0.8:
                        runs.append(r)
                        siguiente += 1
            if len(runs) != len(nums):
                inf["notas"]["motivo"] = f"solo se localizan {len(runs)} de {len(nums)} llamadas en el .docx"
            else:
                plan_notas = runs

    # 3. Cabecera y pie desde las líneas del PDF.
    def lineas_zona(p, zs):
        return [l for i, l in enumerate(a["pags"][p]) if a["zona"].get((p, i)) in zs]

    n = a["n"]
    ref = 1 if n >= 2 else 0
    cab = lineas_zona(ref, {"cab"})
    cab1 = lineas_zona(0, {"cab", "cab1"}) if any(z == "cab1" for (pp, _), z in a["zona"].items() if pp == 0) else None
    pie = lineas_zona(ref, {"pie"})
    pie1 = lineas_zona(0, {"pie"})

    # 4. Una sola sección, con márgenes del PDF.
    nuevo = deepcopy(sects[-1])
    pg = nuevo.find(W + "pgMar")
    cuerpo_l = a["cuerpo"]
    if cuerpo_l:
        # Tamaño de página: Google Docs da A4 como 596 × 842 pt y LibreOffice lo ajusta al A4 de
        # verdad (595,3): la caja de texto quedaba 0,7 pt más estrecha y algunas líneas justificadas se
        # partían antes. A menos de 1,5 pt de un tamaño normalizado, el normalizado, y el margen
        # derecho se calcula con él para que el ancho del texto sea exactamente el del PDF.
        ancho_pg = a["w"]
        pgsz = nuevo.find(W + "pgSz")
        for wn, hn in ((11906, 16838), (12240, 15840), (12240, 20160)):  # A4, carta, legal (twips)
            if pgsz is not None and abs(a["w"] * TW - wn) <= 30 and abs(a["h"] * TW - hn) <= 30:
                pgsz.set(W + "w", str(wn))
                pgsz.set(W + "h", str(hn))
                ancho_pg = wn / TW
                break
        izq_t = round(a["izq"] * TW)
        der_t = round((ancho_pg - a["der"]) * TW)
        sup_p = [min(l["y0"] for pp, _, l in cuerpo_l if pp == p) for p in range(n)
                 if any(pp == p for pp, _, _ in cuerpo_l)]
        sup = min(sup_p[1:] or sup_p)
        abajo = max([l["y1"] for pp, _, l in cuerpo_l] +
                    [l["y1"] for p in range(n) for i, l in enumerate(a["pags"][p]) if a["zona"].get((p, i)) == "nota"])
        pg.set(W + "left", str(izq_t))
        pg.set(W + "right", str(der_t))
        # Con interlineado exacto, LibreOffice (y Word) bajan el texto dentro de la línea: unas dos
        # terceras partes de lo que el interlineado pasa de la altura natural de la línea (medido
        # en el banco: +0,9 pt con 15,85 pt de interlineado, +4,8 pt con 20,7 pt). Se compensa en el
        # margen superior para que la primera línea quede a la altura del PDF.
        alto = statistics.median(l["y1"] - l["y0"] for _, _, l in cuerpo_l)
        paso = a["paso_cuerpo"] or alto
        bajada = max(0.0, 0.65 * (paso - alto) - 0.7) if not auto else 0.0
        pg.set(W + "top", str(round((sup - 1 - bajada) * TW)))
        # Abajo, 2 pt bajo la letra más baja. Probado (tercera ronda): dejar más sitio para la caja de
        # la línea exacta hacía caber la línea suelta del TERCERO de e14 (le faltan unos 5 pt), pero
        # descolocaba la paginación de e11; se deja como estaba.
        pg.set(W + "bottom", str(max(0, round((a["h"] - abajo - 2) * TW))))
        limite = limite_inferior(a, multiplo) if multiplo and not inf["notas"]["pdf"] else None
        if limite:
            pg.set(W + "bottom", str(max(0, round((a["h"] - limite) * TW))))
            inf["limite_inferior"] = round(limite, 1)
        todas_cab = cab + (cab1 or [])
        if todas_cab:
            pg.set(W + "header", str(round(max(10, min(l["y0"] for l in todas_cab) - 1) * TW)))
        if pie or pie1:
            pg.set(W + "footer", str(round(max(10, a["h"] - max(l["y1"] for l in pie + pie1) - 1) * TW)))
    nuevos_m = margenes(nuevo)

    inf["tabuladores_recalculados"] = 0
    for p, elementos in enumerate(segs):
        for el in elementos:
            if el.tag == W + "p":
                s = el.find(f"{W}pPr/{W}sectPr")
                if s is not None:
                    s.getparent().remove(s)
            # Sangrías relativas al nuevo margen.
            if el.tag == W + "p" and clase.get(el) == "cuerpo":
                ind = el.find(f"{W}pPr/{W}ind")
                if ind is not None:
                    absl = mg[p]["left"] + int(ind.get(W + "left", "0"))
                    absr = mg[p]["right"] + int(ind.get(W + "right", "0"))
                    # A menos de 3 pt del margen es el margen: pdf2docx deja 1 pt de sangría derecha en
                    # el texto justificado y LibreOffice partía las líneas en otro sitio.
                    izq_n, der_n = absl - nuevos_m["left"], absr - nuevos_m["right"]
                    ind.set(W + "left", str(izq_n if izq_n > 60 else 0))
                    ind.set(W + "right", str(der_n if der_n > 60 else 0))
                # Tabuladores: w:tab/@pos se mide desde el margen izquierdo, como w:ind. pdf2docx los
                # escribe con el margen de su sección (0 en los PDF de Google Docs, que pintan el fondo
                # de la página entera) y el texto tras el tabulador caía un margen más a la derecha
                # (206,2 pt en vez de 121,1 en un PDF de Google Docs). Los que quedan en el margen o fuera, se quitan.
                tabs = el.find(f"{W}pPr/{W}tabs")
                if tabs is not None:
                    for tab in list(tabs):
                        pos = mg[p]["left"] + int(tab.get(W + "pos", "0")) - nuevos_m["left"]
                        if pos <= 0:
                            tabs.remove(tab)
                        else:
                            tab.set(W + "pos", str(pos))
                    if not len(tabs):
                        tabs.getparent().remove(tabs)
                    inf["tabuladores_recalculados"] += 1
    final = body.find(W + "sectPr")
    body.replace(final, nuevo)

    # 5. Fuera del cuerpo: cabecera, pie, notas (se guardan para crearlas luego) y vacíos de relleno.
    textos_nota = {}
    for el, c in list(clase.items()):
        if c in ("cabpie", "vacio") or (c == "nota" and plan_notas):
            if c == "nota":
                textos_nota.setdefault(pag_de[el], []).append(el)
            el.getparent().remove(el)

    # Los U+200B (y demás Cf) que Google Docs dibuja tras viñetas, números y al principio de los
    # párrafos con tabulador no son texto: fuera del .docx (el run que solo tenía eso, también).
    inf["caracteres_cf"] = 0
    for t in list(body.iter(W + "t")):
        if t.text and sin_cf(t.text) != t.text:
            inf["caracteres_cf"] += len(t.text) - len(sin_cf(t.text))
            t.text = sin_cf(t.text)
            r = t.getparent()
            if not t.text and r.tag == W + "r":
                r.remove(t)
                if not any(x.tag != W + "rPr" for x in r):
                    r.getparent().remove(r)

    # 6. Párrafos que fluyen: unir los trozos de un mismo párrafo del PDF.
    empieza = parrafos_pdf(a)
    cadena, inicios = "", set()
    bloques = []  # (posición en la cadena, líneas) de cada párrafo del PDF
    pagina_de_linea = {id(l): pg for pg, _, l in a["cuerpo"]}
    con_etiqueta = set()  # posiciones en la cadena de los párrafos del PDF que empiezan con etiqueta
    for k, (_, _, l) in enumerate(a["cuerpo"]):
        if empieza[k]:
            inicios.add(len(cadena))
            bloques.append((len(cadena), []))
            if etiqueta_de(l):
                con_etiqueta.add(len(cadena))
        bloques[-1][1].append(l)
        cadena += nz(l["texto"])
    starts = [b[0] for b in bloques]
    inf["parrafos_partidos"] = partir_en_saltos(body, clase, pag_de, cadena, inicios)
    inf["partidos_por_etiqueta"] = partir_en_etiquetas(body, clase, pag_de, cadena, con_etiqueta)
    comienzo = {}
    pars = [el for el in body.iter(W + "p") if clase.get(el) == "cuerpo" and el.getparent() is body]
    pos, previo = 0, None
    lineas_de = {}
    for el in pars:
        t = nz(texto_p(el))
        k = cadena.find(t, pos, pos + len(t) + 400)
        if k < 0:
            previo = None
            continue
        if previo is not None and k not in inicios and k == pos:
            # Continuación: los runs de este pasan al anterior, con un espacio entre medias.
            ult = previo.findall(W + "r")
            rpr = ult[-1].find(W + "rPr") if ult else None
            if not texto_p(previo).endswith((" ", "-")):
                previo.append(run_con(" ", rpr))
            # Lo que pdf2docx pone al principio del trozo para colocarlo (tabuladores, espacios) sobra
            # dentro del párrafo unido: daba «con la  valoración» o un tabulador a mitad de frase.
            inicio = True
            for hijo in list(el):
                if hijo.tag == W + "pPr":
                    continue
                if inicio and hijo.tag == W + "r":
                    for x in [x for x in hijo if x.tag in (W + "tab", W + "t")]:
                        if x.tag == W + "tab":
                            hijo.remove(x)
                        elif not inicio:
                            break
                        elif (x.text or "").strip():
                            x.text = x.text.lstrip()
                            inicio = False
                        else:
                            hijo.remove(x)
                    if not any(x.tag != W + "rPr" for x in hijo):
                        continue
                previo.append(hijo)
            # El espacio de después es el del último trozo: pdf2docx sitúa con él lo que sigue (una
            # tabla tras un párrafo que empezó en la página anterior perdía unos 12 pt, f04).
            sp_el, sp_prev = el.find(f"{W}pPr/{W}spacing"), previo.find(f"{W}pPr/{W}spacing")
            if sp_el is not None and sp_prev is not None:
                sp_prev.set(W + "after", sp_el.get(W + "after", "0"))
            el.getparent().remove(el)
            inf["parrafos_unidos"] += 1
            lineas_de[previo] = lineas_de.get(previo, 1) + 1
        else:
            previo = el
            lineas_de[el] = 1
            comienzo[el] = k
        pos = k + len(t)
    # Justificado: pdf2docx pone a veces «izquierda» a un párrafo justificado del PDF (todas sus
    # líneas menos la última llegan al margen derecho).
    import bisect
    inf["justificados"] = 0
    for el, k in comienzo.items():
        if el.getparent() is None:
            continue
        ls = bloques[bisect.bisect_right(starts, k) - 1][1]
        if len(ls) >= 2 and all(l["x1"] >= a["der"] - 3 for l in ls[:-1]):
            jc = el.find(f"{W}pPr/{W}jc")
            if jc is None or jc.get(W + "val") != "both":
                poner_jc(el, "both")
                inf["justificados"] += 1
    # Alineación: pdf2docx centra a veces un párrafo de varias líneas sin justificar (una viñeta de
    # dos líneas cortas). Si en el PDF sus líneas no están centradas ni alineadas a la derecha y las
    # de después de la primera empiezan todas en la misma x, va a la izquierda.
    inf["a_la_izquierda"] = 0
    medio_c = (a["izq"] + a["der"]) / 2
    for el, k in comienzo.items():
        if el.getparent() is None:
            continue
        ls = bloques[bisect.bisect_right(starts, k) - 1][1]
        jc = el.find(f"{W}pPr/{W}jc")
        if len(ls) < 2 or jc is None or jc.get(W + "val") not in ("center", "right"):
            continue
        centradas = all(abs((l["x0"] + l["x1"]) / 2 - medio_c) <= 4 for l in ls)
        derecha = all(abs(l["x1"] - ls[0]["x1"]) <= 3 for l in ls) and any(abs(l["x0"] - ls[1]["x0"]) > 3 for l in ls)
        if not centradas and not derecha and all(abs(l["x0"] - ls[1]["x0"]) <= 1.5 for l in ls[1:]):
            poner_jc(el, "left")
            inf["a_la_izquierda"] += 1
    registros = [] if listas else None
    sangrias_del_pdf(a, comienzo, bloques, starts, nuevos_m, inf, registros)
    if registros:
        reconocidos = {el for el, k in comienzo.items() if el.getparent() is not None and k in con_etiqueta}
        listas_de_word(doc, body, registros, reconocidos, inf)
    sangria_derecha(a, comienzo, bloques, starts, inf)
    inf["conservar_con_siguiente"] = bloques_juntos(a, comienzo, bloques, pagina_de_linea)
    if multiplo:
        titulos_con_siguiente(a, comienzo, bloques, starts, multiplo, inf.get("limite_inferior"), inf)
    # Viudas y huérfanas: si en el PDF un párrafo partido entre páginas deja una sola línea en una
    # de ellas, el original no tenía ese control; con él (el de pdf2docx), LibreOffice y Word pasan
    # el párrafo entero a la página siguiente y la paginación se aparta del PDF.
    inf["sin_control_viudas"] = 0
    for el, k in comienzo.items():
        if el.getparent() is None:
            continue
        ls = bloques[bisect.bisect_right(starts, k) - 1][1]
        paginas_bloque = [pagina_de_linea[id(l)] for l in ls]
        if len(set(paginas_bloque)) > 1 and (paginas_bloque.count(paginas_bloque[0]) == 1
                                             or paginas_bloque.count(paginas_bloque[-1]) == 1):
            ppr = el.find(W + "pPr")
            wc = ppr.find(W + "widowControl")
            if wc is None:
                wc = OxmlElement("w:widowControl")
                ppr.insert(0, wc)
            wc.set(W + "val", "0")
            inf["sin_control_viudas"] += 1
    for el in body.iter(W + "p"):
        if clase.get(el) != "cuerpo":
            continue
        jc = el.find(f"{W}pPr/{W}jc")
        if jc is not None and jc.get(W + "val") == "both":
            for br in el.iter(W + "br"):
                if br.get(W + "type") in (None, "textWrapping"):
                    t = OxmlElement("w:t")
                    t.set(qn("xml:space"), "preserve")
                    t.text = " "
                    br.getparent().replace(br, t)
                    inf["saltos_de_linea"] += 1
            # Espacios seguidos: pdf2docx coloca con espacios las palabras de una línea muy estirada
            # («que  la  demandada  recibió»); en un párrafo justificado no dicen nada.
            previo_t = None
            for t in el.iter(W + "t"):
                if t.getparent().tag != W + "r":
                    continue
                texto = re.sub(r" {2,}", " ", t.text or "")
                if previo_t is not None and previo_t.text.endswith(" ") and texto.startswith(" "):
                    texto = texto[1:]
                if texto != t.text:
                    t.text = texto
                    inf["espacios_dobles"] = inf.get("espacios_dobles", 0) + 1
                if texto:
                    previo_t = t

    # Centrados: pdf2docx los emula con sangrías (jc derecha o izquierda), o los deja centrados pero
    # con sangrías distintas a cada lado, y el título sale desplazado (0,5 cm en un escrito real).
    # Un párrafo del PDF cuyas líneas están todas centradas en el área de texto (±4 pt) es un
    # centrado sin sangrías; también una línea suelta centrada (±6 pt).
    centradas = set()
    medio = (a["izq"] + a["der"]) / 2
    for _, _, l in a["cuerpo"]:
        if abs((l["x0"] + l["x1"]) / 2 - medio) < 6 and l["x1"] - l["x0"] < (a["der"] - a["izq"]) * 0.8 \
                and l["x0"] > a["izq"] + 20:
            centradas.add(nz(l["texto"]))
    bloques_centrados = set()
    for k0, ls in bloques:
        if all(abs((l["x0"] + l["x1"]) / 2 - medio) <= 4 for l in ls) and \
                all(l["x0"] > a["izq"] + 4 and l["x1"] < a["der"] - 4 for l in ls):
            bloques_centrados.add(k0)
    for el in body.iter(W + "p"):
        if clase.get(el) != "cuerpo" or el.getparent() is not body:
            continue
        k = comienzo.get(el)
        if nz(texto_p(el)) in centradas or (k is not None and k in bloques_centrados):
            poner_jc(el, "center")
            poner_ind(el, left=0, right=0, firstLine=0, hanging=None)
            inf["centrados"] += 1

    # Interlineado único: pdf2docx estima el de cada párrafo por separado (15,5 a 15,9 pt para un
    # mismo 15,85) y en 40 líneas el error ya movía el salto de página. Los que están a menos de un
    # 6 % del interlineado del cuerpo del PDF pasan a ese valor.
    if a["paso_cuerpo"]:
        L = round(a["paso_cuerpo"] * TW)
        for el in body.iter(W + "p"):
            sp = el.find(f"{W}pPr/{W}spacing")
            if clase.get(el) == "cuerpo" and sp is not None and sp.get(W + "lineRule") == "exact" \
                    and abs(int(sp.get(W + "line", "0")) - L) <= 0.06 * L:
                sp.set(W + "line", str(L))

    # Espacio entre párrafos como «después» del anterior, no «antes» del siguiente (pdf2docx lo
    # pone delante): igual separación, pero en el primer párrafo de una página LibreOffice y Word
    # tratan distinto el «antes» y la paginación se iba desplazando respecto al PDF.
    # El primer párrafo de cada página no trae su separación (pdf2docx la pone a 0): se le da la
    # habitual entre párrafos, o el anterior quedaría pegado al cambiar el salto de página.
    befores = [int(sp.get(W + "before", "0")) for el in body.iter(W + "p") if clase.get(el) == "cuerpo"
               for sp in [el.find(f"{W}pPr/{W}spacing")] if sp is not None and int(sp.get(W + "before", "0")) > 0]
    habitual = statistics.median(befores) if befores else 0
    anterior = None
    for el in list(body):
        if el.tag != W + "p" or clase.get(el) != "cuerpo":
            anterior = None
            continue
        sp = el.find(f"{W}pPr/{W}spacing")
        if el in primeros_de_pagina and sp is not None and int(sp.get(W + "before", "0")) == 0 and anterior is not None:
            sp.set(W + "before", str(int(habitual)))
        if anterior is not None and sp is not None and int(sp.get(W + "before", "0")) > 0:
            sp_a = anterior.find(f"{W}pPr/{W}spacing")
            if sp_a is not None:
                sp_a.set(W + "after", str(int(sp_a.get(W + "after", "0")) + int(sp.get(W + "before"))))
                sp.set(W + "before", "0")
        anterior = el

    if multiplo:
        espacios_del_pdf(a, body, clase, comienzo, bloques, starts, multiplo, inf)

    # El primer párrafo del cuerpo, a su altura en la página 1 (el membrete ya no lo empuja).
    if a["cuerpo"]:
        primero = next((el for el in body.iter(W + "p") if clase.get(el) == "cuerpo"), None)
        y0 = min(l["y0"] for pp, _, l in a["cuerpo"] if pp == 0)
        if primero is not None:
            sp = primero.find(f"{W}pPr/{W}spacing")
            if sp is not None:
                # Menos lo que su propio interlineado exacto baja el texto (ver el margen superior).
                baja = 0
                if sp.get(W + "lineRule") == "exact" and cuerpo_l:
                    alto = statistics.median(l["y1"] - l["y0"] for _, _, l in cuerpo_l)
                    baja = max(0.0, 0.65 * (int(sp.get(W + "line")) / TW - alto) - 0.7)
                sp.set(W + "before", str(max(0, round((y0 - 1 - baja) * TW) - nuevos_m["top"])))

    # 7. Cabecera y pie reales.
    sec = doc.sections[-1]

    def llenar(hf, lineas):
        hf.is_linked_to_previous = False
        el = hf._element
        for hijo in list(el):
            el.remove(hijo)
        if not lineas:
            el.append(OxmlElement("w:p"))
            return
        for l in lineas:
            es_num = RE_PAGINA.match(l["texto"]) or re.search(r"p[áa]g(ina|\.)\s*\d+", l["texto"], re.I)
            par = parrafo_zona(l, a, n, "campos" if es_num else None)
            inf["campos_pagina"] += sum(1 for x in par.iter(W + "instrText"))
            el.append(par)

    if cab or cab1 is not None:
        llenar(sec.header, cab)
        inf["cabecera"] = [l["texto"] for l in cab]
    if cab1 is not None:
        sec.different_first_page_header_footer = True
        llenar(sec.first_page_header, cab1)
        inf["cabecera_primera"] = [l["texto"] for l in cab1]
    if pie:
        llenar(sec.footer, pie)
        inf["pie"] = [l["texto"] for l in pie]
    if cab1 is not None:
        llenar(sec.first_page_footer, pie1 or pie)
    # Filete sobre el pie: una línea horizontal larga justo encima del pie en las páginas donde
    # está. El pie se monta con el texto y la perdía (f06): borde superior del primer párrafo.
    inf["filete_pie"] = None
    if pie or pie1:
        f = filete_pie(a, pie or pie1)
        if f:
            inf["filete_pie"] = f
            hfs = [sec.footer] + ([sec.first_page_footer] if cab1 is not None else [])
            for hf in hfs:
                ps = hf._element.findall(W + "p")
                if ps:
                    borde_superior(ps[0], *f)

    # Alto de las filas: pdf2docx lo fija («exacto») y el texto que no cabe al escribir más en una
    # celda se corta. Pasa a «mínimo», menos la mitad del borde superior de la fila: con «mínimo»
    # LibreOffice suma ese medio borde y cada fila crecía 0,25 pt (0,6 pt en la minuta de e16, que
    # bajaba su parecido visual). Medido: así la tabla queda donde con «exacto» o más cerca del PDF.
    inf["filas_alto_minimo"] = 0
    for th in body.iter(W + "trHeight"):
        if th.get(W + "hRule") == "exact":
            tr = th.getparent().getparent()
            bordes = [int(float(b.get(W + "sz", "0"))) for b in tr.iter(W + "top")
                      if b.getparent().tag == W + "tcBorders" and b.get(W + "val") not in (None, "nil", "none")]
            medio_borde = round(max(bordes, default=0) * TW / 8 / 2)  # sz en octavos de punto
            th.set(W + "hRule", "atLeast")
            th.set(W + "val", str(max(0, int(th.get(W + "val")) - medio_borde)))
            inf["filas_alto_minimo"] += 1

    # 8. Notas al pie reales.
    if plan_notas:
        fn = crear_notas(doc)
        todas = [x for ns in a["notas"] for x in ns]
        for num, (r, nota) in enumerate(zip(plan_notas, todas), 1):
            rpr = r.find(W + "rPr")
            nuevo_r = OxmlElement("w:r")
            rp = OxmlElement("w:rPr")
            va = OxmlElement("w:vertAlign")
            va.set(W + "val", "superscript")
            rp.append(va)
            nuevo_r.append(rp)
            ref = OxmlElement("w:footnoteReference")
            ref.set(W + "id", str(num))
            nuevo_r.append(ref)
            r.getparent().replace(r, nuevo_r)
            fuente = rpr.find(W + "rFonts").get(W + "ascii") if rpr is not None and rpr.find(W + "rFonts") is not None else "Liberation Serif"
            tam = round((a["tam_nota"] or max(8, a["tam_cuerpo"] - 2)) * 2)
            linea = f' w:line="{round(a["paso_nota"] * TW)}" w:lineRule="exact"' if a["paso_nota"] else ""
            tras = round(a["extra_nota"] * TW)
            fn.append(parse_xml(
                f'<w:footnote {nsdecls("w")} w:id="{num}"><w:p><w:pPr><w:spacing w:before="0" w:after="{tras}"{linea}/>'
                f'<w:jc w:val="both"/></w:pPr>'
                f'<w:r><w:rPr><w:rFonts w:ascii="{fuente}" w:hAnsi="{fuente}"/><w:vertAlign w:val="superscript"/>'
                f'<w:sz w:val="{tam}"/></w:rPr><w:footnoteRef/></w:r>'
                f'<w:r><w:rPr><w:rFonts w:ascii="{fuente}" w:hAnsi="{fuente}"/><w:sz w:val="{tam}"/></w:rPr>'
                f'<w:t xml:space="preserve"> {escapar(nota["texto"])}</w:t></w:r></w:p></w:footnote>'))
        inf["notas"]["convertidas"] = len(plan_notas)
    elif notas and inf["notas"]["pdf"] and not inf["notas"]["motivo"]:
        inf["notas"]["motivo"] = "sin plan"
    doc.save(str(docx_out))
    return inf


def filete_pie(a, pie):
    """(grosor en pt, separación en pt) del filete sobre el pie si está en al menos el 60 % de
    las páginas (y 2, o la única): horizontal, de más de la mitad del ancho de texto y a menos de
    20 pt por encima de la primera línea del pie."""
    y_pie = min(l["y0"] for l in pie)
    vistos = []
    for page in a["doc"]:
        for d in page.get_drawings():
            r = d["rect"]
            if r.height <= 1.5 and r.width >= 0.5 * (a["der"] - a["izq"]) and 0 < y_pie - r.y1 <= 20:
                vistos.append((max(r.height, d.get("width") or 0.5), y_pie - r.y1))
                break
    if not vistos or len(vistos) < max(1 if a["n"] == 1 else 2, -(-a["n"] * 3 // 5)):
        return None
    return round(statistics.median(v[0] for v in vistos), 2), round(statistics.median(v[1] for v in vistos), 1)


def borde_superior(p, grosor, sep):
    ppr = p.find(W + "pPr")
    if ppr is None:
        ppr = OxmlElement("w:pPr")
        p.insert(0, ppr)
    bdr = OxmlElement("w:pBdr")
    top = OxmlElement("w:top")
    top.set(W + "val", "single")
    top.set(W + "sz", str(max(2, round(grosor * 8))))  # octavos de punto
    top.set(W + "space", str(max(0, round(sep - 1))))
    top.set(W + "color", "000000")
    bdr.append(top)
    # pBdr va antes de spacing y jc en el orden del esquema de pPr.
    ppr.insert(0, bdr)


def escapar(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


if __name__ == "__main__":
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    r = posprocesar(Path(args[0]), Path(args[1]), Path(args[2]), notas="--sin-notas" not in sys.argv,
                    listas="--sin-listas-word" not in sys.argv)
    salida = json.dumps(r, ensure_ascii=False, indent=1)
    destino = [x.split("=", 1)[1] for x in sys.argv[1:] if x.startswith("--informe=")]
    if destino:
        Path(destino[0]).write_text(salida + "\n", encoding="utf-8")
    else:
        print(salida)
