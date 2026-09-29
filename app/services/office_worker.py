"""Linux resource/network restrictions, applied before exec (never in API threads)."""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import resource
import socket
import sys


def restrict_process(memory_mb: int, cpu_seconds: int, output_bytes: int) -> None:
    resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024,) * 2)
    cpu_hard_seconds = cpu_seconds * (os.cpu_count() or 1) + 1
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_hard_seconds))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output_bytes,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Seccomp survives exec and inheritance. LibreOffice may use local UNIX
    # sockets, but documents must not fetch remote images, links, or formulas.
    library = ctypes.util.find_library("seccomp")
    if library is None:
        raise RuntimeError("libseccomp is required for Office conversion")
    lib = ctypes.CDLL(library, use_errno=True)

    class ArgCmp(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                    ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]

    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ArgCmp)]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("Cannot initialize conversion network restrictions")
    try:
        syscall = lib.seccomp_syscall_resolve_name(b"socket")
        # SCMP_CMP_NE: only AF_UNIX sockets are permitted.
        comparison = ArgCmp(0, 1, socket.AF_UNIX, 0)
        if syscall < 0 or lib.seccomp_rule_add_array(context, 0x00050000 | errno.EPERM, syscall, 1, ctypes.byref(comparison)) != 0:
            raise RuntimeError("Cannot configure conversion network restrictions")
        if lib.seccomp_load(context) != 0:
            raise RuntimeError("Cannot apply conversion network restrictions")
    finally:
        lib.seccomp_release(context)


if __name__ == "__main__":
    restrict_process(int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
    os.execv(sys.argv[4], sys.argv[4:])
