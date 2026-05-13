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
import ctypes
import ctypes.wintypes
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from xef_parser import convert_xef_to_jpeg

# Settings file path (stored in same directory as script)
SETTINGS_FILE = Path(__file__).parent / "xef2jpeg_settings.json"


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

# Window procedure type
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.wintypes.HWND,
                              ctypes.wintypes.UINT, ctypes.wintypes.WPARAM,
                              ctypes.wintypes.LPARAM)

# Store original window procedure and callback reference
_original_wndproc = None
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

    # For all messages (including WM_DROPFILES failures), call original proc
    try:
        if _original_wndproc:
            return user32.CallWindowProcW(_original_wndproc, hwnd, msg, wparam, lparam)
    except (OSError, ValueError):
        pass
    return 0


def remove_drag_drop(root):
    """Remove the drag-and-drop subclass before window destruction.

    Must be called before tkinter destroys the window to avoid
    access violations from stale WndProc pointers.
    """
    global _original_wndproc, _wndproc_ref, _app_ref, _dnd_enabled

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


class XEF2JPEGApp:
    """Main application class for XEF to JPEG conversion."""

    def __init__(self, root):
        """Initialize the application."""
        self.root = root
        self.root.title("XEF2JPEG - Kinect V2 to JPEG Converter")
        self.root.geometry("600x480")
        self.root.minsize(500, 380)

        # Load saved settings
        self.settings = load_settings()

        # Application state
        self.input_file = tk.StringVar()
        default_output = self.settings.get('last_output_dir', str(Path.cwd() / "XEF2JPEG_Output"))
        self.output_directory = tk.StringVar(value=default_output)
        self.stream_mode = tk.StringVar(value="depth_ir")
        self.jpeg_quality = tk.IntVar(value=95)
        self.is_converting = False
        self.cancel_event = threading.Event()
        self._conversion_thread = None

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
        title_label.grid(row=0, column=0, columnspan=3, pady=(0, 20))

        # Input file selection
        ttk.Label(main_frame, text="Input XEF File:").grid(row=1, column=0,
                                                           sticky=tk.W, pady=5)
        ttk.Entry(main_frame, textvariable=self.input_file,
                 width=50).grid(row=1, column=1, sticky=(tk.W, tk.E), padx=5)
        ttk.Button(main_frame, text="Browse...",
                  command=self.browse_input_file).grid(row=1, column=2, pady=5)

        # Output directory selection
        ttk.Label(main_frame, text="Output Directory:").grid(row=2, column=0,
                                                             sticky=tk.W, pady=5)
        ttk.Entry(main_frame, textvariable=self.output_directory,
                 width=50).grid(row=2, column=1, sticky=(tk.W, tk.E), padx=5)
        ttk.Button(main_frame, text="Browse...",
                  command=self.browse_output_directory).grid(row=2, column=2, pady=5)

        # Stream type selection
        ttk.Label(main_frame, text="Stream Type:").grid(row=3, column=0,
                                                        sticky=tk.W, pady=5)
        stream_combo = ttk.Combobox(main_frame, textvariable=self.stream_mode,
                                   values=["depth_ir", "depth_only", "ir_only"],
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

        self.convert_button = ttk.Button(button_frame, text="Start Conversion",
                                        command=self.start_conversion)
        self.convert_button.pack(side=tk.LEFT, padx=5)

        self.cancel_button = ttk.Button(button_frame, text="Cancel",
                                       command=self.cancel_conversion, state='disabled')
        self.cancel_button.pack(side=tk.LEFT, padx=5)

    def _show_sdk_info(self):
        """Show Kinect SDK status information on startup."""
        messagebox.showinfo("Kinect SDK Status", self.sdk_message)

    def _on_close(self):
        """Handle window close - clean up resources before destroying."""
        remove_drag_drop(self.root)
        self.root.destroy()

    def browse_input_file(self):
        """Open file dialog to select input XEF file."""
        initial_dir = self.settings.get('last_input_dir', str(Path.cwd()))
        filename = filedialog.askopenfilename(
            title="Select XEF File",
            initialdir=initial_dir,
            filetypes=[("XEF files", "*.xef"), ("All files", "*.*")]
        )
        if filename:
            self.input_file.set(filename)
            # Save the directory for next session
            self.settings['last_input_dir'] = str(Path(filename).parent)
            save_settings(self.settings)

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
        if not self.input_file.get():
            messagebox.showerror("Error", "Please select an input XEF file.")
            return

        if not self.output_directory.get():
            messagebox.showerror("Error", "Please select an output directory.")
            return

        if not os.path.exists(self.input_file.get()):
            messagebox.showerror("Error", "Input file does not exist.")
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
            else:
                target_streams = [3, 4]  # Both depth and IR

            # Perform conversion
            frame_types, saved_files, output_folder = convert_xef_to_jpeg(
                self.input_file.get(),
                self.output_directory.get(),
                max_frames=100,  # Limit for performance
                target_streams=target_streams,
                callback=progress_callback,
                quality=self.jpeg_quality.get()
            )

            # Check if cancelled before showing success
            if self.cancel_event.is_set():
                self.root.after(0, lambda: self._conversion_finished(
                    cancelled=True))
                return

            # Schedule success dialog on main thread
            file_count = len(saved_files)
            stream_names = ", ".join(frame_types)
            self.root.after(0, lambda: self._conversion_finished(
                success=True,
                message=f"Conversion completed successfully!\n\n"
                        f"Stream types: {stream_names}\n"
                        f"Frames converted: {file_count}\n"
                        f"Output saved to:\n{output_folder}"))

        except InterruptedError:
            self.root.after(0, lambda: self._conversion_finished(cancelled=True))

        except Exception as e:
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
            messagebox.showinfo("Cancelled", "Conversion was cancelled.")
        elif success:
            self.status_var.set("Ready")
            messagebox.showinfo("Success", message)
        elif error:
            self.status_var.set("Ready")
            messagebox.showerror("Error", f"Conversion failed: {error}")
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
            app.input_file.set(input_path)

    root.mainloop()


if __name__ == "__main__":
    main()
