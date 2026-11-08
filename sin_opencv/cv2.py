# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Sustituto vacío de OpenCV (`cv2`) para pdf2docx, solo para pdf_a_word.py.

pdf2docx importa cv2 al cargarse (pdf2docx/common/algorithm.py), pero solo lo usa para extraer
imágenes vectoriales (formularios, dibujos) y girar imágenes giradas. Con este módulo en lugar de
la rueda opencv-python-headless (153 MB, con FFmpeg y OpenSSL 1.1.1k dentro), los escritos de
texto del banco sintético dan exactamente el mismo .docx (pdf_a_word_test.py, BancoDelEstudio). Cualquier uso de cv2 lanza OpenCVNoDisponible: pdf_a_word.py lo convierte en
«documento no admitido» (PDF_UNSUPPORTED) en vez de entregar un .docx incompleto.

pdf_a_word.py pone esta carpeta al principio de sys.path; ningún otro script del motor la usa.
"""


class OpenCVNoDisponible(Exception):
    """pdf2docx ha intentado usar OpenCV, que el motor no instala."""


def __getattr__(name):
    # Los atributos especiales (__path__, __file__...) los consulta el sistema de importación y
    # otras herramientas: para ellos, lo normal en un módulo que no los tiene.
    if name.startswith("__"):
        raise AttributeError(name)
    raise OpenCVNoDisponible(name)
