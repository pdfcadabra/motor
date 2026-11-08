# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""PDF a Word (desde el motor 1.4.0): pdf2docx 0.5.13 + un posprocesado propio (posproceso.py:
una sola sección, cabecera y pie reales, párrafos que fluyen, sangrías, notas al pie reales,
tabuladores, listas y fuentes de los PDF de Google Docs), ajustado con un banco de escritos
sintéticos.

    python3 pdf_a_word.py ENTRADA SALIDA INFORME [--max-paginas=N] [--sin-listas-word]

Listas de Word (por defecto, decisión del 2026-11-07): las viñetas y los números de lista
del PDF pasan a listas de Word (numPr), que se renumeran al editar; una lista que no se puede
convertir entera se queda con las etiquetas escritas. Con --sin-listas-word, todas se quedan como
texto con un tabulador.

Escribe SALIDA (.docx) e INFORME (JSON, solo recuentos: nunca texto del documento):
  {"pages", "ocr", "header", "first_page_header", "footer", "page_fields", "notes_pdf",
   "notes", "fake_tables", "lists", "list_items", "lists_not_converted"}
  ("lists" y "list_items": listas de Word y sus elementos; "lists_not_converted": listas que se
  quedan con las etiquetas escritas; los tres a 0 con --sin-listas-word)
o, si no convierte (sin escribir SALIDA), {"refused": CÓDIGO, "reason": MOTIVO, ...}:
  PDF_TOO_LARGE    "pages" (más de --max-paginas: con "pages" y "max_pages") o "memory" (pasa
                   de la memoria de un trabajo; con "max_memory").
  PDF_NO_TEXT      "scanned": ninguna página tiene texto (ni visible ni de un OCR): escaneo.
  PDF_UNSUPPORTED  "form" (campos de formulario), "rotated" (páginas o texto girados), "images"
                   (pdf2docx necesitaría OpenCV: dibujos vectoriales o imágenes giradas) o
                   "layout" (pdf2docx no ha dado una sección por página: sin posprocesado el
                   .docx no se puede editar bien, y no se entrega).
Códigos de salida: 0 (convertido o rechazado: lo dice el informe), 1 error inesperado,
2 argumentos, 3 contraseña de apertura (pdf_check.EXIT_ENCRYPTED).

Texto de un OCR: pdf2docx descarta el texto invisible (modo 3), que es como lo deja cualquier OCR
encima de la imagen escaneada, y entregaría solo la imagen. Si el PDF no tiene texto visible pero
sí invisible en alguna página, se convierte una copia (SALIDA.ocr.pdf, que se borra siempre) en la
que esas páginas pierden sus imágenes y su texto pasa a visible: queda el texto, sin el escaneo, sin firmas ni sellos («ocr»: true en el informe).

Datos: no escribe nada fuera de SALIDA, INFORME y esa copia, ni manda nada a la red. stdout no se
usa; en stderr solo van mensajes fijos (el tipo de la excepción, nunca su texto ni el del PDF):
durante la conversión, los descriptores 1 y 2 van a /dev/null, porque pdf2docx registra líneas
del documento («Ignore Line "…" due to overlap») y MuPDF avisos con nombres de fuentes.

Memoria: RLIMIT_AS del proceso = MEMORY_SHARE × memoria del contenedor ÷ MOTOR_MAX_JOBS (el
reparto de redact.py para el aviso previo de imágenes compartidas). Si se agota, PDF_TOO_LARGE
"memory" en vez de que el núcleo mate al contenedor. Un solo hilo de OpenBLAS (si no, numpy
reserva memoria virtual por núcleo y el límite se gasta en nada).
"""
import os

# Antes de importar numpy (lo importa pdf2docx).
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[_var] = "1"

import contextlib  # noqa: E402
import ctypes  # noqa: E402
import gc  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import resource  # noqa: E402
import sys  # noqa: E402
import zipfile  # noqa: E402

AQUI = os.path.dirname(os.path.abspath(__file__))
# El sustituto vacío de OpenCV, antes que cualquier cv2 instalado.
sys.path.insert(0, os.path.join(AQUI, "sin_opencv"))

import pymupdf  # noqa: E402

import pdf_check  # noqa: E402

EXIT_ENCRYPTED = pdf_check.EXIT_ENCRYPTED
DEFAULT_MAX_PAGES = 300
# Igual que redact.SHARED_MEMORY_SHARE / container_memory / max_jobs (copiado para no cargar
# redact.py, Pillow y los detectores en cada conversión).
MEMORY_SHARE = 0.7
MAX_JOBS_ENV = "MOTOR_MAX_JOBS"
# Margen para dar por agotada la memoria (ver is_out_of_memory).
MEMORY_NEAR = 32 * 2 ** 20
CGROUP_MEMORY_FILES = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")
# Una página cuenta como girada si su /Rotate no es 0 o si más de este tanto por uno de su texto
# visible no va en horizontal (pdf2docx y el posprocesado lo trocean: «Doc Doc Doc»).
ROTATED_TEXT_SHARE = 0.10
# Una página con texto visible e invisible es un escaneo con OCR si una imagen tapa al menos este
# tanto por uno de ella.
SCAN_IMAGE_SHARE = 0.5
# Del texto del OCR no puede faltar nada en el .docx (ver missing_text): ni una palabra ni una
# línea. Medido con 26 escritos sintéticos escaneados con Tesseract 5.3.4: en los 15 que se
# convierten no falta nada.
# Fuente que sustituye a la del OCR en el .docx (GlyphLessFont de Tesseract no tiene glifos).
OCR_FONTS = (b"GlyphLessFont",)
OCR_FONT_SUBSTITUTE = b"Times New Roman"


OCR_TEXT_FIELDS = ("ocr_words", "ocr_words_missing", "ocr_lines", "ocr_lines_missing")


class Refused(Exception):
    def __init__(self, code, reason, **extra):
        super().__init__(code)
        self.report = {"refused": code, "reason": reason, **extra}


# --- Memoria -------------------------------------------------------------------------------

def container_memory():
    try:
        physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        physical = None
    for path in CGROUP_MEMORY_FILES:
        try:
            with open(path, encoding="ascii") as f:
                value = f.read().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if value.isdigit() and (physical is None or int(value) < physical):
            return int(value)
        break
    return physical


def max_jobs():
    try:
        return max(1, int(os.environ.get(MAX_JOBS_ENV, "1")))
    except ValueError:
        return 1


def max_memory():
    memory = container_memory()
    return None if memory is None else int(MEMORY_SHARE * memory / max_jobs())


def limit_memory(limit):
    """Pone el límite blando de RLIMIT_AS y devuelve una función que lo deja como estaba (para
    poder escribir el informe tras agotarlo: el límite duro no se toca)."""
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if limit is not None:
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    return lambda: resource.setrlimit(resource.RLIMIT_AS, (soft, hard))


def virtual_peak():
    """Pico de memoria virtual del proceso (VmPeak), en bytes; None si no se sabe."""
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmPeak:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def is_out_of_memory(exc, limit):
    """MemoryError de Python o numpy, un malloc fallido de MuPDF (FzErrorSystem «malloc…») o
    cualquier otra excepción con el pico de memoria virtual a menos de un MEMORY_NEAR del límite
    (sin memoria, Python o una extensión de C a veces lanzan otra cosa, p. ej. SystemError)."""
    peak = virtual_peak()
    if limit is not None and peak is not None and peak >= limit - MEMORY_NEAR:
        return True
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, MemoryError):
            return True
        if re.search(r"\b(malloc|calloc|realloc)\b.*\bfailed\b", str(exc)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def release_memory():
    gc.collect()
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL("libc.so.6").malloc_trim(0)


# --- Comprobaciones previas ----------------------------------------------------------------

def text_profile(page):
    """(caracteres visibles, de ellos no horizontales, caracteres invisibles) de la página."""
    visible = rotated = invisible = 0
    for span in page.get_texttrace():
        n = sum(1 for c in span["chars"] if chr(c[0]).strip())
        if not n or span["type"] == 7:  # 7: texto de recorte, no se ve
            continue
        if span["type"] == 3 or span.get("opacity", 1) == 0:
            invisible += n
            continue
        visible += n
        dx, dy = span["dir"]
        if abs(dy) > 0.01 or dx < 0:
            rotated += n
    return visible, rotated, invisible


def image_share(page):
    """Parte de la página (0-1) que tapa su imagen más grande."""
    area = abs(page.rect)
    best = 0.0
    for info in page.get_image_info():
        box = pymupdf.Rect(info["bbox"]) & page.rect
        if area and not box.is_empty:
            best = max(best, abs(box) / area)
    return best


def check(doc, max_pages):
    """Rechazos antes de convertir. Devuelve las páginas escaneadas con OCR (las que se convierten
    desde la copia del OCR): las que tienen texto invisible y ninguno visible, y las que tienen los
    dos sobre un escaneo (una imagen que tapa al menos SCAN_IMAGE_SHARE de la página, p. ej. el
    sello visible de una sede electrónica sobre el escaneo). Da igual que las demás páginas tengan
    texto: un PDF mixto se convierte entero. Texto invisible sin escaneo debajo en una página con
    texto visible: PDF_MIXED_SCAN (no se sabe qué es ni si saldría entero)."""
    if doc.page_count > max_pages:
        raise Refused("PDF_TOO_LARGE", "pages", pages=doc.page_count, max_pages=max_pages)
    if doc.is_form_pdf:
        raise Refused("PDF_UNSUPPORTED", "form")
    total_visible = 0
    ocr_pages = set()
    hidden = False
    for page in doc:
        visible, rotated, invisible = text_profile(page)
        if page.rotation % 360 or (visible and rotated > ROTATED_TEXT_SHARE * visible):
            raise Refused("PDF_UNSUPPORTED", "rotated")
        total_visible += visible
        if invisible:
            if not visible or image_share(page) >= SCAN_IMAGE_SHARE:
                ocr_pages.add(page.number)
            else:
                hidden = True
    if hidden:
        raise Refused("PDF_MIXED_SCAN", "hidden_text")
    if not total_visible and not ocr_pages:
        raise Refused("PDF_NO_TEXT", "scanned")
    return ocr_pages


# --- Texto completo ------------------------------------------------------------------------

def _norm(text):
    return re.sub(r"[\W_]+", "", text).casefold()


def docx_text(path):
    """Texto de un .docx (cuerpo, cabeceras, pies y notas), sin espacios ni signos, en minúsculas."""
    parts = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if re.fullmatch(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml", name):
                xml = z.read(name).decode("utf-8", "replace")
                parts.extend(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", xml))
    return _norm("".join(parts).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))


def missing_text(pdf_path, pages, docx_path):
    """Lo que falta en el .docx del texto de esas páginas del PDF: (palabras distintas, cuántas
    faltan, líneas, cuántas faltan). Se compara sin espacios ni signos (pdf2docx y el posprocesado
    parten y unen fragmentos de otra forma); las palabras de una sola letra o cifra no cuentan. Cada
    línea (de 4 letras o más) tiene que estar entera y seguida en el texto del .docx, sin las
    cifras (el número de página pasa a ser un campo): así tampoco se pierde una línea hecha solo de
    palabras que salen en otra parte."""
    found = docx_text(docx_path)
    found_letters = re.sub(r"\d", "", found)
    words, lines = set(), []
    with pymupdf.open(pdf_path) as doc:
        for n in pages:
            for w in doc[n].get_text("words"):
                k = _norm(w[4])
                if len(k) >= 2:
                    words.add(k)
            for block in doc[n].get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    k = re.sub(r"\d", "", _norm("".join(span["text"] for span in line["spans"])))
                    if len(k) >= 4:
                        lines.append(k)
    return (len(words), sum(1 for w in words if w not in found),
            len(lines), sum(1 for k in lines if k not in found_letters))


# --- Texto de un OCR -----------------------------------------------------------------------

TR_INVISIBLE = re.compile(rb"(?<![\w.])3(\s+)Tr\b")


def ocr_copy(src, ocr_pages, path):
    """Copia del PDF en `path` en la que las páginas con OCR no tienen imágenes y su texto es
    visible (modo 0). Así pdf2docx lo lee; el resto de cada página no cambia."""
    doc = pymupdf.open(src)
    for n in sorted(ocr_pages):
        page = doc[n]
        page.add_redact_annot(page.rect)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_NONE)
        page.clean_contents()
        for xref in page.get_contents():
            stream = doc.xref_stream(xref)
            doc.update_stream(xref, TR_INVISIBLE.sub(rb"0\1Tr", stream))
    doc.save(path, garbage=3, deflate=True)
    doc.close()


def replace_ocr_fonts(path):
    """En el .docx, la fuente sin glifos del OCR por una normal."""
    tmp = path + ".fuentes"
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith(".xml"):
                for font in OCR_FONTS:
                    data = data.replace(font, OCR_FONT_SUBSTITUTE)
            zout.writestr(item, data)
    os.replace(tmp, path)


# --- Conversión ----------------------------------------------------------------------------

@contextlib.contextmanager
def silenced():
    """stdout y stderr (también los de C: MuPDF) a /dev/null mientras dura el bloque."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, 1)
        os.dup2(null, 2)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for fd in (null, *saved):
            os.close(fd)


def convert(input_path, output_path, max_pages, lists=True):
    import logging
    logging.disable(logging.CRITICAL)
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)

    doc = pymupdf.open(input_path)
    if pdf_check.needs_password(doc):
        return None
    pages = doc.page_count
    ocr_pages = check(doc, max_pages)
    doc.close()

    from cv2 import OpenCVNoDisponible
    from pdf2docx import Converter

    import posproceso

    raw = output_path + ".pdf2docx.docx"
    ocr_pdf = output_path + ".ocr.pdf"
    source = input_path
    try:
        if ocr_pages:
            ocr_copy(input_path, ocr_pages, ocr_pdf)
            source = ocr_pdf
        try:
            converter = Converter(source)
            try:
                # raw_exceptions: un fallo en una página para la conversión (por defecto pdf2docx la
                # saltaba y entregaba el .docx sin ella).
                converter.convert(raw, raw_exceptions=True)
            finally:
                converter.close()
            del converter
        except OpenCVNoDisponible:
            raise Refused("PDF_UNSUPPORTED", "images")
        # pdf2docx guarda todas las páginas analizadas hasta escribir: se suelta antes del
        # posprocesado (40 págs.: pico de 186 MB en vez de 248).
        release_memory()
        inf = posproceso.posprocesar(source, raw, output_path, listas=lists)
        if inf.get("error"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(output_path)
            raise Refused("PDF_UNSUPPORTED", "layout")
        ocr_text = (0, 0, 0, 0)
        if ocr_pages:
            replace_ocr_fonts(output_path)
            # El texto del OCR tiene que haber llegado entero al .docx (pdf2docx descarta en
            # silencio, p. ej., las líneas que se solapan): si falta algo, no se entrega.
            ocr_text = missing_text(ocr_pdf, sorted(ocr_pages), output_path)
            if ocr_text[1] or ocr_text[3]:
                os.unlink(output_path)
                mixed = len(ocr_pages) < pages
                raise Refused("PDF_MIXED_SCAN" if mixed else "PDF_UNSUPPORTED", "ocr_text",
                              **dict(zip(OCR_TEXT_FIELDS, ocr_text)))
    finally:
        for f in (raw, ocr_pdf, output_path + ".fuentes"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(f)

    notes = inf.get("notas") or {}
    word_lists = inf.get("listas_word") or {}
    return {
        "pages": pages,
        "ocr": bool(ocr_pages),
        "ocr_pages": len(ocr_pages),
        **dict(zip(OCR_TEXT_FIELDS, ocr_text)),
        "header": bool(inf.get("cabecera")),
        "first_page_header": bool(inf.get("cabecera_primera")),
        "footer": bool(inf.get("pie")),
        "page_fields": int(inf.get("campos_pagina") or 0),
        "notes_pdf": int(notes.get("pdf") or 0),
        "notes": int(notes.get("convertidas") or 0),
        "fake_tables": len(inf.get("tablas_falsas") or []),
        "lists": int(word_lists.get("listas") or 0),
        "list_items": int(word_lists.get("elementos") or 0),
        "lists_not_converted": len(word_lists.get("sin_convertir") or []),
    }


def write_report(path, report):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f)


NO_LISTS = "--sin-listas-word"


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--max-paginas=") and a != NO_LISTS]
    opts = [a for a in argv[1:] if a.startswith("--max-paginas=")]
    lists = NO_LISTS not in argv[1:]
    try:
        max_pages = int(opts[-1].split("=", 1)[1]) if opts else DEFAULT_MAX_PAGES
    except ValueError:
        max_pages = 0
    if len(args) != 3 or max_pages < 1 or any(a.startswith("--") for a in args):
        print("Uso: python3 pdf_a_word.py <entrada.pdf> <salida.docx> <informe.json> [--max-paginas=N]"
              " [--sin-listas-word]", file=sys.stderr)
        return 2
    input_path, output_path, report_path = args
    limit = max_memory()
    unlimit = limit_memory(limit)
    try:
        with silenced():
            report = convert(input_path, output_path, max_pages, lists)
    except Refused as r:
        report = r.report
    except BaseException as e:  # noqa: BLE001 - nunca un traceback: puede llevar texto del PDF
        # Lo primero, sin el límite: si se ha agotado, ni leer /proc ni escribir el informe cabría.
        unlimit()
        out_of_memory = is_out_of_memory(e, limit)
        name = type(e).__name__
        del e
        release_memory()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(output_path)
        if not out_of_memory:
            print(f"PDF a Word: error inesperado ({name})", file=sys.stderr)
            return 1
        report = {"refused": "PDF_TOO_LARGE", "reason": "memory", "max_memory": limit}
    unlimit()
    if report is None:
        print("PDF con contraseña de apertura", file=sys.stderr)
        return EXIT_ENCRYPTED
    if report.get("refused"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(output_path)
    write_report(report_path, report)
    return 0


def excepthook(kind, value, tb):
    """Cualquier excepción que llegue arriba: un mensaje fijo, sin traceback ni texto."""
    print(f"PDF a Word: error inesperado ({kind.__name__})", file=sys.stderr)


if __name__ == "__main__":
    sys.excepthook = excepthook
    sys.exit(main(sys.argv))
