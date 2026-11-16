"""Local Windows toast and audible timer completion cue."""

from array import array
import base64
import ctypes
from functools import lru_cache
import io
import math
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID
import wave
from xml.sax.saxutils import escape

APP_ID = 'BountyTracker.Desktop'


class Guid(ctypes.Structure):
    _fields_ = [('bytes', ctypes.c_ubyte * 16)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(UUID(value).bytes_le)


class PropertyKey(ctypes.Structure):
    _fields_ = [('format', Guid), ('id', ctypes.c_uint32)]


class PropertyValue(ctypes.Union):
    _fields_ = [('text', ctypes.c_wchar_p), ('padding', ctypes.c_ubyte * 16)]


class PropertyVariant(ctypes.Structure):
    _fields_ = [('kind', ctypes.c_ushort), ('reserved', ctypes.c_ushort * 3), ('value', PropertyValue)]


def _check_result(status):
    if status < 0:
        raise OSError(f'Windows returned HRESULT 0x{status & 0xFFFFFFFF:08X}.')


def _method(pointer, slot, restype, *argtypes):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[slot])


def _call(pointer, slot, argtypes, *arguments):
    _check_result(_method(pointer, slot, ctypes.c_long, *argtypes)(pointer, *arguments))


def _query(pointer, interface):
    result = ctypes.c_void_p()
    _call(pointer, 0, (ctypes.POINTER(Guid), ctypes.POINTER(ctypes.c_void_p)),
          ctypes.byref(Guid.parse(interface)), ctypes.byref(result))
    return result


def set_app_id():
    if os.name == 'nt':
        set_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_id.argtypes = (ctypes.c_wchar_p,)
        set_id(APP_ID)


def write_shortcut(path, executable, arguments='', *, app_id=APP_ID):
    """Save a Start-menu Shell Link carrying the notification's AppUserModelID."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ole32 = ctypes.OleDLL('ole32')
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    ole32.CoCreateInstance.argtypes = (ctypes.POINTER(Guid), ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.POINTER(Guid), ctypes.POINTER(ctypes.c_void_p))
    ole32.CoInitializeEx(None, 2)
    interfaces = []
    try:
        link = ctypes.c_void_p()
        ole32.CoCreateInstance(
            ctypes.byref(Guid.parse('00021401-0000-0000-C000-000000000046')), None, 1,
            ctypes.byref(Guid.parse('000214F9-0000-0000-C000-000000000046')), ctypes.byref(link))
        interfaces.append(link)
        # IShellLinkW: SetPath, SetArguments, and SetWorkingDirectory.
        _call(link, 20, (ctypes.c_wchar_p,), str(executable))
        _call(link, 11, (ctypes.c_wchar_p,), arguments)
        _call(link, 9, (ctypes.c_wchar_p,), str(Path(executable).parent))
        _call(link, 7, (ctypes.c_wchar_p,), 'BountyTracker')
        properties = _query(link, '886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99')
        interfaces.append(properties)
        key = PropertyKey(Guid.parse('9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3'), 5)
        variant = PropertyVariant()
        variant.kind = 31  # VT_LPWSTR
        variant.value.text = app_id
        _call(properties, 6, (ctypes.POINTER(PropertyKey), ctypes.POINTER(PropertyVariant)),
              ctypes.byref(key), ctypes.byref(variant))
        _call(properties, 7, ())
        file = _query(link, '0000010B-0000-0000-C000-000000000046')
        interfaces.append(file)
        _call(file, 6, (ctypes.c_wchar_p, ctypes.c_int), str(path), 1)
    finally:
        for pointer in reversed(interfaces):
            _method(pointer, 2, ctypes.c_ulong)(pointer)
        ole32.CoUninitialize()


@lru_cache(maxsize=1)
def ensure_notification_shortcut():
    root = os.environ.get('APPDATA')
    if not root:
        raise OSError('The Windows Start menu folder is unavailable.')
    path = Path(root) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'BountyTracker.lnk'
    arguments = '' if getattr(sys, 'frozen', False) else subprocess.list2cmdline(
        [str(Path(__file__).with_name('main.py'))])
    existing = path.exists()
    write_shortcut(path, Path(sys.executable), arguments)
    refresh = ctypes.windll.shell32.SHChangeNotify
    refresh.argtypes = (ctypes.c_long, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_void_p)
    refresh.restype = None
    refresh(0x2000 if existing else 2, 0x1005, str(path), None)


def toast_payload(title, message):
    return ('<toast duration="long"><visual><binding template="ToastGeneric">'
            f'<text>{escape(title)}</text><text>{escape(message)}</text>'
            '</binding></visual><audio silent="true"/></toast>')


def post_toast(title, message):
    if os.name != 'nt':
        raise OSError('Windows notifications are unavailable.')
    ensure_notification_shortcut()
    xml = base64.b64encode(toast_payload(title, message).encode('utf-8')).decode('ascii')
    script = f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {{
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
    [void][Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime]
    [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]
    $document = New-Object Windows.Data.Xml.Dom.XmlDocument
    $document.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{xml}')))
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}')
    if ($null -ne $notifier.Setting -and $notifier.Setting -ne 'Enabled') {{ throw "Notifications disabled by Windows: $($notifier.Setting)" }}
    $toast = [Windows.UI.Notifications.ToastNotification]::new($document)
    $notifier.Show($toast)
}} catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}}
"""
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    command = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    try:
        result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', command],
                                capture_output=True, text=True, errors='replace', timeout=12,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as error:
        raise OSError('Windows did not respond to the notification request.') from error
    if result.returncode:
        raise OSError(result.stderr.strip() or 'Windows rejected the notification.')


@lru_cache(maxsize=1)
def completion_wave():
    """Make an audible three-tone chime without relying on a system sound file."""
    rate = 22050
    samples = array('h')
    for frequency, length in ((660, .28), (880, .31), (1046.5, .50)):
        count = int(length * rate)
        for index in range(count):
            t = index / rate
            envelope = min(1, t / .022, (length - t) / .12)
            angle = 2 * math.pi * frequency * t
            voice = .85 * math.sin(angle) + .15 * math.sin(2 * angle)
            samples.append(round(32767 * .82 * max(0, envelope) * voice))
        samples.extend([0] * int(.075 * rate))
    output = io.BytesIO()
    with wave.open(output, 'wb') as sound:
        sound.setparams((1, 2, rate, len(samples), 'NONE', 'not compressed'))
        sound.writeframes(samples.tobytes())
    return output.getvalue()


def play_chime():
    import winsound

    winsound.PlaySound(completion_wave(), winsound.SND_MEMORY | winsound.SND_SYNC | winsound.SND_NODEFAULT)
