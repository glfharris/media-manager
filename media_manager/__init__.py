from aqt import gui_hooks, mw
from aqt.qt import QAction

from . import browser


def _on_editor_button(editor):
    browser.open_browser(editor=editor)


def _setup_editor_buttons(buttons, editor):
    b = editor.addButton(
        None,
        "MediaMgr",
        _on_editor_button,
        tip="Open Media Manager (browse, insert, replace images)",
    )
    buttons.append(b)
    return buttons


def _setup_tools_menu():
    action = QAction("Media Manager…", mw)
    action.triggered.connect(lambda: browser.open_browser(editor=None))
    mw.form.menuTools.addAction(action)


gui_hooks.editor_did_init_buttons.append(_setup_editor_buttons)
gui_hooks.main_window_did_init.append(_setup_tools_menu)
