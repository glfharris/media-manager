"""Full-image preview dialog.

Modal QDialog that loads an image at native resolution into a QGraphicsView
so we get pan + zoom for free. Wheel zooms anchored under the mouse; click-
and-drag pans.
"""

from __future__ import annotations

import os

from aqt.qt import (
    QDialog,
    QDialogButtonBox,
    QGraphicsScene,
    QGraphicsView,
    QPixmap,
    Qt,
    QVBoxLayout,
)


class _PreviewView(QGraphicsView):
    """QGraphicsView with mouse-anchored wheel zoom."""

    def wheelEvent(self, event):
        zoom_in = event.angleDelta().y() > 0
        factor = 1.2 if zoom_in else 1 / 1.2
        self.scale(factor, factor)


class PreviewDialog(QDialog):
    def __init__(self, parent, image_path: str):
        super().__init__(parent)
        self.setWindowTitle(os.path.basename(image_path))

        layout = QVBoxLayout(self)

        self._scene = QGraphicsScene(self)
        pm = QPixmap(image_path)
        if pm.isNull():
            # Add an empty rect so the view has something to fit.
            self._scene.addText("(could not decode image)")
        else:
            self._pixmap_item = self._scene.addPixmap(pm)

        self._view = _PreviewView(self._scene)
        self._view.setRenderHint(self._view.renderHints())  # default antialiasing
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._view.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self._view.setResizeAnchor(
            QGraphicsView.ViewportAnchor.AnchorViewCenter
        )
        layout.addWidget(self._view, 1)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.resize(900, 700)
        if not pm.isNull():
            # Fit on first show — but the view's viewport doesn't have its
            # final size until show(), so defer.
            self._fit_pending = True
        else:
            self._fit_pending = False

    def showEvent(self, event):
        super().showEvent(event)
        if self._fit_pending:
            self._view.fitInView(
                self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio
            )
            self._fit_pending = False


def open_preview(parent, image_path: str) -> None:
    PreviewDialog(parent, image_path).exec()
