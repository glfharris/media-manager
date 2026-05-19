"""Replace dialog: substitute one image with another, either by overwriting
the file on disk or by rewriting references across notes.
"""

from __future__ import annotations

import os
import re
import shutil

from aqt import mw
from aqt.qt import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPixmap,
    QPushButton,
    QRadioButton,
    Qt,
    QVBoxLayout,
)
from aqt.utils import showWarning, tooltip

from . import media_index, thumbnails


THUMB_PX = 180


def _src_attr_pattern(filename: str) -> re.Pattern[str]:
    """Match `src="filename"` or `src='filename'` regardless of quote style."""
    escaped = re.escape(filename)
    return re.compile(
        rf"""(<img[^>]*\bsrc=)(["']){escaped}\2""",
        re.IGNORECASE,
    )


def rewrite_references(old: str, new: str) -> tuple[int, int]:
    """Rewrite every img src="old" to src="new" across notes.

    Returns (notes_updated, fields_updated).
    """
    pattern = _src_attr_pattern(old)
    nids = media_index.notes_referencing(old)
    notes_updated = 0
    fields_updated = 0
    for nid in nids:
        note = mw.col.get_note(nid)
        changed = False
        for i, field in enumerate(note.fields):
            new_field, n = pattern.subn(rf"\g<1>\g<2>{new}\g<2>", field)
            if n:
                note.fields[i] = new_field
                fields_updated += n
                changed = True
        if changed:
            mw.col.update_note(note)
            notes_updated += 1
    return notes_updated, fields_updated


def replace_on_disk(old_filename: str, new_source_path: str) -> None:
    """Overwrite the bytes of `old_filename` in the media dir with the contents
    of `new_source_path`. Filename is preserved so existing card refs keep
    working.
    """
    dest = media_index.media_path(old_filename)
    shutil.copyfile(new_source_path, dest)


class ReplaceDialog(QDialog):
    """Replace `old_filename` with another image.

    The new image can be either an existing media file or a file from disk.
    """

    MODE_DISK = "disk"
    MODE_REFS = "refs"

    def __init__(self, parent, old_filename: str):
        super().__init__(parent)
        self.setWindowTitle(f"Replace image: {old_filename}")
        self.old_filename = old_filename
        self.new_source_path: str | None = None  # absolute path
        self.new_media_name: str | None = None   # set if picked from media

        self._affected = len(media_index.notes_referencing(old_filename))
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(self._thumb_widget(
            media_index.media_path(self.old_filename),
            f"Current: {self.old_filename}",
        ))
        self.new_thumb_box = QVBoxLayout()
        self.new_thumb_label = QLabel("(no new image selected)")
        self.new_thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.new_thumb_label.setFixedSize(THUMB_PX, THUMB_PX)
        self.new_thumb_label.setStyleSheet("border: 1px dashed gray;")
        self.new_thumb_box.addWidget(self.new_thumb_label)
        self.new_thumb_caption = QLabel("New: —")
        self.new_thumb_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.new_thumb_box.addWidget(self.new_thumb_caption)
        header.addLayout(self.new_thumb_box)
        layout.addLayout(header)

        # Pick new image
        pick_row = QHBoxLayout()
        pick_disk = QPushButton("Pick from disk…")
        pick_disk.clicked.connect(self._pick_from_disk)
        pick_media = QPushButton("Pick from media library…")
        pick_media.clicked.connect(self._pick_from_media)
        pick_row.addWidget(pick_disk)
        pick_row.addWidget(pick_media)
        layout.addLayout(pick_row)

        layout.addWidget(QLabel(
            f"This image is referenced by <b>{self._affected}</b> note(s)."
        ))

        # Mode
        mode_box = QVBoxLayout()
        mode_box.addWidget(QLabel("Replace mode:"))
        self.mode_group = QButtonGroup(self)
        self.mode_disk = QRadioButton(
            "Replace on disk — overwrite the existing file. "
            "Every note that uses it updates automatically."
        )
        self.mode_refs = QRadioButton(
            "Rewrite references — change <img src> in every note from the "
            "old filename to the new one. Old file is left in place."
        )
        self.mode_disk.setChecked(True)
        self.mode_group.addButton(self.mode_disk)
        self.mode_group.addButton(self.mode_refs)
        mode_box.addWidget(self.mode_disk)
        mode_box.addWidget(self.mode_refs)
        layout.addLayout(mode_box)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        self._ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setEnabled(False)
        layout.addWidget(bb)

        self.resize(500, 520)

    def _thumb_widget(self, path: str, caption: str) -> QLabel:
        wrapper_box = QVBoxLayout()
        thumb = QLabel()
        thumb.setFixedSize(THUMB_PX, THUMB_PX)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet("border: 1px solid gray;")
        img = thumbnails.decode_thumbnail(path, THUMB_PX)
        if not img.isNull():
            thumb.setPixmap(QPixmap.fromImage(img))
        else:
            thumb.setText("(no preview)")
        cap = QLabel(caption)
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wrapper_box.addWidget(thumb)
        wrapper_box.addWidget(cap)
        # Wrap in a container widget so layout composition works in header HBox.
        from aqt.qt import QWidget
        container = QWidget()
        container.setLayout(wrapper_box)
        return container

    # ------- pickers -------

    def _pick_from_disk(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose replacement image",
            "",
            "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.svg *.avif)",
        )
        if not path:
            return
        self.new_source_path = path
        self.new_media_name = None
        self._update_new_thumb(path, f"New: {os.path.basename(path)} (from disk)")

    def _pick_from_media(self) -> None:
        from .browser import PickMediaDialog  # local import to avoid cycle
        dlg = PickMediaDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected:
            return
        name = dlg.selected
        self.new_media_name = name
        self.new_source_path = media_index.media_path(name)
        self._update_new_thumb(
            self.new_source_path, f"New: {name} (from media)"
        )

    def _update_new_thumb(self, path: str, caption: str) -> None:
        img = thumbnails.decode_thumbnail(path, THUMB_PX)
        if img.isNull():
            self.new_thumb_label.setText("(preview failed)")
        else:
            self.new_thumb_label.setPixmap(QPixmap.fromImage(img))
        self.new_thumb_caption.setText(caption)
        self._ok_btn.setEnabled(True)

    # ------- accept -------

    def _on_accept(self) -> None:
        if not self.new_source_path:
            return

        mode = self.MODE_DISK if self.mode_disk.isChecked() else self.MODE_REFS

        # Confirm
        verb = (
            "overwrite the bytes of"
            if mode == self.MODE_DISK
            else "rewrite references away from"
        )
        confirm = QMessageBox.question(
            self,
            "Confirm replace",
            f"This will {verb} <b>{self.old_filename}</b> across "
            f"<b>{self._affected}</b> note(s). Continue?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        if mode == self.MODE_DISK:
            try:
                replace_on_disk(self.old_filename, self.new_source_path)
            except OSError as e:
                showWarning(f"Could not replace file: {e}")
                return
            tooltip(f"Replaced {self.old_filename} on disk.")
        else:
            # For ref-rewrite the new file needs to live in the media dir.
            if self.new_media_name is None:
                # File came from disk — add to media collection first.
                new_name = mw.col.media.add_file(self.new_source_path)
            else:
                new_name = self.new_media_name
            n_notes, n_fields = rewrite_references(self.old_filename, new_name)
            tooltip(
                f"Rewrote {n_fields} reference(s) in {n_notes} note(s) "
                f"→ {new_name}."
            )

        self.accept()
