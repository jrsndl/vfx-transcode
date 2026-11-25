"""A last pass over a window: every control without a tooltip gets one from its form label,
its own text or its placeholder, so nothing is left without a hint. The written tooltips
(setToolTip where the control is made) are the real ones; this only fills gaps."""

from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSlider, QAbstractSpinBox,
                               QComboBox, QFormLayout, QLabel, QLineEdit, QPlainTextEdit, QTextEdit,
                               QWidget)

CONTROLS = (QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox, QAbstractSlider,
            QAbstractItemView, QPlainTextEdit, QTextEdit)


def _clean(text):
    return " ".join(str(text or "").replace("&", "").split()).rstrip(":")


def _widgets(item):
    """The widgets of a layout item (a widget, or every widget in a layout)."""
    if item is None:
        return []
    if item.widget() is not None:
        return [item.widget()]
    layout = item.layout()
    if layout is None:
        return []
    out = []
    for index in range(layout.count()):
        out += _widgets(layout.itemAt(index))
    return out


def _is_part(widget):
    """A control inside another control (a combo box's line edit, a spin box's editor)."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, (QComboBox, QAbstractSpinBox)):
            return True
        parent = parent.parentWidget()
    return False


def fill_missing(root):
    """Give every control under root without a tooltip one. Returns how many were filled."""
    filled = 0
    for form in root.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            text = _clean(label.widget().text()) if label is not None and isinstance(label.widget(), QLabel) else ""
            if not text:
                continue
            for widget in _widgets(field):
                if isinstance(widget, CONTROLS) and not widget.toolTip() and not _is_part(widget):
                    widget.setToolTip(text)
                    filled += 1
    for widget in root.findChildren(QWidget):
        if not isinstance(widget, CONTROLS) or widget.toolTip() or _is_part(widget):
            continue
        text = ""
        if isinstance(widget, QAbstractButton):
            text = _clean(widget.text())
        elif isinstance(widget, QLineEdit):
            text = _clean(widget.placeholderText())
        elif isinstance(widget, (QPlainTextEdit, QTextEdit)):
            text = _clean(widget.placeholderText())
        if text:
            widget.setToolTip(text)
            filled += 1
    return filled
