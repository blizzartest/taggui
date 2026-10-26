from pathlib import Path

from PySide6.QtCore import QModelIndex, QSize, Qt, Slot
from PySide6.QtGui import (QImage, QImageReader, QMovie, QPixmap,
                           QResizeEvent)
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from models.proxy_image_list_model import ProxyImageListModel
from utils.image import Image
from utils.video import get_video_thumbnail, is_video_path
from widgets.video_player import VideoPlayer


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

    def set_frame_pixmap(self, image: QImage):
        """Show a video frame from the player, scaled to fit the label."""
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(self.devicePixelRatio())
        pixmap = pixmap.scaled(
            self.size() * pixmap.devicePixelRatio(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(pixmap)

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
            # Videos are handled by the video player, which replaces this
            # label's static content once the first frame is decoded.
            # Until then, show a thumbnail so the user is not left with a
            # blank viewer while ffmpeg starts up.
            thumbnail = get_video_thumbnail(image_path, 1920)
            if thumbnail is not None:
                thumbnail = thumbnail.convert('RGB')
                image = QImage(thumbnail.tobytes(), thumbnail.width,
                               thumbnail.height, thumbnail.width * 3,
                               QImage.Format.Format_RGB888).copy()
                self.set_frame_pixmap(image)
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
    def __init__(self, proxy_image_list_model: ProxyImageListModel):
        super().__init__()
        self.proxy_image_list_model = proxy_image_list_model
        self.image_label = ImageLabel()
        self.video_player = VideoPlayer()
        self.video_player.frame_changed.connect(
            self.image_label.set_frame_pixmap)
        layout = QVBoxLayout(self)
        layout.addWidget(self.image_label)
        layout.addWidget(self.video_player.control_bar)

    @Slot()
    def load_image(self, proxy_image_index: QModelIndex):
        image: Image = self.proxy_image_list_model.data(
            proxy_image_index, Qt.ItemDataRole.UserRole)
        if is_video_path(image.path):
            self.image_label.load_image(image.path)
            self.video_player.load_video(image.path)
        else:
            self.video_player.close_video()
            self.image_label.load_image(image.path)
