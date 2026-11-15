"""Reproduce h5py's documented service-thread/GC deadlock in an isolated process."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_threaded_hdf5_reads_complete_with_cyclic_hdf5_garbage():
    script = textwrap.dedent('''
        import gc, io
        from concurrent.futures import ThreadPoolExecutor
        import h5py
        from terra.acquire import _hdf5_reader

        gc.collect()
        gc.set_threshold(1000000)
        data = io.BytesIO()
        with h5py.File(data, 'w') as f:
            f['values'] = [1, 2, 3]
        payload = data.getvalue()
        victim = h5py.File(io.BytesIO(payload), 'r')
        cycle = {'dataset': victim['values']}
        cycle['self'] = cycle
        del cycle

        with ThreadPoolExecutor(max_workers=1) as service:
            class ThreadedFile(io.BytesIO):
                def readinto(self, buffer):
                    def read():
                        # Model automatic GC caused by HTTP allocations. An
                        # explicit collect() alone would ignore gc.disable().
                        if gc.isenabled():
                            gc.collect()
                        return io.BytesIO.readinto(self, buffer)
                    return service.submit(read).result(timeout=2)
            with _hdf5_reader(), h5py.File(ThreadedFile(payload), 'r') as f:
                assert list(f['values'][:]) == [1, 2, 3]
        assert gc.isenabled()
        try:
            with _hdf5_reader():
                raise RuntimeError('reader failed')
        except RuntimeError:
            pass
        assert gc.isenabled()
        victim.close()
        gc.collect()
    ''')
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'backend')}
    subprocess.run([sys.executable, '-c', script], env=environment, check=True, capture_output=True, timeout=10)
