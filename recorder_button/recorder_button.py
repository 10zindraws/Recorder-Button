# SPDX-FileCopyrightText: 2026 Krita Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Recorder Button Plugin for Krita

A toolbar button that toggles canvas recording via the Recorder Docker
and displays the current recording state with visual feedback.

When recording: Full color media-record icon
When not recording: Desaturated media-record icon at 30% opacity

Right-click: Opens/hides the Recorder Docker at cursor position
"""

from krita import Extension, Krita

from PyQt5.QtCore import QTimer, QSize, QObject, QEvent, Qt, QPoint
from PyQt5.QtGui import QIcon, QPixmap, QColor, QCursor
from PyQt5.QtWidgets import QAction, QApplication, QToolButton, QDockWidget, QSizePolicy


class DockerAutoCloseFilter(QObject):
    """
    Global event filter that closes the recorder docker when the user clicks
    outside of it or taps the canvas with a drawing tablet pen.
    
    This filter is installed only when the docker is opened via right-click
    on the Recorder Button, providing popup-like auto-dismiss behavior.
    """
    
    def __init__(self, docker, extension, parent=None):
        super().__init__(parent)
        self._docker = docker
        self._extension = extension
        self._active = True
    
    def deactivate(self):
        """Deactivate this filter so it stops processing events."""
        self._active = False
    
    def _is_click_inside_docker(self, widget):
        """
        Check if a widget is the docker or a child/descendant of the docker.
        
        Args:
            widget: The widget that received the click event
            
        Returns:
            True if the widget is inside the docker, False otherwise
        """
        if widget is None or self._docker is None:
            return False
        
        # Walk up the widget hierarchy to see if we hit the docker
        current = widget
        while current is not None:
            if current is self._docker:
                return True
            # Also check by object identity in case of proxy objects
            if isinstance(current, QDockWidget):
                obj_name = current.objectName()
                if obj_name and "Recorder" in obj_name:
                    return True
            current = current.parent()
        
        return False
    
    def _close_docker(self):
        """Close the docker and uninstall this event filter."""
        if not self._active:
            return
        
        self._active = False
        
        # Uninstall ourselves from the application
        app = QApplication.instance()
        if app:
            app.removeEventFilter(self)
        
        # Hide the docker
        if self._docker and self._docker.isVisible():
            toggle_action = self._docker.toggleViewAction()
            if toggle_action and toggle_action.isChecked():
                toggle_action.trigger()
            else:
                self._docker.hide()
        
        # Notify extension that auto-close filter is no longer active
        if self._extension:
            self._extension._on_auto_close_filter_removed()
    
    def eventFilter(self, obj, event):
        """Filter events to detect clicks outside the docker."""
        if not self._active or self._docker is None:
            return False
        
        # Check if docker was closed by other means
        if not self._docker.isVisible():
            self._active = False
            app = QApplication.instance()
            if app:
                app.removeEventFilter(self)
            if self._extension:
                self._extension._on_auto_close_filter_removed()
            return False
        
        event_type = event.type()
        
        # Handle mouse button press (left-click)
        if event_type == QEvent.MouseButtonPress:
            if event.button() == Qt.LeftButton:
                # Get the widget under the cursor
                target_widget = QApplication.widgetAt(QCursor.pos())
                
                if not self._is_click_inside_docker(target_widget):
                    # Click is outside the docker - close it
                    # Use a timer to allow the click event to propagate first
                    QTimer.singleShot(0, self._close_docker)
                    return False  # Don't consume the event
        
        # Handle tablet press (drawing tablet pen tap)
        elif event_type == QEvent.TabletPress:
            # Get the widget under the cursor
            target_widget = QApplication.widgetAt(QCursor.pos())
            
            if not self._is_click_inside_docker(target_widget):
                # Tablet tap is outside the docker - close it
                QTimer.singleShot(0, self._close_docker)
                return False  # Don't consume the event
        
        return False  # Never consume events, just observe them


class ToolButtonEventFilter(QObject):
    """
    Event filter to intercept right-click events on toolbar buttons.
    """
    
    def __init__(self, extension, parent=None):
        super().__init__(parent)
        self._extension = extension
    
    def eventFilter(self, obj, event):
        """Filter events for right-click detection."""
        if event.type() == QEvent.MouseButtonPress:
            if event.button() == Qt.RightButton:
                # Handle right-click - toggle docker visibility
                self._extension._toggle_recorder_docker_at_cursor()
                return True  # Event handled
        return super().eventFilter(obj, event)


class RecorderButtonExtension(Extension):
    """
    Extension that provides a toolbar button to toggle recording
    and display the current recording state.
    
    Left-click: Toggle recording on/off
    Right-click: Open/hide Recorder Docker at cursor position
    """

    # Class-level icon cache to avoid recreating for each window
    _icon_recording = None
    _icon_not_recording = None
    _icons_prepared = False

    def __init__(self, parent):
        super().__init__(parent)
        self._actions = []  # Track actions across all windows
        self._windows = []  # Track windows for toolbar button access
        self._recorder_action = None
        self._hooked = False
        self._hook_timer = None
        self._event_filters = []  # Keep references to prevent garbage collection
        self._toolbar_buttons_installed = set()  # Track which buttons have filters
        self._auto_close_filter = None  # Global event filter for auto-closing docker

    def setup(self):
        """Called once when Krita starts, before any windows exist."""
        # Prepare icons once during setup
        self._prepare_icons()

    def createActions(self, window):
        """
        Called when a new window is created.
        Creates the toolbar action for this window.
        """
        # Create our toolbar action for this window
        action = window.createAction(
            "recorder_button_toggle",
            "Recorder Button",
            "tools/scripts"
        )
        action.setCheckable(True)
        action.setToolTip("Toggle canvas recording (Recorder Docker)\nRight-click: Open/hide docker")
        
        # Set initial icon state (not recording)
        action.setIcon(RecorderButtonExtension._icon_not_recording or Krita.instance().icon("media-record"))
        action.setChecked(False)
        
        # Connect our action's trigger
        action.toggled.connect(self._on_button_toggled)
        
        # Track this action and window
        self._actions.append(action)
        self._windows.append(window)
        
        # Hook into the recorder action if not already done
        if not self._hooked:
            self._schedule_hook()
        
        # Install event filter on toolbar button after a brief delay
        # (toolbar buttons are created asynchronously)
        QTimer.singleShot(500, lambda: self._install_event_filter_for_action(action))

    def _prepare_icons(self):
        """
        Prepare the recording and not-recording icons.
        
        Recording: Full color media-record icon
        Not Recording: Desaturated icon at 30% opacity
        """
        if RecorderButtonExtension._icons_prepared:
            return
            
        app = Krita.instance()
        
        # Get the original media-record icon
        original_icon = app.icon("media-record")
        
        if original_icon.isNull():
            # Fallback if icon not found
            RecorderButtonExtension._icon_recording = QIcon()
            RecorderButtonExtension._icon_not_recording = QIcon()
            return
        
        # Store the full-color icon for recording state
        RecorderButtonExtension._icon_recording = original_icon
        
        # Create desaturated, low-opacity version for not-recording state
        RecorderButtonExtension._icon_not_recording = self._create_desaturated_icon(original_icon, 0.30)
        RecorderButtonExtension._icons_prepared = True

    def _create_desaturated_icon(self, icon, opacity):
        """
        Create a desaturated version of the icon with reduced opacity.
        
        Args:
            icon: The original QIcon
            opacity: The opacity level (0.0 to 1.0)
            
        Returns:
            A new QIcon that is desaturated and has reduced opacity
        """
        new_icon = QIcon()
        
        # Process common icon sizes
        target_sizes = [16, 22, 24, 32, 48, 64, 128]
        
        for target_size in target_sizes:
            size = QSize(target_size, target_size)
            src_pixmap = icon.pixmap(size)
            
            if src_pixmap.isNull():
                continue
            
            src_image = src_pixmap.toImage()
            
            if src_image.isNull():
                continue
            
            # Process each pixel for desaturation and opacity
            for y in range(src_image.height()):
                for x in range(src_image.width()):
                    pixel = src_image.pixelColor(x, y)
                    
                    # Skip fully transparent pixels
                    if pixel.alpha() == 0:
                        continue
                    
                    # Desaturate: convert to grayscale while preserving luminance (ITU-R BT.601)
                    gray = int(0.299 * pixel.red() + 0.587 * pixel.green() + 0.114 * pixel.blue())
                    
                    # Apply opacity to the alpha channel
                    new_alpha = int(pixel.alpha() * opacity)
                    
                    # Set the new pixel color (grayscale with reduced opacity)
                    new_color = QColor(gray, gray, gray, new_alpha)
                    src_image.setPixelColor(x, y, new_color)
            
            # Convert back to pixmap and add to icon
            new_pixmap = QPixmap.fromImage(src_image)
            if not new_pixmap.isNull():
                new_icon.addPixmap(new_pixmap)
        
        # Return processed icon, or original if processing failed
        return new_icon if not new_icon.isNull() else icon

    def _schedule_hook(self):
        """Schedule an attempt to hook into the recorder action."""
        if self._hook_timer is not None:
            return
            
        self._hook_timer = QTimer()
        self._hook_timer.setSingleShot(True)
        self._hook_timer.timeout.connect(self._try_hook_recorder_action)
        self._hook_timer.start(100)

    def _try_hook_recorder_action(self):
        """
        Try to hook into the Recorder Docker's toggle action.
        Retries if the recorder docker isn't loaded yet.
        """
        self._hook_timer = None
        
        if self._hooked:
            return
            
        app = Krita.instance()
        
        # Find the recorder's toggle action
        recorder_action = app.action("recorder_record_toggle")
        
        if recorder_action is None:
            # Recorder docker not available yet, retry
            # Limit retries to avoid infinite loop
            self._schedule_hook()
            return
        
        self._recorder_action = recorder_action
        
        # Connect to the recorder action's toggled signal
        # This notifies us when recording state changes (from docker or other sources)
        recorder_action.toggled.connect(self._on_recorder_state_changed)
        
        # Sync initial state
        self._sync_all_actions(recorder_action.isChecked())
        
        self._hooked = True

    def _on_button_toggled(self, checked):
        """
        Called when our toolbar button is toggled by the user.
        Forwards the action to the recorder docker.
        """
        if self._recorder_action is None:
            # Try to find the recorder action
            app = Krita.instance()
            self._recorder_action = app.action("recorder_record_toggle")
        
        if self._recorder_action is not None:
            # Only update if state differs to prevent feedback loop
            if self._recorder_action.isChecked() != checked:
                self._recorder_action.setChecked(checked)
        
        # Update visual state for the action that was clicked
        # Other actions will be updated via _on_recorder_state_changed

    def _on_recorder_state_changed(self, is_recording):
        """
        Called when the recorder docker's recording state changes.
        Updates all our buttons to reflect the current state.
        """
        self._sync_all_actions(is_recording)

    def _sync_all_actions(self, is_recording):
        """
        Synchronize all button states with the recorder's state.
        
        Args:
            is_recording: True if currently recording, False otherwise
        """
        for action in self._actions:
            if action is None:
                continue
                
            # Block signals to prevent feedback loop
            action.blockSignals(True)
            action.setChecked(is_recording)
            action.blockSignals(False)
            
            # Update the icon appearance
            self._update_action_icon(action, is_recording)

    def _update_action_icon(self, action, is_recording):
        """
        Update a toolbar button icon based on recording state.
        
        Args:
            action: The QAction to update
            is_recording: True for full-color icon, False for desaturated icon
        """
        if action is None:
            return
        
        if is_recording:
            if RecorderButtonExtension._icon_recording:
                action.setIcon(RecorderButtonExtension._icon_recording)
            action.setToolTip("Recording... Click to stop\nRight-click: Open/hide docker")
        else:
            if RecorderButtonExtension._icon_not_recording:
                action.setIcon(RecorderButtonExtension._icon_not_recording)
            action.setToolTip("Click to start recording\nRight-click: Open/hide docker")

    def _on_auto_close_filter_removed(self):
        """Called when the auto-close filter is removed or deactivated."""
        self._auto_close_filter = None
    
    def _install_auto_close_filter(self, docker):
        """
        Install a global event filter to auto-close the docker when clicking outside.
        
        Args:
            docker: The recorder docker to monitor
        """
        # Remove any existing auto-close filter
        if self._auto_close_filter is not None:
            self._auto_close_filter.deactivate()
            app = QApplication.instance()
            if app:
                app.removeEventFilter(self._auto_close_filter)
            self._auto_close_filter = None
        
        # Create and install new filter
        self._auto_close_filter = DockerAutoCloseFilter(docker, self)
        app = QApplication.instance()
        if app:
            app.installEventFilter(self._auto_close_filter)
    
    def _install_event_filter_for_action(self, action):
        """
        Find the toolbar button for the given action and install an event filter
        to intercept right-click events.
        
        Args:
            action: The QAction to find the toolbar button for
        """
        # Find all QToolButton widgets that are associated with this action
        app = QApplication.instance()
        if app is None:
            return
        
        for widget in app.allWidgets():
            if isinstance(widget, QToolButton):
                # Check if this toolbar button has our action
                if widget.defaultAction() == action:
                    # Use widget id to avoid installing multiple filters
                    widget_id = id(widget)
                    if widget_id not in self._toolbar_buttons_installed:
                        event_filter = ToolButtonEventFilter(self, widget)
                        widget.installEventFilter(event_filter)
                        self._event_filters.append(event_filter)
                        self._toolbar_buttons_installed.add(widget_id)

    def _find_recorder_docker(self):
        """
        Find the Recorder Docker widget.
        
        Returns:
            The QDockWidget for the Recorder Docker, or None if not found
        """
        app = QApplication.instance()
        if app is None:
            return None
        
        # Look for the recorder docker by object name
        for widget in app.allWidgets():
            if isinstance(widget, QDockWidget):
                # Check by object name - RecorderDockerDock is the class name
                obj_name = widget.objectName()
                if obj_name and "Recorder" in obj_name:
                    return widget
                # Also check window title as fallback
                title = widget.windowTitle()
                if title and "Recorder" in title:
                    return widget
        
        return None

    def _toggle_recorder_docker_at_cursor(self):
        """
        Toggle the Recorder Docker visibility.
        If hidden, show it as a floating window at the cursor position.
        If visible, hide it.
        """
        docker = self._find_recorder_docker()
        
        if docker is None:
            # Recorder docker not available
            return
        
        if docker.isVisible():
            # Docker is visible - hide it using toggleViewAction for proper Qt handling
            toggle_action = docker.toggleViewAction()
            if toggle_action and toggle_action.isChecked():
                toggle_action.trigger()
            else:
                docker.hide()
        else:
            # Docker is hidden - show it at cursor position
            self._show_docker_at_cursor(docker)
            
            # Install auto-close filter for popup-like behavior
            # This makes the docker close when clicking outside of it
            self._install_auto_close_filter(docker)

    def _restore_docker_size_constraints(self, docker):
        """
        Restore normal size constraints for a floating docker.
        This ensures the docker can be resized freely when floating,
        bypassing any constraints from plugins like super_docker_lock.
        
        Args:
            docker: The QDockWidget to restore constraints for
        """
        MAX_QT_DIMENSION = 16777215  # Maximum value for QWidget dimensions
        
        # Check if super_docker_lock has stored original constraints
        stored = docker.property("_super_docker_lock_dock_size_constraints")
        if stored and isinstance(stored, (tuple, list)) and len(stored) >= 4:
            # Restore original constraints
            docker.setMinimumWidth(int(stored[0]))
            docker.setMaximumWidth(int(stored[1]))
            docker.setMinimumHeight(int(stored[2]))
            docker.setMaximumHeight(int(stored[3]))
            docker.setProperty("_super_docker_lock_dock_size_constraints", None)
        else:
            # Set reasonable defaults for floating docker
            docker.setMinimumWidth(100)
            docker.setMaximumWidth(MAX_QT_DIMENSION)
            docker.setMinimumHeight(100)
            docker.setMaximumHeight(MAX_QT_DIMENSION)

    def _restore_docker_title_bar(self, docker):
        """
        Restore the title bar for a floating docker.
        When super_docker_lock is enabled on startup, it may collapse title bars
        for grouped dockers. This restores them for floating dockers.
        
        Args:
            docker: The QDockWidget to restore title bar for
        """
        title_bar = docker.titleBarWidget()
        if not title_bar:
            return
        
        # Check if title bar was collapsed by super_docker_lock
        collapsed = title_bar.property("_super_docker_lock_titlebar_collapsed")
        if not collapsed:
            return
        
        # Restore stored state from super_docker_lock properties
        stored_min = title_bar.property("_super_docker_lock_titlebar_min_height")
        stored_max = title_bar.property("_super_docker_lock_titlebar_max_height")
        stored_style = title_bar.property("_super_docker_lock_titlebar_style")
        stored_style_attr = title_bar.property("_super_docker_lock_titlebar_style_attr")
        stored_policy = title_bar.property("_super_docker_lock_titlebar_size_policy")
        stored_margins = title_bar.property("_super_docker_lock_titlebar_margins")
        stored_layout_margins = title_bar.property("_super_docker_lock_titlebar_layout_margins")
        stored_layout_spacing = title_bar.property("_super_docker_lock_titlebar_layout_spacing")
        
        # Restore stylesheet
        if stored_style is None:
            stored_style = ""
        if stored_style_attr is None:
            stored_style_attr = False
        title_bar.setStyleSheet(stored_style)
        if not stored_style_attr and not stored_style:
            title_bar.setAttribute(Qt.WA_StyleSheet, False)
        
        # Restore height constraints
        if stored_min is None:
            stored_min = 0
        if stored_max is None:
            stored_max = 16777215
        title_bar.setMinimumHeight(int(stored_min))
        title_bar.setMaximumHeight(int(stored_max))
        
        # Restore size policy
        if stored_policy and isinstance(stored_policy, (tuple, list)) and len(stored_policy) >= 2:
            policy = QSizePolicy(int(stored_policy[0]), int(stored_policy[1]))
            if len(stored_policy) >= 3:
                policy.setControlType(QSizePolicy.ControlType(int(stored_policy[2])))
            title_bar.setSizePolicy(policy)
        
        # Restore margins
        if stored_margins and isinstance(stored_margins, (tuple, list)) and len(stored_margins) >= 4:
            title_bar.setContentsMargins(
                int(stored_margins[0]),
                int(stored_margins[1]),
                int(stored_margins[2]),
                int(stored_margins[3]),
            )
        
        # Restore layout margins and spacing
        layout = title_bar.layout()
        if layout:
            if (
                stored_layout_margins
                and isinstance(stored_layout_margins, (tuple, list))
                and len(stored_layout_margins) >= 4
            ):
                layout.setContentsMargins(
                    int(stored_layout_margins[0]),
                    int(stored_layout_margins[1]),
                    int(stored_layout_margins[2]),
                    int(stored_layout_margins[3]),
                )
            if stored_layout_spacing is not None:
                layout.setSpacing(int(stored_layout_spacing))
        
        # Mark title bar as no longer collapsed
        title_bar.setProperty("_super_docker_lock_titlebar_collapsed", None)
        title_bar.setVisible(True)
        
        # Refresh layout
        if layout:
            layout.invalidate()
        title_bar.updateGeometry()
        title_bar.update()
        docker.updateGeometry()
        
        # Clear stored properties
        title_bar.setProperty("_super_docker_lock_titlebar_min_height", None)
        title_bar.setProperty("_super_docker_lock_titlebar_max_height", None)
        title_bar.setProperty("_super_docker_lock_titlebar_style", None)
        title_bar.setProperty("_super_docker_lock_titlebar_style_attr", None)
        title_bar.setProperty("_super_docker_lock_titlebar_size_policy", None)
        title_bar.setProperty("_super_docker_lock_titlebar_margins", None)
        title_bar.setProperty("_super_docker_lock_titlebar_layout_margins", None)
        title_bar.setProperty("_super_docker_lock_titlebar_layout_spacing", None)

    def _show_docker_at_cursor(self, docker):
        """
        Show a docker as a floating window at the current cursor position,
        adjusting to stay within screen bounds.
        
        This method properly handles conflicts with super_docker_lock by:
        1. Setting the docker to floating mode first
        2. Restoring any locked size constraints
        3. Using toggleViewAction for proper Qt visibility handling
        4. Raising the window to ensure it appears on top
        
        Args:
            docker: The QDockWidget to show
        """
        # Make the docker floating so we can position it freely
        # This must be done before showing to avoid super_docker_lock constraints
        docker.setFloating(True)
        
        # Restore size constraints that may have been locked by super_docker_lock
        self._restore_docker_size_constraints(docker)
        
        # Restore title bar if it was collapsed by super_docker_lock
        # This is needed when super_docker_lock is enabled on Krita startup
        self._restore_docker_title_bar(docker)
        
        # Get cursor position
        cursor_pos = QCursor.pos()
        
        # Process pending events to ensure state changes take effect
        # This is important when super_docker_lock is active from startup
        QApplication.processEvents()
        
        # Use toggleViewAction for proper visibility handling
        # This is the Qt-native way to show/hide dock widgets and works
        # properly even when other plugins have event filters installed
        toggle_action = docker.toggleViewAction()
        if toggle_action and not toggle_action.isChecked():
            toggle_action.trigger()
        else:
            # Fallback if toggleViewAction not available or already checked
            docker.show()
        
        # Process events again and ensure visibility
        QApplication.processEvents()
        
        # Force visibility if still not visible
        if not docker.isVisible():
            docker.setVisible(True)
        
        # Ensure the docker is visible and on top
        docker.raise_()
        docker.activateWindow()
        
        # Get docker size
        docker_size = docker.size()
        docker_width = docker_size.width()
        docker_height = docker_size.height()
        
        # Get screen geometry at cursor position
        screen = QApplication.screenAt(cursor_pos)
        if screen is None:
            screen = QApplication.primaryScreen()
        
        if screen is None:
            # Fallback - just position at cursor
            docker.move(cursor_pos)
            return
        
        screen_geometry = screen.availableGeometry()
        
        # Calculate position - start at cursor position
        x = cursor_pos.x()
        y = cursor_pos.y()
        
        # Adjust X to fit within screen bounds
        if x + docker_width > screen_geometry.right():
            # Would overflow right edge - move left
            x = screen_geometry.right() - docker_width
        if x < screen_geometry.left():
            # Would overflow left edge - clamp to left
            x = screen_geometry.left()
        
        # Adjust Y to fit within screen bounds
        if y + docker_height > screen_geometry.bottom():
            # Would overflow bottom edge - move up
            y = screen_geometry.bottom() - docker_height
        if y < screen_geometry.top():
            # Would overflow top edge - clamp to top
            y = screen_geometry.top()
        
        # Move docker to calculated position
        docker.move(QPoint(x, y))


# Krita plugin entry point - register the extension
Krita.instance().addExtension(RecorderButtonExtension(Krita.instance()))
