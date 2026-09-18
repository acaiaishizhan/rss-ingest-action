import os
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from producer_lock import SingleInstanceLock


def test_kernel_lock_is_released_after_owner_crash(tmp_path):
    path = tmp_path/"producer"
    code = "from producer_lock import SingleInstanceLock;import sys;lock=SingleInstanceLock(sys.argv[1]);assert lock.acquire();print('locked',flush=True);sys.stdin.read()"
    child = subprocess.Popen([sys.executable,"-c",code,str(path)], cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        assert child.stdout.readline().strip() == "locked"
        contender = SingleInstanceLock(path)
        assert not contender.acquire()
        child.kill();child.wait(timeout=5)
        deadline=time.monotonic()+2
        acquired=contender.acquire()
        while not acquired and time.monotonic()<deadline:
            time.sleep(0.02)
            acquired=contender.acquire()
        assert acquired
        contender.release()
        assert path.with_name(path.name+".kernel").exists()
    finally:
        if child.poll() is None: child.kill();child.wait(timeout=5)
