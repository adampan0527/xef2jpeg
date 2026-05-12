"""
XEF File Parser for Kinect V2 recordings

Parses .XEF files captured by Kinect V2 sensors and extracts
color and depth frames for conversion to JPEG format.
"""

import struct
import numpy as np
from pathlib import Path
from PIL import Image


class XEFParser:
    """Parser for Kinect V2 XEF event stream files."""

    # Kinect V2 stream types
    STREAM_COLOR = 0
    STREAM_DEPTH = 1
    STREAM_IR = 2

    # Kinect V2 image dimensions
    COLOR_WIDTH = 1920
    COLOR_HEIGHT = 1080
    DEPTH_WIDTH = 512
    DEPTH_HEIGHT = 424

    def __init__(self, filepath, max_frames=None):
        """Initialize parser with XEF file path."""
        self.filepath = Path(filepath)
        self.frames = []
        self.metadata = {}
        self.max_frames = max_frames  # Limit frames to extract (None = all)

    def parse(self):
        """Parse XEF file and extract frames."""
        with open(self.filepath, 'rb') as f:
            # Read and verify header
            header = f.read(8)
            if not header.startswith(b'EVENTS'):
                raise ValueError("Invalid XEF file format")

            # Skip to frame data area
            f.seek(0x100)  # Start searching after header area

            # Read file content
            f.seek(0, 2)  # Go to end
            file_size = f.tell()
            f.seek(0)  # Back to start

            # Read entire file for pattern matching
            data = f.read()

            # Extract frames using pattern matching
            self._extract_frames(data)

        return self.frames

    def _extract_frames(self, data):
        """Extract image frames from binary data."""
        # Search for color frames (1920x1080 BGRA = 8,294,400 bytes)
        color_frame_size = self.COLOR_WIDTH * self.COLOR_HEIGHT * 4

        # Search for depth frames (512x424 uint16 = 434,176 bytes)
        depth_frame_size = self.DEPTH_WIDTH * self.DEPTH_HEIGHT * 2

        # Try to find and extract frames
        self._find_bgra_frames(data)
        self._find_depth_frames(data)

    def _find_bgra_frames(self, data):
        """Find and extract BGRA color frames."""
        frame_size = self.COLOR_WIDTH * self.COLOR_HEIGHT * 4

        # Count color frames found
        color_count = 0
        max_color = self.max_frames // 2 if self.max_frames else None

        # Search for frame boundaries
        # Look for patterns that indicate frame starts
        for offset in range(0, len(data) - frame_size, frame_size // 2):
            # Check frame limit
            if max_color and color_count >= max_color:
                break

            # Try to decode as BGRA image
            chunk = data[offset:offset + frame_size]
            if len(chunk) == frame_size:
                # Validate: check if data looks like image data
                # (not all zeros, not random noise)
                sample = np.frombuffer(chunk[:1000], dtype=np.uint8)
                if np.std(sample) > 10 and np.std(sample) < 100:
                    # Potential frame found
                    try:
                        img_array = np.frombuffer(chunk, dtype=np.uint8)
                        img_array = img_array.reshape((self.COLOR_HEIGHT, self.COLOR_WIDTH, 4))

                        # Convert BGRA to RGB
                        rgb_array = img_array[:, :, [2, 1, 0]]  # BGR to RGB

                        # Check if image has reasonable content
                        if np.mean(rgb_array) > 10 and np.mean(rgb_array) < 240:
                            self.frames.append({
                                'type': 'color',
                                'data': rgb_array,
                                'offset': offset
                            })
                            color_count += 1
                            # Skip ahead to avoid duplicates
                            offset += frame_size
                    except Exception:
                        continue

    def _find_depth_frames(self, data):
        """Find and extract depth frames."""
        frame_size = self.DEPTH_WIDTH * self.DEPTH_HEIGHT * 2

        # Count depth frames found
        depth_count = 0
        max_depth = self.max_frames // 2 if self.max_frames else None

        # Search for depth data patterns
        for offset in range(0, len(data) - frame_size, frame_size // 2):
            # Check frame limit
            if max_depth and depth_count >= max_depth:
                break

            chunk = data[offset:offset + frame_size]
            if len(chunk) == frame_size:
                try:
                    # Depth data is uint16
                    depth_array = np.frombuffer(chunk, dtype=np.uint16)

                    # Validate depth values (typically 500-4500mm)
                    valid_range = (depth_array > 100) & (depth_array < 8000)
                    if np.sum(valid_range) > frame_size // 4:
                        # Valid depth frame
                        depth_image = depth_array.reshape((self.DEPTH_HEIGHT, self.DEPTH_WIDTH))

                        # Normalize to 0-255 for visualization
                        depth_normalized = np.zeros_like(depth_image, dtype=np.uint8)
                        mask = (depth_image > 100) & (depth_image < 8000)
                        if np.any(mask):
                            depth_normalized[mask] = np.interp(
                                depth_image[mask],
                                [depth_image[mask].min(), depth_image[mask].max()],
                                [0, 255]
                            ).astype(np.uint8)

                        self.frames.append({
                            'type': 'depth',
                            'data': depth_normalized,
                            'raw_data': depth_image,
                            'offset': offset
                        })
                        depth_count += 1
                except Exception:
                    continue

    def save_frames_to_jpeg(self, output_dir, base_name=None):
        """Save extracted frames as JPEG files."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        if base_name is None:
            base_name = self.filepath.stem

        saved_files = []

        for i, frame in enumerate(self.frames):
            if frame['type'] == 'color':
                filename = f"{base_name}_color_{i:04d}.jpg"
                filepath = output_path / filename

                img = Image.fromarray(frame['data'], 'RGB')
                img.save(str(filepath), 'JPEG', quality=95)
                saved_files.append(str(filepath))

            elif frame['type'] == 'depth':
                filename = f"{base_name}_depth_{i:04d}.jpg"
                filepath = output_path / filename

                img = Image.fromarray(frame['data'], 'L')
                img.save(str(filepath), 'JPEG', quality=95)
                saved_files.append(str(filepath))

        return saved_files


def convert_xef_to_jpeg(xef_path, output_dir, max_frames=None, callback=None):
    """
    Convert XEF file to JPEG images.

    Args:
        xef_path: Path to input .xef file
        output_dir: Path to output directory
        max_frames: Maximum number of frames to extract (None = all)
        callback: Optional callback function(progress, message)

    Returns:
        List of saved JPEG file paths
    """
    parser = XEFParser(xef_path, max_frames=max_frames)

    if callback:
        callback(0.1, "Parsing XEF file...")

    frames = parser.parse()

    if not frames:
        raise ValueError("No frames found in XEF file")

    if callback:
        callback(0.5, f"Found {len(frames)} frames, converting to JPEG...")

    saved_files = parser.save_frames_to_jpeg(output_dir)

    if callback:
        callback(1.0, "Conversion complete")

    return saved_files
