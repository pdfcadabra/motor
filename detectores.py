# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Detectores de datos personales (españoles, salvo el IBAN de cualquier país) para
redact.py search (opción --detectores).

Cada detector es una expresión regular más una validación que una expresión sola no puede
hacer (el año de un NIG, el dígito de control de un IBAN, la letra de un DNI...). Están todos desactivados
salvo que se pidan por su nombre. La categoría de cada hallazgo es el nombre del detector.

Entre los bloques de un dato se admite un salto de línea (el texto de la página lo tiene
donde el PDF parte la línea): el hallazgo se busca después con `search_for`, que da un
rectángulo por cada línea.
"""
import bisect
import datetime
import re

# Separador entre bloques: el del formato (opcional) y, además, un salto de línea opcional
# (con los espacios que lo rodeen).
def _sep(chars):
    return rf"(?:[{chars}]?[ \t]*\n[ \t]*|[{chars}]?)"


_SPACE_DOT = _sep(r" .")
_SPACE_HYPHEN = _sep(r" \-")
_PHONE = _sep(r" .\-")

# NIG (Número de Identificación General de los órganos judiciales): 19 cifras en bloques
# 5 + 2 + 1 + 4 + 7 (municipio INE, tipo de órgano, jurisdicción, año y número), con o sin
# espacios o puntos entre bloques.
_NIG = re.compile(
    rf"(?<!\d)(?<!\d\.)(\d{{5}}){_SPACE_DOT}(\d{{2}}){_SPACE_DOT}(\d){_SPACE_DOT}(\d{{4}}){_SPACE_DOT}(\d{{7}})(?![\d]|[.,]\d)")
NIG_FIRST_YEAR = 1990

# IBAN de cualquier país: código de país + 2 dígitos de control + el BBAN (cifras y letras),
# con o sin espacios (o guiones) entre los caracteres; tiene que tener la longitud de su país
# y el dígito de control correcto (ISO 13616, módulo 97). Longitudes del registro IBAN de
# SWIFT, release 101 (la que trae python-stdnum 2.2).
IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22,
    "BI": 27, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DJ": 27,
    "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FK": 18, "FO": 18, "FR": 27,
    "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28, "HN": 28, "HR": 21, "HU": 28,
    "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28,
    "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MC": 27, "MD": 24, "ME": 22,
    "MK": 19, "MN": 20, "MR": 27, "MT": 31, "MU": 30, "NI": 28, "NL": 18, "NO": 15, "OM": 23,
    "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "RU": 33, "SA": 24,
    "SC": 31, "SD": 18, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "SO": 23, "ST": 25, "SV": 28,
    "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20, "YE": 30,
}
_IBAN_START = re.compile(rf"(?<![A-Za-z0-9])([A-Za-z]{{2}}){_SPACE_HYPHEN}\d\d")
_IBAN_CHAR = re.compile(rf"{_SPACE_HYPHEN}([0-9A-Za-z])")

# DNI: 8 cifras (seguidas o con puntos o espacios como 12.345.678) y la letra de control;
# NIE: X, Y o Z, 7 cifras (o 1.234.567) y la letra de control. Entre el número y las letras,
# espacio, punto o guion si acaso. Solo con la letra en mayúscula.
DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_ID_SEP = _sep(r" .\-")
_GROUP_SEP = r"[ .]?"
_DNI = re.compile(
    rf"(?<![\w])(?<!\d[.,])(\d{{2}}{_GROUP_SEP}\d{{3}}{_GROUP_SEP}\d{{3}}){_ID_SEP}([A-Z])(?![\w])")
_NIE = re.compile(
    rf"(?<![\w])([XYZ]){_ID_SEP}(\d{_GROUP_SEP}\d{{3}}{_GROUP_SEP}\d{{3}}){_ID_SEP}([A-Z])(?![\w])")

# CIF (NIF de persona jurídica): letra de tipo de entidad + 7 cifras + carácter de control
# (cifra o letra según la entidad). Con espacio, punto o guion tras la letra y antes del
# control si acaso, y las 8 últimas posiciones también como 12.345.678.
CIF_LETTERS = "ABCDEFGHJNPQRSUVW"
CIF_CONTROL_LETTERS = "JABCDEFGHI"
_CIF = re.compile(
    rf"(?<![\w])([{CIF_LETTERS}]){_ID_SEP}(\d{{2}}{_GROUP_SEP}\d{{3}}{_GROUP_SEP}\d{{2}}){_ID_SEP}([0-9A-J])(?![\w])")

# Teléfono español: 9 cifras que empiezan por 6, 7, 8 o 9, con +34 o 0034 delante si acaso
# (también entre paréntesis), agrupadas como se escriben (3-3-3, 3-2-2-2, 2-3-2-2) o seguidas.
# Lo que sigue a las 9 cifras no puede ser otra cifra, ni un decimal o un grupo de miles
# (",50", ".123") ni el símbolo o la palabra de una moneda: entonces es un importe.
_PREFIX = r"\(?(?:\+|00)[ \t]?34\)?" + _PHONE
_PHONE_GROUPS = "|".join([
    rf"[6789]\d{{2}}{_PHONE}\d{{3}}{_PHONE}\d{{3}}",
    rf"[6789]\d{{2}}{_PHONE}\d{{2}}{_PHONE}\d{{2}}{_PHONE}\d{{2}}",
    rf"[6789]\d{_PHONE}\d{{3}}{_PHONE}\d{{2}}{_PHONE}\d{{2}}",
])
# Sin prefijo, lo de delante no puede ser una letra, una cifra, una cifra con un punto o
# una coma (el resto de un importe de miles de millones) ni el prefijo de otro país ("+33 6...").
_TELEFONO = re.compile(
    rf"(?<![\w+])(?:{_PREFIX}|(?<!\d[.,])(?<!\+\d\d[ .\-])(?<!\+\d\d))(?:{_PHONE_GROUPS})"
    r"(?!\d|[.,]\d|[ \t]*(?:€|EUR\b|[Ee]uros?\b))")

# Correo electrónico. El salto de línea solo se admite justo antes o después de la arroba,
# o después de un punto si la línea siguiente empieza en minúscula o cifra (donde se parte
# un correo largo): en cualquier otro sitio uniría la palabra de la línea siguiente, y el
# punto final de una frase seguido de una línea en mayúscula no es parte del correo.
_BR = r"(?:[ \t]*\n[ \t]*)?"
_DOT = r"\.(?:[ \t]*\n[ \t]*(?=[a-z0-9]))?"
_EMAIL = re.compile(
    r"(?<![\w.%+\-])[A-Za-z0-9_%+\-]+(?:" + _DOT + r"[A-Za-z0-9_%+\-]+)*"
    + _BR + "@" + _BR
    + r"[A-Za-z0-9\-]+(?:" + _DOT + r"[A-Za-z0-9\-]+)*" + _DOT + r"[A-Za-z]{2,24}"
    r"(?![\w\-]|\.[A-Za-z0-9])")

# Matrícula. Actual (desde 2000): 4 cifras + 3 consonantes (sin vocales, Ñ ni Q), con o sin
# espacio o guion. Provincial (1971-2000): código de provincia de 1 o 2 letras + 4 cifras +
# 1 o 2 letras. Solo en mayúsculas, como se escriben.
_CONSONANTS = "BCDFGHJKLMNPRSTVWXYZ"
_PROVINCES = (
    "A AB AL AV B BA BI BU C CA CC CE CO CR CS CU GC GE GI GR GU H HU IB J L LE LO LU M MA ML "
    "MU NA O OR OU P PM PO S SA SE SG SO SS T TE TF TO V VA VI Z ZA").split()
_MATRICULA_ACTUAL = re.compile(
    rf"(?<![A-Za-z0-9ÑñÁÉÍÓÚÜáéíóúü])\d{{4}}{_SPACE_HYPHEN}[{_CONSONANTS}]{{3}}(?![A-Za-z0-9ÑñÁÉÍÓÚÜáéíóúü])")
_MATRICULA_PROVINCIAL = re.compile(
    rf"(?<![A-Za-z0-9ÑñÁÉÍÓÚÜáéíóúü])(?:{'|'.join(sorted(_PROVINCES, key=len, reverse=True))})"
    rf"{_SPACE_HYPHEN}\d{{4}}{_SPACE_HYPHEN}[A-Z]{{1,2}}(?![A-Za-z0-9ÑñÁÉÍÓÚÜáéíóúü])")


def _digits(s):
    return re.sub(r"\D", "", s)


def nig_valid(match, year=None):
    """Año (cifras 9-12) entre 1990 y el actual."""
    year = year or datetime.date.today().year
    return NIG_FIRST_YEAR <= int(match.group(4)) <= year


def iban_valid(value):
    """IBAN (con o sin separadores) de un país del registro, con su longitud y el dígito de
    control correcto (módulo 97 igual a 1)."""
    s = re.sub(r"[\s\-]", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[0-9A-Z]+", s) or IBAN_LENGTHS.get(s[:2]) != len(s):
        return False
    rearranged = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def dni_letter(number):
    """Letra de control de un DNI (o de un NIE, con X, Y o Z ya cambiadas por 0, 1 o 2)."""
    return DNI_LETTERS[int(number) % 23]


def cif_control(digits):
    """(cifra, letra) de control de las 7 cifras de un CIF."""
    total = 0
    for i, c in enumerate(digits):
        n = int(c)
        if i % 2 == 0:  # posiciones impares (1.ª, 3.ª...): se dobla y se suman sus cifras
            n = n * 2 // 10 + n * 2 % 10
        total += n
    digit = (10 - total % 10) % 10
    return str(digit), CIF_CONTROL_LETTERS[digit]


def cif_valid(letter, digits, control):
    """El control es una letra con P, Q, R, S, N y W (y con los del extranjero), una cifra con
    A, B, E y H, y cualquiera de los dos con el resto."""
    digit, as_letter = cif_control(digits)
    if letter in "PQRSNW":
        return control == as_letter
    if letter in "ABEH":
        return control == digit
    return control in (digit, as_letter)


def _matches(regex, text, valid=lambda m: True):
    for m in regex.finditer(text):
        if valid(m):
            yield m.start(), m.end()


def _nig(text):
    return _matches(_NIG, text, nig_valid)


def _iban(text):
    """Desde cada código de país y sus 2 dígitos de control, se cuentan los caracteres del BBAN
    hasta la longitud del país: lo que sigue no puede ser una cifra ni una letra pegadas."""
    for m in _IBAN_START.finditer(text):
        length = IBAN_LENGTHS.get(m.group(1).upper())
        if not length:
            continue
        end = m.end()
        for _ in range(length - 4):
            c = _IBAN_CHAR.match(text, end)
            if not c:
                break
            end = c.end()
        else:
            if not (end < len(text) and text[end].isalnum()) and iban_valid(text[m.start():end]):
                yield m.start(), end


def _dni_nie(text):
    for m in _DNI.finditer(text):
        if dni_letter(_digits(m.group(1))) == m.group(2):
            yield m.start(), m.end()
    for m in _NIE.finditer(text):
        if dni_letter("XYZ".index(m.group(1)) * 10 ** 7 + int(_digits(m.group(2)))) == m.group(3):
            yield m.start(), m.end()


def _cif(text):
    return _matches(_CIF, text, lambda m: cif_valid(m.group(1), _digits(m.group(2)), m.group(3)))


def _telefono(text):
    return _matches(_TELEFONO, text)


def _email(text):
    return _matches(_EMAIL, text)


def _matricula(text):
    yield from _matches(_MATRICULA_ACTUAL, text)
    yield from _matches(_MATRICULA_PROVINCIAL, text)


# Orden de prioridad: lo que reconoce uno no lo puede reconocer otro de los de detrás (las
# cifras de un IBAN o de un NIG nunca salen como teléfono ni como DNI), estén o no activados.
DETECTORS = {
    "email": _email,  # el mismo orden que en README.md
    "iban": _iban,
    "nig": _nig,
    "dni_nie": _dni_nie,
    "cif": _cif,
    "telefono": _telefono,
    "matricula": _matricula,
}


def canonical(name, value):
    """Texto del hallazgo: sin el salto de línea. Un correo, sin ningún espacio; en el resto,
    el salto junto a un punto o un guion desaparece y cualquier otro queda como un espacio."""
    if name == "email":
        return re.sub(r"\s+", "", value)
    value = re.sub(r"(?<=[.\-])\s+|\s+(?=[.\-])", "", value.strip())
    return re.sub(r"\s+", " ", value)


def find(text, names):
    """[(detector, inicio, fin)] de los detectores `names` en `text`, por orden de aparición.

    Se ejecutan todos los detectores en orden de prioridad y cada uno salta lo que ya ha
    reconocido otro anterior; solo se devuelven los de `names`.
    """
    # Lo que ya ocupa otro detector, disjunto y en orden: starts[i] < ends[i] <= starts[i + 1].
    starts, ends = [], []
    found = []
    for name, detector in DETECTORS.items():
        for start, end in detector(text):
            i = bisect.bisect_right(starts, start)
            if (i > 0 and ends[i - 1] > start) or (i < len(starts) and starts[i] < end):
                continue
            starts.insert(i, start)
            ends.insert(i, end)
            if name in names:
                found.append((name, start, end))
    found.sort(key=lambda f: f[1])
    return found
