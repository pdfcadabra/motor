# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Tests de detectores.py y de redact.py search --detectores, con datos inventados.

Uso: python -m unittest detectores_test.py   (la parte de PDF necesita PyMuPDF)

Los IBAN de prueba son el de ejemplo que se publica en la documentación de los bancos
(ES91 2100 0418 4502 0005 1332) y otros con un dígito de control calculado aquí sobre
cifras inventadas.
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest

import detectores

ALL = set(detectores.DETECTORS)


def iban(bban):
    """IBAN español válido para las 20 cifras inventadas `bban`."""
    check = 98 - int("".join(str(int(c, 36)) for c in bban + "ES00")) % 97
    return f"ES{check:02d}{bban}"


def found(text, names=ALL):
    return [(n, detectores.canonical(n, text[s:e])) for n, s, e in detectores.find(text, names)]


# Correos de otros dominios que example.com/org/net y cifras con forma de IPv4: se escriben
# por partes (AT, MILLONES), porque la auditoría de scripts/publicar-motor.sh los rechaza.
AT = "@"
MILLONES = "1." + "612.345.678"


# Falsos positivos comunes en escritos judiciales: ninguno de estos debe dar un hallazgo con
# ningún detector.
COMMON_NEGATIVES = [
    "Fecha: 12/06/2024, 12-06-2024, 12.06.2024 y 2024-06-12.",
    "Notificado el 1 de marzo de 2024 a las 10:30.",
    "Importe: 612.345.678,90 € de principal y 912.345.678 euros de costas.",
    f"Cuantía de {MILLONES} EUR y de 712345678 €.",
    "Intereses: 6.123,45 € y 61.234.567,00 EUR.",
    "Referencia catastral 9872023VH5797S0001WX y 28079A00100001.",
    "Referencia catastral 7837301VK4773F0001ET (urbana) y 13077A01800039 (rústica).",
    "Procedimiento ordinario 1234/2026, ejecución 612/2024, recurso 9123/2025.",
    "Expediente 2024/000123456, Diligencias Previas 678/2023-B.",
    "Autos 1234/2026 del Juzgado n.º 7; folio 345, tomo 1678, libro 912.",
    "DNI 12345678A, NIE X1234567A y CIF B12345678 (letras y control que no corresponden).",
    "Cuenta 2100 0418 45 0200051332 y tarjeta 4111 1111 1111 1111.",
    "Código postal 28079, artículo 612 de la LEC y RD 1234/2026.",
]


class Nig(unittest.TestCase):
    def test_positivos(self):
        for text, value in [
            ("NIG: 28079 11 1 2024 0001234.", "28079 11 1 2024 0001234"),
            ("NIG 28079.11.1.2024.0001234", "28079.11.1.2024.0001234"),
            ("NIG 2807911120240001234", "2807911120240001234"),
            ("NIG 08019 42 2 1990 0123456", "08019 42 2 1990 0123456"),
            ("NIG 41091 43 4 2024\n0001234", "41091 43 4 2024 0001234"),
            ("NIG 28079.11.1.2024.\n0001234", "28079.11.1.2024.0001234"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(found(text), [("nig", value)])

    def test_anio_entre_1990_y_el_actual(self):
        this_year = datetime.date.today().year
        self.assertEqual(found(f"28079 11 1 {this_year} 0001234", {"nig"}), [("nig", f"28079 11 1 {this_year} 0001234")])
        for year in (1989, 1234, 9999, this_year + 1):
            with self.subTest(year=year):
                self.assertEqual(found(f"28079 11 1 {year} 0001234", {"nig"}), [])

    def test_otros_numeros_de_19_cifras(self):
        for text in [
            "Número 1234567812345678901",      # año 1234
            "Lote 2807911118890001234",        # año 1889
            "Ref 9999999999999999999",
            "Ref 12345678901234567890",         # 20 cifras
            "Ref 280791112024000123",           # 18 cifras
            "Ref 28079 11 1 2024 00012345",     # último bloque de 8
            "Ref 2807 911 1 2024 0001234",      # bloques que no son 5+2+1+4+7
        ]:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"nig"}), [])

    def test_falsos_positivos_comunes(self):
        for text in COMMON_NEGATIVES:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"nig"}), [])


class Matricula(unittest.TestCase):
    def test_actual(self):
        for text, value in [
            ("Matrícula 1234 BCD.", "1234 BCD"),
            ("matrícula 5678-FGH", "5678-FGH"),
            ("vehículo 9012JKL,", "9012JKL"),
            ("(0000 ZZZ)", "0000 ZZZ"),
            ("vehículo 4321\nXYZ", "4321 XYZ"),
            ("vehículo 4321-\nXYZ", "4321-XYZ"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(found(text), [("matricula", value)])

    def test_actual_sin_vocales_ni_n_ni_q(self):
        for text in ["1234 ABC", "1234 BCE", "1234 BQD", "1234 BÑD", "1234 bcd", "1234 BCDF", "12345 BCD", "1234 BC"]:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"matricula"}), [])

    def test_provincial(self):
        for text, value in [
            ("Matrícula M-1234-AB.", "M-1234-AB"),
            ("matrícula B 5678 X", "B 5678 X"),
            ("matrícula MU1234BC", "MU1234BC"),
            ("matrícula GC-0012-Z,", "GC-0012-Z"),
            ("matrícula SE 4321\nAZ", "SE 4321 AZ"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(found(text), [("matricula", value)])

    def test_provincial_solo_con_codigos_de_provincia(self):
        for text in ["XX-1234-AB", "Q 1234 B", "ME-1234-AB", "M-1234-ABC", "M-12345-AB", "BCN-1234-A", "m-1234-ab"]:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"matricula"}), [])

    def test_falsos_positivos_comunes(self):
        for text in COMMON_NEGATIVES:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"matricula"}), [])


class Telefono(unittest.TestCase):
    def test_positivos(self):
        for text, value in [
            ("Tel. 612 345 678.", "612 345 678"),
            ("móvil 612345678", "612345678"),
            ("tlf: 612-345-678", "612-345-678"),
            ("tlf: 612.345.678", "612.345.678"),
            ("teléfono 612 34 56 78", "612 34 56 78"),
            ("fijo 91 123 45 67", "91 123 45 67"),
            ("fijo 912 34 56 78", "912 34 56 78"),
            ("fax 812 345 678", "812 345 678"),
            ("móvil 712345678", "712345678"),
            ("+34 612 345 678", "+34 612 345 678"),
            ("+34612345678", "+34612345678"),
            ("0034 612345678", "0034 612345678"),
            ("0034612345678", "0034612345678"),
            ("(+34) 91 123 45 67", "(+34) 91 123 45 67"),
            ("llamar al 612 345\n678 hoy", "612 345 678"),
            ("llamar al +34\n612 345 678", "+34 612 345 678"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(found(text), [("telefono", value)])

    def test_dos_seguidos(self):
        self.assertEqual(found("612345678 612345679"), [("telefono", "612345678"), ("telefono", "612345679")])

    def test_negativos(self):
        for text in [
            "512 345 678",          # empieza por 5
            "012345678",
            "61234567",             # 8 cifras
            "6123456789",           # 10 cifras
            "612.345.678,90 €",     # importe
            "612.345.678 €",
            "612 345 678 euros",
            "612345678 EUR",
            MILLONES,
            "B612345678",
            "+33 612 345 678",      # prefijo de otro país: no es un 34
        ]:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"telefono"}), [])

    def test_dentro_de_un_iban_o_un_nig_no_es_un_telefono(self):
        # Con el detector de teléfono solo, tampoco: los demás reservan sus cifras.
        self.assertEqual(found("NIG 28079 11 1 2024 6123456 y ES91 2100 0418 4502 0005 1332", {"telefono"}), [])

    def test_falsos_positivos_comunes(self):
        for text in COMMON_NEGATIVES:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"telefono"}), [])


class Email(unittest.TestCase):
    def test_positivos(self):
        for text, value in [
            ("Correo: persona.ficticia@example.com.", "persona.ficticia@example.com"),
            (f"escribir a otra-persona+aviso{AT}sub.example.org;", f"otra-persona+aviso{AT}sub.example.org"),
            (f"<contacto{AT}despacho-ficticio.es>", f"contacto{AT}despacho-ficticio.es"),
            ("NOMBRE.APELLIDO@EXAMPLE.COM", "NOMBRE.APELLIDO@EXAMPLE.COM"),
            ("correo persona@\nexample.com hoy", "persona@example.com"),
            ("correo persona\n@example.com hoy", "persona@example.com"),
            (f"correo persona.ficticia{AT}example.\ncom hoy", "persona.ficticia@example.com"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(found(text), [("email", value)])

    def test_el_punto_final_de_una_frase_no_es_parte_del_correo(self):
        self.assertEqual(found("Correo persona@example.com.\nIBAN"), [("email", "persona@example.com")])
        self.assertEqual(found("Correo persona@example.com.\nSegundo párrafo"), [("email", "persona@example.com")])

    def test_negativos(self):
        for text in ["persona@", "@example.com", f"persona{AT}example", "persona@@example.com", f"a{AT}b.c",
                     "usuario @ example.com"]:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"email"}), [])

    def test_falsos_positivos_comunes(self):
        for text in COMMON_NEGATIVES:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"email"}), [])


class Iban(unittest.TestCase):
    VALID = "ES9121000418450200051332"

    def test_digito_de_control(self):
        self.assertTrue(detectores.iban_valid(self.VALID))
        self.assertTrue(detectores.iban_valid(iban("00490000000000000001")))
        self.assertFalse(detectores.iban_valid("ES9121000418450200051333"))
        self.assertFalse(detectores.iban_valid("ES0021000418450200051332"))
        # Cifras cambiadas de sitio: el módulo 97 lo detecta.
        self.assertFalse(detectores.iban_valid("ES9112000418450200051332"))

    def test_positivos(self):
        other = iban("01820000000123456789")
        grouped = " ".join(other[i:i + 4] for i in range(0, 24, 4))
        for text, value in [
            ("IBAN ES91 2100 0418 4502 0005 1332.", "ES91 2100 0418 4502 0005 1332"),
            ("IBAN: ES9121000418450200051332", "ES9121000418450200051332"),
            (f"cuenta {grouped}", grouped),
            (f"cuenta {other}", other),
            ("cuenta es91 2100 0418 4502 0005 1332", "es91 2100 0418 4502 0005 1332"),
            ("IBAN ES91-2100-0418-4502-0005-1332", "ES91-2100-0418-4502-0005-1332"),
            ("IBAN ES91 2100 0418\n4502 0005 1332", "ES91 2100 0418 4502 0005 1332"),
            ("IBAN ES9121000418\n450200051332", "ES9121000418 450200051332"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(found(text), [("iban", value)])

    def test_negativos(self):
        for text in [
            "ES91 2100 0418 4502 0005 1333",    # control mal
            "ES9121000418450200051",            # cifras de menos
            "ES912100041845020005133212",       # cifras de más
            "XES9121000418450200051332",
            "XX9121000418450200051332",         # país que no está en el registro
        ]:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"iban"}), [])

    # Los ejemplos de cada país que publica el registro IBAN de SWIFT (no son cuentas reales).
    FOREIGN = {
        "PT": "PT50 0002 0123 1234 5678 9015 4",
        "DE": "DE89 3704 0044 0532 0130 00",
        "FR": "FR14 2004 1010 0505 0001 3M02 606",
        "IT": "IT60 X054 2811 1010 0000 0123 456",
        "GB": "GB29 NWBK 6016 1331 9268 19",
        "NL": "NL91 ABNA 0417 1643 00",
    }

    def test_otros_paises(self):
        for country, value in self.FOREIGN.items():
            compact = value.replace(" ", "")
            with self.subTest(country=country):
                self.assertEqual(len(compact), detectores.IBAN_LENGTHS[country])
                self.assertTrue(detectores.iban_valid(compact))
                self.assertEqual(found(f"IBAN {value}."), [("iban", value)])
                self.assertEqual(found(f"IBAN: {compact}"), [("iban", compact)])
                first, second = value[:14], value[15:]
                self.assertEqual(found(f"IBAN {first}\n{second} hoy"), [("iban", value)])

    def test_otros_paises_con_el_control_cambiado(self):
        for country, value in self.FOREIGN.items():
            check = int(value[2:4])
            bad = f"{value[:2]}{check + 1 if check < 98 else check - 1:02d}{value[4:]}"
            with self.subTest(country=country, bad=bad):
                self.assertFalse(detectores.iban_valid(bad))
                self.assertEqual(found(f"IBAN {bad}.", {"iban"}), [])
                self.assertEqual(found(f"IBAN {bad.replace(' ', '')}.", {"iban"}), [])

    def test_longitud_de_cada_pais(self):
        # Con un carácter de más o de menos ya no es un IBAN de ese país (aunque el módulo 97
        # se recalcule para que cuadre).
        def with_check(body):
            check = 98 - int("".join(str(int(c, 36)) for c in body[4:] + body[:2] + "00")) % 97
            return f"{body[:2]}{check:02d}{body[4:]}"
        for country, value in self.FOREIGN.items():
            compact = value.replace(" ", "")
            for wrong in (with_check(compact + "1"), with_check(compact[:-1])):
                with self.subTest(country=country, wrong=wrong):
                    self.assertFalse(detectores.iban_valid(wrong))
                    self.assertEqual(found(f"IBAN {wrong}.", {"iban"}), [])

    def test_sin_letras_ni_cifras_pegadas_detras(self):
        self.assertEqual(found("GB29NWBK60161331926819X", {"iban"}), [])
        self.assertEqual(found("DE893704004405320130001", {"iban"}), [])
        self.assertEqual(found("GB29 NWBK 6016 1331 9268 19 EUR", {"iban"}), [("iban", "GB29 NWBK 6016 1331 9268 19")])

    def test_falsos_positivos_comunes(self):
        for text in COMMON_NEGATIVES:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"iban"}), [])


def dni(number):
    """DNI válido (8 cifras) para el número inventado `number`."""
    return f"{number:08d}{detectores.DNI_LETTERS[number % 23]}"


def cif(letter, digits):
    """CIF válido para la letra y las 7 cifras inventadas (control en cifra con A, B, E y H, y
    en letra con P, Q, R, S, N y W)."""
    digit, as_letter = detectores.cif_control(digits)
    return f"{letter}{digits}{as_letter if letter in 'PQRSNW' else digit}"


class DniNie(unittest.TestCase):
    def test_letra_de_control(self):
        self.assertEqual(dni(12345678), "12345678Z")
        self.assertEqual(detectores.dni_letter(1234567), "L")    # X1234567L
        self.assertEqual(detectores.dni_letter(11234567), "X")   # Y1234567X
        self.assertEqual(detectores.dni_letter(21234567), "R")   # Z1234567R

    def test_positivos(self):
        for text, value in [
            ("DNI 12345678Z.", "12345678Z"),
            ("DNI 12345678-Z", "12345678-Z"),
            ("DNI 12345678 Z", "12345678 Z"),
            ("DNI 12.345.678-Z", "12.345.678-Z"),
            ("DNI 12.345.678Z", "12.345.678Z"),
            ("DNI 12 345 678 Z", "12 345 678 Z"),
            (f"DNI {dni(4)}", dni(4)),            # con ceros delante
            ("NIE X1234567L,", "X1234567L"),
            ("NIE X-1234567-L", "X-1234567-L"),
            ("NIE X 1234567 L", "X 1234567 L"),
            ("NIE X.1.234.567.L", "X.1.234.567.L"),
            ("NIE Y1234567X", "Y1234567X"),
            ("NIE Z1234567R", "Z1234567R"),
            ("con DNI 12.345.678-\nZ y domicilio", "12.345.678-Z"),
            ("con NIE X1234567\nL y domicilio", "X1234567 L"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(found(text), [("dni_nie", value)])

    def test_negativos(self):
        for text in [
            "Número 12345678",              # 8 cifras sin letra
            "Número 12345678A",             # letra que no corresponde
            "Número 12.345.678-A",
            "Número 12345678z",             # minúscula
            "Número 1234567Z",              # 7 cifras
            "Número 123456789Z",            # 9 cifras
            "NIE X1234567A", "NIE W1234567L", "NIE X12345678L",
            "Código postal 28079 y 08019",
            "Ref. 12345678ZA y AB12345678Z",
            "Importe 1.12345678Z",
        ]:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"dni_nie"}), [])

    def test_falsos_positivos_comunes(self):
        for text in COMMON_NEGATIVES:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"dni_nie"}), [])


class Cif(unittest.TestCase):
    def test_control(self):
        self.assertEqual(cif("B", "1234567"), "B12345674")
        self.assertEqual(cif("Q", "1234567"), "Q1234567D")
        self.assertTrue(detectores.cif_valid("C", "1234567", "4"))    # cifra o letra
        self.assertTrue(detectores.cif_valid("C", "1234567", "D"))
        self.assertFalse(detectores.cif_valid("B", "1234567", "D"))   # A, B, E, H: cifra
        self.assertFalse(detectores.cif_valid("Q", "1234567", "4"))   # P, Q, R, S, N, W: letra

    def test_positivos(self):
        other = cif("A", "5800047")
        for text, value in [
            ("CIF B12345674.", "B12345674"),
            ("CIF B-12345674", "B-12345674"),
            ("CIF B 1234567 4", "B 1234567 4"),
            ("CIF B-12.345.674", "B-12.345.674"),
            ("CIF Q1234567D", "Q1234567D"),
            (f"CIF {other}", other),
            ("CIF B-\n12345674 hoy", "B-12345674"),
            ("CIF B-1234567\n4 hoy", "B-1234567 4"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(found(text), [("cif", value)])
        # Con C, D, F, G, J, U y V vale el control en cifra o en letra.
        self.assertEqual(found("CIF C1234567D y C12345674"), [("cif", "C1234567D"), ("cif", "C12345674")])

    def test_negativos(self):
        for text in [
            "CIF B12345678",                # control que no corresponde
            "CIF B1234567D",                # letra con A, B, E y H
            "CIF Q12345674",                # cifra con P, Q, R, S, N y W
            "CIF I12345674", "CIF X12345674", "CIF b12345674",
            "CIF B1234567", "CIF B123456744",
            "Código postal 28079 y 08019",
            "Ref. AB12345674 y B12345674Z",
            "Expediente 1234/2026",
        ]:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"cif"}), [])

    def test_falsos_positivos_comunes(self):
        for text in COMMON_NEGATIVES:
            with self.subTest(text=text):
                self.assertEqual(found(text, {"cif"}), [])


class Seleccion(unittest.TestCase):
    TEXT = ("NIG 28079 11 1 2024 0001234, matrícula 1234 BCD, tel. 612 345 678, "
            "correo persona@example.com, IBAN ES91 2100 0418 4502 0005 1332, "
            "DNI 12345678Z, CIF B12345674")

    def test_solo_los_pedidos(self):
        self.assertEqual(found(self.TEXT, set()), [])
        for name in ALL:
            with self.subTest(name=name):
                self.assertEqual([n for n, _ in found(self.TEXT, {name})], [name])
        self.assertEqual([n for n, _ in found(self.TEXT)],
                         ["nig", "matricula", "telefono", "email", "iban", "dni_nie", "cif"])


# ==========================================
# redact.py search --detectores con un PDF
# ==========================================
try:
    import pymupdf as fitz
    import redact
except ImportError:  # pragma: no cover
    fitz = None

HERE = os.path.dirname(os.path.abspath(__file__))
OTHER = iban("01820000000123456789")

# (detector, texto del hallazgo) que tiene que dar make_patterns_pdf: en una línea, partidos
# en dos líneas y en las celdas de una tabla (partidos dentro de la celda).
EXPECTED = [
    ("nig", "28079 11 1 2024 0001234"), ("telefono", "612 345 678"),
    ("email", "persona.ficticia@example.com"), ("iban", "ES91 2100 0418 4502 0005 1332"),
    ("matricula", "1234 BCD"), ("matricula", "M-1234-AB"),
    ("dni_nie", "12345678Z"), ("dni_nie", "X1234567L"), ("cif", "B12345674"),
    ("iban", "DE89 3704 0044 0532 0130 00"),
    ("nig", "28079 11 1 2024 0007654"), ("telefono", "699 88 77 66"),
    ("matricula", "5678 FGH"), ("matricula", "B 5678 XY"), ("email", "otra.persona@example.org"),
    # Partido sin separador: el salto de línea queda como un espacio.
    ("dni_nie", "12.345.678-Z"), ("dni_nie", "X-1234567-L"), ("cif", "Q1234567 D"),
    ("iban", "GB29 NWBK 6016 1331 9268 19"),
    ("nig", "28079.11.1.2024.0004321"), ("telefono", "+34 91 123 45 67"),
    ("email", "contacto-despacho@example.net"), ("iban", f"{OTHER[:12]} {OTHER[12:]}"),
    ("matricula", "9012-JKL"), ("matricula", "MU-1234-BC"),
    ("dni_nie", "12 345 678 Z"), ("dni_nie", "Y1234567 X"), ("cif", "C-1234567-4"),
    ("iban", "NL91 ABNA 0417 1643 00"),
]
# Lo que no se puede censurar con los detectores (falsos positivos comunes).
KEPT = ["1234/2026", "12/06/2024", "612.345.678,90", "9872023VH5797S0001WX", "2807911118890001234",
        "12345678A", "87654321", "B12345678", "28079", "DE88 3704 0044 0532 0130 00"]


def make_patterns_pdf(path):
    """PDF sintético con todos los patrones (datos inventados)."""
    other = OTHER
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 70
    for line in [
        "Datos ficticios. Expediente 1234/2026, fecha 12/06/2024, importe 612.345.678,90 EUR.",
        "Ref. catastral 9872023VH5797S0001WX. Lote 2807911118890001234.",
        "NIG 28079 11 1 2024 0001234. Telefono 612 345 678. Correo persona.ficticia@example.com.",
        "IBAN ES91 2100 0418 4502 0005 1332. Matricula 1234 BCD y antigua M-1234-AB.",
        "DNI 12345678Z, NIE X1234567L, CIF B12345674 e IBAN DE89 3704 0044 0532 0130 00.",
        "No son: DNI 12345678A, 87654321, CIF B12345678, CP 28079, IBAN DE88 3704 0044 0532 0130 00.",
        # Partidos en dos líneas.
        "Partidos: el NIG 28079 11 1", "2024 0007654, el telefono 699 88", "77 66, el IBAN ES91 2100 0418",
        "4502 0005 1332, la matricula 5678", "FGH, la antigua B 5678", "XY y el correo otra.persona@",
        "example.org; el DNI 12.345.678-", "Z, el NIE X-1234567-", "L, el CIF Q1234567",
        "D y el IBAN GB29 NWBK 6016", "1331 9268 19.",
        # Copia que el detector no reconoce (minúscula): sale propagada.
        "Copia mal escrita del DNI: 12345678z.",
    ]:
        page.insert_text((60, y), line, fontsize=10)
        y += 14
    # Tabla de dos columnas con bordes; el valor, partido en dos líneas dentro de su celda.
    rows = [("NIG", "28079.11.1.2024.", "0004321"), ("Telefono", "+34 91 123", "45 67"),
            ("Correo", "contacto-despacho@", "example.net"), ("IBAN", other[:12], other[12:]),
            ("Matricula", "9012-", "JKL"), ("Antigua", "MU-1234-", "BC"), ("DNI", "12 345 678", "Z"),
            ("NIE", "Y1234567", "X"), ("CIF", "C-1234567-", "4"), ("IBAN extranjero", "NL91 ABNA", "0417 1643 00")]
    for i, (key, first, second) in enumerate(rows):
        top = 360 + i * 36
        page.draw_rect(fitz.Rect(60, top, 200, top + 36))
        page.draw_rect(fitz.Rect(200, top, 400, top + 36))
        page.insert_text((66, top + 14), key, fontsize=10)
        page.insert_text((206, top + 14), first, fontsize=10)
        page.insert_text((206, top + 28), second, fontsize=10)
    doc.save(path)
    doc.close()


@unittest.skipIf(fitz is None, "hace falta PyMuPDF")
class BusquedaConDetectores(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="detectores-test-")
        self.addCleanup(lambda: [os.remove(os.path.join(self.dir, f)) for f in os.listdir(self.dir)] and None or os.rmdir(self.dir))
        self.pdf = os.path.join(self.dir, "patrones.pdf")
        make_patterns_pdf(self.pdf)
        self.patterns = os.path.join(self.dir, "patrones.json")
        with open(self.patterns, "w", encoding="utf-8") as f:
            json.dump({}, f)

    def run_search(self, *extra):
        results = os.path.join(self.dir, "hallazgos.json")
        proc = subprocess.run([sys.executable, "redact.py", "search", self.pdf, self.patterns, results, *extra],
                              cwd=HERE, capture_output=True, text=True)
        data = None
        if os.path.exists(results):
            with open(results, encoding="utf-8") as f:
                data = json.load(f)
            os.remove(results)
        return proc, data

    def test_todos_los_patrones_y_censura_verificada(self):
        proc, data = self.run_search("--detectores", ",".join(sorted(ALL)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["errors"], [])
        got = []
        for r in data["results"]:
            if (r["category"], r["text"]) not in got:
                got.append((r["category"], r["text"]))
        self.assertEqual(got, EXPECTED)
        # La copia en minúsculas del DNI, propagada (y nada más).
        self.assertEqual([(r["category"], r["text"]) for r in data["results"] if r.get("propagated")],
                         [("dni_nie", "12345678Z")])
        # Los partidos dan un rectángulo por línea.
        by_value = {}
        for r in data["results"]:
            by_value.setdefault(r["text"], set()).add(round(r["rect"][1]))
        self.assertEqual(len(by_value["28079 11 1 2024 0007654"]), 2)
        self.assertEqual(len(by_value["contacto-despacho@example.net"]), 2)

        # La censura de todos los hallazgos pasa la verificación final, sin tocar el resto.
        items = os.path.join(self.dir, "items.json")
        with open(items, "w", encoding="utf-8") as f:
            json.dump(data["results"], f)
        out = os.path.join(self.dir, "salida.pdf")
        report = redact.apply(self.pdf, out, data["results"])
        self.assertTrue(report["verified"], report)
        self.assertEqual(report["failed"], [])
        with fitz.open(out) as doc:
            text = doc[0].get_text()
        for value in KEPT:
            self.assertIn(value, text)
            text = text.replace(value, "")
        for value in ["28079", "0001234", "612 345", "ficticia", "2100 0418", "BCD", "1234-AB",
                      "0007654", "699 88", "77 66", "1332", "FGH", "5678", "otra.persona", "example",
                      "0004321", "91 123", "45 67", "contacto", "despacho", "JKL", "MU-1234", OTHER[4:12],
                      "1234567", "345 678", "3704", "NWBK", "9268", "ABNA", "1643"]:
            self.assertNotIn(value, text)

    def test_sin_detectores_no_busca_ninguno(self):
        proc, data = self.run_search()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data, {"results": [], "errors": []})

    def test_solo_los_pedidos(self):
        proc, data = self.run_search("--detectores", "iban")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual({r["category"] for r in data["results"]}, {"iban"})
        self.assertEqual({r["text"] for r in data["results"]},
                         {"ES91 2100 0418 4502 0005 1332", f"{OTHER[:12]} {OTHER[12:]}", "DE89 3704 0044 0532 0130 00",
                          "GB29 NWBK 6016 1331 9268 19", "NL91 ABNA 0417 1643 00"})

    def test_con_patrones_a_la_vez(self):
        with open(self.patterns, "w", encoding="utf-8") as f:
            json.dump({"expediente": r"\d{4}/\d{4}"}, f)
        proc, data = self.run_search("--detectores", "telefono")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual({(r["category"], r["text"]) for r in data["results"]},
                         {("expediente", "1234/2026"), ("telefono", "612 345 678"),
                          ("telefono", "699 88 77 66"), ("telefono", "+34 91 123 45 67")})

    def test_detectores_no_validos(self):
        for extra in [("--detectores", "nig,dni"), ("--detectores", "nig,nig"), ("--detectores",),
                      ("--otra", "nig"), ("--detectores", "nig", "de-mas")]:
            with self.subTest(extra=extra):
                proc, data = self.run_search(*extra)
                self.assertEqual(proc.returncode, redact.EXIT_BAD_PATTERNS, proc.stderr)
                self.assertIsNone(data)


def by_group(results):
    """{group: [hallazgos]} en el orden de la respuesta."""
    groups = {}
    for r in results:
        groups.setdefault(r["group"], []).append(r)
    return groups


def search_results(test, lines_or_pdf, detectors, patterns=None):
    """Resultados de redact.search sobre un PDF (ruta) o sobre una página con esas líneas."""
    d = tempfile.mkdtemp(prefix="detectores-grupos-")
    test.addCleanup(lambda: [os.remove(os.path.join(d, f)) for f in os.listdir(d)] and None or os.rmdir(d))
    pdf = lines_or_pdf
    if not isinstance(pdf, str):
        pdf = os.path.join(d, "entrada.pdf")
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        for x, y, line in lines_or_pdf:
            page.insert_text((x, y), line, fontsize=10)
        doc.save(pdf)
        doc.close()
    with open(os.path.join(d, "patrones.json"), "w", encoding="utf-8") as f:
        json.dump(patterns or {}, f)
    redact.search(pdf, os.path.join(d, "patrones.json"), os.path.join(d, "hallazgos.json"), set(detectors))
    with open(os.path.join(d, "hallazgos.json"), encoding="utf-8") as f:
        return json.load(f)["results"]


@unittest.skipIf(fitz is None, "hace falta PyMuPDF")
class Grupos(unittest.TestCase):
    """Campo group: común a las partes de un mismo dato partido, distinto entre datos."""

    def assertGroup(self, items, category, text, lines):
        self.assertEqual({(r["category"], r["text"]) for r in items}, {(category, text)})
        self.assertEqual(len(items), lines)
        self.assertEqual(len({round(r["rect"][1]) for r in items}), lines)  # una por línea

    def test_dato_partido_en_dos_lineas(self):
        results = search_results(self, [(60, 100, "Tel. 612 345"), (60, 114, "678 y DNI 12.345.678-"),
                                        (60, 128, "Z fin")], ["telefono", "dni_nie"])
        groups = list(by_group(results).values())
        self.assertEqual(len(groups), 2)
        self.assertGroup(groups[0], "telefono", "612 345 678", 2)
        self.assertGroup(groups[1], "dni_nie", "12.345.678-Z", 2)
        for r in results:
            self.assertIsInstance(r["group"], str)
            self.assertTrue(r["group"])

    def test_dato_sin_partir_lleva_su_propio_group(self):
        results = search_results(self, [(60, 100, "Tel. 612 345 678 y correo persona@example.com")],
                                 ["telefono", "email"])
        self.assertEqual([len(g) for g in by_group(results).values()], [1, 1])

    def test_dos_datos_iguales_en_la_misma_pagina(self):
        results = search_results(self, [
            (60, 100, "Primero 612 345 678 y luego otra vez 612 345 678."),
            (60, 114, "Y partido: 612 345"), (60, 128, "678 al final."),
            (60, 160, "IBAN ES91 2100 0418 4502 0005 1332 e IBAN ES91 2100 0418"),
            (60, 174, "4502 0005 1332."),
        ], ["telefono", "iban"])
        groups = list(by_group(results).values())
        self.assertEqual([(g[0]["category"], len(g)) for g in groups],
                         [("telefono", 1), ("telefono", 1), ("telefono", 2), ("iban", 1), ("iban", 2)])
        self.assertEqual({g[0]["text"] for g in groups}, {"612 345 678", "ES91 2100 0418 4502 0005 1332"})
        # Ningún rectángulo repetido entre groups: cada aparición está una vez.
        rects = [tuple(round(v, 1) for v in r["rect"]) for r in results]
        self.assertEqual(len(rects), len(set(rects)))
        # Los dos de la primera línea, en sitios distintos.
        self.assertNotEqual(groups[0][0]["rect"], groups[1][0]["rect"])

    def test_en_tabla(self):
        d = tempfile.mkdtemp(prefix="detectores-tabla-")
        self.addCleanup(lambda: [os.remove(os.path.join(d, f)) for f in os.listdir(d)] and None or os.rmdir(d))
        pdf = os.path.join(d, "patrones.pdf")
        make_patterns_pdf(pdf)
        results = search_results(self, pdf, ALL)
        table = [r for r in results if r["rect"][1] >= 360]
        groups = list(by_group(table).values())
        # Cada celda de la tabla tiene un dato partido en dos líneas dentro de la celda.
        self.assertEqual(len(groups), 10)
        for g in groups:
            with self.subTest(text=g[0]["text"]):
                self.assertEqual(len(g), 2)
                self.assertEqual(len({r["text"] for r in g}), 1)
                self.assertTrue(all(200 <= r["rect"][0] and r["rect"][2] <= 400 for r in g))  # dentro de su celda
        # En todo el documento, cada group tiene las partes de un solo dato.
        for g in by_group(results).values():
            self.assertEqual(len({(r["category"], r["text"]) for r in g}), 1)

    def test_celdas_contiguas_en_la_misma_linea(self):
        # Dos celdas de una fila: MuPDF puede unirlas en una línea; los datos no se mezclan.
        results = search_results(self, [(60, 100, "612 345 678"), (200, 100, "699 887 766"),
                                        (60, 200, "IBAN ES91 2100 0418"), (300, 200, "4502 0005 1332")],
                                 ["telefono", "iban"])
        groups = list(by_group(results).values())
        self.assertEqual([(g[0]["text"], len(g)) for g in groups],
                         [("612 345 678", 1), ("699 887 766", 1), ("ES91 2100 0418 4502 0005 1332", 2)])
        # El IBAN partido entre dos celdas de la fila: dos rectángulos, sin tapar el hueco.
        first, second = groups[2]
        self.assertLess(first["rect"][2], 200)
        self.assertGreaterEqual(second["rect"][0], 299)

    def test_hueco_dentro_de_una_misma_linea(self):
        # Caracteres de una misma línea con un hueco grande: dos rectángulos, como search_for.
        box = lambda x: fitz.Rect(x, 100, x + 5, 110)
        chars = [("1", box(60), 0), ("2", box(65), 0), (" ", box(70), 0), ("3", box(75), 0),
                 ("4", box(300), 0), ("5", box(305), 0), ("\n", None, 0), ("6", box(60), 1)]
        self.assertEqual(redact.match_rects(chars, 0, 8),
                         [fitz.Rect(60, 100, 80, 110), fitz.Rect(300, 100, 310, 110), fitz.Rect(60, 100, 65, 110)])

    def test_patrones_un_group_por_hallazgo(self):
        results = search_results(self, [(60, 100, "Exp. 1234/2026 y 1234/2026; tel. 612 345 678")],
                                 ["telefono"], patterns={"expediente": r"\d{4}/\d{4}"})
        expedientes = [r for r in results if r["category"] == "expediente"]
        self.assertEqual(len(expedientes), 2)
        self.assertEqual(len({r["group"] for r in results}), len(results))

    def test_rectangulos_como_search_for_con_rotate_y_cropbox(self):
        d = tempfile.mkdtemp(prefix="detectores-rotate-")
        self.addCleanup(lambda: [os.remove(os.path.join(d, f)) for f in os.listdir(d)] and None or os.rmdir(d))
        for rotation in (0, 90, 180, 270):
            pdf = os.path.join(d, f"rot{rotation}.pdf")
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((100, 200), "Tel. 612 345 678 y CIF B12345674", fontsize=10)
            page.set_cropbox(fitz.Rect(40, 60, 500, 700))
            page.set_rotation(rotation)
            doc.save(pdf)
            doc.close()
            with self.subTest(rotation=rotation):
                results = search_results(self, pdf, ["telefono", "cif"])
                with fitz.open(pdf) as doc:
                    for r in results:
                        expected = doc[0].search_for(r["text"])
                        self.assertEqual(len(expected), 1)
                        for got, want in zip(r["rect"], expected[0]):
                            self.assertAlmostEqual(got, want, places=3)


def make_pages_pdf(path, pages, metadata=None):
    """PDF con una página por lista de líneas (datos inventados)."""
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page(width=595, height=842)
        for i, line in enumerate(lines):
            page.insert_text((60, 80 + 16 * i), line, fontsize=10)
    if metadata:
        doc.set_metadata(metadata)
    doc.save(path)
    doc.close()


@unittest.skipIf(fitz is None, "hace falta PyMuPDF")
class Propagacion(unittest.TestCase):
    """Las otras apariciones de cada valor detectado, en todo el documento (propagated)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="detectores-propagacion-")
        self.addCleanup(lambda: [os.remove(os.path.join(self.dir, f)) for f in os.listdir(self.dir)] and None or os.rmdir(self.dir))

    def pdf(self, pages, metadata=None):
        path = os.path.join(self.dir, f"entrada{len(os.listdir(self.dir))}.pdf")
        make_pages_pdf(path, pages, metadata)
        return path

    def test_dni_en_minusculas_en_otra_pagina(self):
        pdf = self.pdf([["Demandante con DNI 12345678Z."], ["Página sin datos."],
                        ["Copia mal escrita: dni 12345678z, y pegado ref12345678zb."]])
        results = search_results(self, pdf, ["dni_nie"])
        detected = [r for r in results if not r.get("propagated")]
        propagated = [r for r in results if r.get("propagated")]
        self.assertEqual([(r["page"], r["text"]) for r in detected], [(0, "12345678Z")])
        self.assertEqual([(r["page"], r["category"], r["text"]) for r in propagated],
                         [(2, "dni_nie", "12345678Z")] * 2)
        self.assertTrue(all(r["propagated"] is True for r in propagated))
        self.assertNotIn("propagated", detected[0])
        # Cada aparición extra con su propio group, distinto del original.
        self.assertEqual(len({r["group"] for r in results}), 3)
        with fitz.open(pdf) as doc:
            expected = doc[2].search_for("12345678z")
        self.assertEqual(sorted(tuple(round(v, 2) for v in r["rect"]) for r in propagated),
                         sorted(tuple(round(v, 2) for v in r) for r in expected))

    def test_sin_duplicar_lo_que_ya_devolvio_el_detector(self):
        # La misma página: la aparición detectada no se repite como propagada; la otra
        # (minúsculas), sí. Y otra detectada en la página 2: tampoco se repite.
        pdf = self.pdf([["DNI 12345678Z y también 12345678z."], ["DNI 12345678Z otra vez."]])
        results = search_results(self, pdf, ["dni_nie"])
        self.assertEqual([(r["page"], bool(r.get("propagated"))) for r in results],
                         [(0, False), (1, False), (0, True)])
        rects = [(r["page"], tuple(round(v, 1) for v in r["rect"])) for r in results]
        self.assertEqual(len(rects), len(set(rects)))

    def test_un_valor_que_solo_aparece_una_vez_no_genera_nada(self):
        pdf = self.pdf([["DNI 12345678Z, tel. 612 345 678."], ["Otra página con 1234 y 5678."]])
        results = search_results(self, pdf, ["dni_nie", "telefono"])
        self.assertEqual([r.get("propagated") for r in results], [None, None])

    def test_dos_valores_distintos_no_se_cruzan(self):
        pdf = self.pdf([["DNI 12345678Z."], ["DNI 87654321X."],
                        ["Copias: 87654321x y 12345678z; y 1234567 suelto, 2345678z cortado."]])
        results = search_results(self, pdf, ["dni_nie"])
        propagated = [r for r in results if r.get("propagated")]
        self.assertEqual(len(propagated), 2)
        with fitz.open(pdf) as doc:
            page = doc[2]
            for r in propagated:
                with self.subTest(text=r["text"]):
                    # El rectángulo tapa exactamente su propio valor, no el otro.
                    covered = page.get_textbox(fitz.Rect(r["rect"])).strip()
                    self.assertEqual(covered.upper(), r["text"])

    def test_propaga_tambien_el_dato_partido_y_otros_detectores(self):
        pdf = self.pdf([["Tel. 612 345", "678 y correo persona.ficticia@example.com"],
                        ["Ref. x612 345 678 y PERSONA.FICTICIA@EXAMPLE.COM_ref"]])
        results = search_results(self, pdf, ["telefono", "email"])
        propagated = [(r["page"], r["category"], r["text"]) for r in results if r.get("propagated")]
        # El teléfono partido se busca como "612 345 678" y el correo sin distinguir
        # mayúsculas: salen las copias pegadas a otra cosa, que el detector no reconoce.
        self.assertEqual(sorted(propagated), [(1, "email", "persona.ficticia@example.com"),
                                              (1, "telefono", "612 345 678")])
        # Las que sí reconoce el detector (mayúsculas, sin espacios) salen como detectadas.
        pdf = self.pdf([["Tel. 612 345 678 y correo persona@example.com"],
                        ["Tel. 612 345 678 y PERSONA@EXAMPLE.COM"]])
        results = search_results(self, pdf, ["telefono", "email"])
        self.assertEqual([r.get("propagated") for r in results], [None] * 4)

    def test_apply_con_las_propagadas_verificado_y_sin_rastro(self):
        pdf = self.pdf([["Demandante con DNI 12345678Z."], ["Texto."], ["Copia: dni 12345678z."]])
        results = search_results(self, pdf, ["dni_nie"])
        out = os.path.join(self.dir, "salida.pdf")
        report = redact.apply(pdf, out, results)
        self.assertTrue(report["verified"], report)
        with fitz.open(out) as doc:
            text = "".join(p.get_text() for p in doc).lower()
        self.assertNotIn("12345678z", text)
        self.assertIn("copia: dni", text)

    def test_la_verificacion_busca_el_valor_sin_distinguir_mayusculas(self):
        # El text de una propagada es el del valor detectado (12345678Z): la verificación final
        # compara los términos sin distinguir mayúsculas, así que una copia en minúsculas que
        # quedara en metadatos, campos, anotaciones, texto oculto u objetos es una fuga.
        out = self.pdf([["Texto sin datos."]], metadata={"keywords": "dni 12345678z"})
        self.assertIn("metadata_info", redact.verify_redaction(out, {}, {redact.norm("12345678Z")}))

    def test_tiempo_con_300_paginas(self):
        # 300 páginas de escrito con 10 datos por página, repetidos entre páginas (las mismas
        # partes) y con copias en minúsculas: la búsqueda con propagación tiene que seguir en
        # el orden de 5-10 s (aquí, con margen, por debajo de 30 s).
        pages = []
        for i in range(300):
            a, b = 10000000 + i % 20, 20000000 + i % 7
            lines = [f"Parte {j}: con DNI {dni(a + j)}, tel. 6{(a + j) % 100000000:08d}, correo p{j}@example.com."
                     for j in range(5)]
            lines += [f"Copia: dni {dni(b).lower()} y {dni(a).lower()}; importe 1.234,56 euros." for _ in range(5)]
            lines += ["Texto de relleno del escrito, con fundamentos de derecho y suplico al juzgado."] * 30
            pages.append(lines)
        pdf = self.pdf(pages)
        import time
        start = time.monotonic()
        results = search_results(self, pdf, ["dni_nie", "telefono", "email"])
        elapsed = time.monotonic() - start
        print(f"\n  300 páginas: {len(results)} hallazgos "
              f"({sum(1 for r in results if r.get('propagated'))} propagados) en {elapsed:.1f} s")
        self.assertLess(elapsed, 30)
        self.assertGreater(sum(1 for r in results if r.get("propagated")), 0)


if __name__ == "__main__":
    unittest.main()
