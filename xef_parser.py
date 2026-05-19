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
  7 = Color               - 1920x1080 BGRA (8,294,400 bytes)
"""

import struct
import logging
import numpy as np
from pathlib import Path
from PIL import Image, JpegImagePlugin

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

logger = logging.getLogger(__name__)

# Log file path (shared with xef2jpeg.py)
LOG_FILE = Path(__file__).parent / "xef2jpeg.log"


class XEFParser:
    """Parser for Kinect V2 XEF event stream files."""

    # Stream type constants
    STREAM_BODY = 1
    STREAM_CALIBRATION = 2
    STREAM_DEPTH = 3
    STREAM_IR = 4
    STREAM_OPAQUE = 5
    STREAM_TELEMETRY = 6
    STREAM_COLOR = 7

    STREAM_NAMES = {
        1: 'body',
        2: 'calibration',
        3: 'depth',
        4: 'ir',
        5: 'opaque',
        6: 'telemetry',
        7: 'color',
    }

    # Kinect V2 depth/IR dimensions
    DEPTH_WIDTH = 512
    DEPTH_HEIGHT = 424
    DEPTH_FRAME_SIZE = DEPTH_WIDTH * DEPTH_HEIGHT * 2  # 434,176 bytes

    # Kinect V2 color dimensions
    COLOR_WIDTH = 1920
    COLOR_HEIGHT = 1080
    COLOR_FRAME_SIZE = COLOR_WIDTH * COLOR_HEIGHT * 4  # 8,294,400 bytes (BGRA)

    # Segment descriptor size (28 bytes: stream_type(4) + frame_size(4) +
    # session_id(8) + hash(4) + frame_size2(4) + event_index(4))
    SEGMENT_DESC_SIZE = 28

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

    def parse(self, use_tqdm=True, callback=None):
        """Parse XEF file and extract frames from target streams.

        Args:
            use_tqdm: Show tqdm progress bar in terminal (default True)
            callback: Optional callback(progress, message) for GUI updates
        """
        with open(self.filepath, 'rb') as f:
            # 1. Read and validate file header
            self._read_header(f)

            # 2. Read stream descriptors
            self._read_stream_descriptors(f)

            # 3. Find start of event data
            if callback:
                callback(0.05, "Scanning for frame data...")
            try:
                event_start = self._find_event_data_start(f)
            except ValueError:
                event_start = None

            # 4. Extract frames sequentially from event data
            if event_start is not None:
                self._extract_frames_sequential(f, event_start, use_tqdm=use_tqdm,
                                                callback=callback)

            # 5. If descriptor-based extraction found nothing, try raw scanning
            if not self.frames:
                logger.info("No frames from descriptors, trying raw frame scanning")
                if callback:
                    callback(0.1, "Scanning for raw frame data...")
                self._extract_frames_raw(f, use_tqdm=use_tqdm, callback=callback)

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

        logger.info("XEF header: streams=%d, unknown1=%d", stream_count, unknown1)

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

        stream_names = [s['name'] for s in self.streams]
        logger.info("Streams found: %s", ', '.join(stream_names))

    def _find_event_data_start(self, f):
        """
        Find the start of event (segment) data.

        The event data begins after stream descriptors and calibration metadata.
        XEF files have a large metadata/calibration section before the actual
        interleaved event frames (depth, IR, body, etc.).  We skip calibration
        (type 2) descriptors because they use a different format.
        """
        f.seek(0)
        file_size = f.seek(0, 2)

        # Stream descriptors and calibration metadata can extend well past 5 MB,
        # so search up to 200 MB for the first non-calibration event segment.
        scan_start = 0x1000
        search_limit = min(scan_start + 200 * 1024 * 1024, file_size)
        f.seek(scan_start)

        while f.tell() < search_limit - self.SEGMENT_DESC_SIZE:
            offset = f.tell()
            desc = f.read(self.SEGMENT_DESC_SIZE)
            if len(desc) < self.SEGMENT_DESC_SIZE:
                break

            if self._is_valid_segment(desc, file_size):
                stream_type = struct.unpack_from('<I', desc, 0)[0]
                # Skip calibration descriptors (type 2) — they use a larger
                # header format and are part of the metadata section.
                if stream_type != self.STREAM_CALIBRATION:
                    return offset

            f.seek(offset + 1)

        raise ValueError("Could not find event data in XEF file")

    def _is_valid_segment(self, desc, file_size):
        """Check if a 28-byte block looks like a valid segment descriptor.

        XEF segment descriptor layout (28 bytes):
          bytes  0-3:  stream_type (uint32)
          bytes  4-7:  frame_size (uint32)
          bytes  8-15: session_id (uint64)
          bytes 16-19: hash/unknown (uint32) — skipped
          bytes 20-23: frame_size2 (uint32) — must equal frame_size
          bytes 24-27: event_index (uint32)
        """
        if len(desc) < self.SEGMENT_DESC_SIZE:
            return False
        try:
            stream_type, frame_size = struct.unpack_from('<II', desc, 0)
            frame_size2 = struct.unpack_from('<I', desc, 20)[0]
        except struct.error:
            return False

        # Stream type must be in valid range
        if stream_type < 1 or stream_type > 10:
            return False

        # Frame size must be positive and match the duplicate
        if frame_size <= 0 or frame_size != frame_size2:
            return False

        # Frame size must be reasonable
        if frame_size > 20_000_000:  # 20 MB max (color frames are ~8.3MB)
            return False

        return True

    def _extract_frames_sequential(self, f, start_offset, use_tqdm=True, callback=None):
        """
        Extract frames by sequentially reading segment descriptors and data.

        Starting from start_offset, read 28-byte descriptors, validate them,
        and for target stream types, read and store the frame data.

        Each segment occupies: descriptor(28) + frame_data(frame_size) + trailer(0-8).
        We search a small window after each frame to locate the next descriptor
        reliably, since the trailer size varies slightly between segment types.

        Args:
            f: Open file object
            start_offset: Starting offset for event data
            use_tqdm: Show tqdm progress bar in terminal
            callback: Optional callback(progress, message) for GUI updates
        """
        f.seek(0)
        file_size = f.seek(0, 2)

        # Track frame counts per stream type
        frame_counts = {st: 0 for st in self.target_streams}
        max_per_stream = self.max_frames

        offset = start_offset
        desc_size = self.SEGMENT_DESC_SIZE  # 28

        # Diagnostic counters
        segments_found = 0
        segments_rejected = 0
        reject_reasons = {}  # reason -> count
        target_mismatch_count = 0

        # Set up progress bar
        file_stem = self.filepath.stem
        pbar = None
        if use_tqdm and tqdm is not None:
            try:
                pbar = tqdm(
                    desc=f"[{file_stem}] Extracting frames",
                    unit=" frames",
                    dynamic_ncols=True,
                    leave=False
                )
            except Exception:
                pbar = None

        try:
            while offset < file_size - desc_size:
                # Check if we have enough frames
                if max_per_stream is not None:
                    if all(frame_counts[st] >= max_per_stream for st in self.target_streams):
                        break

                f.seek(offset)
                desc = f.read(desc_size)
                if len(desc) < desc_size:
                    break

                if not self._is_valid_segment(desc, file_size):
                    # Invalid segment - try next byte alignment
                    segments_rejected += 1
                    if segments_rejected <= 5:
                        try:
                            st, fs = struct.unpack_from('<II', desc, 0)
                            reject_key = f"type={st},size={fs}"
                            reject_reasons[reject_key] = reject_reasons.get(reject_key, 0) + 1
                            logger.debug("Rejected segment at 0x%X: type=%d, size=%d",
                                        offset, st, fs)
                        except Exception:
                            pass
                    offset += 4
                    continue

                segments_found += 1
                stream_type, frame_size = struct.unpack_from('<II', desc, 0)
                event_index = struct.unpack_from('<I', desc, 24)[0]
                seg_counter = struct.unpack_from('<I', desc, 20)[0]

                # Frame data starts right after the 28-byte descriptor
                data_start = offset + desc_size
                if data_start + frame_size > file_size:
                    logger.warning("Segment at 0x%X: frame data exceeds file (need %d, "
                                   "have %d bytes remaining)", offset, frame_size,
                                   file_size - data_start)
                    break

                # Extract frame if it's a target stream type
                if stream_type in self.target_streams:
                    if max_per_stream is None or frame_counts[stream_type] < max_per_stream:
                        f.seek(data_start)
                        frame_data = f.read(frame_size)

                        if len(frame_data) == frame_size:
                            frame_info = self._process_frame(
                                stream_type, frame_data, frame_counts[stream_type],
                                seg_counter, event_index
                            )
                            if frame_info:
                                self.frames.append(frame_info)
                                frame_counts[stream_type] += 1
                                total_extracted = sum(frame_counts.values())

                                if pbar is not None:
                                    pbar.update(1)
                                    pbar.set_postfix({
                                        self.STREAM_NAMES.get(st, str(st)): frame_counts[st]
                                        for st in self.target_streams
                                        if frame_counts[st] > 0
                                    })

                                # Update GUI callback periodically
                                if callback and total_extracted % 10 == 0:
                                    parts = [f"{frame_counts[st]} {self.STREAM_NAMES.get(st, st)}"
                                             for st in self.target_streams if frame_counts[st] > 0]
                                    callback(0.5, f"Extracting: {', '.join(parts)}...")
                else:
                    target_mismatch_count += 1

                # Find the next segment descriptor.
                # Each segment is desc(28) + frame_data + trailer(0-8 bytes).
                # Search a small window around the expected position.
                expected = offset + desc_size + frame_size
                next_offset = None
                for delta in range(-8, 12, 4):
                    candidate = expected + delta
                    if candidate <= offset or candidate + desc_size > file_size:
                        continue
                    f.seek(candidate)
                    ndesc = f.read(desc_size)
                    if len(ndesc) < desc_size:
                        continue
                    if self._is_valid_segment(ndesc, file_size):
                        next_offset = candidate
                        break

                if next_offset is None:
                    # Lost sync — no valid descriptor found near expected position
                    logger.info("Lost segment sync at 0x%X after %d segments",
                                offset, segments_found)
                    break

                offset = next_offset

        finally:
            if pbar is not None:
                pbar.close()

        # Log diagnostic summary
        total_extracted = sum(frame_counts.values())
        logger.info("Extraction summary: %d segments found, %d rejected, "
                    "%d target mismatches, %d frames extracted",
                    segments_found, segments_rejected, target_mismatch_count,
                    total_extracted)
        if segments_rejected > 0 and reject_reasons:
            logger.warning("First rejected segment samples: %s", reject_reasons)
        if segments_found == 0 and segments_rejected > 0:
            logger.error("All %d candidate segments were rejected. "
                         "Possible format mismatch for this XEF file variant.",
                         segments_rejected)

    def _extract_frames_raw(self, f, use_tqdm=True, callback=None):
        """
        Fallback frame extraction for XEF files without segment descriptors.

        Some newer XEF files (stream_count >= 12) store raw frames without
        the standard 28-byte segment descriptors. This method scans the file
        at frame-aligned offsets looking for depth and IR data patterns.

        Args:
            f: Open file object
            use_tqdm: Show tqdm progress bar in terminal
            callback: Optional callback(progress, message) for GUI updates
        """
        f.seek(0)
        file_size = f.seek(0, 2)
        frame_size = self.DEPTH_FRAME_SIZE  # 434,176 bytes (same for depth & IR)

        # Determine which streams to look for
        want_depth = self.STREAM_DEPTH in self.target_streams
        want_ir = self.STREAM_IR in self.target_streams

        if not want_depth and not want_ir:
            return

        # Phase 1: Sample the file to find data regions.
        # Scan at frame-aligned intervals (every 16 frames ≈ 7 MB).
        sample_step = frame_size * 16
        depth_offsets = []
        ir_offsets = []

        if callback:
            callback(0.1, "Sampling file for frame data...")

        scan_limit = file_size - frame_size
        offset = 0x100000  # Skip first 1 MB (header/metadata)

        while offset < scan_limit:
            f.seek(offset)
            sample = f.read(min(1000, frame_size))
            if len(sample) < 100:
                break

            values = struct.unpack_from(f'<{len(sample)//2}H', sample)

            # Depth frames: many values in 50-8000 range, few 0xFFFF
            depth_range = sum(1 for v in values if 50 <= v <= 8000)
            ffff_count = sum(1 for v in values if v == 0xFFFF)

            is_depth = depth_range > len(values) * 0.3 and ffff_count < len(values) * 0.1
            # IR frames: many high values (>10000) or many 0xFFFF
            high_vals = sum(1 for v in values if v > 10000)
            is_ir = (ffff_count > len(values) * 0.3 or high_vals > len(values) * 0.5) and not is_depth

            if is_depth and want_depth:
                depth_offsets.append(offset)
            elif is_ir and want_ir:
                ir_offsets.append(offset)

            offset += sample_step

        logger.info("Raw scan found %d depth samples, %d IR samples",
                    len(depth_offsets), len(ir_offsets))

        if not depth_offsets and not ir_offsets:
            return

        # Phase 2: For each sample region, extract consecutive frames.
        frame_counts = {st: 0 for st in self.target_streams}
        max_per_stream = self.max_frames
        file_stem = self.filepath.stem

        pbar = None
        if use_tqdm and tqdm is not None:
            try:
                pbar = tqdm(
                    desc=f"[{file_stem}] Extracting raw frames",
                    unit=" frames",
                    dynamic_ncols=True,
                    leave=False
                )
            except Exception:
                pbar = None

        try:
            for stream_type, sample_offsets in [
                (self.STREAM_DEPTH, depth_offsets),
                (self.STREAM_IR, ir_offsets),
            ]:
                if stream_type not in self.target_streams:
                    continue
                if max_per_stream is not None and frame_counts[stream_type] >= max_per_stream:
                    continue

                for sample_off in sample_offsets:
                    # Expand around the sample to find consecutive frames
                    # Search backward to find the start of the region
                    region_start = sample_off
                    while region_start >= frame_size:
                        f.seek(region_start - frame_size)
                        check = f.read(min(200, frame_size))
                        vals = struct.unpack_from(f'<{len(check)//2}H', check)

                        if stream_type == self.STREAM_DEPTH:
                            dr = sum(1 for v in vals if 50 <= v <= 8000)
                            ok = dr > len(vals) * 0.3
                        else:
                            fc = sum(1 for v in vals if v == 0xFFFF)
                            hv = sum(1 for v in vals if v > 10000)
                            ok = fc > len(vals) * 0.3 or hv > len(vals) * 0.5

                        if ok:
                            region_start -= frame_size
                        else:
                            break

                    # Extract frames forward from region_start
                    off = region_start
                    while off + frame_size <= file_size:
                        if max_per_stream is not None and frame_counts[stream_type] >= max_per_stream:
                            break

                        f.seek(off)
                        raw = f.read(frame_size)
                        if len(raw) < frame_size:
                            break

                        # Quick validation
                        vals = struct.unpack_from(f'<{len(raw)//2}H', raw)
                        if stream_type == self.STREAM_DEPTH:
                            dr = sum(1 for v in vals[:200] if 50 <= v <= 8000)
                            if dr < 60:  # Less than 30% valid → end of region
                                break
                        else:
                            fc = sum(1 for v in vals[:200] if v == 0xFFFF)
                            hv = sum(1 for v in vals[:200] if v > 10000)
                            if fc < 30 and hv < 50:  # Not IR-like → end of region
                                break

                        frame_info = self._process_frame(
                            stream_type, raw, frame_counts[stream_type], 0, 0
                        )
                        if frame_info:
                            self.frames.append(frame_info)
                            frame_counts[stream_type] += 1
                            total = sum(frame_counts.values())

                            if pbar is not None:
                                pbar.update(1)
                                pbar.set_postfix({
                                    self.STREAM_NAMES.get(st, str(st)): frame_counts[st]
                                    for st in self.target_streams
                                    if frame_counts[st] > 0
                                })

                            if callback and total % 10 == 0:
                                parts = [f"{frame_counts[st]} {self.STREAM_NAMES.get(st, st)}"
                                         for st in self.target_streams if frame_counts[st] > 0]
                                callback(0.5, f"Extracting: {', '.join(parts)}...")

                        off += frame_size

                    if max_per_stream is not None and all(
                        frame_counts[st] >= max_per_stream for st in self.target_streams
                    ):
                        break

        finally:
            if pbar is not None:
                pbar.close()

        logger.info("Raw extraction: %s",
                     ", ".join(f"{frame_counts[st]} {self.STREAM_NAMES.get(st, st)}"
                              for st in self.target_streams))

    def _process_frame(self, stream_type, raw_data, frame_index, seg_counter, event_index):
        """Process raw frame data into a normalized image array."""
        if stream_type == self.STREAM_DEPTH:
            return self._process_depth_frame(raw_data, frame_index, seg_counter, event_index)
        elif stream_type == self.STREAM_IR:
            return self._process_ir_frame(raw_data, frame_index, seg_counter, event_index)
        elif stream_type == self.STREAM_COLOR:
            return self._process_color_frame(raw_data, frame_index, seg_counter, event_index)
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

    def _process_color_frame(self, raw_data, frame_index, seg_counter, event_index):
        """Process a color frame (1920x1080 BGRA) into an RGB image array."""
        expected_size = self.COLOR_WIDTH * self.COLOR_HEIGHT * 4
        if len(raw_data) != expected_size:
            # Try alternative color resolutions
            # Some XEF files may use different resolutions
            total_pixels = len(raw_data) // 4
            if total_pixels <= 0:
                return None
            # Try common Kinect V2 color resolutions
            for w, h in [(1920, 1080), (1280, 720), (960, 540), (640, 480)]:
                if total_pixels == w * h:
                    try:
                        bgra_array = np.frombuffer(raw_data[:w * h * 4], dtype=np.uint8).reshape(
                            (h, w, 4)
                        )
                        # Convert BGRA to RGB
                        rgb_array = bgra_array[:, :, [2, 1, 0]].copy()
                        return {
                            'type': 'color',
                            'stream_type': self.STREAM_COLOR,
                            'stream_name': 'color',
                            'data': rgb_array,
                            'raw_data': bgra_array,
                            'index': frame_index,
                            'seg_counter': seg_counter,
                            'event_index': event_index,
                        }
                    except (ValueError, IndexError):
                        continue
            return None

        try:
            bgra_array = np.frombuffer(raw_data, dtype=np.uint8).reshape(
                (self.COLOR_HEIGHT, self.COLOR_WIDTH, 4)
            )
        except ValueError:
            return None

        # Convert BGRA to RGB
        rgb_array = bgra_array[:, :, [2, 1, 0]].copy()

        return {
            'type': 'color',
            'stream_type': self.STREAM_COLOR,
            'stream_name': 'color',
            'data': rgb_array,
            'raw_data': bgra_array,
            'index': frame_index,
            'seg_counter': seg_counter,
            'event_index': event_index,
        }

    def save_frames_to_jpeg(self, output_dir, base_name=None, quality=95):
        """
        Save extracted frames as JPEG files.

        Depth frames are saved to output_dir/Depth/
        IR frames are saved to output_dir/IR/
        Color frames are saved to output_dir/Color/

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
            'color': 'Color',
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

            if stream_name == 'color':
                # Save color frames as RGB JPEG with EXIF orientation
                img = Image.fromarray(frame['data'], 'RGB')
                img = self._add_exif_orientation(img)
                img.save(str(filepath), 'JPEG', quality=quality)
            else:
                # Save depth/IR frames as grayscale
                img = Image.fromarray(frame['data'], 'L')
                img.save(str(filepath), 'JPEG', quality=quality)

            saved_files.append(str(filepath))

        return saved_files

    def _add_exif_orientation(self, img, orientation=1):
        """Add EXIF orientation tag to a PIL Image.

        Args:
            img: PIL Image object
            orientation: EXIF orientation value (1=normal, 3=180deg, etc.)

        Returns:
            PIL Image with EXIF data set.
        """
        try:
            img.getexif()[0x0112] = orientation
        except (AttributeError, Exception):
            pass
        return img

    def get_stream_info(self):
        """Return information about streams found in the file."""
        return [
            {'type': s['type'], 'name': s['name']}
            for s in self.streams
        ]

    def has_stream(self, stream_type):
        """Check if the file has a specific stream type."""
        return any(s['type'] == stream_type for s in self.streams)


def convert_xef_to_jpeg(xef_path, output_dir, max_frames=None, target_streams=None, callback=None, quality=95, use_tqdm=True):
    """
    Convert XEF file to JPEG images.

    Creates a timestamped folder inside output_dir:
        {output_dir}/{YYYY_MM_DD_HH_MM_SS}/
            Depth/   (if depth frames)
            IR/      (if IR frames)
            Color/   (if color frames)

    Args:
        xef_path: Path to input .xef file
        output_dir: Path to output directory (created if not exists)
        max_frames: Maximum number of frames per stream type (None = unlimited)
        target_streams: List of stream types to extract (default: [3, 4] = depth, ir)
        callback: Optional callback function(progress, message)
        quality: JPEG quality (1-100, default 95)
        use_tqdm: Show tqdm progress bar in terminal (default True)

    Returns:
        Tuple of (stream_types_found, saved_file_paths, output_folder_path)
    """
    from datetime import datetime

    # Create timestamped output folder
    timestamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    base_output = Path(output_dir)
    session_folder = base_output / timestamp
    session_folder.mkdir(parents=True, exist_ok=True)

    logger.info("Converting XEF file: %s", xef_path)
    logger.info("Output folder: %s", session_folder)

    if callback:
        callback(0.0, "Validating XEF file...")

    parser = XEFParser(xef_path, max_frames=max_frames, target_streams=target_streams)
    parser.parse(use_tqdm=use_tqdm, callback=callback)

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
        target_names = [parser.STREAM_NAMES.get(s, f'unknown_{s}') for s in (target_streams or [])]
        logger.warning("No frames extracted. Available streams: %s",
                       ', '.join(available_names) if available_names else 'none')
        raise ValueError(
            f"No frames extracted from target streams.\n\n"
            f"Requested: {', '.join(target_names)}\n"
            f"Available: {', '.join(available_names) if available_names else 'none'}\n\n"
            f"Check the log file for diagnostic details:\n{LOG_FILE}"
        )

    if callback:
        callback(0.5, f"Found {len(frames)} frames, converting to JPEG...")

    logger.info("Found %d frames, saving to JPEG (quality=%d)", len(frames), quality)

    saved_files = parser.save_frames_to_jpeg(session_folder, quality=quality)

    if callback:
        callback(1.0, "Conversion complete")

    frame_types = list(set(f['stream_name'] for f in frames))
    logger.info("Conversion complete: %d files saved", len(saved_files))
    return frame_types, saved_files, str(session_folder)
