from pathlib import Path

from PySide6.QtCore import QModelIndex, QSize, Qt, Slot
from PySide6.QtGui import (QImage, QImageReader, QMovie, QPixmap,
                           QResizeEvent)
from PySide6.QtWidgets import (QLabel, QSizePolicy, QStackedWidget,
                               QVBoxLayout, QWidget)

from models.proxy_image_list_model import ProxyImageListModel
from utils.image import Image
from utils.video import get_video_thumbnail, is_video_path
from widgets.video_player import VideoPlayerWidget


class ImageLabel(QLabel):
    def __init__(self):
        super().__init__()
        self.image_path = None
        self.movie = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        # This allows the label to shrink.
        self.setMinimumSize(QSize(1, 1))

    def resizeEvent(self, event: QResizeEvent):
        """Reload the image whenever the label is resized."""
        if self.image_path:
            self.load_image(self.image_path)

    def load_image(self, image_path: Path):
        self.image_path = image_path
        # Stop and discard any running movie before loading a new image.
        if self.movie is not None:
            self.movie.stop()
            self.setMovie(None)
            self.movie = None
        if image_path.suffix.lower() == '.gif':
            movie = QMovie(str(image_path))
            if movie.isValid() and movie.frameCount() > 1:
                self.movie = movie
                # Scale the movie to fit the label while preserving the
                # aspect ratio, matching how static images are scaled.
                target_size = self.size() * self.devicePixelRatio()
                movie_size = movie.scaledSize()
                if not movie_size.isEmpty():
                    scaled = movie_size.scaled(
                        target_size, Qt.AspectRatioMode.KeepAspectRatio)
                    movie.setScaledSize(scaled)
                self.setMovie(movie)
                movie.start()
                return
        if is_video_path(image_path):
            # Videos are shown as a static frame taken partway into the
            # video; QImageReader cannot read video files.
            thumbnail = get_video_thumbnail(image_path, 1920)
            if thumbnail is not None:
                thumbnail = thumbnail.convert('RGB')
                pixmap = QPixmap.fromImage(
                    QImage(thumbnail.tobytes(), thumbnail.width,
                           thumbnail.height, thumbnail.width * 3,
                           QImage.Format.Format_RGB888).copy())
                pixmap.setDevicePixelRatio(self.devicePixelRatio())
                pixmap = pixmap.scaled(
                    self.size() * pixmap.devicePixelRatio(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self.setPixmap(pixmap)
                return
        image_reader = QImageReader(str(image_path))
        # Rotate the image according to the orientation tag.
        image_reader.setAutoTransform(True)
        pixmap = QPixmap.fromImageReader(image_reader)
        pixmap.setDevicePixelRatio(self.devicePixelRatio())
        pixmap = pixmap.scaled(
            self.size() * pixmap.devicePixelRatio(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(pixmap)


class ImageViewer(QWidget):
    """Shows the selected image, or a video player for video files."""

    def __init__(self, proxy_image_list_model: ProxyImageListModel):
        super().__init__()
        self.proxy_image_list_model = proxy_image_list_model
        self.image_label = ImageLabel()
        self.video_player = VideoPlayerWidget()
        self.stacked_widget = QStackedWidget()
        self.stacked_widget.addWidget(self.image_label)
        self.stacked_widget.addWidget(self.video_player)
        QVBoxLayout(self).addWidget(self.stacked_widget)

    @Slot()
    def load_image(self, proxy_image_index: QModelIndex):
        # Clearing the selection (e.g. when reloading a directory) emits
        # currentChanged with an invalid index; keep showing the current
        # image in that case.
        if not proxy_image_index.isValid():
            return
        image: Image = self.proxy_image_list_model.data(
            proxy_image_index, Qt.ItemDataRole.UserRole)
        if is_video_path(image.path) and self.video_player.load_video(
                image.path):
            self.stacked_widget.setCurrentWidget(self.video_player)
            return
        # Images, and videos that the player cannot decode, are shown as a
        # static image (for videos, a frame taken partway into the video).
        self.video_player.unload_video()
        self.stacked_widget.setCurrentWidget(self.image_label)
        self.image_label.load_image(image.path)
