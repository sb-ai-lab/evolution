"""Start Hermes with filesystem writes confined to its workspace and run state."""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
import stat
import sys
from pathlib import Path

_CREATE_RULESET = 444
_ADD_RULE = 445
_RESTRICT_SELF = 446
_VERSION = 1
_PATH_BENEATH = 1
_NO_NEW_PRIVS = 38

_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000
_SCMP_CMP_NE = 1
_SCMP_CMP_EQ = 4
_AF_UNIX = 1
_UNMEDIATED_MUTATIONS = (
    "truncate",
    "truncate64",
    "chmod",
    "fchmod",
    "fchmodat",
    "fchmodat2",
    "chown",
    "fchown",
    "lchown",
    "fchownat",
    "setxattr",
    "fsetxattr",
    "lsetxattr",
    "removexattr",
    "fremovexattr",
    "lremovexattr",
    "utime",
    "utimes",
    "futimesat",
    "utimensat",
    "utimensat_time64",
    "kill",
    "tkill",
    "tgkill",
    "pidfd_open",
    "pidfd_getfd",
    "pidfd_send_signal",
    "process_vm_readv",
    "process_vm_writev",
    "ptrace",
    "kcmp",
    "setpriority",
    "sched_setparam",
    "sched_setscheduler",
    "sched_setaffinity",
    "ioprio_set",
    "migrate_pages",
    "move_pages",
    # setsid stays allowed: hermes spawns every tool subprocess with
    # preexec_fn=os.setsid; denying it kills all file tools with
    # "Exception occurred in preexec_fn" (landlock still confines fs).
    "symlink",
    "symlinkat",
    "link",
    "linkat",
)

_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
_ALL = (1 << 13) - 1
_WRITE_ROOT_ACCESS = _ALL & ~_MAKE_SYM
_READ_EXEC = _EXECUTE | _READ_FILE | _READ_DIR


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
        ("_padding", ctypes.c_uint32),
    ]


class _ScmpArgCmp(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    ]


def _syscall(libc: ctypes.CDLL, number: int, *args: object) -> int:
    result = int(libc.syscall(number, *args))
    if result < 0:
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value))
    return result


def _add_path(libc: ctypes.CDLL, ruleset_fd: int, path: Path, access: int) -> None:
    if not path.exists():
        return
    allowed = access
    if not stat.S_ISDIR(path.stat().st_mode):
        allowed &= _EXECUTE | _READ_FILE | _WRITE_FILE
    path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        attributes = _PathBeneathAttr(allowed_access=allowed, parent_fd=path_fd)
        _syscall(
            libc,
            _ADD_RULE,
            ruleset_fd,
            _PATH_BENEATH,
            ctypes.byref(attributes),
            0,
        )
    finally:
        os.close(path_fd)


def _block_unmediated_mutations() -> None:
    try:
        seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError as exc:
        raise RuntimeError("libseccomp is required for safe Hermes runs") from exc
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_rule_add.restype = ctypes.c_int
    seccomp.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(_ScmpArgCmp),
    ]
    seccomp.seccomp_rule_add_array.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(_SCMP_ACT_ALLOW)
    if not context:
        raise RuntimeError("seccomp_init failed")
    try:
        deny = _SCMP_ACT_ERRNO | errno.EPERM
        for name in _UNMEDIATED_MUTATIONS:
            number = seccomp.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and seccomp.seccomp_rule_add(context, deny, number, 0) != 0:
                raise RuntimeError(f"seccomp could not deny {name}")
        nonself_pid = _ScmpArgCmp(arg=0, op=_SCMP_CMP_NE, datum_a=0, datum_b=0)
        number = seccomp.seccomp_syscall_resolve_name(b"prlimit64")
        if (
            number >= 0
            and seccomp.seccomp_rule_add_array(context, deny, number, 1, ctypes.byref(nonself_pid))
            != 0
        ):
            raise RuntimeError("seccomp could not restrict prlimit64")
        unix_family = _ScmpArgCmp(arg=0, op=_SCMP_CMP_EQ, datum_a=_AF_UNIX, datum_b=0)
        number = seccomp.seccomp_syscall_resolve_name(b"socket")
        if (
            number >= 0
            and seccomp.seccomp_rule_add_array(context, deny, number, 1, ctypes.byref(unix_family))
            != 0
        ):
            raise RuntimeError("seccomp could not deny socket(AF_UNIX)")
        if seccomp.seccomp_load(context) != 0:
            raise RuntimeError("seccomp_load failed")
    finally:
        seccomp.seccomp_release(context)


def restrict(write_paths: list[Path], read_paths: list[Path]) -> None:
    """Apply a fail-closed Landlock allowlist to this process and its children."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        version = _syscall(libc, _CREATE_RULESET, 0, 0, _VERSION)
    except OSError as exc:
        if exc.errno in {errno.ENOSYS, errno.EOPNOTSUPP, errno.EINVAL}:
            raise RuntimeError("Landlock is unavailable") from exc
        raise
    if version < 1:
        raise RuntimeError("Landlock ABI 1 is required")

    ruleset = _RulesetAttr(handled_access_fs=_ALL)
    ruleset_fd = _syscall(
        libc,
        _CREATE_RULESET,
        ctypes.byref(ruleset),
        ctypes.sizeof(ruleset),
        0,
    )
    try:
        for path in write_paths:
            _add_path(libc, ruleset_fd, path.resolve(strict=True), _WRITE_ROOT_ACCESS)
        for path in read_paths:
            _add_path(libc, ruleset_fd, path.resolve(strict=True), _READ_EXEC)
        for path in (
            Path("/usr"),
            Path("/usr/local"),
            Path("/bin"),
            Path("/lib"),
            Path("/lib64"),
            Path("/etc/ssl"),
            Path("/etc/ca-certificates"),
            Path("/etc/ld.so.cache"),
            Path("/etc/resolv.conf"),
            Path("/etc/hosts"),
            Path("/etc/host.conf"),
            Path("/etc/nsswitch.conf"),
            Path("/etc/gai.conf"),
            Path("/etc/localtime"),
            Path("/dev/urandom"),
            Path("/dev/random"),
        ):
            _add_path(libc, ruleset_fd, path, _READ_EXEC)
        _add_path(libc, ruleset_fd, Path("/dev/null"), _READ_EXEC | _WRITE_FILE)
        if libc.prctl(_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            value = ctypes.get_errno()
            raise OSError(value, os.strerror(value))
        _syscall(libc, _RESTRICT_SELF, ruleset_fd, 0)
    finally:
        os.close(ruleset_fd)
    _block_unmediated_mutations()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--read", type=Path, action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("command required after --")
    try:
        restrict([args.workspace, args.state], args.read)
        os.chdir(args.workspace)
        os.execvpe(command[0], command, os.environ)
    except Exception as exc:
        print(f"hermes_sandbox: {exc}", file=sys.stderr)
        return 125
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
