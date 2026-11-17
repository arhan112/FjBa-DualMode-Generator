
# =========================================================
# DUALMODE POINT / LINE GENERATOR PRO
# Dual Mode: Attribute Points OR Line Endpoint Houses
# FjBa Metallic Logo + Neon UI + Light Mode + QgsTask Engine
# QGIS 3.x
# =========================================================

import math
import traceback

from qgis.PyQt.QtCore import Qt, QTimer, QByteArray, QVariant
from qgis.PyQt.QtGui import (
    QPainter,
    QPainterPath,
    QFont,
    QLinearGradient,
    QColor,
    QPen
)
from qgis.PyQt.QtWidgets import (
    QWidget,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QProgressBar,
    QMessageBox,
    QCheckBox,
    QButtonGroup,
    QApplication
)

from qgis.core import (
    QgsApplication,
    QgsTask,
    QgsProject,
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes
)


# =========================================================
# CONFIGURATION
# =========================================================

OUTPUT_LAYER_NAME = "generated_output"

# Maximum number of generated output features.
MAX_OUTPUT_FEATURES = 1000000

# Number of output features inserted per provider batch.
OUTPUT_BATCH_SIZE = 5000


# =========================================================
# FjBa METALLIC ANIMATED LOGO
# Original logo drawing and colours preserved.
# =========================================================

class FjBaMetallicLogo(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)

        self.phase = 0.0

        self.setMinimumSize(70, 70)
        self.setMaximumSize(90, 90)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.animate)
        self.timer.start(35)

    def animate(self):
        self.phase = (self.phase + 0.018) % 1.5
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect()

        font = QFont("Segoe UI", 27, QFont.Bold)
        painter.setFont(font)

        metrics = painter.fontMetrics()
        text = "FjBa"

        text_width = metrics.horizontalAdvance(text)

        x = (rect.width() - text_width) / 2
        y = rect.height() * 0.62

        path = QPainterPath()
        path.addText(x, y, font, text)

        gradient = QLinearGradient(
            rect.left(), rect.top(),
            rect.right(), rect.bottom()
        )

        gradient.setColorAt(0.0, QColor("#FFF2B0"))
        gradient.setColorAt(0.22, QColor("#D5A832"))
        gradient.setColorAt(0.45, QColor("#F9E7A0"))
        gradient.setColorAt(0.68, QColor("#16A879"))
        gradient.setColorAt(1.0, QColor("#075C43"))

        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawPath(path)

        outline = QPen(QColor("#B68A28"))
        outline.setWidthF(0.8)

        painter.setPen(outline)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

        sweep_x = self.phase * rect.width()

        sweep = QLinearGradient(
            sweep_x - 12, 0,
            sweep_x + 12, 0
        )

        sweep.setColorAt(0.0, QColor(255, 255, 255, 0))
        sweep.setColorAt(0.5, QColor(255, 255, 255, 180))
        sweep.setColorAt(1.0, QColor(255, 255, 255, 0))

        painter.save()
        painter.setClipPath(path)
        painter.fillRect(rect, sweep)
        painter.restore()

    def stop(self):
        self.timer.stop()


# =========================================================
# NUMERIC FIELD VALIDATION
# =========================================================

def is_numeric_field(field):
    """
    Determine whether a QgsField is numeric.
    Uses QVariant types and field type names for compatibility.
    """

    numeric_types = {
        QVariant.Int,
        QVariant.UInt,
        QVariant.LongLong,
        QVariant.ULongLong,
        QVariant.Double
    }

    try:
        field_type = field.type()
    except (TypeError, ValueError, AttributeError):
        field_type = None

    if field_type in numeric_types:
        return True

    try:
        type_name = str(field.typeName()).lower()
    except (TypeError, ValueError, AttributeError):
        type_name = ""

    numeric_names = (
        "int",
        "integer",
        "long",
        "double",
        "real",
        "float",
        "numeric",
        "decimal",
        "number"
    )

    return any(name in type_name for name in numeric_names)


# =========================================================
# GEOMETRY HELPERS
# =========================================================

def extract_point_components(geometry):
    """
    Return point components from single or multipart point geometry.
    """

    if geometry.isEmpty() or geometry.isNull():
        return []

    if geometry.isMultipart():
        points = geometry.asMultiPoint()
        return [QgsPointXY(point) for point in points]

    point = geometry.asPoint()

    if point.isEmpty():
        return []

    return [QgsPointXY(point)]


def extract_line_parts(geometry):
    """
    Return line components from single or multipart line geometry.
    Curved geometries are converted to straight segments in place.
    """

    if geometry.isEmpty() or geometry.isNull():
        return []

    # Convert curved geometry to straight segments where necessary.
    try:
        geometry.convertToStraightSegment()
    except (TypeError, ValueError, AttributeError) as error:
        raise RuntimeError(
            f"Could not convert curved geometry to straight segments: {error}"
        ) from error

    if geometry.isMultipart():
        lines = geometry.asMultiPolyline()
    else:
        line = geometry.asPolyline()
        lines = [line] if line else []

    result = []

    for line in lines:
        if len(line) >= 2:
            result.append([QgsPointXY(point) for point in line])

    return result


def parse_feature_count(value):
    """
    Validate a Point Mode count.

    Accepted:
        0
        1
        2
        10.0

    Rejected:
        Negative numbers
        Non-numeric values
        NaN
        Infinity
        Fractional numbers
        Empty values
    """

    if value is None:
        return None

    try:
        if isinstance(value, bool):
            return None

        count = float(value)

        if not math.isfinite(count):
            return None

        if count < 0:
            return None

        if not count.is_integer():
            return None

        return int(count)

    except (TypeError, ValueError, OverflowError):
        return None


# =========================================================
# BACKGROUND TASK ENGINE
# =========================================================

class DualGeneratorTask(QgsTask):

    def __init__(
        self,
        source_features,
        mode,
        field_index,
        dialog
    ):

        super().__init__(
            "DualMode Point Generator",
            QgsTask.CanCancel
        )

        # These are detached snapshots.
        # No live QgsVectorLayer or QgsFeature is used in run().
        self.source_features = source_features

        self.mode = mode
        self.field_index = field_index

        # Used only by finished(), which runs on the main thread.
        self.dialog = dialog

        self.output_records = []

        self.exception = None
        self.traceback_text = None

        self.invalid_count_features = 0
        self.empty_geometries = 0
        self.processed_features = 0

    # =====================================================
    # TASK RUN
    # =====================================================

    def run(self):

        try:

            total = len(self.source_features)

            if total == 0:
                self.setProgress(100)
                return True

            for index, snapshot in enumerate(self.source_features):

                if self.isCanceled():
                    return False

                wkb, attributes = snapshot

                self.processed_features += 1

                # -----------------------------------------
                # Restore detached geometry from WKB.
                # -----------------------------------------

                if not wkb:
                    self.empty_geometries += 1
                    continue

                geometry = QgsGeometry()

                geometry.fromWkb(QByteArray(wkb))

                if geometry.isNull() or geometry.isEmpty():
                    self.empty_geometries += 1
                    continue

                # =========================================
                # MODE 1: POINT MULTIPLICATION
                # =========================================

                if self.mode == "point":

                    if self.field_index is None:
                        continue

                    try:
                        raw_count = attributes[self.field_index]
                    except (IndexError, TypeError):
                        self.invalid_count_features += 1
                        continue

                    count = parse_feature_count(raw_count)

                    if count is None:
                        self.invalid_count_features += 1
                        continue

                    if count == 0:
                        continue

                    points = extract_point_components(geometry)

                    if not points:
                        continue

                    required = len(points) * count

                    if len(self.output_records) + required > MAX_OUTPUT_FEATURES:
                        self.exception = RuntimeError(
                            "The maximum output limit of "
                            f"{MAX_OUTPUT_FEATURES:,} features "
                            "would be exceeded."
                        )
                        return False

                    for point in points:

                        if self.isCanceled():
                            return False

                        for _ in range(count):

                            self.output_records.append(
                                (
                                    point.x(),
                                    point.y(),
                                    attributes
                                )
                            )

                # =========================================
                # MODE 2: LINE ENDPOINT GENERATION
                # =========================================

                elif self.mode == "line":

                    lines = extract_line_parts(geometry)

                    required = len(lines) * 2

                    if len(self.output_records) + required > MAX_OUTPUT_FEATURES:
                        self.exception = RuntimeError(
                            "The maximum output limit of "
                            f"{MAX_OUTPUT_FEATURES:,} features "
                            "would be exceeded."
                        )
                        return False

                    for line in lines:

                        if self.isCanceled():
                            return False

                        start_point = line[0]
                        end_point = line[-1]

                        # First endpoint.
                        self.output_records.append(
                            (
                                start_point.x(),
                                start_point.y(),
                                attributes
                            )
                        )

                        # Second endpoint.
                        self.output_records.append(
                            (
                                end_point.x(),
                                end_point.y(),
                                attributes
                            )
                        )

                # -----------------------------------------
                # PROGRESS
                # -----------------------------------------

                self.setProgress(
                    int((index + 1) / total * 100)
                )

            self.setProgress(100)

            return True

        except Exception as error:

            self.exception = error
            self.traceback_text = traceback.format_exc()

            return False

    # =====================================================
    # FINISHED
    # Main-thread output creation happens in the dialog.
    # =====================================================

    def finished(self, result):

        self.dialog.task_finished(self, result)


# =========================================================
# MAIN GUI
# =========================================================

class DualGeneratorDialog(QDialog):

    def __init__(self, parent=None):

        super().__init__(parent)

        self.task = None

        self.dark_mode = True

        self.close_when_finished = False

        self.output_fields = None
        self.output_crs = None

        self.setWindowTitle("DualMode Point Generator")
        self.resize(650, 450)

        self.setMinimumWidth(580)

        # -------------------------------------------------
        # MAIN LAYOUT
        # -------------------------------------------------

        main_layout = QVBoxLayout()
        main_layout.setSpacing(9)

        # =================================================
        # HEADER: FjBa LOGO AT TOP-LEFT
        # =================================================

        header = QHBoxLayout()
        header.setSpacing(10)

        self.logo = FjBaMetallicLogo()

        header.addWidget(
            self.logo,
            0,
            Qt.AlignLeft | Qt.AlignVCenter
        )

        title = QLabel("DualMode Point Generator")

        title.setStyleSheet(
            "font-size:22px;font-weight:bold;"
        )

        header.addWidget(
            title,
            0,
            Qt.AlignVCenter
        )

        header.addStretch()

        main_layout.addLayout(header)

        # =================================================
        # INPUT LAYER
        # =================================================

        main_layout.addWidget(QLabel("Input Layer"))

        self.layer_combo = QComboBox()

        main_layout.addWidget(self.layer_combo)

        # =================================================
        # MODE SELECTION
        # =================================================

        main_layout.addWidget(QLabel("Mode"))

        self.mode_point = QCheckBox(
            "Point mode (attribute → copies)"
        )

        self.mode_line = QCheckBox(
            "Line mode (house endpoints)"
        )

        self.mode_point.setChecked(True)

        # Make the two checkboxes mutually exclusive.
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)

        self.mode_group.addButton(self.mode_point)
        self.mode_group.addButton(self.mode_line)

        main_layout.addWidget(self.mode_point)
        main_layout.addWidget(self.mode_line)

        # =================================================
        # FIELD SELECTION
        # =================================================

        main_layout.addWidget(
            QLabel("Field (only for Point Mode)")
        )

        self.field_combo = QComboBox()

        main_layout.addWidget(self.field_combo)

        # =================================================
        # STATUS LABEL
        # =================================================

        self.status_label = QLabel(
            "Ready. Select an input layer and generation mode."
        )

        self.status_label.setWordWrap(True)

        main_layout.addWidget(self.status_label)

        # =================================================
        # PROGRESS BAR
        # =================================================

        self.progress = QProgressBar()
        self.progress.setValue(0)

        main_layout.addWidget(self.progress)

        # =================================================
        # RUN BUTTON
        # =================================================

        self.run_btn = QPushButton("RUN GENERATOR")

        self.run_btn.setMinimumHeight(38)

        main_layout.addWidget(self.run_btn)

        # =================================================
        # THEME BUTTON
        # =================================================

        self.theme_btn = QPushButton("🌙 Toggle Theme")

        main_layout.addWidget(self.theme_btn)

        self.setLayout(main_layout)

        # =================================================
        # SIGNALS
        # =================================================

        self.layer_combo.currentIndexChanged.connect(
            self.load_fields
        )

        self.mode_point.toggled.connect(
            self.update_mode_controls
        )

        self.mode_line.toggled.connect(
            self.update_mode_controls
        )

        self.run_btn.clicked.connect(
            self.run_task
        )

        self.theme_btn.clicked.connect(
            self.toggle_theme
        )

        # =================================================
        # INITIALIZE
        # =================================================

        self.load_layers()

        self.apply_dark_theme()

        self.update_mode_controls()

    # =====================================================
    # LOAD PROJECT LAYERS
    # =====================================================

    def load_layers(self):

        self.layer_combo.clear()

        project = QgsProject.instance()

        for layer in project.mapLayers().values():

            if not isinstance(layer, QgsVectorLayer):
                continue

            if not layer.isValid():
                continue

            self.layer_combo.addItem(
                layer.name(),
                layer.id()
            )

        self.load_fields()

    # =====================================================
    # GET CURRENT LAYER
    # =====================================================

    def get_layer(self):

        layer_id = self.layer_combo.currentData()

        if not layer_id:
            return None

        layer = QgsProject.instance().mapLayer(layer_id)

        if not isinstance(layer, QgsVectorLayer):
            return None

        if not layer.isValid():
            return None

        return layer

    # =====================================================
    # LOAD NUMERIC FIELDS
    # =====================================================

    def load_fields(self, *_args):

        self.field_combo.clear()

        layer = self.get_layer()

        if not layer:
            self.update_mode_controls()
            return

        for index, field in enumerate(layer.fields()):

            if is_numeric_field(field):

                self.field_combo.addItem(
                    field.name(),
                    index
                )

        self.update_mode_controls()

    # =====================================================
    # MODE VALIDATION
    # =====================================================

    def update_mode_controls(self, *_args):

        layer = self.get_layer()

        if self.mode_point.isChecked():

            self.field_combo.setEnabled(True)

            if self.field_combo.count() == 0:
                self.status_label.setText(
                    "Point Mode requires a numeric attribute field."
                )

            elif layer:
                self.status_label.setText(
                    "Point Mode: each point receives the number "
                    "of copies specified by its selected field."
                )

        elif self.mode_line.isChecked():

            self.field_combo.setEnabled(False)

            self.status_label.setText(
                "Line Mode: creates two endpoint points "
                "for each line part."
            )

        self.validate_current_selection()

    # =====================================================
    # VALIDATE CURRENT SELECTION
    # =====================================================

    def validate_current_selection(self):

        layer = self.get_layer()

        if not layer:

            self.run_btn.setEnabled(False)

            return

        if self.task is not None:

            self.run_btn.setEnabled(False)

            return

        if self.mode_point.isChecked():

            if self.field_combo.count() == 0:

                self.run_btn.setEnabled(False)

                return

            geometry_type = QgsWkbTypes.geometryType(
                layer.wkbType()
            )

            if geometry_type != QgsWkbTypes.PointGeometry:

                self.run_btn.setEnabled(False)

                self.status_label.setText(
                    "Point Mode requires a point geometry layer."
                )

                return

        elif self.mode_line.isChecked():

            geometry_type = QgsWkbTypes.geometryType(
                layer.wkbType()
            )

            if geometry_type != QgsWkbTypes.LineGeometry:

                self.run_btn.setEnabled(False)

                self.status_label.setText(
                    "Line Mode requires a line geometry layer."
                )

                return

        else:

            self.run_btn.setEnabled(False)

            return

        self.run_btn.setEnabled(True)

    # =====================================================
    # SNAPSHOT INPUT DATA
    # Main thread only.
    # =====================================================

    def create_source_snapshot(self, layer):

        snapshots = []

        for feature in layer.getFeatures():

            geometry = feature.geometry()

            if geometry is None or geometry.isNull():
                wkb = b""
            else:
                wkb = bytes(geometry.asWkb())

            attributes = list(feature.attributes())

            snapshots.append(
                (
                    wkb,
                    attributes
                )
            )

        return snapshots

    # =====================================================
    # RUN GENERATOR
    # =====================================================

    def run_task(self):

        if self.task is not None:
            return

        layer = self.get_layer()

        if not layer:

            QMessageBox.warning(
                self,
                "Input Error",
                "Please select a valid vector layer."
            )

            return

        # -------------------------------------------------
        # Determine mode.
        # -------------------------------------------------

        if self.mode_point.isChecked():

            mode = "point"

            geometry_type = QgsWkbTypes.geometryType(
                layer.wkbType()
            )

            if geometry_type != QgsWkbTypes.PointGeometry:

                QMessageBox.warning(
                    self,
                    "Geometry Error",
                    "Point Mode requires a point geometry layer."
                )

                return

            field_index = self.field_combo.currentData()

            if field_index is None:

                QMessageBox.warning(
                    self,
                    "Field Error",
                    "Please select a numeric field."
                )

                return

        elif self.mode_line.isChecked():

            mode = "line"
            field_index = None

            geometry_type = QgsWkbTypes.geometryType(
                layer.wkbType()
            )

            if geometry_type != QgsWkbTypes.LineGeometry:

                QMessageBox.warning(
                    self,
                    "Geometry Error",
                    "Line Mode requires a line geometry layer."
                )

                return

        else:

            QMessageBox.warning(
                self,
                "Mode Error",
                "Please select a generation mode."
            )

            return

        # -------------------------------------------------
        # Capture fields and CRS on main thread.
        # -------------------------------------------------

        self.output_fields = layer.fields()
        self.output_crs = layer.crs()

        # -------------------------------------------------
        # Snapshot input features on main thread.
        # -------------------------------------------------

        self.run_btn.setEnabled(False)

        self.progress.setValue(0)

        self.status_label.setText(
            "Preparing input data..."
        )

        QApplication.processEvents()

        try:

            source_features = self.create_source_snapshot(
                layer
            )

        except Exception as error:

            self.run_btn.setEnabled(True)

            self.validate_current_selection()

            QMessageBox.critical(
                self,
                "Snapshot Error",
                str(error)
            )

            return

        if not source_features:

            self.run_btn.setEnabled(True)

            self.validate_current_selection()

            QMessageBox.information(
                self,
                "No Features",
                "The selected layer contains no features."
            )

            return

        # -------------------------------------------------
        # Create background task.
        # -------------------------------------------------

        self.status_label.setText(
            "Generating output in the background..."
        )

        self.task = DualGeneratorTask(
            source_features,
            mode,
            field_index,
            self
        )

        self.task.progressChanged.connect(
            self.progress.setValue
        )

        # -------------------------------------------------
        # Add task to QGIS task manager.
        # -------------------------------------------------

        try:

            accepted = QgsApplication.taskManager().addTask(
                self.task
            )

            if accepted is False:

                self.task = None

                self.run_btn.setEnabled(True)

                self.validate_current_selection()

                QMessageBox.critical(
                    self,
                    "Task Error",
                    "QGIS could not start the background task."
                )

        except Exception as error:

            self.task = None

            self.run_btn.setEnabled(True)

            self.validate_current_selection()

            QMessageBox.critical(
                self,
                "Task Error",
                str(error)
            )

    # =====================================================
    # CREATE OUTPUT LAYER
    # Main thread only.
    # =====================================================

    def create_output_layer(self, task):

        output_layer = QgsVectorLayer(
            "Point",
            OUTPUT_LAYER_NAME,
            "memory"
        )

        if not output_layer.isValid():

            raise RuntimeError(
                "Could not create the output memory layer."
            )

        output_layer.setCrs(self.output_crs)

        provider = output_layer.dataProvider()

        # -----------------------------------------------
        # Copy source attributes.
        # -----------------------------------------------

        if self.output_fields is not None:

            if not provider.addAttributes(
                list(self.output_fields)
            ):

                raise RuntimeError(
                    "Could not copy source attribute fields."
                )

        output_layer.updateFields()

        # -----------------------------------------------
        # Check for generated records.
        # -----------------------------------------------

        if not task.output_records:

            return output_layer

        # -----------------------------------------------
        # Insert features in batches.
        # -----------------------------------------------

        records = task.output_records

        total = len(records)

        for start in range(
            0,
            total,
            OUTPUT_BATCH_SIZE
        ):

            end = min(
                start + OUTPUT_BATCH_SIZE,
                total
            )

            batch = []

            for x, y, attributes in records[start:end]:

                feature = QgsFeature(
                    output_layer.fields()
                )

                feature.setGeometry(
                    QgsGeometry.fromPointXY(
                        QgsPointXY(x, y)
                    )
                )

                feature.setAttributes(
                    attributes
                )

                batch.append(feature)

            result = provider.addFeatures(batch)

            # PyQGIS commonly returns (success, features).
            # Handle either tuple or boolean return formats.
            if isinstance(result, tuple):

                success = result[0]

            else:

                success = result

            if not success:

                raise RuntimeError(
                    "The memory provider failed while inserting "
                    f"output features in batch {start}–{end}."
                )

            self.progress.setValue(
                int(end / total * 100)
            )

            QApplication.processEvents()

        output_layer.updateExtents()

        return output_layer

    # =====================================================
    # TASK FINISHED
    # Called on main QGIS thread.
    # =====================================================

    def task_finished(self, task, result):

        try:

            # ---------------------------------------------
            # Restore UI.
            # ---------------------------------------------

            self.task = None

            self.run_btn.setEnabled(True)

            # ---------------------------------------------
            # Cancellation.
            # ---------------------------------------------

            if task.isCanceled():

                self.status_label.setText(
                    "Generation cancelled."
                )

                self.progress.setValue(0)

                if self.close_when_finished:
                    self.close()
                    return

                QMessageBox.information(
                    self,
                    "Cancelled",
                    "The generation task was cancelled."
                )

                return

            # ---------------------------------------------
            # Task error.
            # ---------------------------------------------

            if not result:

                self.progress.setValue(0)

                self.status_label.setText(
                    "Generation failed."
                )

                error_text = (
                    str(task.exception)
                    if task.exception
                    else "The background task did not complete."
                )

                if task.traceback_text:
                    error_text += (
                        "\n\nTechnical details:\n"
                        + task.traceback_text
                    )

                if self.close_when_finished:
                    self.close()
                    return

                QMessageBox.critical(
                    self,
                    "Generation Error",
                    error_text
                )

                return

            # ---------------------------------------------
            # No generated points.
            # ---------------------------------------------

            if not task.output_records:

                self.progress.setValue(100)

                self.status_label.setText(
                    "Completed. No output points were generated."
                )

                if self.close_when_finished:
                    self.close()
                    return

                QMessageBox.information(
                    self,
                    "No Output",
                    "The task completed, but no output points "
                    "were generated.\n\n"
                    "Check the selected field values and "
                    "input geometries."
                )

                return

            # ---------------------------------------------
            # Create output layer on main thread.
            # ---------------------------------------------

            self.status_label.setText(
                "Creating output layer..."
            )

            QApplication.processEvents()

            output_layer = self.create_output_layer(task)

            if output_layer.featureCount() == 0:

                if self.close_when_finished:
                    self.close()
                    return

                QMessageBox.information(
                    self,
                    "No Output",
                    "No output features were successfully created."
                )

                return

            # ---------------------------------------------
            # Add output to QGIS project.
            # ---------------------------------------------

            QgsProject.instance().addMapLayer(
                output_layer
            )

            self.progress.setValue(100)

            self.status_label.setText(
                "Generation completed successfully."
            )

            # ---------------------------------------------
            # Statistics.
            # ---------------------------------------------

            generated = len(task.output_records)

            statistics = (
                "Generation completed successfully.\n\n"
                f"Generated points: {generated:,}\n"
                f"Processed input features: "
                f"{task.processed_features:,}\n"
                f"Invalid count features: "
                f"{task.invalid_count_features:,}\n"
                f"Empty geometries: "
                f"{task.empty_geometries:,}\n\n"
                f"Output layer: {OUTPUT_LAYER_NAME}"
            )

            if self.close_when_finished:
                self.close()
                return

            QMessageBox.information(
                self,
                "SUCCESS",
                statistics
            )

        except Exception as error:

            self.status_label.setText(
                "An error occurred while finalizing output."
            )

            if not self.close_when_finished:

                QMessageBox.critical(
                    self,
                    "Output Error",
                    str(error)
                )

        finally:

            # Release detached snapshots and records.
            task.source_features = []
            task.output_records = []

            if self.close_when_finished:
                self.close()

            else:
                self.validate_current_selection()

    # =====================================================
    # THEME TOGGLE
    # =====================================================

    def toggle_theme(self):

        self.dark_mode = not self.dark_mode

        if self.dark_mode:
            self.apply_dark_theme()
        else:
            self.apply_light_theme()

    # =====================================================
    # DARK THEME
    # =====================================================

    def apply_dark_theme(self):

        self.setStyleSheet("""

        QDialog {
            background-color: #121826;
            color: white;
        }

        QLabel {
            color: white;
            font-weight: bold;
        }

        QComboBox {
            background-color: #1e293b;
            color: #13ff00;
            border: 2px solid #13ff00;
            padding: 6px;
            font-weight: bold;
        }

        QComboBox QAbstractItemView {
            background-color: #1e293b;
            color: white;
            selection-background-color: #00c2ff;
            selection-color: black;
        }

        QPushButton {
            background-color: #1e293b;
            color: #13ff00;
            border: 2px solid #13ff00;
            padding: 7px;
            font-weight: bold;
            border-radius: 4px;
        }

        QPushButton:hover {
            background-color: #00c2ff;
            color: black;
        }

        QPushButton:disabled {
            background-color: #263244;
            color: #718096;
            border-color: #718096;
        }

        QCheckBox {
            color: white;
            font-weight: bold;
            spacing: 8px;
        }

        QProgressBar {
            border: 2px solid #13ff00;
            text-align: center;
            background: #0f172a;
            color: white;
            font-weight: bold;
            min-height: 20px;
        }

        QProgressBar::chunk {
            background-color: #13ff00;
        }

        """)

    # =====================================================
    # LIGHT THEME
    # =====================================================

    def apply_light_theme(self):

        self.setStyleSheet("""

        QDialog {
            background-color: white;
            color: black;
        }

        QLabel {
            color: black;
            font-weight: bold;
        }

        QComboBox {
            background-color: white;
            color: black;
            border: 1px solid black;
            padding: 6px;
        }

        QComboBox QAbstractItemView {
            background-color: white;
            color: black;
            selection-background-color: #007acc;
            selection-color: white;
        }

        QPushButton {
            background-color: white;
            color: black;
            border: 1px solid black;
            padding: 7px;
            border-radius: 4px;
        }

        QPushButton:hover {
            background-color: #dceeff;
        }

        QPushButton:disabled {
            color: #999999;
            border-color: #aaaaaa;
        }

        QCheckBox {
            color: black;
        }

        QProgressBar {
            border: 1px solid black;
            text-align: center;
            color: black;
            min-height: 20px;
        }

        QProgressBar::chunk {
            background-color: #007acc;
        }

        """)

    # =====================================================
    # SAFE CLOSE
    # =====================================================

    def closeEvent(self, event):

        if self.task is not None:

            self.close_when_finished = True

            self.status_label.setText(
                "Cancelling task before closing..."
            )

            self.run_btn.setEnabled(False)

            self.task.cancel()

            # Do not destroy the dialog while the task
            # still has a finished() callback referencing it.
            event.ignore()

            return

        if hasattr(self, "logo") and self.logo:

            self.logo.stop()

        event.accept()


# =========================================================
# GLOBAL DIALOG REFERENCE
# Prevents the modeless dialog from being garbage collected.
# =========================================================

_dual_generator_dialog = None


# =========================================================
# RUN FROM QGIS PYTHON CONSOLE
# =========================================================

def main(iface):

    global _dual_generator_dialog

    # Close an older idle dialog before opening a new one.
    if _dual_generator_dialog is not None:
        if _dual_generator_dialog.task is None:
            _dual_generator_dialog.close()

    parent = iface.mainWindow() if iface else None

    _dual_generator_dialog = DualGeneratorDialog(parent)

    _dual_generator_dialog.show()
    _dual_generator_dialog.raise_()
    _dual_generator_dialog.activateWindow()

    return _dual_generator_dialog


# =========================================================
# EXECUTION
# =========================================================

# Run this in the QGIS Python Console:
#
# main(iface)
#
# =========================================================
