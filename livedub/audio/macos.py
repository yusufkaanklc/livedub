"""macOS helpers: microphone (TCC) permission status via the Objective-C runtime, no pyobjc needed.

macOS treats every audio input, virtual ones like BlackHole included, as a microphone. Without
permission Core Audio delivers silence instead of an error, so we check the status explicitly.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

# AVAuthorizationStatus
_STATUS = {0: "not_determined", 1: "restricted", 2: "denied", 3: "authorized"}


def microphone_permission() -> str | None:
    """'authorized', 'denied', 'restricted', 'not_determined', or None when it cannot be read."""
    if sys.platform != "darwin":
        return None
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        avf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        media_type = ctypes.c_void_p.in_dll(avf, "AVMediaTypeAudio").value  # NSString constant
        msg_send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        status_for = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(msg_send)
        status = status_for(objc.objc_getClass(b"AVCaptureDevice"),
                            objc.sel_registerName(b"authorizationStatusForMediaType:"), media_type)
        return _STATUS.get(status)
    except Exception:
        return None
