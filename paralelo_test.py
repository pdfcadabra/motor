# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Blanca Molina Rojas (PDFcadabra)
"""Tests de paralelo.py (procesos de trabajo del motor, desde la 1.5.2).

Uso: python -m unittest paralelo_test.py
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import paralelo

HERE = os.path.dirname(os.path.abspath(__file__))
GIB = 2 ** 30


def _double(x):
    return 2 * x


class Cuantos(unittest.TestCase):
    def workers(self, items, cpus=4, memory=6 * GIB, jobs="3", forced=None, worker=256 * 2 ** 20, min_items=1):
        env = {k: v for k, v in os.environ.items() if k not in (paralelo.WORKERS_ENV, paralelo.MAX_JOBS_ENV)}
        if jobs is not None:
            env[paralelo.MAX_JOBS_ENV] = jobs
        if forced is not None:
            env[paralelo.WORKERS_ENV] = forced
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(paralelo, "cpus", lambda: cpus), \
                mock.patch.object(paralelo, "container_memory", lambda: memory):
            return paralelo.workers(items, worker, min_items)

    def test_produccion_tres_de_cuatro_nucleos(self):
        # 4 vCPU sin cuota (el compose no pone `cpus`), 6 GiB y 3 trabajos: uno libre para el resto.
        self.assertEqual(self.workers(398), 3)

    def test_limites(self):
        self.assertEqual(self.workers(398, cpus=16), paralelo.MAX_WORKERS)
        self.assertEqual(self.workers(398, cpus=1), 1)
        self.assertEqual(self.workers(398, cpus=2), 1)
        self.assertEqual(self.workers(2), 2)
        self.assertEqual(self.workers(1), 1)
        self.assertEqual(self.workers(5, min_items=8), 1)
        # Memoria: 0,7 × 1 GiB / 3 trabajos = 239 MiB, menos de lo que necesita uno: 1.
        self.assertEqual(self.workers(398, memory=GIB), 1)
        # Con 2 GiB y 1 trabajo: 1,4 GiB / 256 MiB = 5, se queda en 3 por la CPU.
        self.assertEqual(self.workers(398, memory=2 * GIB, jobs="1"), 3)
        self.assertEqual(self.workers(398, memory=None), 3)

    def test_variable(self):
        self.assertEqual(self.workers(398, forced="1"), 1)
        self.assertEqual(self.workers(398, forced="7"), 7)
        self.assertEqual(self.workers(3, forced="7"), 3)
        self.assertEqual(self.workers(5, forced="2", min_items=8), 2)
        self.assertEqual(self.workers(398, forced="x"), 3)

    def test_cpus_con_cuota_del_cgroup(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: [os.remove(os.path.join(d, f)) for f in os.listdir(d)] and os.rmdir(d))
        v2 = os.path.join(d, "cpu.max")
        with open(v2, "w") as f:
            f.write("150000 100000\n")
        with mock.patch.object(paralelo, "CGROUP_CPU_V2", v2), mock.patch.object(paralelo.os, "sched_getaffinity", lambda pid: set(range(8))):
            self.assertEqual(paralelo.cpus(), 2)
        with open(v2, "w") as f:
            f.write("max 100000\n")
        with mock.patch.object(paralelo, "CGROUP_CPU_V2", v2), mock.patch.object(paralelo.os, "sched_getaffinity", lambda pid: set(range(4))):
            self.assertEqual(paralelo.cpus(), 4)


class Procesos(unittest.TestCase):
    def test_resultados_en_orden_con_y_sin_procesos(self):
        for n in (1, 3):
            with paralelo.Pool(n) as pool:
                self.assertEqual(list(pool.imap(_double, range(20))), [2 * i for i in range(20)])

    @unittest.skipUnless(sys.platform.startswith("linux"), "PR_SET_PDEATHSIG es de Linux")
    def test_mueren_con_el_principal(self):
        # Un proceso con dos de trabajo que no acaban: al matar al principal con SIGKILL (como la
        # API con su tope), mueren también.
        code = ("import sys, time; sys.path.insert(0, %r); import paralelo\n"
                "if __name__ == '__main__':\n"
                "    with paralelo.Pool(2) as p:\n"
                "        print('listo', flush=True); list(p.imap(time.sleep, [600, 600]))\n") % HERE
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.assertEqual(proc.stdout.readline().strip(), "listo")
        children = []
        for _ in range(100):
            with open(f"/proc/{proc.pid}/task/{proc.pid}/children") as f:
                children = [int(p) for p in f.read().split()]
            if len(children) >= 2:
                break
            time.sleep(0.05)
        self.assertGreaterEqual(len(children), 2)
        proc.send_signal(signal.SIGKILL)
        proc.wait()

        def alive(pid):
            try:
                with open(f"/proc/{pid}/stat") as f:
                    return f.read().split(") ")[1][0] not in "ZX"
            except FileNotFoundError:
                return False

        for _ in range(100):
            if not any(alive(p) for p in children):
                break
            time.sleep(0.05)
        self.assertEqual([p for p in children if alive(p)], [])


if __name__ == "__main__":
    unittest.main()
