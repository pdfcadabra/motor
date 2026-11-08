# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Perfil de un PDF para el aviso de «no se puede comprimir más». No predice si Comprimir lo va a reducir: eso lo dice el resultado (ahorro < 2 % ->
se devuelve el original). Solo elige el motivo (`hint`) con el que la web escoge el texto.

Reparto de los bytes de los flujos tal como están en el archivo (fracción del tamaño total):
- prot: imágenes que Comprimir nunca toca (lossless_images.is_protected: 1 bit, ≤ 16 colores);
- jpeg: imágenes JPEG, con la calidad IJG estimada por su tabla de luminancia (media ponderada
  por bytes) y los ppp efectivos (los de su colocación más grande en la página, media ponderada);
- sinperd: el resto de imágenes (Flate, LZW, sin filtro...);
- fuentes: programas de fuente.

Motivos (en este orden):
- bn_sin_perdida: imágenes protegidas ≥ 80 % (escaneo en blanco y negro);
- vectorial: imágenes < 5 % (texto y gráficos);
- jpeg_ya_optimizado: JPEG ≥ 80 %, calidad ≤ 75 y ≤ 200 ppp;
- escaneo_o_fotos_jpeg: JPEG ≥ 80 % (el resto);
- otro.

De los JPEG solo lee la cabecera; is_protected sí decodifica las imágenes de 8 bits sin JPEG
para contar colores (hasta 0,9 s por PDF en el banco del estudio).

Uso: python3 perfil.py <entrada.pdf> <informe.json>   -> {"hint": motivo, "shares": {...}}
"""
import io
import json
import os
import sys
import zlib

import pymupdf
from PIL import Image

import lossless_images
import pdf_check

# Tabla de luminancia estándar de JPEG (anexo K), en zigzag como la da Pillow.
STD_LUMA = [16, 11, 12, 14, 12, 10, 16, 14, 13, 14, 18, 17, 16, 19, 24, 40, 26, 24, 22, 22, 24, 49,
            35, 37, 29, 40, 58, 51, 61, 60, 57, 51, 56, 55, 64, 72, 92, 78, 64, 68, 87, 69, 55, 56,
            80, 109, 81, 87, 95, 98, 103, 104, 103, 62, 77, 113, 121, 112, 100, 120, 92, 101, 103, 99]

HINTS = ("bn_sin_perdida", "vectorial", "jpeg_ya_optimizado", "escaneo_o_fotos_jpeg", "otro")


def jpeg_quality(data):
    """Calidad IJG aproximada por la tabla de luminancia, o None si no se puede leer."""
    try:
        q = Image.open(io.BytesIO(data)).quantization[0]
    except Exception:  # noqa: BLE001
        return None
    scale = sum(a * 100 / b for a, b in zip(q, STD_LUMA)) / 64
    return round((200 - scale) / 2 if scale <= 100 else 5000 / scale)


def _dpi(doc):
    """{xref: ppp efectivos de su colocación más grande} de las imágenes de las páginas. Con
    get_image_bbox: get_image_info(xrefs=True) decodifica cada imagen para emparejarla por su
    MD5 (8,5 s con un escaneo de 50 MB, frente a 0,1 s)."""
    dpi = {}
    for page in doc:
        for item in page.get_images(full=True):
            try:
                w = page.get_image_bbox(item).width
            except Exception:  # noqa: BLE001 - imagen que no se coloca o no se encuentra
                continue
            if item[0] > 0 and w > 0:
                d = item[2] / (w / 72)
                dpi[item[0]] = min(dpi.get(item[0], d), d)
    return dpi


def profile(doc, size):
    dpi = _dpi(doc)
    b = dict(prot=0, jpeg=0, sinperd=0, fuentes=0)
    jq = jd = 0.0
    for x in range(1, doc.xref_length()):
        try:
            if not doc.xref_is_stream(x):
                continue
            if doc.xref_get_key(x, "Subtype")[1] == "/Image":
                n = len(doc.xref_stream_raw(x) or b"")
                filt = doc.xref_get_key(x, "Filter")[1]
                if lossless_images.is_protected(doc, x):
                    b["prot"] += n
                elif "DCT" in filt:
                    b["jpeg"] += n
                    raw = doc.xref_stream_raw(x)
                    q = jpeg_quality(zlib.decompress(raw) if "Flate" in filt else raw)
                    if q is not None:
                        jq += q * n
                    jd += dpi.get(x, 0) * n
                else:
                    b["sinperd"] += n
        except Exception:  # noqa: BLE001 - un objeto dañado no cuenta
            continue
    for x in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(x, "Type")[1] != "/FontDescriptor":
                continue
            for k in ("FontFile", "FontFile2", "FontFile3"):
                t, v = doc.xref_get_key(x, k)
                if t == "xref":
                    b["fuentes"] += len(doc.xref_stream_raw(int(v.split()[0])) or b"")
        except Exception:  # noqa: BLE001
            continue
    shares = {k: round(v / size, 3) if size else 0 for k, v in b.items()}
    shares["jpeg_q"] = round(jq / b["jpeg"]) if b["jpeg"] else None
    shares["jpeg_ppp"] = round(jd / b["jpeg"]) if b["jpeg"] else None
    return shares


def hint(shares):
    images = shares["prot"] + shares["jpeg"] + shares["sinperd"]
    if shares["prot"] >= 0.8:
        return "bn_sin_perdida"
    if images < 0.05:
        return "vectorial"
    if shares["jpeg"] >= 0.8 and (shares["jpeg_q"] or 100) <= 75 and (shares["jpeg_ppp"] or 999) <= 200:
        return "jpeg_ya_optimizado"
    if shares["jpeg"] >= 0.8:
        return "escaneo_o_fotos_jpeg"
    return "otro"


def main(argv):
    if len(argv) != 3:
        print("Uso: python3 perfil.py <entrada.pdf> <informe.json>", file=sys.stderr)
        return 2
    with pymupdf.open(argv[1]) as doc:
        if pdf_check.needs_password(doc):
            print("PDF con contraseña de apertura", file=sys.stderr)
            return pdf_check.EXIT_ENCRYPTED
        shares = profile(doc, os.path.getsize(argv[1]))
    with open(argv[2], "w", encoding="utf-8") as f:
        json.dump({"hint": hint(shares), "shares": shares}, f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
