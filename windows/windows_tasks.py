"""User-scoped Windows one-shot tasks. No passwords, elevation or wake requests.

Task XML follows Microsoft's Task Scheduler 2.0 schema:
https://learn.microsoft.com/windows/win32/taskschd/task-scheduler-schema
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import locale
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from xml.etree import ElementTree as ET

from app_state import due_eligible, schedule_id, utc_timestamp


NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
ET.register_namespace('', NS)
TASK_PREFIX = 'PawchiveDownloader-'


class PlanTaskError(RuntimeError):
    pass


def _system_executable(name):
    if sys.platform != 'win32':
        raise PlanTaskError('Windows Task Scheduler is only available on Windows.')
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    count = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not count or count >= len(buffer):
        raise PlanTaskError('The Windows system directory could not be located.')
    return Path(buffer.value) / name


def current_user_sid(*, runner=None):
    runner = runner or subprocess.run
    try:
        result = runner([str(_system_executable('whoami.exe')), '/user', '/fo', 'csv', '/nh'],
                        capture_output=True, check=False, timeout=10,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise PlanTaskError('The current Windows account could not be identified.')
        output = result.stdout.decode('ascii', 'ignore') if isinstance(result.stdout, bytes) else result.stdout
        for row in csv.reader(io.StringIO(output)):
            if row and re.fullmatch(r'S-\d+(?:-\d+)+', row[-1].strip(), re.I):
                return row[-1].strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PlanTaskError('The current Windows account could not be identified.') from error
    raise PlanTaskError('The current Windows account did not return a valid SID.')


def task_name(identifier, user_sid):
    identifier = schedule_id(identifier)
    if not re.fullmatch(r'S-\d+(?:-\d+)+', str(user_sid), re.I):
        raise ValueError('Invalid Windows account SID.')
    owner = hashlib.sha256(str(user_sid).encode('ascii')).hexdigest()[:12]
    return TASK_PREFIX + owner + '-' + uuid_hex(identifier)


def uuid_hex(identifier):
    return schedule_id(identifier).replace('-', '')


def boot_time(now=None):
    """Approximate kernel boot time; pending jobs still have a short grace bound."""
    now = time.time() if now is None else utc_timestamp(now)
    if sys.platform == 'win32':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetTickCount64.restype = ctypes.c_ulonglong
        return now - kernel.GetTickCount64() / 1000.
    # Development/testing fallback only; no cross-platform scheduler is installed.
    return max(0., now - time.monotonic())


def _absolute_path(value, label):
    value = os.fspath(value)
    if not value or any(ord(character) < 32 for character in value) or not os.path.isabs(value):
        raise ValueError(f'{label} must be an absolute path without control characters.')
    return str(Path(value))


@dataclass(frozen=True)
class TaskAction:
    executable: str
    arguments: tuple[str, ...]
    working_directory: str

    def __post_init__(self):
        _absolute_path(self.executable, 'Executable')
        _absolute_path(self.working_directory, 'Working directory')
        if not isinstance(self.arguments, tuple) or any(
                not isinstance(argument, str) or any(ord(character) < 32 for character in argument)
                for argument in self.arguments):
            raise ValueError('Invalid scheduled application arguments.')
        # Appointment data never becomes arbitrary command line content.
        args = self.arguments
        if len(args) == 3:
            _absolute_path(args[0], 'Source script')
            args = args[1:]
        if len(args) != 2 or args[0] != '--scheduled':
            raise ValueError('A task action may only dispatch a known scheduled download.')
        schedule_id(args[1])

    @classmethod
    def for_application(cls, identifier, *, executable=None, script=None, frozen=None):
        identifier = schedule_id(identifier)
        # An explicit standalone application path is used for the stable runtime
        # copy maintained by the application controller.
        if frozen is None:
            frozen = bool(getattr(sys, 'frozen', False)) or (executable is not None and script is None)
        program = Path(executable or sys.executable).resolve()
        if frozen:
            return cls(str(program), ('--scheduled', identifier), str(program.parent))
        main = Path(script or Path(__file__).with_name('download_pawchive_images.py')).resolve()
        if executable is None and program.name.lower() == 'python.exe':
            windowed = program.with_name('pythonw.exe')
            if windowed.is_file():
                program = windowed
        return cls(str(program), (str(main), '--scheduled', identifier), str(main.parent))


def build_task_xml(schedule, action, user_sid, *, description='Pawchive Downloader scheduled download'):
    identifier = schedule_id(schedule.id)
    due_at = utc_timestamp(schedule.due_at)
    if not isinstance(action, TaskAction):
        raise TypeError('A schedule needs a validated TaskAction.')
    if action.arguments[-1] != identifier:
        raise ValueError('The scheduled action does not match this appointment.')
    task_name(identifier, user_sid)  # Validate the account without executing tools.
    task = ET.Element(f'{{{NS}}}Task', {'version': '1.2'})

    def child(parent, name, text=None, **attributes):
        element = ET.SubElement(parent, f'{{{NS}}}{name}', attributes)
        if text is not None:
            element.text = str(text)
        return element

    registration = child(task, 'RegistrationInfo')
    child(registration, 'Author', 'Pawchive Downloader')
    child(registration, 'Description', str(description)[:1000])
    triggers = child(task, 'Triggers')
    trigger = child(triggers, 'TimeTrigger')
    child(trigger, 'StartBoundary', datetime.fromtimestamp(due_at, timezone.utc).isoformat(timespec='seconds'))
    child(trigger, 'Enabled', 'true')
    principals = child(task, 'Principals')
    principal = child(principals, 'Principal', id='CurrentUser')
    child(principal, 'UserId', user_sid)
    child(principal, 'LogonType', 'InteractiveToken')
    child(principal, 'RunLevel', 'LeastPrivilege')
    settings = child(task, 'Settings')
    for name, value in (
            ('MultipleInstancesPolicy', 'IgnoreNew'),
            ('DisallowStartIfOnBatteries', 'false'), ('StopIfGoingOnBatteries', 'false'),
            ('AllowHardTerminate', 'false'), ('StartWhenAvailable', 'false'),
            ('RunOnlyIfNetworkAvailable', 'false'), ('AllowStartOnDemand', 'true'),
            ('Enabled', 'true'), ('Hidden', 'false'), ('RunOnlyIfIdle', 'false'),
            ('WakeToRun', 'false'), ('ExecutionTimeLimit', 'PT0S'), ('Priority', '7')):
        child(settings, name, value)
    actions = child(task, 'Actions', Context='CurrentUser')
    execute = child(actions, 'Exec')
    child(execute, 'Command', action.executable)
    child(execute, 'Arguments', subprocess.list2cmdline(action.arguments))
    child(execute, 'WorkingDirectory', action.working_directory)
    return ET.tostring(task, encoding='utf-16', xml_declaration=True)


class WindowsTaskScheduler:
    def __init__(self, *, user_sid=None, runner=None, executable=None):
        self._user_sid = user_sid
        self._runner = runner or subprocess.run
        self._executable = executable

    @property
    def user_sid(self):
        if self._user_sid is None:
            self._user_sid = current_user_sid(runner=self._runner)
        return self._user_sid

    def name(self, identifier):
        return task_name(identifier, self.user_sid)

    def _run(self, arguments):
        program = self._executable or _system_executable('schtasks.exe')
        try:
            result = self._runner([str(program), *arguments], capture_output=True, check=False,
                                  timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PlanTaskError('Windows Task Scheduler is unavailable. Check the Task Scheduler service and current account permissions.') from error
        return result

    def install(self, schedule, action):
        data = build_task_xml(schedule, action, self.user_sid)
        # The XML contains only an executable path and UUID, never passwords,
        # invitation keys, artist links or selected download directories.
        with tempfile.TemporaryDirectory(prefix='pawchive_task_') as folder:
            xml_file = Path(folder) / 'task.xml'
            xml_file.write_bytes(data)
            result = self._run(['/Create', '/TN', self.name(schedule.id), '/XML', str(xml_file), '/F'])
        if result.returncode:
            raise PlanTaskError('Windows could not save this schedule. Check the Task Scheduler service and current account permissions.')
        return self.name(schedule.id)

    def remove(self, identifier):
        name = self.name(identifier)
        # A missing task is already removed. A failed query is not silently
        # treated as success: task-service/permission failures remain visible.
        result = self._run(['/Delete', '/TN', name, '/F'])
        if result.returncode:
            query = self._run(['/Query', '/TN', name, '/XML'])
            if query.returncode:
                output = (query.stderr or b'') + (query.stdout or b'') if isinstance(query.stdout, bytes) else str(query.stderr or '') + str(query.stdout or '')
                text = output
                if isinstance(output, bytes):
                    for encoding in ('utf-8-sig', locale.getencoding(), 'mbcs'):
                        try:
                            text=output.decode(encoding); break
                        except (UnicodeError, LookupError):
                            continue
                    if isinstance(text, bytes):
                        text=output.decode('utf-8', 'replace')
                missing = any(token in text.lower() for token in ('0x80070002', 'cannot find the file', 'specified task name', '找不到指定的文件', '找不到指定的任务'))
                if missing:
                    return False
            raise PlanTaskError('Windows could not remove this application schedule.')
        return True
