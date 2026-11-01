"""Windows taskbar progress integration.

Uses the native ITaskbarList3 COM interface so that captioning progress is
shown on the taskbar icon, like browsers do when downloading things. On
non-Windows platforms, or if the COM interface cannot be used, every method
is a no-op instead of raising.
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
        # `CLSIDFromString` requires a UTF-16 string; passing a `str` with
        # `ctypes` does that automatically for wide-character APIs.
        result = ctypes.oledll.ole32.CLSIDFromString(guid_string,
                                                     byref(guid))
        if result != 0:
            raise OSError(f'CLSIDFromString failed: {result:#x}')
        return guid

    _CLSID_TASKBAR_LIST = '{56FDF344-FD6D-11d0-958A-006097C9A090}'
    _IID_IUNKNOWN = '{00000000-0000-0000-C000-000000000046}'
    _IID_ITASKBAR_LIST3 = '{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}'
    _TBP_NOPROGRESS = 0x0
    _TBP_ERROR = 0x4

    _HRESULT = ctypes.HRESULT

    # COM method signatures take the object pointer (`this`) as the first
    # argument, followed by the parameters declared in the IDL. All methods
    # return an HRESULT that must be checked manually.
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

    def _check_hresult(hresult, operation: str) -> None:
        """Raise an OSError if the COM call returned a failure HRESULT."""
        if hresult is not None and hresult != 0:
            raise OSError(f'{operation} failed: {hresult & 0xffffffff:#x}')


class TaskbarProgress:
    """Shows and updates a progress indicator on the Windows taskbar icon.

    Any widget (e.g. a dock widget) can be passed; the top-level window is
    resolved from it, because only top-level windows have a taskbar button.

    All failures (including on non-Windows platforms) degrade to a no-op so
    that captioning is never blocked by the taskbar integration.
    """

    def __init__(self):
        self.taskbar_list = None
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
                _check_hresult(
                    unknown_methods.query_interface(
                        unknown_pointer, byref(_guid_from_string(
                            _IID_ITASKBAR_LIST3)),
                        byref(taskbar_list_pointer)),
                    'QueryInterface')
                if not taskbar_list_pointer:
                    raise OSError('ITaskbarList3 was not returned.')
            finally:
                _TaskbarList3Methods.from_address(
                    ctypes.cast(unknown_pointer,
                                POINTER(c_void_p)).contents.value).release(
                    unknown_pointer)
            self.methods = _TaskbarList3Methods.from_address(
                ctypes.cast(taskbar_list_pointer,
                            POINTER(c_void_p)).contents.value)
            _check_hresult(self.methods.hr_init(taskbar_list_pointer),
                           'HrInit')
            self.taskbar_list = taskbar_list_pointer
        except OSError:
            self.taskbar_list = None
            self.methods = None
        except Exception:
            # Never let taskbar problems break captioning.
            self.taskbar_list = None
            self.methods = None

    @staticmethod
    def _get_hwnd(widget) -> int:
        """Return the native window handle of the top-level window that
        contains the widget. Calling `winId()` forces the creation of the
        native window if it does not exist yet."""
        if widget is None:
            return 0
        top_level_widget = widget.window()
        if top_level_widget is None:
            return 0
        return int(top_level_widget.winId())

    def set_progress(self, widget, value: int, maximum: int):
        """Show the progress on the taskbar icon of the top-level window of
        the widget. If `maximum` is not positive, the progress indicator is
        hidden."""
        if self.methods is None:
            return
        try:
            window_id = self._get_hwnd(widget)
            if not window_id:
                return
            if maximum <= 0:
                self.methods.set_progress_state(
                    self.taskbar_list, window_id, _TBP_NOPROGRESS)
            else:
                self.methods.set_progress_value(
                    self.taskbar_list, window_id, max(0, value), maximum)
        except Exception:
            pass

    def show_error(self, widget):
        """Turn the taskbar progress indicator red to indicate an error."""
        if self.methods is None:
            return
        try:
            window_id = self._get_hwnd(widget)
            if not window_id:
                return
            self.methods.set_progress_state(
                self.taskbar_list, window_id, _TBP_ERROR)
        except Exception:
            pass
