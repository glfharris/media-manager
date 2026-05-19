"""Right-side info panel for the selected media file.

Shows a preview, basic file stats, ref-count, and tags-from-referencing-notes
(clickable to filter the grid).

Notes-referencing and tag aggregation can be slow on huge collections, so
updates are debounced 150 ms — rapid arrow-key navigation only triggers a
fetch for where you land.
"""

from __future__ import annotations

import html
import os
from datetime import datetime
from typing import Optional

from aqt import mw
from aqt.qt import (
    QFrame,
    QHBoxLayout,
    QImageReader,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPixmap,
    QPushButton,
    Qt,
    QTimer,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
)
from aqt.utils import showWarning, tooltip

from . import media_index, thumbnails
from .preview import open_preview


PREVIEW_PX = 260
DEBOUNCE_MS = 150


def _humanize_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    value = float(n)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TB"


class InfoPanel(QWidget):
    # Tag clicked → caller should set the search box and re-filter.
    search_requested = pyqtSignal(str)
    # View-references button → caller opens Anki's note browser.
    view_refs_requested = pyqtSignal(str)
    # File deleted → caller should refresh the grid.
    file_deleted = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(300)
        self._current: Optional[str] = None
        self._reasons: list[str] = []

        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self._do_update)

        self._build_ui()
        self._clear()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.preview = QLabel()
        self.preview.setFixedSize(PREVIEW_PX, PREVIEW_PX)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("border: 1px solid gray;")
        self.preview.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview.setToolTip("Click to open full preview")
        # QLabel doesn't emit clicked, so override mousePressEvent.
        self.preview.mousePressEvent = self._on_preview_click  # type: ignore[assignment]
        layout.addWidget(self.preview)

        self.name_lbl = QLabel("")
        self.name_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.name_lbl.setWordWrap(True)
        self.name_lbl.setStyleSheet("font-weight: bold;")
        self.name_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.name_lbl)

        self.stats_lbl = QLabel("")
        self.stats_lbl.setStyleSheet("color: gray;")
        layout.addWidget(self.stats_lbl)

        self.why_header = QLabel("Why suggested:")
        layout.addWidget(self.why_header)
        self.why_list = QListWidget()
        self.why_list.setMaximumHeight(120)
        layout.addWidget(self.why_list)

        refs_row = QHBoxLayout()
        self.refs_lbl = QLabel("")
        refs_row.addWidget(self.refs_lbl, 1)
        self.view_btn = QPushButton("View")
        self.view_btn.setEnabled(False)
        self.view_btn.clicked.connect(self._on_view_refs_click)
        refs_row.addWidget(self.view_btn)
        layout.addLayout(refs_row)

        layout.addWidget(QLabel("Tags on referencing notes:"))
        self.tags_list = QListWidget()
        self.tags_list.itemClicked.connect(self._on_tag_click)
        self.tags_list.setToolTip("Click a tag to filter the grid")
        layout.addWidget(self.tags_list, 1)

        # Duplicates section — hidden when current file has no twins.
        self.dupes_separator = QFrame()
        self.dupes_separator.setFrameShape(QFrame.Shape.HLine)
        self.dupes_separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(self.dupes_separator)
        self.dupes_header = QLabel("Duplicates (same content):")
        layout.addWidget(self.dupes_header)
        self.dupes_list = QListWidget()
        self.dupes_list.setMaximumHeight(120)
        self.dupes_list.itemClicked.connect(self._on_dupe_click)
        self.dupes_list.setToolTip("Click to preview")
        layout.addWidget(self.dupes_list)

        # Delete row.
        del_row = QHBoxLayout()
        del_row.addStretch(1)
        self.delete_btn = QPushButton("Delete…")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self._on_delete)
        del_row.addWidget(self.delete_btn)
        layout.addLayout(del_row)

    # ------- public API -------

    def show_image(
        self,
        filename: Optional[str],
        reasons: Optional[list[str]] = None,
    ) -> None:
        """Called by the grid when selection changes. Debounced."""
        self._current = filename
        self._reasons = reasons or []
        if filename is None:
            self._update_timer.stop()
            self._clear()
        else:
            self._update_timer.start(DEBOUNCE_MS)

    # ------- update -------

    def _clear(self) -> None:
        self.preview.clear()
        self.preview.setText("(select an image)")
        self.name_lbl.setText("")
        self.stats_lbl.setText("")
        self.why_list.clear()
        self.why_header.setVisible(False)
        self.why_list.setVisible(False)
        self.refs_lbl.setText("")
        self.view_btn.setEnabled(False)
        self.tags_list.clear()
        self.dupes_list.clear()
        self.dupes_separator.setVisible(False)
        self.dupes_header.setVisible(False)
        self.dupes_list.setVisible(False)
        self.delete_btn.setEnabled(False)
        self._ref_count = 0

    def _do_update(self) -> None:
        filename = self._current
        if not filename:
            self._clear()
            return

        path = media_index.media_path(filename)

        # Preview — use the same scaled-during-decode pipeline as the grid.
        img = thumbnails.decode_thumbnail(path, PREVIEW_PX)
        if not img.isNull():
            self.preview.setPixmap(QPixmap.fromImage(img))
        else:
            self.preview.clear()
            self.preview.setText("(no preview)")

        # Name
        self.name_lbl.setText(filename)

        # Stats — dimensions via QImageReader.size() avoids a full decode.
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = 0
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        dims = QImageReader(path).size()
        if dims.isValid() and dims.width() > 0:
            dim_str = f"{dims.width()}×{dims.height()}"
        else:
            dim_str = "?"
        date_str = (
            datetime.fromtimestamp(mtime).strftime("%Y-%m-%d") if mtime else "?"
        )
        self.stats_lbl.setText(
            f"{dim_str}  ·  {_humanize_bytes(size_bytes)}  ·  {date_str}"
        )

        self.why_list.clear()
        show_why = bool(self._reasons)
        self.why_header.setVisible(show_why)
        self.why_list.setVisible(show_why)
        for reason in self._reasons:
            self.why_list.addItem(QListWidgetItem(reason))

        # Refs + tags
        nids = media_index.notes_referencing(filename)
        self._ref_count = len(nids)
        self.refs_lbl.setText(f"Referenced by {len(nids)} note(s)")
        self.view_btn.setEnabled(len(nids) > 0)

        tag_counts = media_index.tags_for_notes(nids)
        self.tags_list.clear()
        for tag, count in tag_counts.most_common():
            item = QListWidgetItem(f"{tag}  ({count})")
            item.setData(Qt.ItemDataRole.UserRole, tag)
            self.tags_list.addItem(item)

        # Duplicates — only render section when the file has any.
        dupes = media_index.duplicates_of(filename)
        self.dupes_list.clear()
        show_dupes = bool(dupes)
        self.dupes_separator.setVisible(show_dupes)
        self.dupes_header.setVisible(show_dupes)
        self.dupes_list.setVisible(show_dupes)
        for dn in dupes:
            item = QListWidgetItem(dn)
            item.setData(Qt.ItemDataRole.UserRole, dn)
            self.dupes_list.addItem(item)

        self.delete_btn.setEnabled(True)

    # ------- handlers -------

    def _on_preview_click(self, _event) -> None:
        if self._current:
            open_preview(self, media_index.media_path(self._current))

    def _on_view_refs_click(self) -> None:
        if self._current:
            self.view_refs_requested.emit(self._current)

    def _on_tag_click(self, item: QListWidgetItem) -> None:
        tag = item.data(Qt.ItemDataRole.UserRole)
        if tag:
            self.search_requested.emit(f'tag:"{tag}"')

    def _on_dupe_click(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        if name:
            open_preview(self, media_index.media_path(name))

    def _on_delete(self) -> None:
        filename = self._current
        if not filename:
            return
        msg = (
            f"Move <b>{html.escape(filename)}</b> to Anki's media trash? "
            "It can be recovered from there."
        )
        if self._ref_count > 0:
            msg = (
                f"<b>{html.escape(filename)}</b> is referenced by "
                f"<b>{self._ref_count}</b> "
                "note(s). Moving it to trash will break those references "
                "until you restore the file.<br><br>" + msg
            )
        confirm = QMessageBox.question(self, "Delete file", msg)
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            mw.col.media.trash_files([filename])
        except Exception as e:
            showWarning(f"Delete failed: {e}")
            return
        media_index.invalidate_media_caches()
        tooltip(f"Moved {filename} to media trash.")
        self.file_deleted.emit(filename)
