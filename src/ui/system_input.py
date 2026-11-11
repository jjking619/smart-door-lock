"""Use GNOME's system input channel instead of bypassing its screen keyboard."""

from __future__ import annotations

from collections.abc import MutableMapping
import os


def configure_system_input_method(
    environ: MutableMapping[str, str] | None = None,
) -> bool:
    env = os.environ if environ is None else environ
    desktops = {part.lower() for part in env.get("XDG_CURRENT_DESKTOP", "").split(":")}
    platform = env.get("QT_QPA_PLATFORM", "").strip().lower().split(":", 1)[0]
    wayland = platform in {"wayland", "wayland-egl"} or (
        not platform and env.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    )
    if (
        "gnome" not in desktops
        or not wayland
        or env.get("QT_IM_MODULES", "").strip()
        or env.get("QT_IM_MODULE", "").strip().lower() != "ibus"
    ):
        return False

    # GNOME still supplies IBus, through the compositor's text-input protocol.
    env["QT_IM_MODULE"] = "wayland"
    return True


# Qt 6.8's text-input-v3 backend enables a field only once, but Mutter shows its
# input panel when an already-focused client enables text input again.
_TEXT_INPUT_ENABLE = 1
_TEXT_INPUT_COMMIT = 7
_MANAGER_GET_TEXT_INPUT = 1
_REGISTRY_BIND = 0
_DISPLAY_GET_REGISTRY = 1
_MANAGER_NAME = b"zwp_text_input_manager_v3"


def _uses_wayland_text_input(environ: MutableMapping[str, str]) -> bool:
    modules = environ.get("QT_IM_MODULES", "").strip() or environ.get("QT_IM_MODULE", "").strip()
    return not modules or modules.split(";", 1)[0].strip().lower() == "wayland"


class _WaylandPanelRequester:
    def __init__(self, display: int, seat: int) -> None:
        import ctypes

        self._ct = ctypes
        lib = ctypes.CDLL("libwayland-client.so.0")
        self._lib = lib
        vp, u32 = ctypes.c_void_p, ctypes.c_uint32

        class Message(ctypes.Structure):
            _fields_ = [("name", ctypes.c_char_p), ("signature", ctypes.c_char_p),
                        ("types", ctypes.POINTER(ctypes.c_void_p))]

        class Interface(ctypes.Structure):
            _fields_ = [("name", ctypes.c_char_p), ("version", ctypes.c_int),
                        ("method_count", ctypes.c_int), ("methods", ctypes.POINTER(Message)),
                        ("event_count", ctypes.c_int), ("events", ctypes.POINTER(Message))]

        def messages(specs, types):
            array = (Message * len(specs))()
            for item, (name, signature) in zip(array, specs):
                item.name, item.signature, item.types = name, signature, types
            return array

        self._input = Interface()
        self._manager = Interface()
        null_types = (vp * 2)()
        manager_types = (vp * 2)(ctypes.addressof(self._input),
                                 ctypes.addressof(Interface.in_dll(lib, "wl_seat_interface")))
        self._keep = [
            null_types, manager_types,
            messages([(b"destroy", b""), (b"enable", b""), (b"disable", b""),
                      (b"set_surrounding_text", b"sii"), (b"set_text_change_cause", b"u"),
                      (b"set_content_type", b"uu"), (b"set_cursor_rectangle", b"iiii"),
                      (b"commit", b"")], null_types),
            messages([(b"enter", b"o"), (b"leave", b"o"), (b"preedit_string", b"?sii"),
                      (b"commit_string", b"?s"), (b"delete_surrounding_text", b"uu"),
                      (b"done", b"u")], null_types),
            messages([(b"destroy", b""), (b"get_text_input", b"no")], manager_types),
        ]
        self._input.name, self._input.version = b"zwp_text_input_v3", 1
        self._input.method_count, self._input.methods = 8, self._keep[2]
        self._input.event_count, self._input.events = 6, self._keep[3]
        self._manager.name, self._manager.version = _MANAGER_NAME, 1
        self._manager.method_count, self._manager.methods = 2, self._keep[4]

        lib.wl_proxy_marshal_array_flags.restype = vp
        lib.wl_proxy_marshal_array_flags.argtypes = [vp, u32, vp, u32, u32, vp]
        lib.wl_proxy_get_version.restype = u32
        lib.wl_proxy_get_version.argtypes = [vp]
        for name in ("wl_proxy_create_wrapper", "wl_display_create_queue"):
            getattr(lib, name).restype = vp
            getattr(lib, name).argtypes = [vp]
        lib.wl_proxy_set_queue.argtypes = [vp, vp]
        lib.wl_proxy_wrapper_destroy.argtypes = [vp]
        lib.wl_proxy_destroy.argtypes = [vp]
        lib.wl_event_queue_destroy.argtypes = [vp]
        lib.wl_proxy_add_listener.argtypes = [vp, vp, vp]
        lib.wl_display_roundtrip_queue.argtypes = [vp, vp]
        lib.wl_display_flush.argtypes = [vp]

        self._display = display
        queue = lib.wl_display_create_queue(display)
        wrapper = lib.wl_proxy_create_wrapper(display)
        registry = None
        found: list[int] = []
        try:
            lib.wl_proxy_set_queue(wrapper, queue)
            registry = self._marshal(wrapper, _DISPLAY_GET_REGISTRY,
                                     Interface.in_dll(lib, "wl_registry_interface"), [("o", None)])
            Global = ctypes.CFUNCTYPE(None, vp, vp, u32, ctypes.c_char_p, u32)
            Remove = ctypes.CFUNCTYPE(None, vp, vp, u32)

            def on_global(_data, _registry, name, interface, _version):
                if interface == _MANAGER_NAME:
                    found.append(name)

            callbacks = (Global(on_global), Remove(lambda *_: None))
            listener = (vp * 2)(*(ctypes.cast(cb, vp).value for cb in callbacks))
            lib.wl_proxy_add_listener(registry, listener, None)
            if lib.wl_display_roundtrip_queue(display, queue) < 0 or not found:
                raise OSError("text-input-v3 is unavailable")
            manager = self._marshal(registry, _REGISTRY_BIND, self._manager,
                                    [("u", found[0]), ("s", _MANAGER_NAME), ("u", 1), ("o", None)])
            # Unhandled events on the default queue are discarded by libwayland.
            lib.wl_proxy_set_queue(manager, None)
            self._text_input = self._marshal(manager, _MANAGER_GET_TEXT_INPUT,
                                             self._input, [("o", None), ("o", seat)])
            lib.wl_display_flush(display)
        finally:
            if registry:
                lib.wl_proxy_destroy(registry)
            lib.wl_proxy_wrapper_destroy(wrapper)
            lib.wl_event_queue_destroy(queue)

    def _marshal(self, proxy: int, opcode: int, interface, values: list[tuple[str, object]]) -> int:
        ct = self._ct

        class Argument(ct.Union):
            _fields_ = [("i", ct.c_int32), ("u", ct.c_uint32), ("s", ct.c_char_p),
                        ("o", ct.c_void_p)]

        args = (Argument * max(1, len(values)))()
        for arg, (kind, value) in zip(args, values):
            setattr(arg, kind, value)
        version = self._lib.wl_proxy_get_version(proxy)
        return self._lib.wl_proxy_marshal_array_flags(
            proxy, opcode, ct.addressof(interface) if interface is not None else None,
            version or 1, 0, args,
        ) or 0

    def show(self) -> None:
        self._marshal(self._text_input, _TEXT_INPUT_ENABLE, None, [])
        self._marshal(self._text_input, _TEXT_INPUT_COMMIT, None, [])
        self._lib.wl_display_flush(self._display)


def _qt_wayland_handles(app) -> tuple[int, int]:
    import ctypes
    import shiboken6

    from PySide6 import QtGui

    library = os.path.join(os.path.dirname(QtGui.__file__), "Qt", "lib", "libQt6Gui.so.6")
    resolve = getattr(ctypes.CDLL(library), "_ZNK15QGuiApplication16resolveInterfaceEPKci")
    resolve.restype = ctypes.c_void_p
    resolve.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    native = resolve(shiboken6.getCppPointer(app)[0], b"QWaylandApplication", 1)
    if not native:
        raise OSError("Qt Wayland native interface is unavailable")
    table = ctypes.cast(ctypes.c_void_p.from_address(native).value,
                        ctypes.POINTER(ctypes.c_void_p))
    getter = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)
    # QNativeInterface::QWaylandApplication: two destructors, display(), compositor(), seat().
    display = getter(table[2])(native)
    seat = getter(table[4])(native)
    if not display or not seat:
        raise OSError("Qt Wayland display or seat is unavailable")
    return display, seat


_requester: _WaylandPanelRequester | None = None
_requester_failed = False


def request_system_input_panel() -> None:
    """Ask the platform input method, and GNOME Wayland if needed, to show its panel."""
    global _requester, _requester_failed
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.inputMethod().show()
    app = QGuiApplication.instance()
    if (
        _requester_failed
        or app is None
        or not QGuiApplication.platformName().startswith("wayland")
        or not _uses_wayland_text_input(os.environ)
    ):
        return
    try:
        if _requester is None:
            _requester = _WaylandPanelRequester(*_qt_wayland_handles(app))
        _requester.show()
    except (AttributeError, ImportError, OSError, ValueError) as exc:
        _requester_failed = True
        import sys

        print(f"Input method: cannot request GNOME's screen keyboard: {exc}", file=sys.stderr)
