# UI runtime

The GUI can run in a separate Python environment while vision and audio keep
using the original service environment. On this device, the managed Python
3.11.17 / PySide6 6.12.0 environment reproduces singleton reference-count
corruption (`bool_dealloc`) when Qt signals are exercised. Downgrading PySide6
alone did not reliably fix interpreter shutdown (`none_dealloc`). System
Python 3.13.5 with PySide6 6.8.3 passed repeated start/stop and shutdown tests.

### Compatibility checks

The following combinations were checked on the ARM64 board:

| Python | PySide6 / shiboken6 | Result |
| --- | --- | --- |
| uv-managed 3.11.17 | 6.12.0 | A standalone `Signal(bool)` loop decreases the reference count of `True`; interpreter shutdown can abort with `bool_dealloc`. |
| uv-managed 3.11.17 | 6.8.3 | The signal check passes, but a standalone `QImage` / `QPainter` drawing loop aborts with `none_dealloc`; the UI runtime regression also aborts. |
| System 3.13.5 | 6.8.3 | Both standalone checks and the UI runtime regression pass. |

The drawing reproduction uses only PySide6, without project code, a display,
camera, or NPU. This points to a native binding / interpreter compatibility
problem in the tested environment rather than application business logic.
It does not establish that every Python 3.11 build is affected or identify the
exact defective component. Python 3.13 uses immortal singleton objects, so a
passing reference-count check there alone cannot prove the binding is correct;
the successful GUI regression is also required.

The project's requirements pin PySide6 to 6.8.3. An existing backend environment
may still contain 6.12.0 if it has not been synchronized with those requirements.
Correcting that mismatch alone did not resolve the 3.11 drawing crash in the
isolated test. Keep the separate UI interpreter unless a replacement combination
passes both minimal checks and the GUI runtime regression.

An isolated Python 3.11.17 trial with PySide6 6.7.3 passed the signal and
drawing checks and three UI runtime regression runs. However, the UI suite
still had one error: its `QVideoFrame(QImage)` constructor is unavailable in
6.7.3. On this Debian 13 image the default FFmpeg multimedia plugin also
failed to load because it needs older FFmpeg shared libraries. Explicitly
setting `QT_MEDIA_BACKEND=gstreamer` allowed a short local H.264 playback
check, but does not establish full UI, multimedia, or hardware compatibility.
Versions 6.5.3 and 6.6.3.1 were checked only with the minimal signal and
drawing probes. These are candidates for further investigation, not verified
replacements for the current two-environment setup.

From the project root:

```sh
uv venv --python /usr/bin/python3 .venv-ui
uv pip install --python .venv-ui/bin/python -r requirements.txt
.venv/bin/python main.py --ui-only --base http://127.0.0.1:8083
```

The launcher automatically prefers `.venv-ui/bin/python` for the GUI only.
Backend and audio processes still use the launcher's Python interpreter.
`DOORLOCK_UI_PYTHON` overrides the GUI interpreter when set. Without either
the override or the isolated environment, the launcher uses its own interpreter.
On affected devices, ensure the isolated environment exists before launching.

Direct GUI launch:

```sh
PYTHONPATH=src .venv-ui/bin/python -X faulthandler -m ui.app --base http://127.0.0.1:8083
```

## Touchscreen text input

The UI uses the system input method and does not create an application keyboard.
On GNOME Wayland, an inherited `QT_IM_MODULE=ibus` is changed to `wayland` for
the UI process before Qt starts: direct IBus connections bypass GNOME's
screen-keyboard focus tracking. The compositor still supplies the selected
system IBus engine; this is not Qt Virtual Keyboard. Other desktops, explicitly
selected X11/offscreen platforms, Fcitx, and `QT_IM_MODULES` overrides are left
unchanged. No display platform is forced. The former `DOORLOCK_TOUCH_KEYBOARD`
setting is no longer used. Tap the name, finger note, URL, or token field to request the
system input panel; physical keyboards continue to work, and the settings
pages support touch scrolling. The token field tells the input method that
the text is sensitive and should not be predicted.

The desktop decides whether a system on-screen keyboard is available and
whether to show it. For Chinese input on GNOME, add and select **Chinese
(Intelligent Pinyin)** in the system keyboard/input-source settings. If the
screen keyboard does not appear automatically for the touchscreen, enable
it in the desktop's accessibility settings. GNOME's automatic mode requires
both tablet/touch mode and the last-used device to be a touchscreen; clicking
with a mouse alone does not satisfy those conditions. Enable **Accessibility
> Typing > Screen Keyboard** to make the system keyboard available outside
automatic touch mode. The application does not change these desktop settings.

There is an additional limitation with the pinned Qt 6.8.3 runtime and GNOME
48: Qt's Wayland text-input-v3 backend does not implement `showInputPanel()`.
GNOME accepts the field's native input focus, but that alone does not send its
explicit panel-show request. Consequently, `QInputMethod.show()` and the
native-channel correction do **not** guarantee that tapping a field opens the
system keyboard. Swipe up from the bottom screen edge to open GNOME's keyboard
manually, then type into the focused field. A click-to-open bridge would require
an additional GNOME integration; none is installed by this application.

Qt's direct IBus plugin does not itself open GNOME's on-screen keyboard.
Normal launches in a GNOME Wayland session now use the native channel
automatically. To select it explicitly when launching from another environment:

```sh
# WAYLAND_DISPLAY must be set by the desktop session.
env -u QT_IM_MODULES QT_QPA_PLATFORM=wayland QT_IM_MODULE=wayland \
  .venv/bin/python main.py --ui-only --base http://127.0.0.1:8083
```

This only affects that UI process; the compositor still uses the selected
system input source. Other desktops and input methods should retain their
own configuration. Set `QT_IM_MODULES=ibus` to explicitly retain a direct IBus
connection for troubleshooting (GNOME screen-keyboard focus tracking will be
bypassed). Restart the UI after changing input backends; an already-running
Qt application cannot switch input contexts. A request to show the input panel
is not a guarantee that the system keyboard is installed, enabled, or supported
by the active platform input context.

The records page loads the selected tab on demand and paints only visible
list rows so the interface remains responsive. Recording thumbnails are
requested only for visible rows and cached while the UI is open. Refresh
reloads the currently selected tab; changing the server clears this cache.
Entering the records page selects and plays the first item, when it has a
recording. A placeholder is shown until a video frame is ready. A newly
created clip may still be writing, so format errors are retried briefly and
the player reports an error if it remains unavailable.

Events can be filtered by All, Security, Access, Parcel, or System. Security
includes person detection, loitering, and tamper alarms; fingerprint management
belongs to System, while authentication and unlock requests belong to Access.
Counts describe the most recently loaded batch (up to 120 events), not the
entire event history. Events are ordered newest first and grouped by local
calendar date, with Today / Yesterday labels and collapsible date headers.
Changing categories selects the first visible event and clears unrelated
details when the category is empty.

Event details show their summary and available fields, but do not display the
internal `zone` label: detection defaults to the full frame and the label does
not describe the active ROI. The event data and optional backend ROI
configuration remain unchanged. For events with video or a snapshot, details
start collapsed and can be opened with **Show details**, leaving more space for
the media. Playback controls sit above the video; snapshot-only events show an
aspect-preserving image without playback controls. Events without media show a
text card instead of an empty video area. Snapshot errors are displayed
explicitly, and responses from an earlier selection or server connection cannot
replace the current view. Recording filters, date groups, and playback remain
independent of event filters.

Regression checks (offscreen; no real audio session is started):

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_application.py -v
PYTHONPATH=src .venv-ui/bin/python -m unittest tests.test_ui_conversation tests.test_ui_runtime -v
PYTHONPATH=src .venv-ui/bin/python -m unittest tests.test_ui_records tests.test_ui_i18n -v
PYTHONPATH=src .venv-ui/bin/python -m unittest tests.test_ui_fingerprint tests.test_ui_system_input -v
```