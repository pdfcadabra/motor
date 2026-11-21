# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Trabajo en paralelo dentro de un proceso del motor (desde la 1.5.2).

MuPDF no suelta el GIL: para usar más de un núcleo en una tarea de muchas imágenes (los escaneos en
blanco y negro de jbig2.py y reduce_bn.py) hay que repartirla entre procesos. Cada proceso de
trabajo abre su propia copia del PDF (solo lectura: nunca escribe el archivo) y devuelve por una
tubería lo calculado; el proceso principal es el único que escribe la salida, en el mismo orden que
sin procesos, así que el resultado no depende de cuántos haya.

Cuántos (`workers`): los núcleos que puede usar el contenedor (afinidad y cuota del cgroup) menos
uno, que queda para la API, Caddy y el sistema; como mucho MAX_WORKERS y nunca más de los que caben
en la memoria de un trabajo (MEMORY_SHARE de la memoria del contenedor entre los trabajos a la vez,
MOTOR_MAX_JOBS, como redact.py y pdf_a_word.py) con `worker_bytes` cada uno. Con 1, todo en este
mismo proceso (sin procesos aparte). MOTOR_WORKERS lo fija (para medir y para los tests).

Los procesos de trabajo mueren con el principal (PR_SET_PDEATHSIG con SIGKILL): si a este le mata
la API (tope de tiempo, cliente que se va), no quedan huérfanos. Se arrancan con «spawn» (sin
heredar la memoria ni los documentos abiertos del principal).
"""
import ctypes
import multiprocessing
import os
import signal

WORKERS_ENV = "MOTOR_WORKERS"
MAX_JOBS_ENV = "MOTOR_MAX_JOBS"
MAX_WORKERS = 4
MEMORY_SHARE = 0.7
CGROUP_MEMORY_FILES = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")
CGROUP_CPU_V2 = "/sys/fs/cgroup/cpu.max"
CGROUP_CPU_V1 = ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us")
_PR_SET_PDEATHSIG = 1


def _read(path):
    try:
        with open(path, encoding="ascii") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def cpus():
    """Núcleos que puede usar este proceso: su afinidad, recortada por la cuota de CPU del cgroup
    (v2 cpu.max o v1 cfs_quota_us / cfs_period_us) si la hay (redondeada hacia arriba)."""
    try:
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count() or 1
    quota = None
    v2 = _read(CGROUP_CPU_V2)
    if v2:
        parts = v2.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]) > 0:
            quota = int(parts[0]) / int(parts[1])
    else:
        q, p = (_read(f) for f in CGROUP_CPU_V1)
        if q and p and q.lstrip("-").isdigit() and p.isdigit() and int(q) > 0 and int(p) > 0:
            quota = int(q) / int(p)
    if quota is not None:
        n = min(n, max(1, -(-quota // 1)))
    return max(1, int(n))


def container_memory():
    """Bytes de memoria del contenedor (como redact.container_memory): el límite del cgroup o, sin
    límite, la RAM física; None si no se sabe."""
    try:
        physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        physical = None
    for path in CGROUP_MEMORY_FILES:
        value = _read(path)
        if value is None:
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


def workers(items, worker_bytes, min_items=1):
    """Procesos de trabajo para `items` tareas que usan unos `worker_bytes` cada proceso. Con menos
    de `min_items` tareas, 1 (arrancar procesos cuesta más de lo que ahorran)."""
    if items <= 1:
        return 1
    forced = os.environ.get(WORKERS_ENV, "")
    if forced.isdigit() and int(forced) > 0:
        return min(int(forced), items)
    if items < min_items:
        return 1
    n = min(MAX_WORKERS, max(1, cpus() - 1))
    memory = container_memory()
    if memory is not None:
        n = min(n, max(1, int(MEMORY_SHARE * memory / max_jobs() // worker_bytes)))
    return max(1, min(n, items))


def _start(parent, init, args):
    """Al arrancar cada proceso de trabajo: que muera con el principal; Ctrl-C solo al principal."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except (OSError, AttributeError):  # sin prctl (no es Linux): no se usan procesos (ver Pool)
        pass
    if os.getppid() != parent:  # el principal ya ha muerto
        os._exit(1)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if init is not None:
        init(*args)


def _can_fork_children():
    try:
        ctypes.CDLL(None).prctl  # noqa: B018
        return True
    except (OSError, AttributeError):
        return False


class Pool:
    """`with Pool(n, init, args) as p: for r in p.imap(fn, items): ...` — resultados en el orden
    de `items`. Con n == 1 (o sin prctl), en este proceso: `init(*args)` aquí, `fn` una a una y
    `done()` al salir (lo que en un proceso aparte se libera al terminar este)."""

    def __init__(self, n, init=None, args=(), done=None):
        self.n = n if n > 1 and _can_fork_children() else 1
        self._init, self._args, self._done = init, args, done
        self._pool = None

    def __enter__(self):
        if self.n == 1:
            if self._init is not None:
                self._init(*self._args)
        else:
            ctx = multiprocessing.get_context("spawn")
            self._pool = ctx.Pool(self.n, initializer=_start, initargs=(os.getpid(), self._init, self._args))
        return self

    def imap(self, fn, items):
        if self._pool is None:
            return map(fn, items)
        return self._pool.imap(fn, items)

    def terminate(self):
        if self._pool is not None:
            self._pool.terminate()
            self._pool.join()
            self._pool = None

    def __exit__(self, *exc):
        if self.n == 1 and self._done is not None:
            self._done()
        if self._pool is not None:
            if exc[0] is None:
                self._pool.close()
                self._pool.join()
            else:
                self._pool.terminate()
                self._pool.join()
            self._pool = None
        return False
