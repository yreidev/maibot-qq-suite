"""照片存档：保存生成过的照片，供下一张参考（保持长相、延续场景），也可以把某一张设为定妆照。

目录结构（放在插件数据目录里，更新插件不会被覆盖）：
  reference.jpg / .png / .webp   定妆照：画机器人自己时总带上它，长相就能保持一致
  history/<时间>.jpg              最近生成的照片，只留最近 keep 张
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from .base import Picture

_REFERENCE = "reference"


class PhotoAlbum:
    def __init__(self, directory: Path, *, keep: int = 30, clock: Callable[[], float] = time.time) -> None:
        self._dir = directory
        self._history = directory / "history"
        self._keep = max(1, keep)
        self._clock = clock

    @staticmethod
    def _load(path: Path) -> Picture | None:
        mime = Picture.mime_of(path.suffix)
        if mime is None or not path.is_file():
            return None
        return Picture(path.read_bytes(), mime)

    def _photos(self) -> list[Path]:
        """历史照片，旧的在前（文件名就是时间）。"""
        if not self._history.is_dir():
            return []
        return sorted(p for p in self._history.iterdir() if Picture.mime_of(p.suffix))

    def save(self, picture: Picture) -> Path:
        self._history.mkdir(parents=True, exist_ok=True)
        now = self._clock()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
        path = self._history / f"{stamp}{picture.suffix}"
        path.write_bytes(picture.data)
        for old in self._photos()[: -self._keep]:
            old.unlink(missing_ok=True)
        return path

    @staticmethod
    def _taken_at(path: Path) -> float:
        """文件名里的拍摄时间（本地时间）。"""
        try:
            return time.mktime(time.strptime(path.stem[:15], "%Y%m%d-%H%M%S"))
        except ValueError:
            return path.stat().st_mtime

    def latest(self, *, max_age: float | None = None) -> Picture | None:
        photos = self._photos()
        if not photos:
            return None
        path = photos[-1]
        if max_age is not None and self._clock() - self._taken_at(path) > max_age:
            return None
        return self._load(path)

    def count_today(self) -> int:
        today = time.strftime("%Y%m%d", time.localtime(self._clock()))
        return sum(1 for p in self._photos() if p.name.startswith(today))

    def _reference_path(self) -> Path | None:
        for suffix in (".jpg", ".png", ".webp", ".jpeg"):
            path = self._dir / f"{_REFERENCE}{suffix}"
            if path.is_file():
                return path
        return None

    def reference(self) -> Picture | None:
        path = self._reference_path()
        return self._load(path) if path else None

    def recent_count(self) -> int:
        return len(self._photos())

    def set_reference(self, nth: int = 1) -> bool:
        """把倒数第 nth 张照片（1 = 最近一张）设为定妆照；没有这张时返回 False。"""
        photos = self._photos()
        if nth < 1 or nth > len(photos):
            return False
        picture = self._load(photos[-nth])
        if picture is None:
            return False
        self.clear_reference()
        (self._dir / f"{_REFERENCE}{picture.suffix}").write_bytes(picture.data)
        return True

    def set_reference_from_latest(self) -> bool:
        return self.set_reference(1)

    def clear_reference(self) -> bool:
        path = self._reference_path()
        if path is None:
            return False
        path.unlink()
        return True
