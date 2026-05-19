"""Rename an image file in the media collection and rewrite every note
reference to use the new name.
"""

from __future__ import annotations

import os

from aqt import mw
from aqt.qt import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPixmap,
    Qt,
    QVBoxLayout,
)
from aqt.utils import showWarning, tooltip

from . import media_index, thumbnails
from .replace import rewrite_references


INVALID_CHARS = set('/\\:*?"<>|')
THUMB_PX = 180


def _is_valid_name(name: str) -> tuple[bool, str]:
    name = name.strip()
    if not name:
        return False, "Name is empty."
    if any(c in INVALID_CHARS for c in name):
        return False, "Name contains an invalid character (/ \\ : * ? \" < > |)."
    if "." not in name:
        return False, "Name needs a file extension."
    if name.startswith("."):
        return False, "Name cannot start with a dot."
    return True, ""


def rename_image(old: str, new: str) -> tuple[int, int]:
    """Rename `old` to `new` on disk and rewrite img src refs.

    Returns (notes_updated, fields_updated).
    Raises OSError on filesystem failure, ValueError on collision.
    """
    media_dir = mw.col.media.dir()
    src = os.path.join(media_dir, old)
    dst = os.path.join(media_dir, new)
    if os.path.exists(dst):
        raise ValueError(f"A file named {new!r} already exists in the media folder.")
    os.rename(src, dst)
    try:
        return rewrite_references(old, new)
    except Exception:
        # Best-effort rollback: put the file back so refs still resolve.
        try:
            os.rename(dst, src)
        except OSError:
            pass
        raise


class RenameDialog(QDialog):
    def __init__(self, parent, old_filename: str):
        super().__init__(parent)
        self.setWindowTitle(f"Rename image: {old_filename}")
        self.old_filename = old_filename
        self._affected = len(media_index.notes_referencing(old_filename))
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        thumb = QLabel()
        thumb.setFixedSize(THUMB_PX, THUMB_PX)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet("border: 1px solid gray;")
        img = thumbnails.decode_thumbnail(
            media_index.media_path(self.old_filename), THUMB_PX
        )
        if not img.isNull():
            thumb.setPixmap(QPixmap.fromImage(img))
        else:
            thumb.setText("(no preview)")
        layout.addWidget(thumb, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addWidget(QLabel(f"Current name: <b>{self.old_filename}</b>"))
        layout.addWidget(QLabel("New name:"))
        self.name_edit = QLineEdit(self.old_filename)
        self.name_edit.textChanged.connect(self._validate)
        layout.addWidget(self.name_edit)

        self.validation_label = QLabel("")
        self.validation_label.setStyleSheet("color: #b00;")
        layout.addWidget(self.validation_label)

        layout.addWidget(QLabel(
            f"This image is referenced by <b>{self._affected}</b> note(s). "
            "References will be updated to the new name."
        ))

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        self._ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
        layout.addWidget(bb)

        self.resize(420, 380)
        self._validate(self.name_edit.text())

    def _validate(self, text: str) -> None:
        new = text.strip()
        if new == self.old_filename:
            self.validation_label.setText("(unchanged)")
            self._ok_btn.setEnabled(False)
            return
        ok, msg = _is_valid_name(new)
        if not ok:
            self.validation_label.setText(msg)
            self._ok_btn.setEnabled(False)
            return
        dst = os.path.join(mw.col.media.dir(), new)
        if os.path.exists(dst):
            self.validation_label.setText(
                f"A file named {new!r} already exists."
            )
            self._ok_btn.setEnabled(False)
            return
        self.validation_label.setText("")
        self._ok_btn.setEnabled(True)

    def _on_accept(self) -> None:
        new = self.name_edit.text().strip()
        confirm = QMessageBox.question(
            self,
            "Confirm rename",
            f"Rename <b>{self.old_filename}</b> → <b>{new}</b> and update "
            f"<b>{self._affected}</b> note(s)?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            n_notes, n_fields = rename_image(self.old_filename, new)
        except (OSError, ValueError) as e:
            showWarning(f"Rename failed: {e}")
            return
        tooltip(
            f"Renamed → {new}; updated {n_fields} reference(s) "
            f"in {n_notes} note(s)."
        )
        self.new_filename = new
        self.accept()
