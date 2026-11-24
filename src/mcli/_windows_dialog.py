"""Isolate the modal Win32 approval dialog so the daemon can time it out."""

import ctypes
import sys
from ctypes import wintypes


UOI_FLAGS = 1
WSF_VISIBLE = 0x0001
WS_EX_TOPMOST = 0x00000008
WS_POPUP = 0x80000000
MB_YESNO = 0x0004
MB_ICONWARNING = 0x0030
MB_DEFBUTTON2 = 0x0100
MB_SETFOREGROUND = 0x10000
IDYES = 6
IDNO = 7


class UserObjectFlags(ctypes.Structure):
    _fields_ = [
        ("inherit", wintypes.BOOL),
        ("reserved", wintypes.BOOL),
        ("flags", wintypes.DWORD),
    ]


def main() -> int:
    message = sys.stdin.buffer.read().decode("utf-8")
    user32 = ctypes.WinDLL("user32")
    get_station = user32.GetProcessWindowStation
    get_station.argtypes = []
    get_station.restype = wintypes.HANDLE
    get_info = user32.GetUserObjectInformationW
    get_info.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    get_info.restype = wintypes.BOOL

    flags = UserObjectFlags()
    required = wintypes.DWORD()
    station = get_station()
    if not station or not get_info(
        station, UOI_FLAGS, ctypes.byref(flags), ctypes.sizeof(flags), ctypes.byref(required)
    ):
        return 1
    if not flags.flags & WSF_VISIBLE:
        return 1

    # A dialog owned by a topmost window remains topmost when focus changes.
    create_window = user32.CreateWindowExW
    create_window.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    create_window.restype = wintypes.HWND
    owner = create_window(
        WS_EX_TOPMOST, "Static", None, WS_POPUP, 0, 0, 0, 0, None, None, None, None,
    )
    if not owner:
        return 1

    destroy_window = user32.DestroyWindow
    destroy_window.argtypes = [wintypes.HWND]
    destroy_window.restype = wintypes.BOOL
    message_box = user32.MessageBoxW
    message_box.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT,
    ]
    message_box.restype = ctypes.c_int
    try:
        result = message_box(
            owner, message, "mcli permission",
            MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SETFOREGROUND,
        )
    finally:
        destroy_window(owner)
    return result if result in (IDYES, IDNO) else 1


if __name__ == "__main__":
    sys.exit(main())
