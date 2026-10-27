"""Bounded, durable recovery policy for the video worker only.

Unknown model receipts remain unknown even when a standing operator policy permits
one replacement. Uncertain business writes never gain that permission.
"""
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import time


class RecoveryDeferred(RuntimeError):
    def __init__(self, message, retry_at):
        super().__init__(message)
        self.retry_at = retry_at


class RecoveryExhausted(RuntimeError):
    pass


class ReviewRejected(ValueError):
    def __init__(self, issues):
        super().__init__('Second-pass review exhausted bounded evidence repairs')
        self.issues = list(issues)


def policy(env):
    enabled = str(env.get('FINANCE_AUTO_RECOVERY', '0')).lower() in {'1', 'true'}
    def number(key, default, low, high):
        raw = env.get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError('Invalid finance recovery policy: ' + key) from None
        if str(value) != str(raw) or not low <= value <= high:
            raise ValueError('Invalid finance recovery policy: ' + key)
        return value
    return {'enabled': enabled,
        'max_calls': number('FINANCE_MAX_CALLS_PER_VIDEO', 32, 8, 64),
        'semantic_repairs': number('FINANCE_SEMANTIC_REPAIRS', 3 if enabled else 1, 1, 3),
        'max_attempts': number('FINANCE_MAX_TASK_ATTEMPTS', 8 if enabled else 5, 1, 12),
        'unknown_grace': number('FINANCE_UNKNOWN_GRACE_SECONDS', 1800, 300, 7200),
        'unknown_replacements': number('FINANCE_UNKNOWN_REPLACEMENTS', 1 if enabled else 0, 0, 1)}


def atomic(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def boot_id():
    path = Path('/proc/sys/kernel/random/boot_id')
    return path.read_text().strip() if path.exists() else None


def process_id(pid):
    path = Path('/proc') / str(pid) / 'stat'
    if path.exists():
        return path.read_text().rsplit(')', 1)[1].split()[19]
    return None


def pending_marker(payload_hash, instruction_hash, frame_hashes):
    return {'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'started_epoch': time.time(), 'input_sha256': payload_hash,
        'instruction_sha256': instruction_hash, 'frames': frame_hashes,
        'owner_pid': os.getpid(), 'owner_start': process_id(os.getpid()), 'owner_boot': boot_id()}


def pending_started(marker):
    if 'started_epoch' in marker:
        return float(marker['started_epoch'])
    try:
        moment = dt.datetime.fromisoformat(marker['started_utc'].replace('Z', '+00:00'))
        if moment.tzinfo is None:
            raise ValueError()
        return moment.timestamp()
    except (KeyError, ValueError, TypeError):
        raise RecoveryExhausted('Unknown request has no trustworthy start time; inspect receipt') from None


def owner_alive(marker):
    if marker.get('request_ended_utc'):
        return False
    if marker.get('owner_boot') and marker['owner_boot'] != boot_id():
        return False
    pid = marker.get('owner_pid')
    if not isinstance(pid, int) or pid <= 0:
        return False
    if marker.get('owner_start'):
        return process_id(pid) == marker['owner_start']
    if os.name == 'nt':
        # os.kill(pid, 0) on Windows terminates a process; query it read-only.
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            status = ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # Lack of access is not proof that another worker is gone.


def admit_request(directory, env, name=None):
    rules = policy(env)
    if not rules['enabled']:
        return
    directory = Path(directory)
    for path in directory.glob('*.pending.json'):
        parent = path.name.removesuffix('.pending.json')
        if (directory / (parent + '.response.json')).exists():
            continue
        audits = list(directory.glob(parent + '-transport-recovery-001.transport-recovery.audit.json'))
        if audits:
            audit = json.loads(audits[0].read_text())
            child = audit.get('replacement_name', '')
            if (audit.get('parent_pending_sha256') == digest(path) and child and
                    (name == child or (name or '').startswith(child + '-output-recovery-001') or
                     (directory / (child + '.response.json')).exists())):
                continue
        marker = json.loads(path.read_text())
        due = pending_started(marker) + rules['unknown_grace']
        if time.time() < due or owner_alive(marker):
            raise RecoveryDeferred('An earlier model request still needs its receipt or grace period', max(due, time.time()+300))
        raise RecoveryExhausted('An older unknown model request cannot be bypassed by a new payload')
    names = {p.name.removesuffix(suffix) for suffix in ('.response.json', '.pending.json')
             for p in directory.glob('*' + suffix)}
    if len(names) >= rules['max_calls']:
        raise RecoveryExhausted('Video model-call ceiling reached; preserve cached evidence')


def replacement(directory, name, marker, env, input_hash, *, is_replacement=False):
    directory = Path(directory)
    rules = policy(env)
    if not rules['enabled'] or not rules['unknown_replacements']:
        raise RecoveryExhausted('Unresolved model request; automatic replacement is disabled')
    if marker.get('input_sha256') != input_hash:
        raise RecoveryExhausted('Unresolved request input changed; cannot bypass the original fence')
    due = pending_started(marker) + rules['unknown_grace']
    if owner_alive(marker):
        raise RecoveryDeferred('Original model request is still owned by a live worker', max(due, time.time() + 300))
    if time.time() < due:
        raise RecoveryDeferred('Waiting for unknown model receipt grace period', due)
    if is_replacement:
        raise RecoveryExhausted('Automatic unknown-request replacement is exhausted; receipt still unknown')
    child = name + '-transport-recovery-001'
    audit = directory / (child + '.transport-recovery.audit.json')
    audits = list(directory.glob('*.transport-recovery.audit.json'))
    if audit.exists():
        saved = json.loads(audit.read_text())
        if saved.get('parent_input_sha256') != input_hash or saved.get('parent_pending_sha256') != digest(directory / (name + '.pending.json')):
            raise RecoveryExhausted('Unknown replacement audit does not match the original request')
    elif len(audits) >= rules['unknown_replacements']:
        raise RecoveryExhausted('Per-video unknown replacement allowance is exhausted')
    else:
        atomic(audit, {'parent_pending_file': name + '.pending.json',
            'parent_pending_sha256': digest(directory / (name + '.pending.json')),
            'parent_input_sha256': input_hash, 'parent_result_remains_unknown': True,
            'replacement_name': child, 'standing_policy': 'FINANCE_AUTO_RECOVERY',
            'max_replacements_per_video': rules['unknown_replacements'],
            'possible_duplicate_billing': True, 'created_utc': dt.datetime.now(dt.timezone.utc).isoformat()})
    return child


def retry_delay(attempt):
    return (300, 900, 1800)[min(max(attempt - 1, 0), 2)]


@contextlib.contextmanager
def advisory_lock(path):
    """Crash-safe lock; the persistent inode avoids unlink/recreate races."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b'0')
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)
