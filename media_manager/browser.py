"""Media browser dialog: thumbnail grid, search, related panel, insert, replace."""

from __future__ import annotations

import json
from typing import Optional

from aqt import dialogs, mw
from aqt.editor import Editor
from aqt.qt import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QIcon,
    QKeySequence,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QShortcut,
    QSize,
    Qt,
    QVBoxLayout,
)
from aqt.utils import showWarning, tooltip

from . import media_index, thumbnails
from .info_panel import InfoPanel
from .preview import open_preview
from .rename import RenameDialog
from .replace import ReplaceDialog


def _combined_search(
    query: str,
    all_files: list[str],
    *,
    note_cap: int,
) -> tuple[list[str], int, int]:
    """Union of filename-substring matches and images on notes matching `query`.

    Empty query → returns `all_files` unchanged (treated as 'no filter').
    Filename matches come first; note-derived images are appended in
    note-frequency order, deduped against the filename set.

    Returns (combined, n_filename_hits, n_note_derived_hits).
    """
    q = query.strip()
    if not q:
        return all_files, len(all_files), 0
    q_lower = q.lower()
    name_matches = [f for f in all_files if q_lower in f.lower()]
    note_hits = media_index.images_from_note_search(q, cap=note_cap)
    existing = set(all_files)
    seen = set(name_matches)
    note_filenames = [fn for fn, _ in note_hits if fn in existing and fn not in seen]
    return name_matches + note_filenames, len(name_matches), len(note_filenames)


def open_references_in_browser(filename: str) -> None:
    """Open Anki's note Browser pre-filtered to notes that reference the file."""
    nids = media_index.notes_referencing(filename)
    if not nids:
        tooltip(f"No notes reference {filename}.")
        return
    browser = dialogs.open("Browser", mw)
    query = "nid:" + ",".join(str(n) for n in nids)
    browser.search_for(query)
    browser.activateWindow()


def _cfg() -> dict:
    return mw.addonManager.getConfig(__name__.rsplit(".", 1)[0]) or {}


def _thumb_px() -> int:
    return int(_cfg().get("thumbnail_px", 128))


def _page_size() -> int:
    return int(_cfg().get("page_size", 200))


class _MediaGrid(QListWidget):
    """Icon-mode grid that populates via the async thumbnail loader.

    Items are created immediately with a placeholder icon; real thumbnails
    drop in as the loader finishes decoding them off-thread.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        # Single-line filenames with middle ellipsis. Word wrap is what causes
        # the icon-overlaps-text bug when the parent theme (e.g. Anki's
        # Browser) pushes the font size up and text wraps to 2+ lines into the
        # icon's allocated space.
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._thumb_px = _thumb_px()
        self.setIconSize(QSize(self._thumb_px, self._thumb_px))
        # Tight cell: icon + one line of text + small padding. Explicit
        # spacing prevents parent stylesheets from injecting larger gaps.
        self.setGridSize(QSize(self._thumb_px + 12, self._thumb_px + 28))
        self.setSpacing(4)
        # Override any inherited per-item padding from parent themes.
        self.setStyleSheet("QListView::item { padding: 0px; margin: 0px; }")
        # filename -> list of items currently awaiting a thumbnail
        self._pending: dict[str, list[QListWidgetItem]] = {}
        thumbnails.loader.loaded.connect(self._on_thumb_loaded)

    def populate(self, filenames: list[str]) -> None:
        self.clear()
        self._pending.clear()
        placeholder_icon = QIcon(thumbnails.placeholder(self._thumb_px))
        for name in filenames[: _page_size()]:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setToolTip(name)
            pm = thumbnails.loader.get_or_load(
                media_index.media_path(name), self._thumb_px
            )
            if pm is not None:
                item.setIcon(QIcon(pm))
            else:
                item.setIcon(placeholder_icon)
                self._pending.setdefault(name, []).append(item)
            self.addItem(item)

    def _on_thumb_loaded(self, filename: str, size: int) -> None:
        if size != self._thumb_px:
            return
        items = self._pending.pop(filename, None)
        if not items:
            return
        pm = thumbnails.loader.get_or_load(
            media_index.media_path(filename), self._thumb_px
        )
        if pm is None:
            return
        icon = QIcon(pm)
        for item in items:
            item.setIcon(icon)
        # setIcon should invalidate the cell, but IconMode + uniformItemSizes
        # can keep stale paint cached; force a repaint of the visible area.
        self.viewport().update()

    def current_filename(self) -> Optional[str]:
        item = self.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


# ---------------------------------------------------------------------------
# Main browser
# ---------------------------------------------------------------------------


class MediaBrowserDialog(QDialog):
    def __init__(self, parent, editor: Optional[Editor] = None):
        super().__init__(parent)
        self.editor = editor
        self.setWindowTitle("Media Manager")
        self._all_files: list[str] = []
        self._build_ui()
        self._reload()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Top: search row + count label
        top = QHBoxLayout()
        top.addWidget(QLabel("Search:"))
        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "filename, note keyword (tag:, deck:, …), "
            "is:unused, is:duplicate, or * — press Enter"
        )
        self.search.returnPressed.connect(self._apply_filter)
        top.addWidget(self.search, 1)
        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._apply_filter)
        top.addWidget(search_btn)
        self.count_label = QLabel("")
        top.addWidget(self.count_label)
        root.addLayout(top)

        # Middle: grid + info panel
        mid = QHBoxLayout()
        self.grid = _MediaGrid()
        self.grid.itemDoubleClicked.connect(self._on_insert)
        self.grid.currentItemChanged.connect(self._on_selection_changed)
        mid.addWidget(self.grid, 1)

        self.info_panel = InfoPanel()
        self.info_panel.search_requested.connect(self._on_search_requested)
        self.info_panel.view_refs_requested.connect(open_references_in_browser)
        self.info_panel.file_deleted.connect(lambda _f: self._reload())
        mid.addWidget(self.info_panel)
        root.addLayout(mid, 1)

        # Bottom: actions
        actions = QHBoxLayout()
        self.insert_btn = QPushButton("Insert into card")
        self.insert_btn.clicked.connect(self._on_insert)
        self.preview_btn = QPushButton("Preview (Space)")
        self.preview_btn.clicked.connect(self._on_preview)
        self.replace_btn = QPushButton("Replace…")
        self.replace_btn.clicked.connect(self._on_replace)
        self.rename_btn = QPushButton("Rename…")
        self.rename_btn.clicked.connect(self._on_rename)
        self.view_refs_btn = QPushButton("View references")
        self.view_refs_btn.clicked.connect(self._on_view_refs)
        self.bulk_delete_btn = QPushButton("")
        self.bulk_delete_btn.clicked.connect(self._on_bulk_delete)
        self.bulk_delete_btn.setVisible(False)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        actions.addWidget(self.insert_btn)
        actions.addWidget(self.preview_btn)
        actions.addWidget(self.replace_btn)
        actions.addWidget(self.rename_btn)
        actions.addWidget(self.view_refs_btn)
        actions.addWidget(self.bulk_delete_btn)
        actions.addStretch(1)
        actions.addWidget(close_btn)

        QShortcut(QKeySequence(Qt.Key.Key_Space), self).activated.connect(
            self._on_preview
        )
        root.addLayout(actions)

        if self.editor is None:
            self.insert_btn.setEnabled(False)
            self.insert_btn.setToolTip(
                "Open the Media Manager from a card editor to insert."
            )

        self.resize(1300, 760)

    # ------- data -------

    def _reload(self) -> None:
        self._all_files = media_index.list_image_files()
        self._apply_filter()

    def _apply_filter(self) -> None:
        cfg = _cfg()
        raw = self.search.text().strip()
        total = len(self._all_files)
        # Reset bulk delete button; specific branches re-enable it.
        self.bulk_delete_btn.setVisible(False)

        if not raw:
            related = self._build_related_list()
            if related is not None:
                self.grid.populate(related)
                shown = min(len(related), _page_size())
                self.count_label.setText(
                    f"{shown} related to this card / {total} total "
                    "— type * to show all"
                )
            else:
                self.grid.populate(self._all_files)
                shown = min(total, _page_size())
                self.count_label.setText(f"{shown} shown / {total} total")
            return

        if raw == "*":
            self.grid.populate(self._all_files)
            shown = min(total, _page_size())
            self.count_label.setText(f"{shown} shown / {total} total — all images")
            return

        if raw == "is:unused":
            orphans = media_index.find_orphans()
            self.grid.populate(orphans)
            shown = min(len(orphans), _page_size())
            self.count_label.setText(
                f"{shown} unused / {total} total"
            )
            if orphans:
                self.bulk_delete_btn.setText(f"Delete {len(orphans)} unused…")
                self.bulk_delete_btn.setVisible(True)
            return

        if raw == "is:duplicate":
            groups = media_index.find_duplicates()
            files = sorted(
                {fn for v in groups.values() for fn in v},
                key=str.lower,
            )
            self.grid.populate(files)
            shown = min(len(files), _page_size())
            self.count_label.setText(
                f"{shown} duplicated / {total} total — "
                f"{len(groups)} group(s)"
            )
            return

        combined, n_name, n_note = _combined_search(
            raw,
            self._all_files,
            note_cap=int(cfg.get("keyword_search_cap", 500)),
        )
        self.grid.populate(combined)
        shown = min(len(combined), _page_size())
        self.count_label.setText(
            f"{shown} shown — {n_name} by name + {n_note} from notes "
            f"/ {total} total"
        )

    def _build_related_list(self) -> Optional[list[str]]:
        """Compute related-image list for the editor's current note.

        Returns None if there's no current note OR no related results were
        found (caller treats both as 'fall through to all images').
        """
        note = self._current_note()
        if note is None:
            return None
        cfg = _cfg()
        min_len = int(cfg.get("min_token_length", 3))
        limit = int(cfg.get("related_limit", 40))

        by_name = media_index.related_by_filename(
            note, self._all_files, min_len=min_len, limit=limit,
        )
        by_sim = media_index.related_by_similar_notes(
            note,
            limit=limit,
            cap=int(cfg.get("candidate_cap", 300)),
            rare_tag_max_fraction=float(cfg.get("rare_tag_max_fraction", 0.2)),
            weight_tag=float(cfg.get("weight_tag", 1.0)),
            weight_image=float(cfg.get("weight_image", 2.0)),
            weight_text=float(cfg.get("weight_text", 1.0)),
        )

        existing = set(self._all_files)
        seen: set[str] = set()
        result: list[str] = []
        # 1. Images already on this card — most directly relevant.
        for fn in media_index.images_in_note(note):
            if fn in existing and fn not in seen:
                result.append(fn)
                seen.add(fn)
        # 2. Filename-token matches.
        for fn, _ in by_name:
            if fn in existing and fn not in seen:
                result.append(fn)
                seen.add(fn)
        # 3. Images on similar notes.
        for fn, _ in by_sim:
            if fn in existing and fn not in seen:
                result.append(fn)
                seen.add(fn)
        return result or None

    def _current_note(self):
        if self.editor is None or self.editor.note is None:
            return None
        return self.editor.note

    # ------- actions -------

    def _selected_filename(self) -> Optional[str]:
        return self.grid.current_filename()

    def _on_insert(self) -> None:
        name = self._selected_filename()
        if not name:
            tooltip("Select an image first.")
            return
        if self.editor is None:
            tooltip("No editor to insert into.")
            return
        html_snippet = f'<img src="{name}">'
        self.editor.web.eval(
            f"pasteHTML({json.dumps(html_snippet)}, true, false);"
        )
        tooltip(f"Inserted {name}.")

    def _on_replace(self) -> None:
        name = self._selected_filename()
        if not name:
            tooltip("Select an image first.")
            return
        dlg = ReplaceDialog(self, name)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._reload()

    def _on_rename(self) -> None:
        name = self._selected_filename()
        if not name:
            tooltip("Select an image first.")
            return
        dlg = RenameDialog(self, name)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._reload()

    def _on_view_refs(self) -> None:
        name = self._selected_filename()
        if not name:
            tooltip("Select an image first.")
            return
        open_references_in_browser(name)

    def _on_preview(self) -> None:
        name = self._selected_filename()
        if not name:
            tooltip("Select an image first.")
            return
        open_preview(self, media_index.media_path(name))

    def _on_selection_changed(self, current, _previous) -> None:
        filename = (
            current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        )
        self.info_panel.show_image(filename)

    def _on_search_requested(self, query: str) -> None:
        self.search.setText(query)
        self._apply_filter()

    def _on_bulk_delete(self) -> None:
        # Only fires when filter is is:unused.
        orphans = media_index.find_orphans()
        if not orphans:
            tooltip("No unused files to delete.")
            return
        confirm = QMessageBox.question(
            self,
            "Delete unused files",
            f"Move <b>{len(orphans)}</b> file(s) to Anki's media trash? "
            "They can be recovered from there.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            mw.col.media.trash_files(orphans)
        except Exception as e:
            showWarning(f"Delete failed: {e}")
            return
        media_index.invalidate_media_caches()
        tooltip(f"Moved {len(orphans)} file(s) to media trash.")
        self._reload()


# ---------------------------------------------------------------------------
# Lightweight picker used by ReplaceDialog
# ---------------------------------------------------------------------------


class PickMediaDialog(QDialog):
    """Compact picker — like the main browser but without the related panel
    and without insert/replace actions."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Pick image from media library")
        self.selected: Optional[str] = None
        self._all_files = media_index.list_image_files()

        root = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Search:"))
        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "filename or note keyword — press Enter"
        )
        self.search.returnPressed.connect(self._apply_filter)
        top.addWidget(self.search, 1)
        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._apply_filter)
        top.addWidget(search_btn)
        root.addLayout(top)

        self.grid = _MediaGrid()
        self.grid.itemDoubleClicked.connect(self._on_pick)
        root.addWidget(self.grid)

        btn_row = QHBoxLayout()
        preview = QPushButton("Preview (Space)")
        preview.clicked.connect(self._on_preview)
        pick = QPushButton("Pick")
        pick.clicked.connect(self._on_pick)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(preview)
        btn_row.addStretch(1)
        btn_row.addWidget(pick)
        btn_row.addWidget(cancel)
        root.addLayout(btn_row)

        QShortcut(QKeySequence(Qt.Key.Key_Space), self).activated.connect(
            self._on_preview
        )

        self.resize(800, 600)
        self._apply_filter()

    def _apply_filter(self) -> None:
        cfg = _cfg()
        combined, _, _ = _combined_search(
            self.search.text(),
            self._all_files,
            note_cap=int(cfg.get("keyword_search_cap", 500)),
        )
        self.grid.populate(combined)

    def _on_pick(self) -> None:
        name = self.grid.current_filename()
        if not name:
            tooltip("Select an image first.")
            return
        self.selected = name
        self.accept()

    def _on_preview(self) -> None:
        name = self.grid.current_filename()
        if not name:
            tooltip("Select an image first.")
            return
        open_preview(self, media_index.media_path(name))


def open_browser(editor: Optional[Editor] = None) -> None:
    parent = editor.parentWindow if editor else mw
    dlg = MediaBrowserDialog(parent, editor=editor)
    dlg.show()
