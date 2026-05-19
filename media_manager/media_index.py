"""Read-only helpers for inspecting the media collection and finding
relationships between media files and notes.

Everything here goes through `mw.col` — no I/O beyond `os.listdir` on the
media directory and standard Anki search.
"""

from __future__ import annotations

import html
import math
import os
import re
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional

from aqt import mw

IMG_TAG_RE = re.compile(r"""<img\b[^>]*>""", re.IGNORECASE)
SRC_ATTR_RE = re.compile(
    r"""(?P<prefix>(?:^|[\s<])src\s*=\s*)"""
    r"""(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))""",
    re.IGNORECASE,
)
HTML_TAG_RE = re.compile(r"<[^>]+>")
TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

# Length floor for tokens used in TF-IDF scoring; tokens shorter than this
# are dropped. 3 lets short acronyms (ECG, MRI, DNA) through; their idf will
# naturally weight them down if they're common.
_TFIDF_TOKEN_MIN_LEN = 3


def _config() -> dict:
    return mw.addonManager.getConfig(__name__.rsplit(".", 1)[0]) or {}


def image_extensions() -> set[str]:
    cfg = _config()
    return {e.lower().lstrip(".") for e in cfg.get(
        "image_extensions",
        ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif"],
    )}


def list_image_files() -> list[str]:
    """Return filenames in collection.media that look like images."""
    media_dir = mw.col.media.dir()
    exts = image_extensions()
    out: list[str] = []
    for name in os.listdir(media_dir):
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in exts:
            out.append(name)
    out.sort(key=str.lower)
    return out


def media_path(filename: str) -> str:
    return os.path.join(mw.col.media.dir(), filename)


def img_html(filename: str) -> str:
    """Return an HTML img tag for an Anki media filename."""
    return f'<img src="{html.escape(filename, quote=True)}">'


def _src_value(match: re.Match[str]) -> str:
    for name in ("double", "single", "bare"):
        value = match.group(name)
        if value is not None:
            return value
    return ""


def _filename_from_src(src: str) -> str:
    value = html.unescape(src.strip())
    value = value.split("?", 1)[0].split("#", 1)[0]
    return urllib.parse.unquote(value)


def _reference_search_terms(filename: str) -> list[str]:
    terms = [
        filename,
        html.escape(filename, quote=True),
        urllib.parse.quote(filename),
    ]
    out: list[str] = []
    for term in terms:
        if term and term not in out:
            out.append(term)
    return out


def rewrite_img_srcs(field: str, old: str, new: str) -> tuple[str, int]:
    """Rewrite matching img src attributes in an HTML field.

    Handles whitespace around `=`, single/double/unquoted attrs, HTML-escaped
    filenames, URL-encoded filenames, and query/fragment suffixes.
    """
    new_src = html.escape(new, quote=True)
    replacements = 0

    def rewrite_tag(tag_match: re.Match[str]) -> str:
        nonlocal replacements
        tag = tag_match.group(0)

        def rewrite_src(src_match: re.Match[str]) -> str:
            nonlocal replacements
            if _filename_from_src(_src_value(src_match)) != old:
                return src_match.group(0)
            replacements += 1
            return f'{src_match.group("prefix")}"{new_src}"'

        return SRC_ATTR_RE.sub(rewrite_src, tag)

    return IMG_TAG_RE.sub(rewrite_tag, field), replacements


# ---------------------------------------------------------------------------
# Field / note text utilities
# ---------------------------------------------------------------------------


def strip_html(text: str) -> str:
    return html.unescape(HTML_TAG_RE.sub(" ", text))


def tokens(text: str, min_len: int = 3) -> set[str]:
    return {
        t.lower()
        for t in TOKEN_RE.findall(text)
        if len(t) >= min_len and not t.isdigit()
    }


def note_text(note) -> str:
    return " ".join(strip_html(f) for f in note.fields)


def images_in_note(note) -> list[str]:
    return _images_from_flds("\x1f".join(note.fields))


def _images_from_flds(flds: str) -> list[str]:
    """Extract <img src=...> filenames from a flds blob (or any HTML string)."""
    seen: list[str] = []
    for tag_match in IMG_TAG_RE.finditer(flds):
        for src_match in SRC_ATTR_RE.finditer(tag_match.group(0)):
            name = _filename_from_src(_src_value(src_match))
            if name and name not in seen:
                seen.append(name)
    return seen


# ---------------------------------------------------------------------------
# Reverse lookup: which notes reference a filename
# ---------------------------------------------------------------------------


def images_from_note_search(
    query: str,
    *,
    cap: int = 500,
) -> list[tuple[str, int]]:
    """Run an Anki note search and return images referenced on the matching notes.

    `query` is passed straight to Anki, so search operators (`tag:`, `deck:`,
    `is:new`, …) work. If more than `cap` notes match, only the most recent
    `cap` (highest note id) are scanned.

    Returns [(filename, note_count)] sorted by note_count desc.
    """
    q = query.strip()
    if not q:
        return []
    try:
        nids = mw.col.find_notes(q)
    except Exception:
        return []
    if not nids:
        return []
    if len(nids) > cap:
        # Note ids encode creation time — newer cards are more likely to use
        # whatever images you're currently working with.
        nids = sorted(nids, reverse=True)[:cap]
    counts: Counter[str] = Counter()
    for _, flds in _bulk_fetch_flds(nids):
        for img in _images_from_flds(flds or ""):
            counts[img] += 1
    return counts.most_common()


def notes_referencing(filename: str) -> list[int]:
    """Note IDs whose fields contain the given filename string.

    Uses Anki's full-text search to narrow, then re-checks each candidate's
    image refs to filter false positives from coincidental substrings.
    """
    nids: set[int] = set()
    for term in _reference_search_terms(filename):
        safe = term.replace('"', '\\"')
        try:
            nids.update(mw.col.find_notes(f'"{safe}"'))
        except Exception:
            continue
    if not nids:
        return []
    confirmed: list[int] = []
    for nid, flds in _bulk_fetch_flds(nids):
        if filename in _images_from_flds(flds or ""):
            confirmed.append(nid)
    return confirmed


# ---------------------------------------------------------------------------
# Orphans (unused files) and duplicates (same content, different names)
# ---------------------------------------------------------------------------


_ORPHANS_CACHE: Optional[set[str]] = None
_DUPES_CACHE: Optional[dict[str, list[str]]] = None  # csum -> [filenames]


def find_orphans() -> list[str]:
    """Image files in the media dir that no note references.

    Uses Anki's own media check when available (it understands template /
    CSS refs too); falls back to a flds-only scan otherwise. Cached for the
    rest of the session — call invalidate_media_caches() after delete/add.
    """
    global _ORPHANS_CACHE
    if _ORPHANS_CACHE is None:
        _ORPHANS_CACHE = _compute_orphans()
    return sorted(_ORPHANS_CACHE, key=str.lower)


def _compute_orphans() -> set[str]:
    images = set(list_image_files())
    # Preferred path: Anki's media-check report.
    try:
        report = mw.col.media.check()
        unused = getattr(report, "unused", None)
        if unused is not None:
            return images & set(unused)
    except Exception:
        pass
    # Fallback: scan note fields only. Template/CSS refs would show as
    # false-positive orphans here — acceptable for a fallback.
    referenced: set[str] = set()
    for (flds,) in mw.col.db.all("SELECT flds FROM notes"):
        for img in _images_from_flds(flds or ""):
            referenced.add(img)
    return images - referenced


def find_duplicates() -> dict[str, list[str]]:
    """Map of csum -> filenames sharing that csum (only groups of size ≥ 2).

    Cached for the session. Uses Anki's media DB (cheap — csum is already
    maintained for sync); falls back to streaming SHA1 of each image file.
    """
    global _DUPES_CACHE
    if _DUPES_CACHE is None:
        _DUPES_CACHE = _compute_duplicates()
    return _DUPES_CACHE


def _compute_duplicates() -> dict[str, list[str]]:
    images = set(list_image_files())
    groups: dict[str, list[str]] = {}
    # Preferred path: Anki's media DB has csums.
    try:
        rows = mw.col.media.db.all(
            "SELECT fname, csum FROM media "
            "WHERE csum IS NOT NULL AND csum != ''"
        )
        for fname, csum in rows:
            if fname in images:
                groups.setdefault(csum, []).append(fname)
    except Exception:
        groups = _compute_duplicates_manual(images)
    return {k: sorted(v, key=str.lower) for k, v in groups.items() if len(v) > 1}


def _compute_duplicates_manual(images: Iterable[str]) -> dict[str, list[str]]:
    import hashlib
    groups: dict[str, list[str]] = {}
    for fname in images:
        path = media_path(fname)
        try:
            h = hashlib.sha1()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            csum = h.hexdigest()
        except OSError:
            continue
        groups.setdefault(csum, []).append(fname)
    return groups


def duplicates_of(filename: str) -> list[str]:
    """Other filenames with the same content as `filename`. Empty if none."""
    for files in find_duplicates().values():
        if filename in files:
            return [f for f in files if f != filename]
    return []


def is_duplicate(filename: str) -> bool:
    return bool(duplicates_of(filename))


def invalidate_media_caches() -> None:
    """Drop orphan and duplicate caches. Call after add/delete/rename."""
    global _ORPHANS_CACHE, _DUPES_CACHE
    _ORPHANS_CACHE = None
    _DUPES_CACHE = None


# ---------------------------------------------------------------------------
# Session-cached collection stats (tag + token frequencies)
# ---------------------------------------------------------------------------


@dataclass
class _Stats:
    total: int                  # number of notes
    tag_counts: Counter[str]    # tag -> notes-containing
    token_counts: Counter[str]  # sort-field token -> notes-containing
    image_counts: Counter[str]  # image filename -> notes-containing


@dataclass(frozen=True)
class RelatedImage:
    filename: str
    score: float
    reasons: tuple[str, ...]


_STATS: Optional[_Stats] = None


def _flds_text(flds: str) -> str:
    """Render the \\x1f-joined flds blob as flat searchable text."""
    return strip_html((flds or "").replace("\x1f", " "))


def _build_stats() -> _Stats:
    rows = mw.col.db.all("SELECT flds, tags FROM notes")
    tag_counts: Counter[str] = Counter()
    token_counts: Counter[str] = Counter()
    image_counts: Counter[str] = Counter()
    for flds, tags in rows:
        for t in (tags or "").split():
            tag_counts[t] += 1
        for tok in tokens(_flds_text(flds), min_len=_TFIDF_TOKEN_MIN_LEN):
            token_counts[tok] += 1
        for img in _images_from_flds(flds or ""):
            image_counts[img] += 1
    return _Stats(
        total=len(rows),
        tag_counts=tag_counts,
        token_counts=token_counts,
        image_counts=image_counts,
    )


def _stats() -> _Stats:
    global _STATS
    if _STATS is None:
        _STATS = _build_stats()
    return _STATS


def invalidate_stats() -> None:
    """Drop the session-cached stats. Call when the collection changes
    materially (e.g. after a sync or bulk import)."""
    global _STATS
    _STATS = None


# ---------------------------------------------------------------------------
# Bulk SQL: one query for N notes, far cheaper than per-note get_note().
# ---------------------------------------------------------------------------


def _bulk_fetch_flds(nids: Iterable[int]) -> list[tuple[int, str]]:
    nids = list(nids)
    if not nids:
        return []
    placeholders = ",".join("?" * len(nids))
    return mw.col.db.all(
        f"SELECT id, flds FROM notes WHERE id IN ({placeholders})", *nids
    )


def tags_for_notes(nids: Iterable[int]) -> Counter[str]:
    """Aggregate tag frequencies across the given notes."""
    nids = list(nids)
    if not nids:
        return Counter()
    placeholders = ",".join("?" * len(nids))
    rows = mw.col.db.all(
        f"SELECT tags FROM notes WHERE id IN ({placeholders})", *nids
    )
    counts: Counter[str] = Counter()
    for (tags_str,) in rows:
        for t in (tags_str or "").split():
            counts[t] += 1
    return counts


def _bulk_fetch_full(
    nids: Iterable[int],
) -> list[tuple[int, str, str]]:
    """Returns rows of (id, tags, flds)."""
    nids = list(nids)
    if not nids:
        return []
    placeholders = ",".join("?" * len(nids))
    return mw.col.db.all(
        f"SELECT id, tags, flds FROM notes WHERE id IN ({placeholders})",
        *nids,
    )


# ---------------------------------------------------------------------------
# TF-IDF helpers
# ---------------------------------------------------------------------------


def _idf(df: int, total: int) -> float:
    return math.log((1 + total) / (1 + df)) if total > 0 else 0.0


def _bounded_idf(df: int, total: int) -> float:
    """IDF normalized to roughly 0..1 for mixing with other scores."""
    if total <= 2 or df <= 0:
        return 1.0
    max_idf = math.log((1 + total) / 2)
    if max_idf <= 0:
        return 0.0
    return max(0.0, min(1.0, _idf(df, total) / max_idf))


def _tfidf_vector(toks: set[str], stats: _Stats) -> dict[str, float]:
    if not toks or stats.total == 0:
        return {}
    return {
        t: _idf(stats.token_counts.get(t, 0), stats.total)
        for t in toks
    }


def _cosine(v1: dict[str, float], v2: dict[str, float]) -> float:
    if not v1 or not v2:
        return 0.0
    keys = v1.keys() & v2.keys()
    if not keys:
        return 0.0
    dot = sum(v1[k] * v2[k] for k in keys)
    n1 = math.sqrt(sum(v * v for v in v1.values()))
    n2 = math.sqrt(sum(v * v for v in v2.values()))
    return dot / (n1 * n2) if n1 and n2 else 0.0


# ---------------------------------------------------------------------------
# Relatedness scoring
# ---------------------------------------------------------------------------


def _filename_tokens(filename: str, min_len: int) -> set[str]:
    stem = filename.rsplit(".", 1)[0]
    return tokens(stem, min_len=min_len)


def _fmt_names(names: Iterable[str], *, limit: int = 3) -> str:
    all_values = sorted(names, key=str.lower)
    shown = all_values[:limit]
    extra = f" +{len(all_values) - limit}" if len(all_values) > limit else ""
    return ", ".join(shown) + extra


def _top_terms_by_rarity(
    terms: Iterable[str],
    counts: Counter[str],
    *,
    limit: int = 3,
) -> list[str]:
    unique = set(terms)
    return sorted(unique, key=lambda t: (counts.get(t, 0), t.lower()))[:limit]


def related_by_filename(
    note,
    candidates: Iterable[str],
    *,
    min_len: int = 3,
    limit: int = 40,
) -> list[tuple[str, int]]:
    """Score candidate filenames by token overlap with the note's field text."""
    note_toks = tokens(note_text(note), min_len=min_len)
    if not note_toks:
        return []
    scored: list[tuple[str, int]] = []
    for fname in candidates:
        ftoks = _filename_tokens(fname, min_len)
        score = len(note_toks & ftoks)
        if score:
            scored.append((fname, score))
    scored.sort(key=lambda x: (-x[1], x[0].lower()))
    return scored[:limit]


def _filename_suggestions(
    note,
    candidates: Iterable[str],
    stats: _Stats,
    *,
    min_len: int,
    weight_filename: float,
) -> dict[str, tuple[float, list[str]]]:
    note_toks = tokens(note_text(note), min_len=min_len)
    if not note_toks:
        return {}
    out: dict[str, tuple[float, list[str]]] = {}
    for fname in candidates:
        shared = note_toks & _filename_tokens(fname, min_len)
        if not shared:
            continue
        rare_terms = _top_terms_by_rarity(
            shared, stats.token_counts, limit=4
        )
        score = sum(
            _bounded_idf(stats.token_counts.get(t, 0), stats.total)
            for t in shared
        )
        score = min(1.0, score) * weight_filename
        out[fname] = (
            score,
            [f"Filename matches note text: {_fmt_names(rare_terms, limit=4)}"],
        )
    return out


# ---------- similar-notes pipeline ----------


def _candidates(
    note,
    stats: _Stats,
    cap: int,
    rare_tag_max_fraction: float,
) -> set[int]:
    """Union of candidate sources, deduplicated.

    1. Notes sharing a *rare* tag with the current note.
    2. Notes sharing any image with the current note.
    3. Notes matching rare note tokens, individually and in small pairs.

    The returned set may be larger than `cap`; final truncation happens after
    lightweight scoring so older-but-stronger matches are not discarded just
    because their note ids are lower.
    """
    cands: set[int] = set()
    rare_tag_ceiling = max(1, int(stats.total * rare_tag_max_fraction))
    gather_cap = max(cap * 4, cap)

    # Rarer tags first; never query for tags too common to discriminate.
    sorted_tags = sorted(
        (t for t in note.tags if stats.tag_counts.get(t, 0) > 0),
        key=lambda t: stats.tag_counts.get(t, 0),
    )
    for tag in sorted_tags[:5]:
        if stats.tag_counts.get(tag, 0) > rare_tag_ceiling:
            break  # any subsequent tags are even more common
        try:
            nids = mw.col.find_notes(f'tag:"{tag}"')
        except Exception:
            continue
        # 100 per tag keeps any single popular tag from monopolising the cap.
        cands.update(nids[:100])
        if len(cands) >= gather_cap:
            break

    # Image neighborhood — precise, cheap.
    for img in images_in_note(note):
        nids = notes_referencing(img)
        cands.update(nids)
        if len(cands) >= gather_cap:
            break

    # Rare tokens from anywhere in the note. Individual token searches improve
    # recall; rare pairs add precision when the note has enough signal.
    if note.fields:
        note_toks = tokens(note_text(note), min_len=_TFIDF_TOKEN_MIN_LEN)
        rare_toks = sorted(
            (t for t in note_toks
             if 0 < stats.token_counts.get(t, 0) < stats.total * 0.5),
            key=lambda t: stats.token_counts.get(t, 0),
        )[:6]
        for tok in rare_toks:
            try:
                nids = mw.col.find_notes(tok)
                cands.update(nids[:100])
            except Exception:
                pass
            if len(cands) >= gather_cap:
                break
        for i, left in enumerate(rare_toks[:4]):
            for right in rare_toks[i + 1:4]:
                try:
                    nids = mw.col.find_notes(f"{left} {right}")
                    cands.update(nids[:100])
                except Exception:
                    pass
                if len(cands) >= gather_cap:
                    break
            if len(cands) >= gather_cap:
                break

    cands.discard(note.id)
    return cands


def related_by_similar_notes(
    note,
    *,
    limit: int = 40,
    cap: int = 300,
    rare_tag_max_fraction: float = 0.2,
    weight_tag: float = 1.0,
    weight_image: float = 2.0,
    weight_text: float = 1.0,
) -> list[tuple[str, float]]:
    """Find images on notes deemed similar to `note`.

    Pipeline:
      1. Build a candidate set (rare-tag ∪ image-neighbour ∪
         rare-token search).
      2. Bulk-fetch fields+tags+sort field for all candidates in one SQL call.
      3. Score each candidate note with bounded tag/image/text components.
      4. Sum candidate scores into per-image scores, downrank common images,
         drop images already on the current note, and return top `limit`.
    """
    existing = set(list_image_files())
    suggestions = _similar_image_suggestions(
        note,
        existing,
        limit=limit,
        cap=cap,
        rare_tag_max_fraction=rare_tag_max_fraction,
        weight_tag=weight_tag,
        weight_image=weight_image,
        weight_text=weight_text,
    )
    return [(s.filename, s.score) for s in suggestions]


def _similar_image_suggestions(
    note,
    existing: set[str],
    *,
    limit: int,
    cap: int,
    rare_tag_max_fraction: float,
    weight_tag: float,
    weight_image: float,
    weight_text: float,
) -> list[RelatedImage]:
    stats = _stats()
    if stats.total == 0:
        return []

    cands = _candidates(
        note,
        stats,
        cap=cap,
        rare_tag_max_fraction=rare_tag_max_fraction,
    )
    if not cands:
        return []

    rows = _bulk_fetch_full(cands)
    cur_tags = set(note.tags)
    cur_imgs = set(images_in_note(note))
    cur_toks = tokens(note_text(note), min_len=_TFIDF_TOKEN_MIN_LEN)
    cur_vec = _tfidf_vector(cur_toks, stats)

    scored_notes: list[
        tuple[float, set[str], set[str], set[str], set[str], float]
    ] = []
    for _, tags_str, flds in rows:
        cand_tags = set((tags_str or "").split())
        shared_tags = cur_tags & cand_tags
        tag_score = min(
            1.0,
            sum(
                _bounded_idf(stats.tag_counts.get(t, 0), stats.total)
                for t in shared_tags
            ),
        )

        cand_imgs = set(_images_from_flds(flds or ""))
        shared_imgs = cur_imgs & cand_imgs
        img_score = (
            len(shared_imgs) / max(1, len(cur_imgs))
            if cur_imgs else 0.0
        )

        cand_toks = tokens(_flds_text(flds), min_len=_TFIDF_TOKEN_MIN_LEN)
        shared_toks = cur_toks & cand_toks
        cand_vec = _tfidf_vector(cand_toks, stats)
        text_score = _cosine(cur_vec, cand_vec)

        total = (
            weight_tag * tag_score
            + weight_image * img_score
            + weight_text * text_score
        )
        if total <= 0:
            continue
        scored_notes.append(
            (total, cand_imgs, shared_tags, shared_imgs, shared_toks, text_score)
        )

    scored_notes.sort(key=lambda row: row[0], reverse=True)
    scored_notes = scored_notes[:cap]

    image_scores: Counter[str] = Counter()
    note_counts: Counter[str] = Counter()
    reasons: dict[str, list[str]] = {}
    for (
        total,
        cand_imgs,
        shared_tags,
        shared_imgs,
        shared_toks,
        text_score,
    ) in scored_notes:
        for img in cand_imgs:
            if img in cur_imgs or img not in existing:
                continue
            image_ref_count = stats.image_counts.get(img, 0)
            rarity = _bounded_idf(image_ref_count, stats.total)
            # Common images still can be relevant, but they should not dominate
            # solely by appearing on many notes.
            rarity_factor = 0.1 + 0.9 * rarity * rarity
            image_scores[img] += total * rarity_factor
            note_counts[img] += 1

            img_reasons = reasons.setdefault(img, [])
            if shared_tags:
                reason = (
                    "Shared tags: "
                    + _fmt_names(
                        _top_terms_by_rarity(
                            shared_tags, stats.tag_counts, limit=3
                        )
                    )
                )
                if reason not in img_reasons:
                    img_reasons.append(reason)
            if shared_imgs:
                reason = "Appears near same image(s): " + _fmt_names(shared_imgs)
                if reason not in img_reasons:
                    img_reasons.append(reason)
            if text_score >= 0.05 and shared_toks:
                reason = (
                    "Text overlap: "
                    + _fmt_names(
                        _top_terms_by_rarity(
                            shared_toks, stats.token_counts, limit=4
                        ),
                        limit=4,
                    )
                )
                if reason not in img_reasons:
                    img_reasons.append(reason)
            if rarity < 0.3 and image_ref_count > 1:
                reason = f"Downranked because it is used on {image_ref_count} notes."
                if reason not in img_reasons:
                    img_reasons.append(reason)

    out: list[RelatedImage] = []
    for filename, score in image_scores.most_common(limit):
        why = [f"Used on {note_counts[filename]} similar note(s)."]
        why.extend(reasons.get(filename, [])[:5])
        out.append(RelatedImage(filename, score, tuple(why)))
    return out


def related_images(
    note,
    candidates: Iterable[str],
    *,
    min_len: int = 3,
    limit: int = 40,
    cap: int = 300,
    rare_tag_max_fraction: float = 0.2,
    weight_filename: float = 1.0,
    weight_tag: float = 1.0,
    weight_image: float = 2.0,
    weight_text: float = 1.0,
) -> list[RelatedImage]:
    """Rank images related to `note` using filename and similar-note signals."""
    stats = _stats()
    existing = set(candidates)
    cur_imgs = set(images_in_note(note))

    image_scores: Counter[str] = Counter()
    reasons: dict[str, list[str]] = {}

    for filename, (score, why) in _filename_suggestions(
        note,
        existing,
        stats,
        min_len=min_len,
        weight_filename=weight_filename,
    ).items():
        if filename in cur_imgs:
            continue
        image_scores[filename] += score
        reasons.setdefault(filename, []).extend(why)

    for suggestion in _similar_image_suggestions(
        note,
        existing,
        limit=max(limit * 3, limit),
        cap=cap,
        rare_tag_max_fraction=rare_tag_max_fraction,
        weight_tag=weight_tag,
        weight_image=weight_image,
        weight_text=weight_text,
    ):
        if suggestion.filename in cur_imgs:
            continue
        image_scores[suggestion.filename] += suggestion.score
        img_reasons = reasons.setdefault(suggestion.filename, [])
        for reason in suggestion.reasons:
            if reason not in img_reasons:
                img_reasons.append(reason)

    ranked = sorted(
        image_scores.items(),
        key=lambda item: (-item[1], item[0].lower()),
    )
    return [
        RelatedImage(filename, score, tuple(reasons.get(filename, [])[:6]))
        for filename, score in ranked[:limit]
    ]
