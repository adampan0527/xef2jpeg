"""
XEF File Parser for Kinect V2 recordings

Parses .XEF files captured by Kinect V2 sensors by reading the actual
file structure: EVENTS1 header, stream descriptors, segment descriptors,
and interleaved event data segments.

XEF File Structure:
  1. File Header (44 bytes): EVENTS1 magic, stream count, timestamps
  2. Stream Descriptors: 0x3333-tagged entries describing each stream
  3. Event Data: Interleaved 32-byte segment descriptors + frame data
  4. Tail Index (last 8192 bytes): Optional seek table

Stream Types:
  1 = Body (skeleton)     - variable size
  2 = Calibration         - variable size
  3 = Depth               - 512x424 uint16 (434,176 bytes)
  4 = IR                  - 512x424 uint16 (434,176 bytes)
  5 = Opaque              - 640 bytes metadata
  6 = Telemetry           - 20 bytes sensor data
"""

import struct
import numpy as np
from pathlib import Path
from PIL import Image


class XEFParser:
    """Parser for Kinect V2 XEF event stream files."""

    # Stream type constants
    STREAM_BODY = 1
    STREAM_CALIBRATION = 2
    STREAM_DEPTH = 3
    STREAM_IR = 4
    STREAM_OPAQUE = 5
    STREAM_TELEMETRY = 6

    STREAM_NAMES = {
        1: 'body',
        2: 'calibration',
        3: 'depth',
        4: 'ir',
        5: 'opaque',
        6: 'telemetry',
    }

    # Kinect V2 depth/IR dimensions
    DEPTH_WIDTH = 512
    DEPTH_HEIGHT = 424
    DEPTH_FRAME_SIZE = DEPTH_WIDTH * DEPTH_HEIGHT * 2  # 434,176 bytes

    # Segment descriptor size
    SEGMENT_DESC_SIZE = 32

    # File header magic
    HEADER_MAGIC = b'EVENTS1\x00'

    # Stream descriptor marker
    STREAM_DESC_MARKER = 0x3333

    def __init__(self, filepath, max_frames=None, target_streams=None):
        """
        Initialize parser with XEF file path.

        Args:
            filepath: Path to .xef file
            max_frames: Max frames per stream type to extract (None = unlimited)
            target_streams: List of stream type IDs to extract (default: [3, 4])
        """
        self.filepath = Path(filepath)
        self.frames = []
        self.streams = {}  # stream_type -> {'name': ..., 'frame_count': ...}
        self.max_frames = max_frames
        self.target_streams = target_streams if target_streams is not None else [self.STREAM_DEPTH, self.STREAM_IR]

    def parse(self):
        """Parse XEF file and extract frames from target streams."""
        with open(self.filepath, 'rb') as f:
            # 1. Read and validate file header
            self._read_header(f)

            # 2. Read stream descriptors
            self._read_stream_descriptors(f)

            # 3. Find start of event data
            event_start = self._find_event_data_start(f)

            # 4. Extract frames sequentially from event data
            self._extract_frames_sequential(f, event_start)

        return self.frames

    def _read_header(self, f):
        """Read and validate the 44-byte file header."""
        f.seek(0)
        header = f.read(44)

        if len(header) < 44:
            raise ValueError("File too small to be a valid XEF file")

        if not header.startswith(self.HEADER_MAGIC):
            raise ValueError(
                f"Invalid XEF file: expected '{self.HEADER_MAGIC}' magic, "
                f"got '{header[:8]}'"
            )

        magic, unknown1, stream_count, timestamp1, field_24, field_28, field_32, field_36, field_40 = \
            struct.unpack_from('<8sII Q I I I I I', header)

        self.header = {
            'magic': magic,
            'unknown1': unknown1,
            'stream_count': stream_count,
            'timestamp1': timestamp1,
            'field_24': field_24,
            'field_28': field_28,
            'field_32': field_32,
            'field_36': field_36,
            'field_40': field_40,
        }
        self.stream_count = stream_count

    def _read_stream_descriptors(self, f):
        """Read stream descriptors from the area after the file header."""
        # Stream descriptors are between 0x2C and the start of event data.
        # They are marked with 0x3333.
        f.seek(0x2C)

        # Read a generous chunk to find all stream descriptors
        chunk = f.read(0x2000)  # 8KB should be enough

        stream_types_found = []
        pos = 0
        marker_bytes = struct.pack('<H', self.STREAM_DESC_MARKER)

        while pos < len(chunk) - 2:
            pos = chunk.find(marker_bytes, pos)
            if pos < 0:
                break

            region = chunk[pos:pos + 60]
            if len(region) < 16:
                pos += 2
                continue

            # The 2-byte value after the marker determines the stream type offset
            marker_val = struct.unpack_from('<H', region, 2)[0]

            # For marker_val=0x0018 (24), the stream type is at offset 28
            # For other marker_val values (0, 4), the stream type is at offset 12
            if marker_val == 24:
                # Special case: longer descriptor format
                type_offset = 28
            else:
                # Standard descriptor format
                type_offset = 12

            if len(region) >= type_offset + 4:
                stream_type = struct.unpack_from('<I', region, type_offset)[0]
                if 1 <= stream_type <= 10:
                    if stream_type not in [s['type'] for s in stream_types_found]:
                        stream_types_found.append({
                            'type': stream_type,
                            'name': self.STREAM_NAMES.get(stream_type, f'unknown_{stream_type}'),
                            'offset': 0x2C + pos,
                        })

            pos += 2

        self.streams = stream_types_found

        # If no streams found from markers, check for default Kinect V2 streams
        if not stream_types_found:
            self.streams = [
                {'type': 1, 'name': 'body', 'offset': 0},
                {'type': 2, 'name': 'calibration', 'offset': 0},
                {'type': 3, 'name': 'depth', 'offset': 0},
                {'type': 4, 'name': 'ir', 'offset': 0},
                {'type': 5, 'name': 'opaque', 'offset': 0},
                {'type': 6, 'name': 'telemetry', 'offset': 0},
            ]

    def _find_event_data_start(self, f):
        """
        Find the start of event (segment) data.

        The event data begins after the stream descriptors area.
        We scan from the header end to find the first valid segment descriptor.
        """
        # Start scanning from after the stream descriptor area
        # Stream descriptors go from 0x2C to roughly 0x1000-0x2000
        f.seek(0x2C)

        # Read a chunk and find the last stream descriptor 0x3333 marker
        chunk = f.read(0x2000)
        marker_bytes = struct.pack('<H', self.STREAM_DESC_MARKER)
        last_marker_pos = 0
        pos = 0

        while pos < len(chunk) - 2:
            pos = chunk.find(marker_bytes, pos)
            if pos < 0:
                break
            last_marker_pos = 0x2C + pos
            pos += 2

        # Event data starts after the last stream descriptor
        # Each stream descriptor is roughly 200 bytes
        scan_start = max(0x1000, last_marker_pos + 200)

        # Now scan for the first valid segment descriptor
        f.seek(0)
        file_size = f.seek(0, 2)
        f.seek(scan_start)

        # Read chunks to find first valid segment descriptor
        search_limit = min(scan_start + 5 * 1024 * 1024, file_size)  # Search up to 5MB
        f.seek(scan_start)

        while f.tell() < search_limit - self.SEGMENT_DESC_SIZE:
            offset = f.tell()
            desc = f.read(self.SEGMENT_DESC_SIZE)
            if len(desc) < self.SEGMENT_DESC_SIZE:
                break

            stream_type, frame_size = struct.unpack_from('<II', desc, 0)

            # Validate segment descriptor
            if self._is_valid_segment(desc, file_size):
                # Found first segment
                return offset

            # Step forward one byte to find alignment
            f.seek(offset + 1)

        raise ValueError("Could not find event data in XEF file")

    def _is_valid_segment(self, desc, file_size):
        """Check if a 32-byte block looks like a valid segment descriptor."""
        try:
            stream_type, frame_size, session_id, seg_counter, frame_size2, event_index, padding = \
                struct.unpack_from('<IIQIIII', desc)
        except struct.error:
            return False

        # Stream type must be in valid range
        if stream_type < 1 or stream_type > 10:
            return False

        # Frame size must be positive and match the duplicate
        if frame_size <= 0 or frame_size != frame_size2:
            return False

        # Frame size must be reasonable
        if frame_size > 10_000_000:  # 10 MB max
            return False

        # Session ID should be reasonable (not all zeros for a valid recording)
        # (Relaxed: allow zeros for initial segments)

        return True

    def _extract_frames_sequential(self, f, start_offset):
        """
        Extract frames by sequentially reading segment descriptors and data.

        Starting from start_offset, read 32-byte descriptors, validate them,
        and for target stream types, read and store the frame data.
        """
        f.seek(0)
        file_size = f.seek(0, 2)

        # Track frame counts per stream type
        frame_counts = {st: 0 for st in self.target_streams}
        max_per_stream = self.max_frames

        offset = start_offset

        while offset < file_size - self.SEGMENT_DESC_SIZE:
            # Check if we have enough frames
            if max_per_stream is not None:
                if all(frame_counts[st] >= max_per_stream for st in self.target_streams):
                    break

            f.seek(offset)
            desc = f.read(self.SEGMENT_DESC_SIZE)
            if len(desc) < self.SEGMENT_DESC_SIZE:
                break

            if not self._is_valid_segment(desc, file_size):
                # Invalid segment - try next byte alignment
                offset += 4
                continue

            stream_type, frame_size, session_id, seg_counter, frame_size2, event_index, padding = \
                struct.unpack_from('<IIQIIII', desc)

            # Validate that frame data fits within file
            data_start = offset + self.SEGMENT_DESC_SIZE
            if data_start + frame_size > file_size:
                break

            # Extract frame if it's a target stream type
            if stream_type in self.target_streams:
                if max_per_stream is None or frame_counts[stream_type] < max_per_stream:
                    f.seek(data_start)
                    frame_data = f.read(frame_size)

                    if len(frame_data) == frame_size:
                        frame_info = self._process_frame(
                            stream_type, frame_data, frame_counts[stream_type], seg_counter, event_index
                        )
                        if frame_info:
                            self.frames.append(frame_info)
                            frame_counts[stream_type] += 1

            # Move to next segment
            offset = data_start + frame_size

    def _process_frame(self, stream_type, raw_data, frame_index, seg_counter, event_index):
        """Process raw frame data into a normalized image array."""
        if stream_type == self.STREAM_DEPTH:
            return self._process_depth_frame(raw_data, frame_index, seg_counter, event_index)
        elif stream_type == self.STREAM_IR:
            return self._process_ir_frame(raw_data, frame_index, seg_counter, event_index)
        return None

    def _process_depth_frame(self, raw_data, frame_index, seg_counter, event_index):
        """Process a depth frame (512x424 uint16) into a normalized 8-bit image."""
        try:
            depth_array = np.frombuffer(raw_data, dtype=np.uint16).reshape(
                (self.DEPTH_HEIGHT, self.DEPTH_WIDTH)
            )
        except ValueError:
            return None

        # Normalize to 0-255 for visualization
        depth_normalized = np.zeros_like(depth_array, dtype=np.uint8)

        # Valid depth range for Kinect V2: typically 500-4500mm
        # Values of 0 typically indicate invalid/no data
        valid_mask = (depth_array > 0) & (depth_array < 8000)

        if np.any(valid_mask):
            valid_values = depth_array[valid_mask]
            vmin, vmax = valid_values.min(), valid_values.max()
            if vmax > vmin:
                depth_normalized[valid_mask] = (
                    (valid_values - vmin) * 255.0 / (vmax - vmin)
                ).astype(np.uint8)
            else:
                depth_normalized[valid_mask] = 128

        return {
            'type': 'depth',
            'stream_type': self.STREAM_DEPTH,
            'stream_name': 'depth',
            'data': depth_normalized,
            'raw_data': depth_array,
            'index': frame_index,
            'seg_counter': seg_counter,
            'event_index': event_index,
        }

    def _process_ir_frame(self, raw_data, frame_index, seg_counter, event_index):
        """Process an IR frame (512x424 uint16) into a normalized 8-bit image."""
        try:
            ir_array = np.frombuffer(raw_data, dtype=np.uint16).reshape(
                (self.DEPTH_HEIGHT, self.DEPTH_WIDTH)
            )
        except ValueError:
            return None

        # Normalize to 0-255 for visualization
        # IR values are typically 0-65535, with real data in a smaller range
        ir_normalized = np.zeros_like(ir_array, dtype=np.uint8)

        # Filter out obvious invalid values (0xFFFF and 0 are typically invalid)
        valid_mask = (ir_array > 0) & (ir_array < 0xFFFE)

        if np.any(valid_mask):
            valid_values = ir_array[valid_mask]
            vmin, vmax = np.percentile(valid_values, [2, 98])
            if vmax > vmin:
                # Clip to percentile range and normalize
                clipped = np.clip(ir_array, vmin, vmax)
                ir_normalized = ((clipped - vmin) * 255.0 / (vmax - vmin)).astype(np.uint8)

        return {
            'type': 'ir',
            'stream_type': self.STREAM_IR,
            'stream_name': 'ir',
            'data': ir_normalized,
            'raw_data': ir_array,
            'index': frame_index,
            'seg_counter': seg_counter,
            'event_index': event_index,
        }

    def save_frames_to_jpeg(self, output_dir, base_name=None):
        """
        Save extracted frames as JPEG files.

        Depth frames are saved to output_dir/Depth/
        IR frames are saved to output_dir/IR/

        Naming format: {basename}_{stream_name}_{index:04d}.jpg

        Returns:
            List of saved file paths.
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        if base_name is None:
            base_name = self.filepath.stem

        # Stream type to folder name mapping
        STREAM_FOLDER_NAMES = {
            'depth': 'Depth',
            'ir': 'IR',
            'body': 'Body',
            'calibration': 'Calibration',
            'opaque': 'Opaque',
            'telemetry': 'Telemetry',
        }

        # Create stream-specific subdirectories
        subdirs = {}
        saved_files = []

        for frame in self.frames:
            stream_name = frame.get('stream_name', frame['type'])

            # Ensure subdirectory exists for this stream type
            if stream_name not in subdirs:
                folder_name = STREAM_FOLDER_NAMES.get(stream_name, stream_name.capitalize())
                subdir_path = output_path / folder_name
                subdir_path.mkdir(parents=True, exist_ok=True)
                subdirs[stream_name] = subdir_path

            filename = f"{base_name}_{stream_name}_{frame['index']:04d}.jpg"
            filepath = subdirs[stream_name] / filename

            # Save as grayscale
            img = Image.fromarray(frame['data'], 'L')
            img.save(str(filepath), 'JPEG', quality=95)

            saved_files.append(str(filepath))

        return saved_files

    def get_stream_info(self):
        """Return information about streams found in the file."""
        return [
            {'type': s['type'], 'name': s['name']}
            for s in self.streams
        ]

    def has_stream(self, stream_type):
        """Check if the file has a specific stream type."""
        return any(s['type'] == stream_type for s in self.streams)


def convert_xef_to_jpeg(xef_path, output_dir, max_frames=None, target_streams=None, callback=None):
    """
    Convert XEF file to JPEG images.

    Creates a timestamped folder inside output_dir:
        {output_dir}/{YYYY_MM_DD_HH_MM_SS}/
            Depth/   (if depth frames)
            IR/      (if IR frames)

    Args:
        xef_path: Path to input .xef file
        output_dir: Path to output directory (created if not exists)
        max_frames: Maximum number of frames per stream type (None = unlimited)
        target_streams: List of stream types to extract (default: [3, 4] = depth, ir)
        callback: Optional callback function(progress, message)

    Returns:
        Tuple of (stream_types_found, saved_file_paths, output_folder_path)
    """
    from datetime import datetime

    # Create timestamped output folder
    timestamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    base_output = Path(output_dir)
    session_folder = base_output / timestamp
    session_folder.mkdir(parents=True, exist_ok=True)

    if callback:
        callback(0.0, "Validating XEF file...")

    parser = XEFParser(xef_path, max_frames=max_frames, target_streams=target_streams)
    parser.parse()

    if callback:
        stream_info = parser.get_stream_info()
        available = [s['name'] for s in stream_info]
        callback(0.1, f"Found streams: {', '.join(available)}")

    frames = parser.frames

    if not frames:
        # Clean up empty timestamp folder
        try:
            session_folder.rmdir()
        except OSError:
            pass

        stream_info = parser.get_stream_info()
        available_names = [s['name'] for s in stream_info]
        raise ValueError(
            f"No frames extracted from target streams. "
            f"Available streams: {', '.join(available_names) if available_names else 'none'}"
        )

    if callback:
        callback(0.5, f"Found {len(frames)} frames, converting to JPEG...")

    saved_files = parser.save_frames_to_jpeg(session_folder)

    if callback:
        callback(1.0, "Conversion complete")

    frame_types = list(set(f['stream_name'] for f in frames))
    return frame_types, saved_files, str(session_folder)
