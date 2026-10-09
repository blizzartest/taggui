import operator
import random
import re
from fnmatch import translate
from functools import lru_cache

import imagesize

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt
from transformers import PreTrainedTokenizerBase

from models.image_list_model import ImageListModel
from utils.image import Image


@lru_cache(maxsize=1024)
def get_filter_matcher(pattern: str):
    """Compile a glob pattern once so that it can be reused for every image
    while a filter is active."""
    return re.compile(translate(pattern)).match


class ProxyImageListModel(QSortFilterProxyModel):
    def __init__(self, image_list_model: ImageListModel,
                 tokenizer: PreTrainedTokenizerBase, tag_separator: str):
        super().__init__()
        self.setSourceModel(image_list_model)
        self.tokenizer = tokenizer
        self.tag_separator = tag_separator
        self.filter: list | None = None
        self.sort_mode = 'Name'
        self.reverse_sort = False
        self.creation_times: dict[str, float] = {}
        self.modification_times: dict[str, float] = {}
        self.random_keys: dict[str, float] = {}
        self.dimensions: dict[str, tuple[int, int] | None] = {}
        self.caption_cache: dict[int, tuple] = {}
        self.token_count_cache: dict[int, tuple] = {}

    def set_sort_mode(self, sort_mode: str):
        self.sort_mode = sort_mode
        self.creation_times.clear()
        self.modification_times.clear()
        if sort_mode == 'Name':
            # Restore the source model order (sorted by name) unless the sort
            # is reversed, in which case `lessThan` handles the ordering.
            if self.reverse_sort:
                self.sort(0, Qt.SortOrder.AscendingOrder)
            else:
                self.sort(-1, Qt.SortOrder.AscendingOrder)
        else:
            self.sort(0, Qt.SortOrder.AscendingOrder)
        # `QSortFilterProxyModel.sort()` does nothing when it is called with
        # the same column and sort order as before (e.g. when switching
        # between two non-name sort modes), so the sort must be re-applied
        # explicitly.
        self.invalidate()

    def set_reverse_sort(self, reverse_sort: bool):
        self.reverse_sort = reverse_sort
        # Re-apply the current sort to reflect the new direction.
        self.set_sort_mode(self.sort_mode)

    def get_creation_time(self, image: Image) -> float:
        path_string = str(image.path)
        if path_string not in self.creation_times:
            try:
                stat = image.path.stat()
                # Creation time is not available on all platforms, so fall
                # back to the last modification time where it is missing.
                self.creation_times[path_string] = getattr(
                    stat, 'st_birthtime', stat.st_mtime)
            except OSError:
                self.creation_times[path_string] = 0
        return self.creation_times[path_string]

    def get_modification_time(self, image: Image) -> float:
        path_string = str(image.path)
        if path_string not in self.modification_times:
            try:
                self.modification_times[path_string] = image.path.stat().st_mtime
            except OSError:
                self.modification_times[path_string] = 0
        return self.modification_times[path_string]

    def get_dimensions(self, image: Image) -> tuple[int, int] | None:
        """
        Get the dimensions of an image, reading them from the image file if
        they are not loaded yet (as is the case in tags-only mode). Reading
        only the image header is fast, and the result is cached.
        """
        if image.dimensions is not None:
            return image.dimensions
        path_string = str(image.path)
        if path_string not in self.dimensions:
            try:
                # `imagesize.get()` returns `(-1, -1)` when the dimensions
                # cannot be read.
                width, height = imagesize.get(image.path)
                self.dimensions[path_string] = ((width, height)
                                                if width > 0 and height > 0
                                                else None)
            except (OSError, ValueError):
                self.dimensions[path_string] = None
        return self.dimensions[path_string]

    def get_random_key(self, image: Image) -> float:
        """
        Get a stable random key for an image so that the image order does not
        change every time the view is re-sorted.
        """
        path_string = str(image.path)
        if path_string not in self.random_keys:
            self.random_keys[path_string] = random.random()
        return self.random_keys[path_string]

    def get_sort_key(self, image: Image):
        # The path is used as a tiebreaker to keep the order stable.
        if self.sort_mode == 'Date created':
            return self.get_creation_time(image), image.path
        if self.sort_mode == 'Date modified':
            return self.get_modification_time(image), image.path
        if self.sort_mode == 'Tag count':
            return len(image.tags), image.path
        if self.sort_mode == 'Dimensions':
            # Sort by the total number of pixels (area), with images of
            # unknown dimensions placed first.
            dimensions = self.get_dimensions(image)
            if dimensions is None:
                return -1, image.path
            width, height = dimensions
            return width * height, image.path
        if self.sort_mode == 'Aspect ratio':
            # Sort by the width-to-height ratio, from tall to wide, with
            # images of unknown dimensions placed first.
            dimensions = self.get_dimensions(image)
            if dimensions is None:
                return -1.0, image.path
            width, height = dimensions
            return width / height, image.path
        if self.sort_mode == 'Random':
            return self.get_random_key(image), image.path
        return image.path

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        if self.sort_mode == 'Name' and not self.reverse_sort:
            return False
        left_image: Image = left.data(Qt.ItemDataRole.UserRole)
        right_image: Image = right.data(Qt.ItemDataRole.UserRole)
        if left_image is None or right_image is None:
            return False
        left_key = self.get_sort_key(left_image)
        right_key = self.get_sort_key(right_image)
        if self.reverse_sort:
            return right_key < left_key
        return left_key < right_key

    def get_image_caption(self, image: Image) -> str:
        """Get the caption of an image, caching the result so that repeated
        filter evaluations do not rejoin the tags for every image. The image
        object is stored with the caption so that a recycled id of a garbage
        collected image is never mistaken for a cache hit."""
        image_id = id(image)
        cached = self.caption_cache.get(image_id)
        if cached is not None and cached[0] is image:
            return cached[1]
        caption = self.tag_separator.join(image.tags)
        self.caption_cache[image_id] = (image, caption)
        return caption

    def get_image_token_count(self, image: Image) -> int:
        """Get the token count of an image's caption, caching the result."""
        image_id = id(image)
        cached = self.token_count_cache.get(image_id)
        if cached is not None and cached[0] is image:
            return cached[1]
        caption = self.get_image_caption(image)
        # Subtract 2 for the start-of-text and end-of-text tokens.
        token_count = len(self.tokenizer(caption).input_ids) - 2
        self.token_count_cache[image_id] = (image, token_count)
        return token_count

    def clear_image_caches(self):
        self.caption_cache.clear()
        self.token_count_cache.clear()

    def does_image_match_filter(self, image: Image,
                                filter_: list | str) -> bool:
        if isinstance(filter_, str):
            if filter_.lower() == 'untagged':
                return not image.tags
            caption_matcher = get_filter_matcher(f'*{filter_}*')
            return (caption_matcher(self.get_image_caption(image))
                    or caption_matcher(str(image.path)))
        if len(filter_) == 1:
            return self.does_image_match_filter(image, filter_[0])
        if len(filter_) == 2:
            if filter_[0] == 'NOT':
                return not self.does_image_match_filter(image, filter_[1])
            if filter_[0] == 'tag':
                tag_matcher = get_filter_matcher(filter_[1])
                return any(tag_matcher(tag) for tag in image.tags)
            if filter_[0] == 'caption':
                caption_matcher = get_filter_matcher(f'*{filter_[1]}*')
                return caption_matcher(self.get_image_caption(image))
            if filter_[0] == 'name':
                name_matcher = get_filter_matcher(f'*{filter_[1]}*')
                return name_matcher(image.path.name)
            if filter_[0] == 'path':
                path_matcher = get_filter_matcher(f'*{filter_[1]}*')
                return path_matcher(str(image.path))
        if filter_[1] == 'AND':
            return (self.does_image_match_filter(image, filter_[0])
                    and self.does_image_match_filter(image, filter_[2:]))
        if filter_[1] == 'OR':
            return (self.does_image_match_filter(image, filter_[0])
                    or self.does_image_match_filter(image, filter_[2:]))
        comparison_operators = {
            '=': operator.eq,
            '==': operator.eq,
            '!=': operator.ne,
            '<': operator.lt,
            '>': operator.gt,
            '<=': operator.le,
            '>=': operator.ge
        }
        comparison_operator = comparison_operators[filter_[1]]
        number_to_compare = None
        if filter_[0] == 'tags':
            number_to_compare = len(image.tags)
        elif filter_[0] == 'chars':
            number_to_compare = len(self.get_image_caption(image))
        elif filter_[0] == 'tokens':
            number_to_compare = self.get_image_token_count(image)
        return comparison_operator(number_to_compare, int(filter_[2]))

    def filterAcceptsRow(self, source_row: int,
                         source_parent: QModelIndex) -> bool:
        # Show all images if there is no filter.
        if self.filter is None:
            return True
        image_index = self.sourceModel().index(source_row, 0)
        image: Image = self.sourceModel().data(image_index,
                                               Qt.ItemDataRole.UserRole)
        return self.does_image_match_filter(image, self.filter)

    def is_image_in_filtered_images(self, image: Image) -> bool:
        return (self.filter is None
                or self.does_image_match_filter(image, self.filter))
