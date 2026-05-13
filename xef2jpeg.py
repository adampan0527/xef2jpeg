#!/usr/bin/env python3
"""
XEF2JPEG - Convert Kinect V2 XEF files to JPEG format

A Windows desktop application for converting .XEF files captured by Kinect V2
sensors to JPEG image format.

Target Platform: Windows 10 & Windows 11
"""

import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from xef_parser import convert_xef_to_jpeg


class XEF2JPEGApp:
    """Main application class for XEF to JPEG conversion."""

    def __init__(self, root):
        """Initialize the application."""
        self.root = root
        self.root.title("XEF2JPEG - Kinect V2 to JPEG Converter")
        self.root.geometry("600x450")

        # Application state
        self.input_file = tk.StringVar()
        self.output_directory = tk.StringVar(value=str(Path.cwd() / "XEF2JPEG_Output"))
        self.stream_mode = tk.StringVar(value="depth_ir")
        self.is_converting = False

        # Setup UI
        self.setup_ui()

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

        # Progress bar
        self.progress = ttk.Progressbar(main_frame, mode='indeterminate')
        self.progress.grid(row=4, column=0, columnspan=3, sticky=(tk.W, tk.E),
                          pady=20)

        # Status label
        self.status_var = tk.StringVar(value="Ready")
        status_label = ttk.Label(main_frame, textvariable=self.status_var)
        status_label.grid(row=5, column=0, columnspan=3, pady=5)

        # Convert button
        self.convert_button = ttk.Button(main_frame, text="Start Conversion",
                                        command=self.start_conversion)
        self.convert_button.grid(row=6, column=0, columnspan=3, pady=10)

    def browse_input_file(self):
        """Open file dialog to select input XEF file."""
        filename = filedialog.askopenfilename(
            title="Select XEF File",
            initialdir=str(Path.cwd()),
            filetypes=[("XEF files", "*.xef"), ("All files", "*.*")]
        )
        if filename:
            self.input_file.set(filename)

    def browse_output_directory(self):
        """Open directory dialog to select output directory."""
        directory = filedialog.askdirectory(
            title="Select Output Directory",
            initialdir=str(Path.cwd())
        )
        if directory:
            self.output_directory.set(directory)

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
        self.convert_button.config(state='disabled')
        self.progress.start()
        self.status_var.set("Converting...")

        # Perform conversion (placeholder - actual implementation needed)
        self.root.after(100, self.perform_conversion)

    def perform_conversion(self):
        """Perform the actual XEF to JPEG conversion."""
        try:
            # Update status
            self.status_var.set("Parsing XEF file...")
            self.root.update()

            # Define progress callback
            def progress_callback(progress, message):
                self.status_var.set(message)
                self.root.update()

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
                callback=progress_callback
            )

            # Show success message with stream type info
            file_count = len(saved_files)
            stream_names = ", ".join(frame_types)
            messagebox.showinfo("Success",
                              f"Conversion completed successfully!\n\n"
                              f"Stream types: {stream_names}\n"
                              f"Frames converted: {file_count}\n"
                              f"Output saved to:\n{output_folder}")

        except Exception as e:
            messagebox.showerror("Error", f"Conversion failed: {str(e)}")

        finally:
            # Re-enable UI
            self.is_converting = False
            self.convert_button.config(state='normal')
            self.progress.stop()
            self.status_var.set("Ready")


def main():
    """Main entry point."""
    root = tk.Tk()
    app = XEF2JPEGApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
