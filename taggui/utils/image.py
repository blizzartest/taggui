from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QIcon

if TYPE_CHECKING:
    from PySide6.QtGui import QMovie


@dataclass
class Image:
    path: Path
    dimensions: tuple[int, int] | None
    tags: list[str] = field(default_factory=list)
    thumbnail: QIcon | None = None
    movie: 'QMovie | None' = None
    is_animated: bool = False
