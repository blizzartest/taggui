from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (QColor, QPalette, QPainter, QPen)
from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QFrame,
                               QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QMenu, QScrollArea, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from utils.settings import get_settings

CHART_MARGIN_LEFT = 60
CHART_MARGIN_RIGHT = 30
CHART_MARGIN_TOP = 20
CHART_MARGIN_BOTTOM = 40
POINT_CLICK_RADIUS = 12
CREATION_COLOR = QColor('#4fc3f7')
MODIFICATION_COLOR = QColor('#ffb74d')
GEOMETRY_SETTINGS_KEY = 'tag_statistics_dialog_geometry'
SHOW_CREATION_SETTINGS_KEY = 'tag_statistics_show_creation'
SHOW_MODIFICATION_SETTINGS_KEY = 'tag_statistics_show_modification'

MONTH_FORMAT = '%Y-%m'


@dataclass
class TagStatistics:
    image_count: int
    tagged_image_count: int
    tag_image_count: int
    average_position: float | None
    average_tag_count_with_tag: float | None
    average_tag_count_without_tag: float | None
    co_occurrence_counts: Counter
    tag_image_counts: Counter
    creation_month_counts: dict[str, int]
    modification_month_counts: dict[str, int]


def calculate_tag_statistics(tag: str, images: list) -> TagStatistics:
    tagged_images = [image for image in images if image.tags]
    tag_images = [image for image in images if tag in image.tags]
    co_occurrence_counts = Counter()
    tag_image_counts = Counter()
    positions = []
    tag_count_with_tag = 0
    for image in tag_images:
        co_occurrence_counts.update(
            co_tag for co_tag in image.tags if co_tag != tag)
        positions.append(image.tags.index(tag) + 1)
        tag_count_with_tag += len(image.tags)
    tag_count_without_tag = sum(
        len(image.tags) for image in images
        if image.tags and tag not in image.tags)
    for image in tagged_images:
        tag_image_counts.update(set(image.tags))

    creation_month_counts = {}
    modification_month_counts = {}
    for image in tag_images:
        try:
            stat = image.path.stat()
        except OSError:
            continue
        # Creation time is not available on all platforms, so fall back to
        # the last modification time where it is missing.
        creation_time = getattr(stat, 'st_birthtime', stat.st_mtime)
        for timestamp, month_counts in (
                (creation_time, creation_month_counts),
                (stat.st_mtime, modification_month_counts)):
            if timestamp <= 0:
                continue
            month = datetime.fromtimestamp(timestamp).strftime(MONTH_FORMAT)
            month_counts[month] = month_counts.get(month, 0) + 1

    tagged_image_count = len(tagged_images)
    without_tag_count = tagged_image_count - len(tag_images)
    return TagStatistics(
        image_count=len(images),
        tagged_image_count=tagged_image_count,
        tag_image_count=len(tag_images),
        average_position=(sum(positions) / len(positions)
                          if positions else None),
        average_tag_count_with_tag=(tag_count_with_tag / len(tag_images)
                                    if tag_images else None),
        average_tag_count_without_tag=(tag_count_without_tag / without_tag_count
                                       if without_tag_count else None),
        co_occurrence_counts=co_occurrence_counts,
        tag_image_counts=tag_image_counts,
        creation_month_counts=creation_month_counts,
        modification_month_counts=modification_month_counts)


class TrendChart(QWidget):
    """
    A line chart showing the number of images containing the tag per month,
    with separate series for the creation and modification dates. Clicking a
    point emits `point_clicked` with the month and date type of the point.
    """

    point_clicked = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.months = []
        self.creation_counts = []
        self.modification_counts = []
        self.show_creation = True
        self.show_modification = True
        self.creation_points = []
        self.modification_points = []
        self.setMinimumHeight(300)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_data(self, months: list[str],
                 creation_counts: list[int],
                 modification_counts: list[int]):
        self.months = months
        self.creation_counts = creation_counts
        self.modification_counts = modification_counts
        self.update()

    def set_series_visibility(self, show_creation: bool,
                              show_modification: bool):
        self.show_creation = show_creation
        self.show_modification = show_modification
        self.update()

    def get_series_points(self, counts: list[int], chart_width: int,
                          chart_height: int, max_count: int) -> list[tuple]:
        points = []
        for month_number, count in enumerate(counts):
            if len(counts) > 1:
                x = (CHART_MARGIN_LEFT
                     + chart_width * month_number / (len(counts) - 1))
            else:
                x = CHART_MARGIN_LEFT + chart_width / 2
            y = (CHART_MARGIN_TOP + chart_height
                 - chart_height * count / max_count)
            points.append((x, y))
        return points

    def mousePressEvent(self, event):
        """Emit `point_clicked` for the nearest visible point, if any."""
        series = []
        if self.show_creation:
            series.append((self.creation_points, 'created'))
        if self.show_modification:
            series.append((self.modification_points, 'modified'))
        nearest = None
        nearest_distance = POINT_CLICK_RADIUS
        for points, date_type in series:
            for month_number, (x, y) in enumerate(points):
                distance = ((event.position().x() - x) ** 2
                            + (event.position().y() - y) ** 2) ** 0.5
                if distance < nearest_distance:
                    nearest_distance = distance
                    nearest = (month_number, date_type)
        if nearest is not None:
            month_number, date_type = nearest
            self.point_clicked.emit(self.months[month_number], date_type)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        chart_width = self.width() - CHART_MARGIN_LEFT - CHART_MARGIN_RIGHT
        chart_height = self.height() - CHART_MARGIN_TOP - CHART_MARGIN_BOTTOM
        if chart_width <= 0 or chart_height <= 0 or not self.months:
            painter.end()
            return
        max_count = 1
        if self.show_creation:
            max_count = max(max_count, max(self.creation_counts, default=1))
        if self.show_modification:
            max_count = max(max_count, max(self.modification_counts,
                                           default=1))
        # Round the maximum up to a nice value for the y-axis.
        max_count = ((max_count + 4) // 5) * 5
        pen = QPen()
        pen.setColor(self.palette().color(QPalette.ColorRole.WindowText))
        painter.setPen(pen)

        # Grid lines and y-axis labels.
        label_font = self.font()
        label_font.setPointSize(9)
        painter.setFont(label_font)
        grid_line_count = 5
        for line_number in range(grid_line_count + 1):
            y = (CHART_MARGIN_TOP + chart_height
                 - chart_height * line_number / grid_line_count)
            painter.drawLine(CHART_MARGIN_LEFT, y,
                             CHART_MARGIN_LEFT + chart_width, y)
            count = int(max_count * line_number / grid_line_count)
            painter.drawText(0, y - 10, CHART_MARGIN_LEFT - 10, 20,
                             Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter, str(count))

        # X-axis labels, showing at most ~10 labels so they do not overlap.
        label_count = min(10, len(self.months))
        if label_count > 0:
            step = max(1, len(self.months) // label_count)
            for month_number in range(0, len(self.months), step):
                x = (CHART_MARGIN_LEFT
                     + chart_width * month_number / max(1,
                                                        len(self.months) - 1)
                     if len(self.months) > 1
                     else CHART_MARGIN_LEFT + chart_width / 2)
                painter.drawText(int(x) - 40, CHART_MARGIN_TOP + chart_height,
                                 80, 20,
                                 Qt.AlignmentFlag.AlignHCenter
                                 | Qt.AlignmentFlag.AlignVCenter,
                                 self.months[month_number])

        def draw_series(counts: list[int], color: QColor):
            if not counts:
                return
            pen = QPen()
            pen.setColor(color)
            pen.setWidth(2)
            painter.setPen(pen)
            points = self.get_series_points(counts, chart_width, chart_height,
                                            max_count)
            for point_number in range(len(points) - 1):
                painter.drawLine(int(points[point_number][0]),
                                 int(points[point_number][1]),
                                 int(points[point_number + 1][0]),
                                 int(points[point_number + 1][1]))
            for x, y in points:
                painter.setBrush(color)
                painter.drawEllipse(int(x) - 3, int(y) - 3, 6, 6)

        self.creation_points = self.get_series_points(
            self.creation_counts, chart_width, chart_height, max_count)
        self.modification_points = self.get_series_points(
            self.modification_counts, chart_width, chart_height, max_count)
        if self.show_creation:
            draw_series(self.creation_counts, CREATION_COLOR)
        if self.show_modification:
            draw_series(self.modification_counts, MODIFICATION_COLOR)
        painter.end()


class CoOccurrenceTable(QTableWidget):
    """A table of co-occurring tags with a context menu for adding a tag to
    the image list search."""

    tag_addition_requested = Signal(str)

    def contextMenuEvent(self, event):
        clicked_row = self.rowAt(event.pos().y())
        if clicked_row < 0:
            return
        item = self.item(clicked_row, 0)
        if item is None:
            return
        tag = item.text()
        context_menu = QMenu(self)
        context_menu.addAction(
            'Add to Search', lambda: self.tag_addition_requested.emit(tag))
        context_menu.exec_(event.globalPos())


class TagStatisticsDialog(QDialog):
    """A fullscreen dialog showing statistics for a single tag."""

    CO_OCCURRENCE_TAG_COUNT = 50
    image_list_month_filter_requested = Signal(str, str, str)
    image_list_filter_addition_requested = Signal(str)

    def __init__(self, tag: str, images: list, parent=None):
        super().__init__(parent)
        self.tag = tag
        self.statistics = calculate_tag_statistics(tag, images)
        self.setWindowTitle(f'Tag Statistics: {tag}')
        self.setModal(True)

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setSpacing(20)

        header_label = QLabel(f'<h1>{tag}</h1>')
        content_layout.addWidget(header_label)

        content_layout.addWidget(self.create_summary_group())
        content_layout.addWidget(self.create_trend_group())
        content_layout.addWidget(self.create_co_occurrence_group())

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(content_widget)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll_area)

    def done(self, result):
        """Save the window geometry so that the last used size is restored
        the next time the dialog is opened."""
        get_settings().setValue(GEOMETRY_SETTINGS_KEY, self.saveGeometry())
        super().done(result)

    def restore_geometry(self) -> bool:
        """Restore the saved window geometry, returning whether it existed."""
        geometry = get_settings().value(GEOMETRY_SETTINGS_KEY)
        if geometry is None:
            return False
        self.restoreGeometry(geometry)
        return True

    def create_summary_group(self) -> QGroupBox:
        statistics = self.statistics
        group_box = QGroupBox('Summary')
        form_layout = QFormLayout(group_box)
        if statistics.tagged_image_count > 0:
            percentage = (100 * statistics.tag_image_count
                          / statistics.tagged_image_count)
            prevalence_text = (f'{statistics.tag_image_count} of '
                               f'{statistics.tagged_image_count} tagged '
                               f'images ({percentage:.1f}%)')
        else:
            prevalence_text = '0 of 0 tagged images'
        form_layout.addRow('Images with tag:', QLabel(prevalence_text))
        form_layout.addRow('Total images in folder:',
                           QLabel(str(statistics.image_count)))
        position_text = ('-' if statistics.average_position is None
                         else f'{statistics.average_position:.2f}')
        form_layout.addRow('Average tag position:',
                           QLabel(position_text))
        with_tag_text = ('-' if statistics.average_tag_count_with_tag is None
                         else f'{statistics.average_tag_count_with_tag:.2f}')
        form_layout.addRow('Average tag count (images with tag):',
                          QLabel(with_tag_text))
        without_tag_text = (
            '-' if statistics.average_tag_count_without_tag is None
            else f'{statistics.average_tag_count_without_tag:.2f}')
        form_layout.addRow('Average tag count (images without tag):',
                          QLabel(without_tag_text))
        return group_box

    def create_trend_group(self) -> QGroupBox:
        group_box = QGroupBox('Usage Over Time')
        group_layout = QVBoxLayout(group_box)
        statistics = self.statistics
        months = sorted(set(statistics.creation_month_counts)
                        | set(statistics.modification_month_counts))
        if not months:
            group_layout.addWidget(QLabel(
                'No date information is available for the images with this '
                'tag.'))
            return group_box
        creation_counts = [statistics.creation_month_counts.get(month, 0)
                           for month in months]
        modification_counts = [
            statistics.modification_month_counts.get(month, 0)
            for month in months]

        chart = TrendChart()
        chart.set_data(months, creation_counts, modification_counts)

        legend_layout = QHBoxLayout()
        settings = get_settings()
        creation_check_box = QCheckBox('Creation Date')
        creation_check_box.setChecked(settings.value(
            SHOW_CREATION_SETTINGS_KEY, defaultValue=True, type=bool))
        modification_check_box = QCheckBox('Modification Date')
        modification_check_box.setChecked(settings.value(
            SHOW_MODIFICATION_SETTINGS_KEY, defaultValue=True, type=bool))

        def handle_visibility_change():
            show_creation = creation_check_box.isChecked()
            show_modification = modification_check_box.isChecked()
            settings.setValue(SHOW_CREATION_SETTINGS_KEY, show_creation)
            settings.setValue(SHOW_MODIFICATION_SETTINGS_KEY,
                             show_modification)
            chart.set_series_visibility(show_creation, show_modification)

        creation_check_box.toggled.connect(handle_visibility_change)
        modification_check_box.toggled.connect(handle_visibility_change)
        chart.set_series_visibility(creation_check_box.isChecked(),
                                    modification_check_box.isChecked())
        chart.point_clicked.connect(self.handle_chart_point_clicked)

        creation_legend = QFrame()
        creation_legend.setFixedSize(16, 16)
        creation_legend.setStyleSheet(
            f'background-color: {CREATION_COLOR.name()}; border: none;')
        modification_legend = QFrame()
        modification_legend.setFixedSize(16, 16)
        modification_legend.setStyleSheet(
            f'background-color: {MODIFICATION_COLOR.name()}; border: none;')
        legend_layout.addWidget(creation_legend)
        legend_layout.addWidget(creation_check_box)
        legend_layout.addSpacing(20)
        legend_layout.addWidget(modification_legend)
        legend_layout.addWidget(modification_check_box)
        legend_layout.addStretch()

        group_layout.addLayout(legend_layout)
        group_layout.addWidget(chart)
        hint_label = QLabel('Click a point to filter the image list for '
                            'images with this tag from that month.')
        group_layout.addWidget(hint_label)
        return group_box

    def handle_chart_point_clicked(self, month: str, date_type: str):
        self.image_list_month_filter_requested.emit(self.tag, month, date_type)
        self.accept()

    def create_co_occurrence_group(self) -> QGroupBox:
        group_box = QGroupBox('Most Associated Tags')
        group_layout = QVBoxLayout(group_box)
        statistics = self.statistics
        if not statistics.co_occurrence_counts:
            group_layout.addWidget(QLabel('No other tags co-occur with this '
                                          'tag.'))
            return group_box
        table = CoOccurrenceTable()
        table.tag_addition_requested.connect(
            self.image_list_filter_addition_requested.emit)
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(
            ['Tag', 'Count', '% of Images With Tag', 'Lift'])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setRowCount(min(self.CO_OCCURRENCE_TAG_COUNT,
                              len(statistics.co_occurrence_counts)))
        tagged_image_count = statistics.tagged_image_count
        tag_image_count = statistics.tag_image_count
        top_co_tags = statistics.co_occurrence_counts.most_common(
            self.CO_OCCURRENCE_TAG_COUNT)
        for row_number, (co_tag, count) in enumerate(top_co_tags):
            percentage = (100 * count / tag_image_count
                          if tag_image_count else 0)
            # Lift is the co-occurrence frequency relative to the frequency
            # expected if the tags were independent.
            co_tag_image_count = statistics.tag_image_counts[co_tag]
            lift = 0.0
            if (tagged_image_count > 0 and tag_image_count > 0
                    and co_tag_image_count > 0):
                lift = ((count / tagged_image_count)
                        / ((tag_image_count / tagged_image_count)
                           * (co_tag_image_count / tagged_image_count)))
            values = (co_tag, str(count), f'{percentage:.1f}%', f'{lift:.2f}')
            for column_number, value in enumerate(values):
                table.setItem(row_number, column_number,
                              QTableWidgetItem(value))
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        group_layout.addWidget(table)
        return group_box
