"""Windows taskbar progress integration.

Uses the native ITaskbarList3 COM interface so that captioning progress is
shown on the taskbar icon, like browsers do when downloading things. On
non-Windows platforms every method is a no-op. On Windows, if the COM
interface cannot be used, the class degrades to a no-op instead of raising.
"""
import sys

if sys.platform == 'win32':
    import ctypes
    from ctypes import (POINTER, Structure, WINFUNCTYPE, byref, c_ubyte,
                       c_uint, c_ulong, c_ulonglong, c_ushort, c_void_p)
    from ctypes.wintypes import HWND

    class _GUID(Structure):
        _fields_ = [('data1', c_ulong),
                    ('data2', c_ushort),
                    ('data3', c_ushort),
                    ('data4', c_ubyte * 8)]

    def _guid_from_string(guid_string: str) -> '_GUID':
        guid = _GUID()
        ctypes.oledll.ole32.CLSIDFromString(guid_string, byref(guid))
        return guid

    _CLSID_TASKBAR_LIST = '{56FDF344-FD6D-11d0-958A-006097C9A090}'
    _IID_IUNKNOWN = '{00000000-0000-0000-C000-000000000046}'
    _IID_ITASKBAR_LIST3 = '{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}'
    _TBP_NOPROGRESS = 0x0
    _TBP_ERROR = 0x4

    # COM method signatures take the object pointer (`this`) as the first
    # argument, followed by the parameters declared in the IDL.
    _HRESULT = ctypes.HRESULT

    class _TaskbarList3Methods(Structure):
        _fields_ = [
            ('query_interface',
             WINFUNCTYPE(_HRESULT, c_void_p, POINTER(_GUID),
                         POINTER(c_void_p))),
            ('add_ref', WINFUNCTYPE(_HRESULT, c_void_p)),
            ('release', WINFUNCTYPE(_HRESULT, c_void_p)),
            ('hr_init', WINFUNCTYPE(_HRESULT, c_void_p)),
            ('add_tab', WINFUNCTYPE(_HRESULT, c_void_p, HWND)),
            ('delete_tab', WINFUNCTYPE(_HRESULT, c_void_p, HWND)),
            ('activate_tab', WINFUNCTYPE(_HRESULT, c_void_p, HWND)),
            ('set_active_alt', WINFUNCTYPE(_HRESULT, c_void_p, HWND)),
            ('mark_fullscreen_window',
             WINFUNCTYPE(_HRESULT, c_void_p, HWND, c_uint)),
            ('set_progress_value',
             WINFUNCTYPE(_HRESULT, c_void_p, HWND, c_ulonglong,
                         c_ulonglong)),
            ('set_progress_state',
             WINFUNCTYPE(_HRESULT, c_void_p, HWND, c_uint)),
            ('register_tab', WINFUNCTYPE(_HRESULT, c_void_p, HWND, HWND)),
            ('unregister_tab', WINFUNCTYPE(_HRESULT, c_void_p, HWND))
        ]


class TaskbarProgress:
    """Shows and updates a progress indicator on the Windows taskbar icon."""

    def __init__(self):
        self.taskbar_list = c_void_p()
        self.methods = None
        if sys.platform != 'win32':
            return
        try:
            # `CoInitializeEx` fails if the thread already uses a different
            # threading model, but the taskbar interface still works then.
            try:
                ctypes.oledll.ole32.CoInitializeEx(None, 0x2)
            except OSError:
                pass
            unknown_pointer = c_void_p()
            ctypes.oledll.ole32.CoCreateInstance(
                byref(_guid_from_string(_CLSID_TASKBAR_LIST)), None, 1,
                byref(_guid_from_string(_IID_IUNKNOWN)),
                byref(unknown_pointer))
            try:
                taskbar_list_pointer = c_void_p()
                # The vtable pointer is the first field of a COM object.
                unknown_methods = _TaskbarList3Methods.from_address(
                    ctypes.cast(unknown_pointer,
                                POINTER(c_void_p)).contents.value)
                unknown_methods.query_interface(
                    unknown_pointer, byref(_guid_from_string(
                        _IID_ITASKBAR_LIST3)), byref(taskbar_list_pointer))
            finally:
                _TaskbarList3Methods.from_address(
                    ctypes.cast(unknown_pointer,
                                POINTER(c_void_p)).contents.value).release(
                    unknown_pointer)
            self.methods = _TaskbarList3Methods.from_address(
                ctypes.cast(taskbar_list_pointer,
                            POINTER(c_void_p)).contents.value)
            self.methods.hr_init(taskbar_list_pointer)
            self.taskbar_list = taskbar_list_pointer
        except OSError:
            self.taskbar_list = c_void_p()
            self.methods = None

    def set_progress(self, window, value: int, maximum: int):
        """Show the progress on the taskbar icon of the window. If `maximum`
        is not positive, the progress indicator is hidden."""
        if self.methods is None:
            return
        window_id = int(window.windowHandle().winId()) if window else 0
        if not window_id:
            return
        try:
            if maximum <= 0:
                self.methods.set_progress_state(
                    self.taskbar_list, window_id, _TBP_NOPROGRESS)
            else:
                self.methods.set_progress_value(
                    self.taskbar_list, window_id, max(0, value), maximum)
        except OSError:
            pass

    def show_error(self, window):
        if self.methods is None:
            return
        window_id = int(window.windowHandle().winId()) if window else 0
        if not window_id:
            return
        try:
            self.methods.set_progress_state(
                self.taskbar_list, window_id, _TBP_ERROR)
        except OSError:
            pass
