"""get.sh warns before the installer starts when the CPU has no AVX (MongoDB 5+ needs it), backlog 712."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "get.sh").read_text(encoding="utf-8")


def run_check(tmp_path, cpuinfo):
    f = tmp_path / "cpuinfo"
    f.write_text(cpuinfo, encoding="utf-8")
    func = re.search(r"^check_avx\(\) \{.*?^\}\n", SCRIPT, re.S | re.M).group(0)
    prog = 'say() { printf "%s\\n" "$*"; }\nuname() { echo x86_64; }\n' + func + "check_avx\n"
    env = {"SCALIO_CPUINFO": str(f), "PATH": "/usr/bin:/bin"}
    cmd = ["/bin/bash", "-c", prog]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=True).stdout  # noqa: S603


def test_no_avx_prints_hint_with_remedies(tmp_path):
    out = run_check(tmp_path, "processor : 0\nflags : fpu sse sse2 ssse3 sse4_2\n")
    assert "kein AVX" in out and "Proxmox" in out and "Hyper-V" in out and "VMware" in out


def test_avx_is_silent(tmp_path):
    assert run_check(tmp_path, "flags : fpu sse sse4_2 avx avx2\n") == ""


def test_avx2_alone_is_not_avx(tmp_path):
    assert "kein AVX" in run_check(tmp_path, "flags : fpu avx2\n")


def test_check_runs_before_installer_start():
    assert SCRIPT.index("\ncheck_avx\n") < SCRIPT.index('say "Scalio Installer starten"')
