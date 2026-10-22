# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
#
# Motor PDFcadabra solo (ver README.md): Python, Ghostscript (cadena de compresión y tests)
# y las dependencias de requirements.txt en un entorno virtual.
#
#   docker build -t pdfcadabra-motor .
#   docker run --rm -v "$PWD:/datos" pdfcadabra-motor redact.py search /datos/entrada.pdf /datos/patrones.json /datos/hallazgos.json
#
# La construcción ejecuta los tests (etapa "tests") y falla si alguno falla.

FROM debian:trixie-slim AS motor

# Paquetes oficiales de Debian, sin modificar
RUN apt-get update \
    && apt-get install -y --no-install-recommends ghostscript python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python3 -m venv /opt/venv

WORKDIR /motor
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY LICENSE README.md VERSION *.py ./

ENTRYPOINT ["python3"]

# Tests con las dependencias de requirements-test.txt (no van en la imagen final)
FROM motor AS tests
COPY requirements-test.txt ./
RUN pip install -r requirements-test.txt
RUN python3 -m unittest redact_test.py redact_leaks_test.py lossless_images_test.py pdf_check_test.py \
    && touch /tmp/tests-ok

# Imagen final: la de "motor", solo si los tests han pasado
FROM motor
COPY --from=tests /tmp/tests-ok /tmp/tests-ok
