"""Background thumbnail loader.

Decoding and scaling images on the GUI thread is the main reason the media
browser feels sluggish — one open re-decodes hundreds of files synchronously.

This module owns:
  * a global QPixmapCache keyed on `path|size|mtime` (so edits invalidate),
  * a QThreadPool that decodes QImages off-thread,
  * a `loaded(filename, size)` Qt signal that grids subscribe to and use to
    re-fetch their pixmap from the cache once a load completes.

We use QImageReader (not QImage(path)) for decoding so we can hint the
desired scaled size during decode. This matters for SVG and similar formats
where rendering at native size first produces a tiny image that then has to
be enlarged.
"""

from __future__ import annotations

import os

from aqt.qt import (
    QColor,
    QImage,
    QImageReader,
    QObject,
    QPainter,
    QPen,
    QPixmap,
    QPixmapCache,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    pyqtSignal,
    pyqtSlot,
)


# Default QPixmapCache is ~10MB. With 128px thumbnails that's only ~640 items
# resident, so we bump it. 50MB is still trivial vs. card decks but lets us
# hold ~3k thumbs without thrashing.
QPixmapCache.setCacheLimit(50 * 1024)  # KB


def _cache_key(path: str, size: int, mtime: float) -> str:
    return f"{path}|{size}|{mtime}"


def decode_thumbnail(path: str, size: int) -> QImage:
    """Decode `path` at approximately `size`×`size` pixels (KeepAspectRatio).

    Uses QImageReader.setScaledSize so format plugins that support hinted
    scaling (SVG, large JPEG) decode directly at the target size. Returns a
    null QImage on failure.
    """
    reader = QImageReader(path)
    reader.setAutoTransform(True)  # honour EXIF orientation
    orig = reader.size()
    if orig.isValid() and orig.width() > 0 and orig.height() > 0:
        target = orig.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(target)
    else:
        # Format didn't report a size (some SVGs). Request a square decode and
        # let the plugin do its best.
        reader.setScaledSize(QSize(size, size))
    img = reader.read()
    if img.isNull():
        return img
    # Belt-and-braces: if the plugin ignored setScaledSize and gave us
    # something bigger or much smaller than asked, do a CPU scale to the
    # target. KeepAspectRatio preserves shape.
    if max(img.width(), img.height()) != size:
        img = img.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return img


class _WorkerSignals(QObject):
    # path, size, image, mtime — image is QImage so it can cross threads.
    done = pyqtSignal(str, int, object, float)


class _ThumbJob(QRunnable):
    def __init__(self, path: str, size: int, mtime: float, signals: _WorkerSignals):
        super().__init__()
        self.path = path
        self.size = size
        self.mtime = mtime
        self.signals = signals

    def run(self) -> None:
        img = decode_thumbnail(self.path, self.size)
        self.signals.done.emit(self.path, self.size, img, self.mtime)


class _Loader(QObject):
    """Singleton thumbnail loader. Use `loader` exposed at module level."""

    loaded = pyqtSignal(str, int)  # filename, size — listeners refetch from cache

    def __init__(self) -> None:
        super().__init__()
        self._pool = QThreadPool(self)
        # Cap this add-on's thumbnail decoding without changing Anki's global
        # thread pool, which other add-ons may also use.
        self._pool.setMaxThreadCount(4)
        self._inflight: set[tuple[str, int]] = set()
        self._signals = _WorkerSignals()
        self._signals.done.connect(self._on_done)

    def get_or_load(self, path: str, size: int) -> QPixmap | None:
        """Return cached pixmap if available; else schedule a load and return None.

        Callers should set a placeholder when None is returned and listen for
        `loaded` to re-fetch.
        """
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        key = _cache_key(path, size, mtime)
        pm = QPixmapCache.find(key)
        if pm is not None:
            return pm
        token = (path, size)
        if token in self._inflight:
            return None
        self._inflight.add(token)
        self._pool.start(_ThumbJob(path, size, mtime, self._signals))
        return None

    @pyqtSlot(str, int, object, float)
    def _on_done(self, path: str, size: int, image: QImage, mtime: float) -> None:
        self._inflight.discard((path, size))
        # Cache the result either way: a null decode becomes the broken
        # pixmap, which we still want to display (and not redecode).
        if image is None or image.isNull():
            pm = broken(size)
        else:
            pm = QPixmap.fromImage(image)
        QPixmapCache.insert(_cache_key(path, size, mtime), pm)
        # Listeners key on basename — that's what _MediaGrid tracks.
        filename = os.path.basename(path)
        self.loaded.emit(filename, size)


loader = _Loader()


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------

_PLACEHOLDERS: dict[int, QPixmap] = {}
_BROKEN: dict[int, QPixmap] = {}


def placeholder(size: int) -> QPixmap:
    """Visible-but-unobtrusive 'loading' tile — flat light grey."""
    pm = _PLACEHOLDERS.get(size)
    if pm is not None:
        return pm
    pm = QPixmap(size, size)
    pm.fill(QColor(220, 220, 220))
    _PLACEHOLDERS[size] = pm
    return pm


def broken(size: int) -> QPixmap:
    """Tile shown when a file couldn't be decoded — light grey with an X."""
    pm = _BROKEN.get(size)
    if pm is not None:
        return pm
    pm = QPixmap(size, size)
    pm.fill(QColor(235, 220, 220))
    painter = QPainter(pm)
    pen = QPen(QColor(160, 80, 80))
    pen.setWidth(max(2, size // 32))
    painter.setPen(pen)
    pad = size // 5
    painter.drawLine(pad, pad, size - pad, size - pad)
    painter.drawLine(size - pad, pad, pad, size - pad)
    painter.end()
    _BROKEN[size] = pm
    return pm
