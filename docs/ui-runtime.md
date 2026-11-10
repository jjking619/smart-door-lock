# UI runtime

The GUI can run in a separate Python environment while vision and audio keep
using the original service environment. On this device, the managed Python
3.11.17 / PySide6 6.12.0 environment reproduces singleton reference-count
corruption (`bool_dealloc`) when Qt signals are exercised. Downgrading PySide6
alone did not reliably fix interpreter shutdown (`none_dealloc`). System
Python 3.13.5 with PySide6 6.8.3 passed repeated start/stop and shutdown tests.

From the project root:

```sh
uv venv --python /usr/bin/python3 .venv-ui
uv pip install --python .venv-ui/bin/python -r requirements-ui.txt
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

The records page loads the selected tab on demand and paints only visible
list rows so the interface remains responsive. Recording thumbnails are
requested only for visible rows and cached while the UI is open. Refresh
reloads the currently selected tab; changing the server clears this cache.
Entering the records page selects and plays the first item, when it has a
recording. A placeholder is shown until a video frame is ready. A newly
created clip may still be writing, so format errors are retried briefly and
the player reports an error if it remains unavailable.

Regression checks (offscreen; no real audio session is started):

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_application.py -v
PYTHONPATH=src .venv-ui/bin/python -m unittest tests.test_ui_conversation tests.test_ui_runtime -v
```