from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (QColor, QPalette, QPainter, QPen)
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout,
                               QFrame, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QMenu, QScrollArea, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from utils.settings import (DEFAULT_SETTINGS, get_settings,
                            get_tag_separator)

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
FOLDER_SORT_MODE_SETTINGS_KEY = 'tag_statistics_folder_sort_mode'
SORT_BY_COUNT = 'Count'
SORT_BY_PERCENTAGE = '% of Folder Images'

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
    folder_image_counts: Counter
    folder_tag_counts: Counter
    tag_variants: list
    tag_token_count: int | None
    average_caption_tokens_with_tag: float | None


def get_token_count(tokenizer, text: str) -> int:
    """Get the token count of a text, subtracting the start-of-text and
    end-of-text tokens."""
    return len(tokenizer(text).input_ids) - 2


def get_month_range_summary(month_counts: dict[str, int]):
    """Get the first month, last month and trend ('Rising', 'Falling' or
    'Steady') of a month count dictionary, or None if it is empty."""
    months = sorted(month_counts)
    if not months:
        return None
    first_month = months[0]
    last_month = months[-1]
    trend = 'Steady'
    if len(months) > 1:
        middle = len(months) // 2
        first_half_count = sum(month_counts[month]
                               for month in months[:middle])
        second_half_count = sum(month_counts[month]
                                for month in months[middle:])
        if second_half_count > first_half_count * 1.2:
            trend = 'Rising'
        elif first_half_count > second_half_count * 1.2:
            trend = 'Falling'
    return first_month, last_month, trend


def get_tag_variants(tag: str, tag_image_counts: Counter) -> list:
    """Find near-duplicate variants of a tag in a tag image count
    dictionary: case variants, underscore/space variants, and simple
    singular/plural variants."""
    normalized_tag = tag.lower().strip().replace(' ', '_')
    singular_tag = normalized_tag.rstrip('es') if normalized_tag.endswith(
        'es') else normalized_tag.rstrip('s')
    variants = []
    for other_tag, count in tag_image_counts.items():
        if other_tag == tag:
            continue
        normalized_other_tag = other_tag.lower().strip().replace(' ', '_')
        singular_other_tag = (normalized_other_tag.rstrip('es')
                              if normalized_other_tag.endswith('es')
                              else normalized_other_tag.rstrip('s'))
        if (normalized_other_tag == normalized_tag
                or singular_other_tag == singular_tag):
            variants.append((other_tag, count))
    return variants


def get_image_folder(image_path, directory_path) -> str:
    """Get the folder an image is in, relative to the loaded directory if
    possible."""
    folder_path = image_path.parent
    if directory_path is not None:
        try:
            folder_path = folder_path.relative_to(directory_path)
        except ValueError:
            pass
    return str(folder_path)


def count_image_files_in_folder(folder_path) -> int:
    """Count the image files in a folder according to the configured image
    file formats."""
    settings = get_settings()
    image_suffixes_string = settings.value(
        'image_list_file_formats',
        defaultValue=DEFAULT_SETTINGS['image_list_file_formats'], type=str)
    image_suffixes = {
        suffix.strip().lower().lstrip('.')
        for suffix in image_suffixes_string.split(',') if suffix.strip()}
    try:
        return sum(1 for entry in folder_path.iterdir()
                   if entry.is_file()
                   and entry.suffix.lower().lstrip('.') in image_suffixes)
    except OSError:
        return 0


def calculate_tag_statistics(tag: str, images: list, tokenizer=None,
                              directory_path=None,
                              count_folders_from_disk=False) -> TagStatistics:
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
    folder_image_counts = Counter()
    folder_tag_counts = Counter()
    for image in tag_images:
        folder_tag_counts[get_image_folder(image.path,
                                            directory_path)] += 1
    if count_folders_from_disk:
        # In tags-only mode, only the loaded images are known, so the folder
        # image counts are taken from the folders on disk instead.
        for folder in folder_tag_counts:
            if directory_path is None:
                folder_path = Path(folder)
            else:
                folder_path = directory_path / folder
            folder_image_counts[folder] = count_image_files_in_folder(
                folder_path)
    else:
        for image in images:
            folder_image_counts[get_image_folder(image.path,
                                                  directory_path)] += 1
    tag_variants = get_tag_variants(tag, tag_image_counts)
    tag_token_count = None
    average_caption_tokens_with_tag = None
    if tokenizer is not None and tag_images:
        tag_token_count = get_token_count(tokenizer, tag)
        tag_separator = get_tag_separator()
        caption_token_counts = [
            get_token_count(tokenizer, tag_separator.join(image.tags))
            for image in tag_images]
        average_caption_tokens_with_tag = (sum(caption_token_counts)
                                           / len(caption_token_counts))

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
        modification_month_counts=modification_month_counts,
        folder_image_counts=folder_image_counts,
        folder_tag_counts=folder_tag_counts,
        tag_variants=tag_variants,
        tag_token_count=tag_token_count,
        average_caption_tokens_with_tag=average_caption_tokens_with_tag)


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

    def __init__(self, tag: str, images: list, parent=None, tokenizer=None,
                 directory_path=None, count_folders_from_disk=False):
        super().__init__(parent)
        self.tag = tag
        self.statistics = calculate_tag_statistics(tag, images, tokenizer,
                                                   directory_path,
                                                   count_folders_from_disk)
        self.setWindowTitle(f'Tag Statistics: {tag}')
        self.setModal(True)

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setSpacing(20)

        header_label = QLabel(f'<h1>{tag}</h1>')
        content_layout.addWidget(header_label)

        for group in (self.create_summary_group(),
                      self.create_trend_group(),
                      self.create_co_occurrence_group(),
                      self.create_antagonistic_tags_group(),
                      self.create_tag_hygiene_group(),
                      self.create_folder_usage_group()):
            if group is not None:
                content_layout.addWidget(group)

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

    def finalize_table(self, table: QTableWidget):
        """Make a table show all of its rows at once, stretched to the
        window width, with the dialog's scroll area handling the scrolling."""
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        table.resizeColumnsToContents()
        table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.resizeRowsToContents()
        row_height = table.verticalHeader().defaultSectionSize()
        table.setMinimumHeight(table.horizontalHeader().height()
                                + row_height * table.rowCount() + 4)

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
        if statistics.tag_token_count is not None:
            form_layout.addRow('Tag token count:',
                              QLabel(str(statistics.tag_token_count)))
        if statistics.average_caption_tokens_with_tag is not None:
            form_layout.addRow(
                'Average caption tokens (images with tag):',
                QLabel(f'{statistics.average_caption_tokens_with_tag:.1f}'))
        for date_type, month_counts in (
                ('created', statistics.creation_month_counts),
                ('modified', statistics.modification_month_counts)):
            range_summary = get_month_range_summary(month_counts)
            if range_summary is None:
                continue
            first_month, last_month, trend = range_summary
            form_layout.addRow(f'First seen ({date_type}):',
                              QLabel(first_month))
            form_layout.addRow(f'Last seen ({date_type}):',
                              QLabel(last_month))
            form_layout.addRow(f'Trend ({date_type}):', QLabel(trend))
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
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(
            ['Tag', 'Count', '% of Images With Tag', '% of Co-Tag Images',
             'Lift'])
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
            reverse_percentage = (100 * count / co_tag_image_count
                                  if co_tag_image_count else 0)
            lift = 0.0
            if (tagged_image_count > 0 and tag_image_count > 0
                    and co_tag_image_count > 0):
                lift = ((count / tagged_image_count)
                        / ((tag_image_count / tagged_image_count)
                           * (co_tag_image_count / tagged_image_count)))
            values = (co_tag, str(count), f'{percentage:.1f}%',
                      f'{reverse_percentage:.1f}%', f'{lift:.2f}')
            for column_number, value in enumerate(values):
                table.setItem(row_number, column_number,
                              QTableWidgetItem(value))
        self.finalize_table(table)
        group_layout.addWidget(table)
        return group_box

    def create_antagonistic_tags_group(self) -> QGroupBox | None:
        """Create a group listing tags that co-occur with the tag less often
        than would be expected by chance, or None if there are none."""
        statistics = self.statistics
        antagonistic_tags = []
        tagged_image_count = statistics.tagged_image_count
        tag_image_count = statistics.tag_image_count
        if tagged_image_count > 0 and tag_image_count > 0:
            tag_probability = tag_image_count / tagged_image_count
            for other_tag, other_tag_image_count in (
                    statistics.tag_image_counts.items()):
                if other_tag == self.tag:
                    continue
                expected_count = (tag_probability * other_tag_image_count)
                if expected_count < 2:
                    continue
                actual_count = statistics.co_occurrence_counts[other_tag]
                if actual_count < expected_count:
                    antagonistic_tags.append((other_tag, actual_count,
                                              expected_count))
            antagonistic_tags.sort(key=lambda entry: (
                entry[1] / entry[2], entry[0]))
            antagonistic_tags = antagonistic_tags[:15]
        if not antagonistic_tags:
            return None
        group_box = QGroupBox('Antagonistic Tags (Less Common Together '
                              'Than Expected)')
        group_layout = QVBoxLayout(group_box)
        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(
            ['Tag', 'Count', 'Expected Count', 'Lift'])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setRowCount(len(antagonistic_tags))
        for row_number, (other_tag, actual_count,
                         expected_count) in enumerate(antagonistic_tags):
            lift = actual_count / expected_count
            values = (other_tag, str(actual_count), f'{expected_count:.1f}',
                      f'{lift:.2f}')
            for column_number, value in enumerate(values):
                table.setItem(row_number, column_number,
                              QTableWidgetItem(value))
        self.finalize_table(table)
        group_layout.addWidget(table)
        return group_box

    def create_tag_hygiene_group(self) -> QGroupBox | None:
        """Create a group listing near-duplicate variants of the tag, or
        None if there are none."""
        tag_variants = self.statistics.tag_variants
        if not tag_variants:
            return None
        group_box = QGroupBox('Tag Hygiene (Near-Duplicate Variants)')
        group_layout = QVBoxLayout(group_box)
        group_layout.addWidget(QLabel(
            'These near-duplicate variants of the tag also exist in the '
            'dataset and may be worth merging or renaming.'))
        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(['Variant', 'Count'])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setRowCount(len(tag_variants))
        for row_number, (variant, count) in enumerate(tag_variants):
            for column_number, value in enumerate((variant, str(count))):
                table.setItem(row_number, column_number,
                              QTableWidgetItem(value))
        self.finalize_table(table)
        group_layout.addWidget(table)
        return group_box

    def create_folder_usage_group(self) -> QGroupBox | None:
        """Create a group showing the tag usage per folder, or None if the
        tag does not appear in any folder."""
        statistics = self.statistics
        folder_tag_counts = statistics.folder_tag_counts
        if not folder_tag_counts:
            return None
        group_box = QGroupBox('Usage by Folder')
        group_layout = QVBoxLayout(group_box)
        table = QTableWidget()
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(
            ['Folder', 'Count', '% of Folder Images'])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        settings = get_settings()
        sort_mode = settings.value(FOLDER_SORT_MODE_SETTINGS_KEY,
                                   defaultValue=SORT_BY_COUNT, type=str)
        if sort_mode not in (SORT_BY_COUNT, SORT_BY_PERCENTAGE):
            sort_mode = SORT_BY_COUNT

        def get_folder_percentage(folder: str) -> float:
            folder_image_count = statistics.folder_image_counts[folder]
            if not folder_image_count:
                return 0
            return folder_tag_counts[folder] / folder_image_count

        def populate_table(sort_mode: str):
            if sort_mode == SORT_BY_PERCENTAGE:
                key = get_folder_percentage
            else:
                key = folder_tag_counts.get
            folders = sorted(folder_tag_counts, key=key, reverse=True)
            table.setRowCount(len(folders))
            for row_number, folder in enumerate(folders):
                tag_count = folder_tag_counts[folder]
                folder_image_count = statistics.folder_image_counts[folder]
                percentage = (100 * tag_count / folder_image_count
                              if folder_image_count else 0)
                values = (folder, str(tag_count), f'{percentage:.1f}%')
                for column_number, value in enumerate(values):
                    table.setItem(row_number, column_number,
                                  QTableWidgetItem(value))
            self.finalize_table(table)

        def handle_sort_mode_change(new_sort_mode: str):
            settings.setValue(FOLDER_SORT_MODE_SETTINGS_KEY, new_sort_mode)
            populate_table(new_sort_mode)

        sort_combo_box = QComboBox()
        sort_combo_box.addItems([SORT_BY_COUNT, SORT_BY_PERCENTAGE])
        sort_combo_box.setCurrentText(sort_mode)
        sort_combo_box.currentTextChanged.connect(handle_sort_mode_change)
        sort_layout = QHBoxLayout()
        sort_layout.addWidget(QLabel('Sort by:'))
        sort_layout.addWidget(sort_combo_box)
        sort_layout.addStretch()
        group_layout.addLayout(sort_layout)
        populate_table(sort_mode)
        group_layout.addWidget(table)
        return group_box
