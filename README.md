# Motor PDFcadabra

Motor de procesado de PDF de [PDFcadabra](https://pdfcadabra.com): anonimiza documentos de
forma irreversible y protege las imágenes que no admiten pérdida (códigos de barras, QR,
sellos) al comprimir con un compresor externo. Está escrito en Python sobre
[PyMuPDF](https://pypi.org/project/PyMuPDF/1.28.2/) y se usa por línea de comandos.

- Versión: la del archivo [`VERSION`](VERSION).
- Licencia: GNU Affero General Public License v3.0 o posterior (`AGPL-3.0-or-later`), en
  [`LICENSE`](LICENSE).
- Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra).

## Qué hace

| Script | Comando | Función |
|---|---|---|
| `redact.py` | `search` | Busca expresiones regulares en el texto de las páginas (también en los campos de formulario y las anotaciones, que aplana antes) y devuelve cada aparición con su rectángulo. Opcionalmente, también datos españoles con validación propia (`detectores.py`: NIG, matrícula, teléfono, correo e IBAN). |
| `redact.py` | `apply` | Censura de forma irreversible las zonas indicadas: elimina el texto, los píxeles de las imágenes y los trazos que tocan cada zona, y pinta encima un recuadro negro. Después limpia lo que pueda contener los términos censurados fuera de las páginas (metadatos Info y XMP, marcadores, adjuntos, nombres de capas, acciones, texto oculto…) y vuelve a buscarlos en el resultado. Si queda cualquier rastro, no escribe la salida. |
| `lossless_images.py` | `protect` | Antes de comprimir, sustituye las imágenes que no deben perder calidad (1 bit, paletas de hasta 16 colores, imágenes de 8 bits sin JPEG con hasta 16 colores) por un marcador diminuto. |
| `jpeg_flate.py` | (restore) | Después de comprimir, vuelve a poner las imágenes originales en lugar de sus marcadores y envuelve en Flate los JPEG cuando así ocupan menos (sin pérdida). |
| `pdf_check.py` | `verify` | Comprueba que un resultado se abre, tiene las mismas páginas que la entrada y no ha dejado vacía ninguna página con contenido. |

Cada comando es un proceso independiente: no usa la red, no guarda estado entre
ejecuciones y solo lee y escribe las rutas que recibe como argumentos.

## Requisitos

- Python 3.13, el de Debian 13 «trixie», que es con el que se prueba (PyMuPDF 1.28.2 pide
  3.10 o posterior).
- Las dependencias de [`requirements.txt`](requirements.txt), con versiones exactas:
  PyMuPDF 1.28.2 y Pillow 12.3.0.
- Solo para la cadena de compresión y parte de los tests: Ghostscript (`gs`).
- Solo para los tests: [`requirements-test.txt`](requirements-test.txt) (añade zxing-cpp,
  que genera y decodifica los QR y códigos de barras de prueba).

## Construir y ejecutar

### Con un entorno virtual

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

python3 redact.py search entrada.pdf patrones.json hallazgos.json
```

Tests:

```bash
pip install -r requirements-test.txt
python3 -m unittest redact_test.py redact_leaks_test.py detectores_test.py lossless_images_test.py pdf_check_test.py
```

Las pruebas que comprimen de verdad necesitan Ghostscript. Las de la clase
`ComprimirConGhostscript` toman los argumentos de Ghostscript del servidor que usa este
motor, que no forma parte de este repositorio, y aquí se saltan; esa misma cadena, con
esos mismos argumentos, está en [Cadena de compresión](#cadena-de-compresión).

### Con Docker

```bash
docker build -t pdfcadabra-motor .
```

La imagen se basa en Debian 13 «trixie» con Python, Ghostscript y las dependencias en un
entorno virtual. La construcción ejecuta todos los tests en una etapa aparte (`tests`) y
falla si alguno falla; la imagen final no lleva las dependencias de los tests.

El punto de entrada es `python3`, con el motor en `/motor`. Para trabajar con archivos
locales, monta una carpeta (aquí, en `/datos`):

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/datos" pdfcadabra-motor \
  redact.py search /datos/entrada.pdf /datos/patrones.json /datos/hallazgos.json
```

## Interfaz de línea de comandos

Esta sección es la especificación pública y estable de la interfaz. Sigue
[versionado semántico](https://semver.org/lang/es/) con el número de `VERSION`: dentro de
una misma versión mayor no se quitan comandos, argumentos, campos JSON ni códigos de
salida, y su significado no cambia. Un cambio incompatible sube la versión mayor.

Reglas comunes:

- Invocación: `python3 <script> <argumentos>`. Los scripts importan a sus vecinos, así que
  deben estar todos en el mismo directorio; el directorio de trabajo da igual.
- Las rutas pueden ser absolutas o relativas al directorio de trabajo.
- Los JSON se leen y escriben en UTF-8.
- stdout: solo `protect` escribe en él (ver abajo). Los demás no escriben nada.
- stderr: mensajes para personas, en español. No forman parte de la interfaz: pueden
  cambiar en cualquier versión. No incluyen el contenido de los documentos.
- Los PDF con contraseña de apertura no se procesan (código 3). Los que solo tienen
  contraseña de propietario se abren con la contraseña vacía y se procesan; la salida va
  sin cifrar.

### Códigos de salida

| Código | Significado |
|---|---|
| 0 | Correcto. En `apply`, el informe dice si la censura se ha verificado. |
| 1 | Error inesperado (por ejemplo, un PDF que no se puede abrir). |
| 2 | Argumentos incorrectos (`protect`, `jpeg_flate.py` y `verify`). |
| 3 | El PDF de entrada tiene contraseña de apertura: no se ha procesado. |
| 4 | Según el comando: `search`, patrones no válidos; `jpeg_flate.py`, no se han podido restaurar las imágenes protegidas; `verify`, la salida no es válida. |

### Variables de entorno

| Variable | Comando | Significado |
|---|---|---|
| `MOTOR_MAX_JOBS` | `apply` | Desde la versión 1.2.0. Cuántos trabajos pueden correr a la vez en la misma máquina o contenedor (entero ≥ 1; por defecto, 1). Reparte la memoria entre ellos en el aviso previo `SHARED_IMAGE_TOO_LARGE`. |

### Coordenadas

Los rectángulos son `[x0, y0, x1, y1]` en puntos (1/72 de pulgada), en el espacio de la
página **sin girar** (antes de aplicar `/Rotate`), con el origen en la esquina superior
izquierda de la zona visible (CropBox recortada a la MediaBox) y la `y` hacia abajo. Es el
espacio de `Page.search_for` de PyMuPDF.

### `redact.py search ENTRADA PATRONES RESULTADOS [--detectores LISTA]`

- `ENTRADA`: PDF.
- `PATRONES` (lee): objeto JSON `{categoría: expresión}`. Cada expresión es una cadena no
  vacía con la sintaxis del módulo `re` de Python. La categoría es libre y se devuelve en
  cada hallazgo.

  ```json
  {"dni": "\\b\\d{8}[A-Z]\\b", "custom_0": "Juan Ejemplo"}
  ```

- `--detectores LISTA` (opcional, desde la versión 1.1.0): nombres separados por comas, sin
  repetir, de los detectores que buscar además de `PATRONES` (que puede ser `{}`). Sin la
  opción, o con `LISTA` vacía, no se busca ninguno. La categoría de sus hallazgos es el
  nombre del detector.

  | Detector | Qué reconoce |
  |---|---|
  | `nig` | NIG: 19 cifras en bloques 5 + 2 + 1 + 4 + 7, con o sin espacios o puntos entre bloques. Las cifras 9 a 12 (el año) tienen que estar entre 1990 y el año en curso. |
  | `matricula` | Matrícula actual: 4 cifras y 3 consonantes (sin vocales, Ñ ni Q), con o sin espacio o guion. Provincial: código de provincia de 1 o 2 letras, 4 cifras y 1 o 2 letras, con o sin espacio o guion. Solo en mayúsculas. |
  | `telefono` | Teléfono español: 9 cifras que empiezan por 6, 7, 8 o 9, seguidas o en grupos 3-3-3, 3-2-2-2 o 2-3-2-2 separados por espacio, punto o guion, con `+34` o `0034` delante si acaso (también entre paréntesis). No cuenta si sigue un decimal, un grupo de miles o una moneda (`€`, `EUR`, `euros`): es un importe. |
  | `email` | Correo electrónico. |
  | `iban` | IBAN de cualquier país del registro IBAN de SWIFT (release 101): código de país, 2 cifras de control y el BBAN (cifras y letras), con o sin espacios o guiones, con la longitud de ese país y el dígito de control correcto (ISO 13616, módulo 97). No puede ir pegado a otra letra o cifra. |
  | `dni_nie` | DNI: 8 cifras (o `12.345.678`, `12 345 678`) y la letra de control. NIE: `X`, `Y` o `Z`, 7 cifras (o `1.234.567`) y la letra de control. Espacio, punto o guion entre el número y las letras si acaso. Solo con letras en mayúscula. |
  | `cif` | CIF: letra de entidad (`ABCDEFGHJNPQRSUVW`), 7 cifras y el control: cifra con `A`, `B`, `E` y `H`; letra con `P`, `Q`, `R`, `S`, `N` y `W`; cualquiera de los dos con el resto. Espacio, punto o guion tras la letra y antes del control si acaso, y las cifras también como `12.345.67`. |

  Entre los bloques de un dato se admite un salto de línea (en un correo, junto a la arroba,
  o tras un punto si la línea siguiente empieza en minúscula o cifra): ese dato da un
  hallazgo por línea, todos con el mismo `group` y el mismo `text`, sin el salto (un correo, sin espacios; los demás, sin
  espacio junto a un punto o un guion y con uno en cualquier otro caso). Las cifras que
  reconoce un detector no las reconoce otro detrás de él en este orden, esté o no pedido:
  `email`, `iban`, `nig`, `dni_nie`, `cif`, `telefono`, `matricula` (las cifras de un IBAN o
  de un NIG nunca son un teléfono ni un DNI).

  Propagación: cada valor que encuentra un detector se busca después en todas las páginas
  del documento sin distinguir mayúsculas (con `Page.search_for`, tal como aparece y en la
  forma de su `text`), para que desaparezca también donde el detector no lo reconoce (en
  minúsculas, pegado a otro texto…). Cada una de esas apariciones da un hallazgo más por
  rectángulo, con la categoría y el `text` del valor, su propio `group` y `"propagated": true`.
  No repite lo que ya cubre un hallazgo del detector.

- `RESULTADOS` (escribe):

  ```json
  {
    "results": [
      {
        "id": "2f1d0a8e-7c1b-4f0e-9a51-3c8f0e6b9d12",
        "page": 0,
        "page_width": 595.0,
        "page_height": 842.0,
        "text": "12345678Z",
        "context": "...DNI 12345678Z, con domicilio...",
        "category": "dni",
        "rect": [72.0, 100.5, 140.2, 114.0],
        "group": "8c3e5b1a-4d2f-4b6e-9f0a-7d1c2e3b4a5f"
      }
    ],
    "errors": [
      {"category": "custom_1", "message": "El patrón de búsqueda para 'custom_1' no es válido: revisa paréntesis o caracteres especiales."}
    ]
  }
  ```

  | Campo | Tipo | Descripción |
  |---|---|---|
  | `id` | cadena | UUID v4 del hallazgo. |
  | `page` | entero | Página, desde 0. |
  | `page_width`, `page_height` | número | Tamaño de la página tal como se muestra (con `/Rotate` aplicado), en puntos. |
  | `text` | cadena | Texto encontrado, sin espacios al principio ni al final. |
  | `context` | cadena | Hasta 30 caracteres a cada lado, entre `...` y `...`. |
  | `category` | cadena | Categoría del patrón. |
  | `rect` | 4 números | Rectángulo de la aparición (ver [Coordenadas](#coordenadas)). |
  | `group` | cadena | Desde la versión 1.1.0. Identificador del dato, único dentro de `RESULTADOS`: los hallazgos de un detector que son partes de un mismo dato partido (uno por línea, o por trozo de línea si hay un hueco grande en medio) comparten `group`; un dato sin partir, y cada hallazgo de `PATRONES`, tienen el suyo. No dice nada fuera de la respuesta en que viene. |
  | `propagated` | booleano | Desde la versión 1.1.0, opcional: solo aparece, con `true`, en las otras apariciones de un valor que ha encontrado un detector (ver `--detectores`). |

  Cada aparición da un hallazgo, sin duplicados (mismo texto en el mismo sitio). Una
  expresión que no compila no detiene la búsqueda: va a `errors` con su categoría.

- Salida: 0; 1; 3; 4 si `PATRONES` no es un objeto JSON o alguno de sus valores no es una
  cadena no vacía, o si los argumentos tras `RESULTADOS` no son `--detectores LISTA` con
  detectores conocidos y sin repetir (entonces no escribe `RESULTADOS`).

### `redact.py apply ENTRADA SALIDA HALLAZGOS [INFORME]`

- `ENTRADA`: PDF.
- `SALIDA` (escribe): PDF censurado. Solo existe si el informe dice `"verified": true`.
- `HALLAZGOS` (lee): array JSON de zonas que censurar. Lo habitual es enviar los hallazgos
  de `search` que se quieren censurar, más las zonas dibujadas a mano.

  | Campo | Tipo | Obligatorio | Descripción |
  |---|---|---|---|
  | `page` | entero | sí | Página, desde 0. |
  | `rect` | 4 números | sí | Zona que censurar (ver [Coordenadas](#coordenadas)). Finita, con `x0 < x1` y `y0 < y1`, y que toque la página. |
  | `id` | cualquiera | no | Se devuelve en `failed` si esa zona no se puede aplicar. |
  | `category` | cadena | no | `"Censura Manual"` marca una zona dibujada a mano: su `text` es una etiqueta y no se busca. |
  | `text` | cadena | no | Término censurado. Si tiene 3 caracteres o más (tras normalizarlo: Unicode NFC, minúsculas y espacios seguidos como uno solo), la verificación final comprueba que no queda en ninguna parte del documento fuera de las zonas visibles. Los demás campos se ignoran. |

  ```json
  [
    {"id": "2f1d0a8e-7c1b-4f0e-9a51-3c8f0e6b9d12", "page": 0, "rect": [72.0, 100.5, 140.2, 114.0], "category": "dni", "text": "12345678Z"},
    {"id": "manual-1", "page": 1, "rect": [300, 400, 420, 460], "category": "Censura Manual", "text": "Zona manual"}
  ]
  ```

- `INFORME` (escribe; opcional, pero sin él no se puede saber si la censura se ha
  verificado):

  ```json
  {"success": true, "verified": true, "applied": 2, "failed": [], "text_loss_pages": []}
  ```

  ```json
  {"success": false, "verified": false, "leaks": ["metadata_xmp"], "applied": 2, "failed": []}
  ```

  | Campo | Tipo | Descripción |
  |---|---|---|
  | `success`, `verified` | booleano | `true` si la verificación final no ha encontrado nada. |
  | `applied` | entero | Zonas aplicadas. |
  | `failed` | array | `{id, error}` de cada zona que no se ha podido aplicar (página inexistente, rectángulo no válido o fuera de la página…). |
  | `leaks` | array de cadenas | Solo si `verified` es `false`: dónde sigue algo. Valores: `content_syntax` (un contenido de página no se puede analizar), `nesting_depth` (un objeto demasiado anidado para limpiarlo), `page_text`, `hidden_text`, `render_not_black`, `form_fields`, `annotations`, `links`, `metadata_info`, `metadata_xmp`, `outline`, `attachments`, `pdf_objects`, `REDACT_HIDDEN_PIXELS` (desde la versión 1.2.0: alguna imagen de la salida que viene de una imagen censurada conserva píxeles de una zona con su valor original sin que se vean en ninguna página: ver más abajo). Nunca incluye los términos. |
  | `text_loss_pages` | array de enteros | Solo si `verified` es `true`: páginas (desde 1) que han perdido texto fuera de las zonas censuradas (por ejemplo, un trazo o un relleno con patrón que tocaba una zona). |
  | `refused` | cadena | Desde la versión 1.2.0. Solo si no se ha censurado nada por un aviso previo (entonces `verified` es `false`, `applied` es 0 y no hay `leaks`). Valores: `SHARED_IMAGE_TOO_LARGE` y `SHARED_IMAGE_UNSUPPORTED` (ver más abajo). |
  | `estimate` | objeto | Desde la versión 1.2.0. Con `refused`: la estimación que lo ha decidido (`bytes`, `seconds`, `memory`, `copies` y `max_memory`, enteros; `max_memory` es `null` si no se sabe la memoria disponible). |

  Imágenes compartidas entre páginas: una zona borra los píxeles de la imagen en su página
  (todo píxel que toca, aunque sea en parte). Cada otra página que usa la misma imagen y no
  muestra entera esa parte (otra CropBox, un recorte, la imagen sale de la página o no la
  dibuja) recibe su propia copia con los píxeles que no muestra borrados; lo que sí muestra no
  cambia (en JPEG y JPEG 2000 la copia se recomprime como la original). Lo que otra página
  muestra de una zona se queda, como cualquier aparición visible que no se ha marcado. La
  verificación final (`REDACT_HIDDEN_PIXELS`) lo comprueba en cada imagen de la salida.

  `SHARED_IMAGE_UNSUPPORTED`: una zona toca una imagen que no se puede copiar por página: una
  imagen dibujada desde un patrón de mosaico que usa también otra página (con el mismo patrón o
  con otro; p. ej. un fondo CSS en mosaico impreso con Chrome, que hace un patrón por página con
  la misma imagen), o una imagen en línea dentro de un Form XObject común. Censurarla dejaría sus
  píxeles en las otras páginas: `apply` no censura nada:

  ```json
  {"success": false, "verified": false, "refused": "SHARED_IMAGE_UNSUPPORTED", "applied": 0, "failed": []}
  ```

  `SHARED_IMAGE_TOO_LARGE`: antes de censurar, por cada imagen que usan varias páginas
  (también desde un Form XObject común), cada página con una zona que la toca dejaría su
  propia copia entera de la imagen, y también cada página sin zona que no muestre entera la
  de otra (una copia por cada borrado distinto). Si la estimación de esas copias pasa de 150 MB
  de salida (la entrada más una copia del tamaño del flujo de la imagen por copia), de 180 s o
  de la memoria máxima de un trabajo (`max_memory`), `apply` no censura nada:

  ```json
  {"success": false, "verified": false, "refused": "SHARED_IMAGE_TOO_LARGE",
   "estimate": {"bytes": 222279891, "seconds": 157, "copies": 250, "memory": 1733783150,
                "max_memory": 1503238553},
   "applied": 0, "failed": []}
  ```

  Memoria máxima de un trabajo: el 70 % de la memoria disponible entre los trabajos que pueden
  correr a la vez. La memoria disponible es el límite del cgroup del proceso
  (`/sys/fs/cgroup/memory.max` en v2, `/sys/fs/cgroup/memory/memory.limit_in_bytes` en v1: el
  `mem_limit` del contenedor) o, sin límite, la RAM física; si no se puede saber, ese límite no
  se aplica. Los trabajos a la vez los dice la variable de entorno `MOTOR_MAX_JOBS` (entero; sin
  ella o si no es válida, 1).

  Regla de uso: entregar `SALIDA` solo si `verified` es `true` y `failed` está vacío. Si
  `failed` no está vacío, lo que debían tapar esas zonas sigue en el documento.

- Salida: 0 (se ha escrito el informe: mirar `verified`); 1; 3.

### `lossless_images.py protect ENTRADA SALIDA`

- `ENTRADA`: PDF.
- `SALIDA` (escribe): copia de la entrada con cada imagen protegida sustituida por un
  marcador (una ImageMask de 64x2 píxeles que lleva el número de objeto de la original).
  Solo se escribe si hay alguna imagen protegida.
- stdout: una línea con el número de imágenes protegidas (`0` si ninguna; entonces no
  escribe `SALIDA`).
- Salida: 0; 1; 2; 3.

Las imágenes protegidas son las de 1 bit (DeviceGray, ICCBased, Indexed de 2 colores,
ImageMask, JBIG2…), las Indexed de hasta 16 colores (o con una paleta mayor de la que solo
se usan hasta 16 índices) y las de 8 bits sin JPEG que usan hasta 16 colores. Las máscaras
de las imágenes (`/SMask`, `/Mask`) no se sustituyen.

El compresor que se use entre `protect` y `jpeg_flate.py` debe copiar los marcadores sin
tocarlos: sin reducir ni recodificar las imágenes de 1 bit y sin meterlas en el contenido
de la página como imágenes en línea. Con Ghostscript: `-dDownsampleMonoImages=false` y
`-dMaxInlineImageSize=0`.

### `jpeg_flate.py ENTRADA SALIDA [--originals ORIGINAL]`

- `ENTRADA`: PDF que ha devuelto el compresor.
- `ORIGINAL`: el PDF que recibió `protect` (sin marcadores). Obligatorio si `protect`
  protegió alguna imagen; si no, se omite.
- `SALIDA` (escribe): el PDF con las imágenes originales en su sitio (su flujo comprimido
  tal cual; las de 1 bit en Flate pasan a CCITT G4 si así ocupan menos) y con los JPEG
  envueltos en Flate cuando ocupan menos. No cambia nada más.
- Salida: 0; 1; 2; 4 si queda algún marcador sin restaurar, un marcador apunta a algo que
  no es una imagen en `ORIGINAL` o una imagen está demasiado anidada (entonces no escribe
  `SALIDA`).

### `pdf_check.py verify ENTRADA SALIDA`

- Lee los dos PDF y no escribe nada.
- Salida: 0 si `SALIDA` se abre sin errores, tiene las mismas páginas que `ENTRADA` y
  ninguna página que tenía contenido (texto o imágenes) ha quedado vacía; 2; 3 si
  `ENTRADA` tiene contraseña de apertura; 4 en cualquier otro caso.

## Cadena de compresión

Comprimir un PDF sin estropear sus códigos de barras ni sus QR:

1. `lossless_images.py protect`: sustituye las imágenes que no admiten pérdida por
   marcadores.
2. Compresor externo (aquí, Ghostscript con `pdfwrite`): reduce y recodifica el resto; los
   marcadores pasan sin tocar.
3. `jpeg_flate.py --originals` (restore): vuelve a poner las imágenes originales.
4. `pdf_check.py verify`: comprueba el resultado antes de usarlo.

Este script ejecuta la cadena completa en local, sin nada más que el motor, Python y
Ghostscript, con los mismos parámetros de Ghostscript que el servicio de PDFcadabra para
cada nivel (`recommended`, `extreme` y `low`):

```bash
#!/usr/bin/env bash
# Uso: MOTOR=<directorio del motor> ./comprimir.sh entrada.pdf salida.pdf [recommended|extreme|low]
set -euo pipefail

ENTRADA=$1
SALIDA=$2
NIVEL=${3:-recommended}
MOTOR=${MOTOR:-.}

# Preset de Ghostscript, resolución de las imágenes en color y gris (ppp) y calidad JPEG
# (QFactor) de cada nivel. "low" no reduce ni recodifica imágenes.
case "$NIVEL" in
  recommended) AJUSTE=/ebook;   PPP=150; Q=0.7 ;;
  extreme)     AJUSTE=/screen;  PPP=100; Q=0.8 ;;
  low)         AJUSTE=/printer; PPP="";  Q="" ;;
  *) echo "Nivel desconocido: $NIVEL" >&2; exit 2 ;;
esac

TRABAJO=$(mktemp -d)
trap 'rm -rf "$TRABAJO"' EXIT

# 1. protect: N imágenes sustituidas por marcadores (con 0 no escribe nada).
N=$(python3 "$MOTOR/lossless_images.py" protect "$ENTRADA" "$TRABAJO/marcado.pdf")
if [ "$N" -gt 0 ]; then
  GS_ENTRADA=$TRABAJO/marcado.pdf
  ORIGINALES=(--originals "$ENTRADA")
else
  GS_ENTRADA=$ENTRADA
  ORIGINALES=()
fi

# 2. Ghostscript. Las imágenes de 1 bit no se reducen y van en CCITT G4 sin pérdida, y
# -dMaxInlineImageSize=0 impide que meta los marcadores en el contenido de la página.
GS_ARGS=(-sDEVICE=pdfwrite -dCompatibilityLevel=1.4 "-dPDFSETTINGS=$AJUSTE")
if [ -n "$PPP" ]; then
  GS_ARGS+=(-dDownsampleColorImages=true "-dColorImageResolution=$PPP" -dColorImageDownsampleThreshold=1
            -dDownsampleGrayImages=true "-dGrayImageResolution=$PPP" -dGrayImageDownsampleThreshold=1)
else
  GS_ARGS+=(-dDownsampleColorImages=false -dDownsampleGrayImages=false)
fi
GS_ARGS+=(-dColorImageDownsampleType=/Bicubic -dGrayImageDownsampleType=/Bicubic)
if [ -n "$Q" ]; then
  GS_ARGS+=(-dAutoFilterColorImages=false -dColorImageFilter=/DCTEncode
            -dAutoFilterGrayImages=false -dGrayImageFilter=/DCTEncode
            -dPassThroughJPEGImages=false)
fi
GS_ARGS+=(-dDownsampleMonoImages=false -dMonoImageFilter=/CCITTFaxEncode
          -dMaxInlineImageSize=0
          -dNOPAUSE -dQUIET -dBATCH "-sOutputFile=$TRABAJO/gs.pdf")
if [ -n "$Q" ]; then
  # La calidad JPEG solo se puede fijar con setdistillerparams, después del preset.
  DICT="<< /QFactor $Q /Blend 1 /HSamples [2 1 1 2] /VSamples [2 1 1 2] >>"
  GS_ARGS+=(-c "<< /ColorImageDict $DICT /GrayImageDict $DICT >> setdistillerparams" -f)
fi
gs "${GS_ARGS[@]}" "$GS_ENTRADA"

# 3. restore: imágenes originales en su sitio y JPEG envueltos en Flate.
python3 "$MOTOR/jpeg_flate.py" "$TRABAJO/gs.pdf" "$TRABAJO/final.pdf" "${ORIGINALES[@]}"

# 4. verify: si el resultado no es válido, sale con un código distinto de 0 (set -e).
python3 "$MOTOR/pdf_check.py" verify "$ENTRADA" "$TRABAJO/final.pdf"

cp "$TRABAJO/final.pdf" "$SALIDA"
ls -l "$ENTRADA" "$SALIDA"
```

Con la imagen de Docker (Ghostscript incluido), guardando el script como `comprimir.sh` en
la carpeta montada:

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/datos" -e MOTOR=/motor \
  --entrypoint bash pdfcadabra-motor /datos/comprimir.sh /datos/entrada.pdf /datos/salida.pdf recommended
```

El servicio de PDFcadabra hace además algo que el script deja fuera: si el nivel pedido no
reduce el tamaño al menos un 2 %, repite la cadena con `low`; si tampoco, o si `verify`
falla, entrega la entrada sin tocar.

## Dependencias y licencias

| Componente | Versión | Licencia | Uso |
|---|---|---|---|
| [PyMuPDF](https://pypi.org/project/PyMuPDF/1.28.2/) (incluye MuPDF) | 1.28.2 | AGPL-3.0 (o licencia comercial de Artifex) | Todo el motor. |
| [Pillow](https://pypi.org/project/pillow/12.3.0/) | 12.3.0 | MIT-CMU | Recodificar las imágenes censuradas. |
| [zxing-cpp](https://pypi.org/project/zxing-cpp/3.1.1/) | 3.1.1 | Apache-2.0 | Solo tests. |
| Ghostscript | la del sistema | AGPL-3.0-or-later | Compresor externo de la cadena de compresión (programa aparte, por línea de comandos). |

## Código fuente de las bibliotecas

PyMuPDF 1.28.2 se usa sin modificar, instalado desde PyPI; su rueda lleva MuPDF 1.28.2
compilado, también sin modificar. Cada versión publicada de este motor (release de GitHub)
lleva adjunto el código fuente exacto de los dos, con un archivo `SHA256SUMS`:

| Biblioteca | Archivo adjunto | Origen oficial |
|---|---|---|
| PyMuPDF 1.28.2 | `pymupdf-1.28.2.tar.gz` (sdist de PyPI; su SHA-256 es el que publica PyPI) | <https://pypi.org/project/PyMuPDF/1.28.2/#files> y <https://github.com/pymupdf/PyMuPDF> (etiqueta `1.28.2`) |
| MuPDF 1.28.2 | `mupdf-1.28.2-source.tar.gz` (el sdist de PyMuPDF no lo incluye: lo descarga al construirse) | <https://mupdf.com/downloads/archive/mupdf-1.28.2-source.tar.gz> y <https://github.com/ArtifexSoftware/mupdf> |

SHA-256 de los adjuntos (formato de `sha256sum -c`):

```
5e0be7908a715aa20333caddd73f1d6f01e4cd0c26e869fa2dd0b7f344da2249  pymupdf-1.28.2.tar.gz
44075a84e329db55b9bef5f342a70fd26d69e48ad1d33cb89d9664581c641156  mupdf-1.28.2-source.tar.gz
```

## Licencia

Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)

Este programa es software libre: puedes redistribuirlo y modificarlo bajo los términos de
la GNU Affero General Public License publicada por la Free Software Foundation, en su
versión 3 o (a tu elección) cualquier versión posterior.

Este programa se distribuye con la esperanza de que sea útil, pero SIN NINGUNA GARANTÍA,
ni siquiera la garantía implícita de COMERCIABILIDAD o IDONEIDAD PARA UN PROPÓSITO
PARTICULAR. Consulta la GNU Affero General Public License para más detalles. Deberías haber
recibido una copia junto con este programa, en [`LICENSE`](LICENSE); si no, consulta
<https://www.gnu.org/licenses/>.
