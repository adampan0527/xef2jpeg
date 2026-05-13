#!/usr/bin/env python3
"""
XEF2JPEG - Convert Kinect V2 XEF files to JPEG format

A Windows desktop application for converting .XEF files captured by Kinect V2
sensors to JPEG image format.

Target Platform: Windows 10 & Windows 11
"""

import os
import sys
import json
import signal
import logging
import ctypes
import ctypes.wintypes
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from datetime import datetime
from xef_parser import convert_xef_to_jpeg

# Application version
__version__ = "1.0.0"

# Settings file path (stored in same directory as script)
SETTINGS_FILE = Path(__file__).parent / "xef2jpeg_settings.json"

# Log file path
LOG_FILE = Path(__file__).parent / "xef2jpeg.log"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)


def load_settings():
    """Load application settings from JSON file.

    Returns:
        dict with settings, or empty dict if file doesn't exist.
    """
    try:
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_settings(settings):
    """Save application settings to JSON file.

    Args:
        settings: dict with settings to save.
    """
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass


# Windows API constants for drag-and-drop
WM_DROPFILES = 0x0233
WM_CLOSE = 0x0010
GWL_WNDPROC = -4

# Windows API functions
shell32 = ctypes.windll.shell32
user32 = ctypes.windll.user32

# Function signatures
shell32.DragQueryFileW.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT,
                                   ctypes.wintypes.LPWSTR, ctypes.wintypes.UINT]
shell32.DragQueryFileW.restype = ctypes.wintypes.UINT
shell32.DragFinish.argtypes = [ctypes.wintypes.HANDLE]
shell32.DragFinish.restype = None
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.wintypes.HWND,
                                    ctypes.wintypes.UINT, ctypes.wintypes.WPARAM,
                                    ctypes.wintypes.LPARAM]
user32.CallWindowProcW.restype = ctypes.wintypes.LPARAM

# Window procedure type
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.wintypes.HWND,
                              ctypes.wintypes.UINT, ctypes.wintypes.WPARAM,
                              ctypes.wintypes.LPARAM)

# Store original window procedure and callback reference
_original_wndproc = None
_saved_wndproc = None  # Preserved during cleanup so messages keep forwarding
_wndproc_ref = None
_app_ref = None
_dnd_enabled = False


def _new_wndproc(hwnd, msg, wparam, lparam):
    """Custom window procedure to handle WM_DROPFILES."""
    try:
        if msg == WM_DROPFILES and _app_ref is not None:
            # Get number of files dropped
            num_files = shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
            if num_files > 0:
                # Get first file path
                buf = ctypes.create_unicode_buffer(520)
                shell32.DragQueryFileW(wparam, 0, buf, 520)
                file_path = buf.value
                if file_path and file_path.lower().endswith('.xef'):
                    _app_ref.input_file.set(file_path)
            shell32.DragFinish(wparam)
            return 0
    except Exception:
        pass

    # For all messages, forward to original proc with explicit type casting
    try:
        wndproc = _original_wndproc or _saved_wndproc
        if wndproc:
            result = user32.CallWindowProcW(
                wndproc,
                ctypes.wintypes.HWND(hwnd),
                ctypes.wintypes.UINT(msg),
                ctypes.wintypes.WPARAM(wparam),
                ctypes.wintypes.LPARAM(lparam)
            )
            return result
    except Exception:
        pass
    return 0


def remove_drag_drop(root):
    """Remove the drag-and-drop subclass before window destruction.

    Must be called before tkinter destroys the window to avoid
    access violations from stale WndProc pointers.
    """
    global _original_wndproc, _saved_wndproc, _wndproc_ref, _app_ref, _dnd_enabled

    if not _dnd_enabled:
        return

    try:
        hwnd = ctypes.wintypes.HWND(root.winfo_id())
        # Restore original window procedure
        if _original_wndproc:
            user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, _original_wndproc)
        # Stop accepting drops
        shell32.DragAcceptFiles(hwnd, False)
    except (OSError, ValueError):
        pass

    # Preserve original WndProc so pending messages still get forwarded
    _saved_wndproc = _original_wndproc
    _original_wndproc = None
    _wndproc_ref = None
    _app_ref = None
    _dnd_enabled = False


def setup_drag_drop(root, app):
    """Enable drag-and-drop for .xef files on the tkinter window.

    Args:
        root: tkinter root window
        app: XEF2JPEGApp instance
    """
    global _original_wndproc, _wndproc_ref, _app_ref, _dnd_enabled

    _app_ref = app

    try:
        # Get the window handle
        hwnd = ctypes.wintypes.HWND(root.winfo_id())

        # Accept drag-and-drop files
        shell32.DragAcceptFiles(hwnd, True)

        # Store original window procedure
        _original_wndproc = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)

        # Create new window procedure (must keep reference to prevent GC)
        _wndproc_ref = WNDPROC(_new_wndproc)
        user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, _wndproc_ref)
        _dnd_enabled = True
    except (OSError, ValueError):
        # Drag-and-drop setup failed silently - not critical
        _original_wndproc = None
        _dnd_enabled = False


def check_kinect_sdk():
    """Check if Kinect for Windows SDK 2.0 is installed.

    Returns:
        Tuple of (is_installed: bool, message: str)
    """
    # Check for Kinect SDK registry key (Windows)
    if sys.platform == 'win32':
        try:
            import winreg
            key_path = r"SOFTWARE\Microsoft\Kinect\v2.0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path):
                return True, "Kinect for Windows SDK 2.0 is installed."
        except (OSError, ImportError):
            pass

    # Check common installation paths
    program_files = os.environ.get('ProgramFiles', r'C:\Program Files')
    sdk_paths = [
        Path(program_files) / "Microsoft SDKs" / "Kinect" / "v2.0",
        Path(program_files) / "Microsoft Kinect" / "v2.0",
    ]
    for sdk_path in sdk_paths:
        if sdk_path.exists():
            return True, "Kinect for Windows SDK 2.0 is installed."

    return False, (
        "Kinect for Windows SDK 2.0 was not detected on this system.\n\n"
        "Note: The Kinect SDK is NOT required for XEF to JPEG conversion.\n"
        "This application uses its own parser to read XEF files directly.\n\n"
        "If you encounter issues with specific XEF files, installing the\n"
        "Kinect SDK may help with additional format support."
    )


def _detect_system_theme():
    """Detect Windows system theme (light or dark).

    Returns:
        str: 'dark' or 'light'
    """
    if sys.platform == 'win32':
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return 'light' if value == 1 else 'dark'
        except (OSError, ImportError):
            pass
    return 'light'


class XEF2JPEGApp:
    """Main application class for XEF to JPEG conversion."""

    def __init__(self, root):
        """Initialize the application."""
        self.root = root
        self.root.title(f"XEF2JPEG v{__version__} - Kinect V2 to JPEG Converter")
        self.root.geometry("600x520")
        self.root.minsize(500, 420)

        # Restore window position from settings
        self.settings = load_settings()
        saved_geom = self.settings.get('window_geometry')
        if saved_geom:
            try:
                self.root.geometry(saved_geom)
            except tk.TclError:
                pass

        # Set application icon (feat-126)
        self._set_app_icon()

        # Detect system theme (feat-133)
        self._current_theme = _detect_system_theme()

        # Application state
        self.input_file = tk.StringVar()
        default_output = self.settings.get('last_output_dir', str(Path.cwd() / "XEF2JPEG_Output"))
        self.output_directory = tk.StringVar(value=default_output)
        self.stream_mode = tk.StringVar(value="depth_ir")
        self.jpeg_quality = tk.IntVar(value=95)
        self.is_converting = False
        self.cancel_event = threading.Event()
        self._conversion_thread = None
        self._file_queue = []  # Batch conversion file list

        # Setup UI
        self.setup_ui()

        # Check for Kinect SDK on startup
        self.sdk_installed, self.sdk_message = check_kinect_sdk()
        if not self.sdk_installed:
            self.root.after(500, self._show_sdk_info)

        # Setup drag-and-drop support
        self.root.after(100, lambda: setup_drag_drop(self.root, self))

        # Clean up drag-and-drop subclass before window closes
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Periodic theme change check (feat-133)
        self.root.after(5000, self._check_theme_change)

        logger.info("XEF2JPEG v%s started on %s (theme: %s)",
                    __version__, sys.platform, self._current_theme)

    def _set_app_icon(self):
        """Generate and set the application icon programmatically (feat-126)."""
        try:
            from PIL import Image, ImageDraw

            icon_cache = Path(__file__).parent / ".icon_cache.ico"

            if not icon_cache.exists():
                # Generate a simple icon: blue camera/XEF icon
                size = 32
                img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)

                # Draw a rounded rectangle background (blue)
                draw.rounded_rectangle([2, 2, size - 3, size - 3], radius=4,
                                       fill=(37, 99, 235, 255))

                # Draw "X" letter for XEF
                draw.line([(8, 8), (24, 24)], fill=(255, 255, 255, 255), width=3)
                draw.line([(24, 8), (8, 24)], fill=(255, 255, 255, 255), width=3)

                img.save(str(icon_cache), format='ICO',
                         sizes=[(16, 16), (32, 32)])

            self.root.iconbitmap(str(icon_cache))
        except Exception:
            pass

    def setup_ui(self):
        """Setup the user interface."""
        # Main frame
        main_frame = ttk.Frame(self.root, padding="20")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Configure grid weights
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)

        # Title
        title_label = ttk.Label(main_frame, text="XEF2JPEG Converter",
                               font=("Helvetica", 16, "bold"))
        title_label.grid(row=0, column=0, columnspan=3, pady=(0, 15))

        # Input files listbox (batch support - feat-165)
        ttk.Label(main_frame, text="Input Files:").grid(row=1, column=0,
                                                         sticky=tk.NW, pady=5)
        list_frame = ttk.Frame(main_frame)
        list_frame.grid(row=1, column=1, columnspan=2, sticky=(tk.W, tk.E), padx=5)
        list_frame.columnconfigure(0, weight=1)

        self.file_listbox = tk.Listbox(list_frame, height=4, width=55)
        self.file_listbox.grid(row=0, column=0, sticky=(tk.W, tk.E))
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL,
                                  command=self.file_listbox.yview)
        scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))
        self.file_listbox.config(yscrollcommand=scrollbar.set)

        # File management buttons
        file_btn_frame = ttk.Frame(list_frame)
        file_btn_frame.grid(row=1, column=0, columnspan=2, pady=(5, 0))
        ttk.Button(file_btn_frame, text="Add Files...", width=12,
                  command=self.add_files).pack(side=tk.LEFT, padx=2)
        ttk.Button(file_btn_frame, text="Remove", width=12,
                  command=self.remove_file).pack(side=tk.LEFT, padx=2)

        # Output directory selection
        self.output_entry = ttk.Entry(main_frame, textvariable=self.output_directory,
                                      width=50)
        ttk.Label(main_frame, text="Output Directory:").grid(row=2, column=0,
                                                             sticky=tk.W, pady=5)
        self.output_entry.grid(row=2, column=1, sticky=(tk.W, tk.E), padx=5)
        ttk.Button(main_frame, text="Browse...", width=12,
                  command=self.browse_output_directory).grid(row=2, column=2, pady=5)
        self._setup_placeholder(self.output_entry, "Select output folder...")

        # Stream type selection
        ttk.Label(main_frame, text="Stream Type:").grid(row=3, column=0,
                                                        sticky=tk.W, pady=5)
        stream_combo = ttk.Combobox(main_frame, textvariable=self.stream_mode,
                                   values=["depth_ir", "depth_only", "ir_only",
                                           "color_only", "all"],
                                   state="readonly", width=20)
        stream_combo.grid(row=3, column=1, sticky=tk.W, padx=5, pady=5)

        # JPEG quality selection
        ttk.Label(main_frame, text="JPEG Quality:").grid(row=4, column=0,
                                                         sticky=tk.W, pady=5)
        quality_combo = ttk.Combobox(main_frame, textvariable=self.jpeg_quality,
                                    values=[60, 70, 80, 85, 90, 95, 100],
                                    state="readonly", width=10)
        quality_combo.grid(row=4, column=1, sticky=tk.W, padx=5, pady=5)

        # Progress bar
        self.progress = ttk.Progressbar(main_frame, mode='indeterminate')
        self.progress.grid(row=5, column=0, columnspan=3, sticky=(tk.W, tk.E),
                          pady=20)

        # Status label
        self.status_var = tk.StringVar(value="Ready")
        status_label = ttk.Label(main_frame, textvariable=self.status_var)
        status_label.grid(row=6, column=0, columnspan=3, pady=5)

        # Button frame (holds Convert and Cancel buttons side by side)
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=7, column=0, columnspan=3, pady=10)

        self.convert_button = ttk.Button(button_frame, text="Convert All",
                                        command=self.start_conversion)
        self.convert_button.pack(side=tk.LEFT, padx=5)

        self.cancel_button = ttk.Button(button_frame, text="Cancel",
                                       command=self.cancel_conversion, state='disabled')
        self.cancel_button.pack(side=tk.LEFT, padx=5)

        # Menu bar with Help > About (feat-166)
        menubar = tk.Menu(self.root)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About XEF2JPEG...", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menubar)

    def _show_sdk_info(self):
        """Show Kinect SDK status information on startup."""
        messagebox.showinfo("Kinect SDK Status", self.sdk_message, parent=self.root)

    def _show_about(self):
        """Show about dialog with version information."""
        messagebox.showinfo(
            "About XEF2JPEG",
            f"XEF2JPEG v{__version__}\n\n"
            f"Convert Kinect V2 XEF files to JPEG format.\n\n"
            f"Platform: {sys.platform}\n"
            f"Python: {sys.version.split()[0]}\n"
            f"Theme: {self._current_theme}\n"
            f"Log file: {LOG_FILE}",
            parent=self.root
        )

    def _on_close(self):
        """Handle window close - save state and clean up resources."""
        # Save window position (feat-177)
        try:
            self.settings['window_geometry'] = self.root.geometry()
            save_settings(self.settings)
        except (tk.TclError, OSError):
            pass
        remove_drag_drop(self.root)
        self.root.destroy()

    def _check_theme_change(self):
        """Periodically check for Windows theme changes (feat-133)."""
        try:
            new_theme = _detect_system_theme()
            if new_theme != self._current_theme:
                self._current_theme = new_theme
                logger.info("System theme changed to: %s", new_theme)
                # Theme change doesn't require UI rebuild - ttk adapts automatically
        except Exception:
            pass
        # Schedule next check
        self.root.after(5000, self._check_theme_change)

    def _setup_placeholder(self, entry, placeholder):
        """Set up placeholder text for a ttk.Entry widget."""
        entry._placeholder = placeholder
        entry._has_placeholder = False
        if not entry.get():
            entry._has_placeholder = True
            entry.insert(0, placeholder)
            entry.configure(foreground='gray')
        entry.bind('<FocusIn>', self._on_entry_focus_in)
        entry.bind('<FocusOut>', self._on_entry_focus_out)

    def _on_entry_focus_in(self, event):
        """Remove placeholder text when entry gets focus."""
        entry = event.widget
        if hasattr(entry, '_has_placeholder') and entry._has_placeholder:
            entry.delete(0, tk.END)
            entry.configure(foreground='black')
            entry._has_placeholder = False

    def _on_entry_focus_out(self, event):
        """Restore placeholder text when entry loses focus and is empty."""
        entry = event.widget
        if hasattr(entry, '_placeholder') and not entry.get():
            entry._has_placeholder = True
            entry.insert(0, entry._placeholder)
            entry.configure(foreground='gray')

    def add_files(self):
        """Open file dialog to add multiple XEF files to the queue."""
        initial_dir = self.settings.get('last_input_dir', str(Path.cwd()))
        filenames = filedialog.askopenfilenames(
            title="Select XEF Files",
            initialdir=initial_dir,
            filetypes=[("XEF files", "*.xef"), ("All files", "*.*")]
        )
        if filenames:
            for f in filenames:
                if f not in self._file_queue:
                    self._file_queue.append(f)
            # Save the directory for next session
            self.settings['last_input_dir'] = str(Path(filenames[-1]).parent)
            save_settings(self.settings)
            self._refresh_file_list()
            logger.info("Added %d file(s) to queue (total: %d)",
                       len(filenames), len(self._file_queue))

    def remove_file(self):
        """Remove the selected file from the queue."""
        selection = self.file_listbox.curselection()
        if selection:
            idx = selection[0]
            removed = self._file_queue.pop(idx)
            self._refresh_file_list()
            logger.info("Removed file from queue: %s", removed)

    def _refresh_file_list(self):
        """Update the file listbox to reflect current queue."""
        self.file_listbox.delete(0, tk.END)
        for filepath in self._file_queue:
            self.file_listbox.insert(tk.END, Path(filepath).name)

    def browse_output_directory(self):
        """Open directory dialog to select output directory."""
        initial_dir = self.settings.get('last_output_dir', str(Path.cwd()))
        directory = filedialog.askdirectory(
            title="Select Output Directory",
            initialdir=initial_dir
        )
        if directory:
            self.output_directory.set(directory)
            # Save the directory for next session
            self.settings['last_output_dir'] = directory
            save_settings(self.settings)

    def start_conversion(self):
        """Start the XEF to JPEG conversion process."""
        # Validate inputs
        if not self._file_queue:
            messagebox.showerror("Error",
                                "Please add at least one XEF file to the queue.",
                                parent=self.root)
            return

        if not self.output_directory.get():
            messagebox.showerror("Error", "Please select an output directory.",
                                parent=self.root)
            return

        # Check all input files exist
        for filepath in self._file_queue:
            if not os.path.exists(filepath):
                messagebox.showerror("Error",
                                    f"Input file does not exist:\n{filepath}",
                                    parent=self.root)
                return

        # Disable UI during conversion
        self.is_converting = True
        self.cancel_event.clear()
        self.convert_button.config(state='disabled')
        self.cancel_button.config(state='normal')
        self.progress.start()
        self.status_var.set("Converting...")

        # Run conversion in background thread
        self._conversion_thread = threading.Thread(target=self._conversion_worker, daemon=True)
        self._conversion_thread.start()

    def cancel_conversion(self):
        """Cancel the current conversion."""
        if self.is_converting:
            self.cancel_event.set()
            self.status_var.set("Cancelling...")
            self.cancel_button.config(state='disabled')
            logger.info("Conversion cancelled by user")

    def _update_status(self, message):
        """Thread-safe status update."""
        self.root.after(0, lambda: self.status_var.set(message))

    def _conversion_worker(self):
        """Background worker thread for XEF to JPEG conversion."""
        try:
            # Define progress callback (thread-safe)
            def progress_callback(progress, message):
                if self.cancel_event.is_set():
                    raise InterruptedError("Conversion cancelled by user")
                self._update_status(message)

            # Determine target streams based on selection
            mode = self.stream_mode.get()
            if mode == "depth_only":
                target_streams = [3]  # Depth only
            elif mode == "ir_only":
                target_streams = [4]  # IR only
            elif mode == "color_only":
                target_streams = [7]  # Color only
            elif mode == "all":
                target_streams = [3, 4, 7]  # Depth + IR + Color
            else:
                target_streams = [3, 4]  # Both depth and IR

            output_dir = self.output_directory.get()
            quality = self.jpeg_quality.get()
            total_files = len(self._file_queue)

            all_saved_files = []
            all_frame_types = set()
            last_output_folder = ""

            logger.info("Starting batch conversion of %d file(s)", total_files)

            for i, xef_path in enumerate(self._file_queue):
                if self.cancel_event.is_set():
                    raise InterruptedError("Conversion cancelled by user")

                file_num = f"[{i + 1}/{total_files}] " if total_files > 1 else ""
                self._update_status(f"{file_num}Converting {Path(xef_path).name}...")
                logger.info("Converting file %d/%d: %s", i + 1, total_files, xef_path)

                frame_types, saved_files, output_folder = convert_xef_to_jpeg(
                    xef_path,
                    output_dir,
                    max_frames=100,
                    target_streams=target_streams,
                    callback=progress_callback,
                    quality=quality,
                    use_tqdm=True
                )

                all_saved_files.extend(saved_files)
                all_frame_types.update(frame_types)
                last_output_folder = output_folder
                logger.info("File %d/%d complete: %d frames saved to %s",
                           i + 1, total_files, len(saved_files), output_folder)

            # Check if cancelled before showing success
            if self.cancel_event.is_set():
                self.root.after(0, lambda: self._conversion_finished(
                    cancelled=True))
                return

            logger.info("Batch conversion complete: %d files, %d frames",
                       total_files, len(all_saved_files))

            # Schedule success dialog on main thread
            file_count = len(all_saved_files)
            stream_names = ", ".join(all_frame_types)
            self.root.after(0, lambda: self._conversion_finished(
                success=True,
                message=f"Conversion completed successfully!\n\n"
                        f"Files converted: {total_files}\n"
                        f"Stream types: {stream_names}\n"
                        f"Total frames: {file_count}\n"
                        f"Output saved to:\n{last_output_folder}"))

        except InterruptedError:
            self.root.after(0, lambda: self._conversion_finished(cancelled=True))

        except Exception as e:
            logger.error("Conversion failed: %s", str(e))
            self.root.after(0, lambda: self._conversion_finished(
                error=str(e)))

    def _conversion_finished(self, success=False, error=None, cancelled=False,
                            message=""):
        """Handle conversion completion on the main thread."""
        # Re-enable UI
        self.is_converting = False
        self._conversion_thread = None
        self.convert_button.config(state='normal')
        self.cancel_button.config(state='disabled')
        self.progress.stop()

        if cancelled:
            self.status_var.set("Cancelled")
            messagebox.showinfo("Cancelled", "Conversion was cancelled.",
                               parent=self.root)
        elif success:
            self.status_var.set("Ready")
            messagebox.showinfo("Success", message, parent=self.root)
        elif error:
            self.status_var.set("Ready")
            messagebox.showerror("Error", f"Conversion failed: {error}",
                                parent=self.root)
        else:
            self.status_var.set("Ready")


def main():
    """Main entry point."""
    # Enable DPI awareness on Windows for proper scaling (feat-081)
    if sys.platform == 'win32':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass

    root = tk.Tk()
    app = XEF2JPEGApp(root)

    # Support command-line argument for input file
    if len(sys.argv) > 1:
        input_path = sys.argv[1]
        if os.path.isfile(input_path):
            app._file_queue.append(input_path)
            app._refresh_file_list()

    # Register Ctrl+C signal handler for clean exit (feat-175)
    def signal_handler(sig, frame):
        logger.info("Received interrupt signal, exiting...")
        root.quit()
        root.destroy()

    try:
        signal.signal(signal.SIGINT, signal_handler)
    except (ValueError, OSError):
        pass

    try:
        root.mainloop()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, exiting...")
        try:
            root.quit()
            root.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    main()
