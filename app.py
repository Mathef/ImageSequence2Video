import os
import re
import subprocess
import threading
import signal
import logging
import shutil
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename
import json
from PIL import Image
import numpy as np

try:
    import OpenEXR
    import Imath
except ImportError:
    OpenEXR = None
    Imath = None

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max-limit
app.config['UPLOAD_FOLDER'] = 'uploads'

# libx264 CRF + preset per GUI "Quality" option (lower CRF = higher quality, larger files)
ENCODE_QUALITY_PRESETS = {
    'high': ('18', 'slow'),
    'balanced': ('21', 'medium'),
    'compact': ('24', 'slow'),
    'draft': ('26', 'veryfast'),
}

SUPPORTED_SEQUENCE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.exr')
EXR_PREPROCESS_WORKERS = 4

# Global variables to store conversion state
conversion_progress = {
    'current_file': '',
    'progress': 0,
    'current_stage': '',
    'total_files': 0,
    'current_file_index': 0,
    'is_converting': False,
    'current_message': '',  # For storing FFmpeg output
    'log_messages': []  # Store last N log messages
}

current_process = {
    'process': None,
    'should_stop': False
}

def add_log_message(message):
    """Add a message to the log history"""
    conversion_progress['current_message'] = message
    conversion_progress['log_messages'].append(message)
    # Keep only last 50 messages
    if len(conversion_progress['log_messages']) > 50:
        conversion_progress['log_messages'].pop(0)
    logger.info(message)

def parse_ffmpeg_progress(line):
    """Parse FFmpeg progress information from a line of output"""
    if 'frame=' in line:
        try:
            frame_match = re.search(r'frame=\s*(\d+)', line)
            if frame_match:
                frame = int(frame_match.group(1))
                # Calculate progress based on frame count and total frames (including loops)
                total_frames = conversion_progress.get('total_frames', 240)
                return min(99, (frame / total_frames) * 100)
        except Exception as e:
            logger.error(f"Error parsing progress: {e}")
    return None

def sanitize_name(value):
    """Return a filesystem-friendly name component."""
    sanitized = re.sub(r'[^A-Za-z0-9._-]+', '_', str(value or '')).strip('._')
    return sanitized or 'output'

def is_exr_sequence(sequence_info):
    return sequence_info.get('source_type') == 'exr'

def get_first_frame_path(sequence_info):
    return os.path.join(
        sequence_info['folder'],
        sequence_info['pattern'] % sequence_info['start_frame']
    )

def parse_pattern_padding(pattern):
    match = re.search(r'%0(\d+)d', pattern)
    if match:
        return int(match.group(1))
    fallback = re.search(r'%(\d+)d', pattern)
    if fallback:
        return int(fallback.group(1))
    return 5

def ensure_exr_dependencies():
    if OpenEXR is None or Imath is None:
        return (
            False,
            "OpenEXR Python package is not installed. "
            "Install with: pip install OpenEXR (or conda install -c conda-forge openexr-python)."
        )
    return True, ""

def get_exr_aov_map(first_frame_path):
    """Return AOV map from EXR channel names."""
    ok, error = ensure_exr_dependencies()
    if not ok:
        raise RuntimeError(error)
    if not os.path.exists(first_frame_path):
        raise FileNotFoundError(f"EXR frame not found: {first_frame_path}")

    exr_file = OpenEXR.InputFile(first_frame_path)
    header = exr_file.header()
    raw_channels = sorted((header.get('channels') or {}).keys())
    exr_file.close()

    grouped = {}
    top_level_channels = set(raw_channels)
    has_beauty_rgb = all(channel in top_level_channels for channel in ('R', 'G', 'B'))
    if has_beauty_rgb:
        grouped['Beauty'] = {
            'channels': {
                'R': 'R',
                'G': 'G',
                'B': 'B',
            }
        }
        if 'A' in top_level_channels:
            grouped['Beauty']['channels']['A'] = 'A'

    for channel_name in raw_channels:
        if has_beauty_rgb and channel_name in ('R', 'G', 'B', 'A'):
            # Top-level RGBA channels are exposed as a merged "Beauty" AOV.
            continue
        rgba_match = re.match(r'^(.*)\.([RGBA])$', channel_name)
        if rgba_match:
            group_name = rgba_match.group(1) or 'Beauty'
            suffix = rgba_match.group(2)
            grouped.setdefault(group_name, {'channels': {}})
            grouped[group_name]['channels'][suffix] = channel_name
        else:
            grouped.setdefault(channel_name, {'channels': {}})
            grouped[channel_name]['channels']['Y'] = channel_name

    aov_map = {}
    for aov_name, info in grouped.items():
        channels = info['channels']
        if all(c in channels for c in ('R', 'G', 'B')) or 'Y' in channels:
            aov_map[aov_name] = info

    return aov_map

def list_exr_aovs(sequence_info):
    first_frame_path = get_first_frame_path(sequence_info)
    aov_map = get_exr_aov_map(first_frame_path)
    return sorted(aov_map.keys()), aov_map

def read_exr_channel(exr_file, channel_name, width, height):
    pixel_type = Imath.PixelType(Imath.PixelType.FLOAT)
    raw = exr_file.channel(channel_name, pixel_type)
    channel_data = np.frombuffer(raw, dtype=np.float32)
    if channel_data.size != width * height:
        raise ValueError(f"Unexpected EXR channel size for {channel_name}")
    return channel_data.reshape((height, width))

def linear_to_srgb(rgb_linear):
    """Convert linear RGB to display-referred sRGB."""
    rgb_linear = np.clip(rgb_linear, 0.0, None)
    return np.where(
        rgb_linear <= 0.0031308,
        rgb_linear * 12.92,
        1.055 * np.power(rgb_linear, 1.0 / 2.4) - 0.055
    )

def convert_exr_frame_to_png(exr_path, png_path, aov_spec, aov_name):
    exr_file = OpenEXR.InputFile(exr_path)
    header = exr_file.header()
    data_window = header['dataWindow']
    width = data_window.max.x - data_window.min.x + 1
    height = data_window.max.y - data_window.min.y + 1

    channels = aov_spec['channels']
    if all(c in channels for c in ('R', 'G', 'B')):
        red = read_exr_channel(exr_file, channels['R'], width, height)
        green = read_exr_channel(exr_file, channels['G'], width, height)
        blue = read_exr_channel(exr_file, channels['B'], width, height)
        rgb = np.stack([red, green, blue], axis=-1)
    elif 'Y' in channels:
        luminance = read_exr_channel(exr_file, channels['Y'], width, height)
        rgb = np.stack([luminance, luminance, luminance], axis=-1)
    else:
        exr_file.close()
        raise ValueError("AOV does not have RGB or single-channel data")

    alpha = None
    if 'A' in channels:
        alpha = read_exr_channel(exr_file, channels['A'], width, height)
    exr_file.close()

    rgb = np.nan_to_num(rgb, nan=0.0, posinf=1.0, neginf=0.0)

    # Composite over black when alpha exists.
    if alpha is not None:
        alpha = np.nan_to_num(alpha, nan=0.0, posinf=1.0, neginf=0.0)
        alpha = np.clip(alpha, 0.0, 1.0)[..., np.newaxis]
        rgb = rgb * alpha

    # Beauty is usually stored in linear space, so convert to display-referred sRGB.
    if str(aov_name).lower() == 'beauty':
        rgb = linear_to_srgb(rgb)

    rgb = np.clip(rgb, 0.0, 1.0)
    rgb_u8 = (rgb * 255.0).astype(np.uint8)
    Image.fromarray(rgb_u8, mode='RGB').save(png_path)

def preprocess_exr_frame_task(task):
    """Process-pool worker for EXR -> PNG conversion."""
    exr_path, png_path, aov_spec, aov_name, frame_number = task
    if not os.path.exists(exr_path):
        raise FileNotFoundError(f"Missing EXR frame: {exr_path}")
    convert_exr_frame_to_png(exr_path, png_path, aov_spec, aov_name)
    return frame_number

def convert_exr_sequence_to_videos(sequence_info, framerate):
    ok, error = ensure_exr_dependencies()
    if not ok:
        return False, error

    try:
        available_aovs, aov_map = list_exr_aovs(sequence_info)
    except Exception as e:
        return False, f"Failed to inspect EXR AOVs: {e}"

    requested_aovs = sequence_info.get('selected_aovs') or available_aovs
    selected_aovs = [aov for aov in requested_aovs if aov in aov_map]
    if not selected_aovs:
        return False, "No valid EXR AOV selected for conversion."

    base_name = sequence_info['base_name'].strip('_')
    pad_len = parse_pattern_padding(sequence_info['pattern'])
    delete_temp_files = bool(sequence_info.get('delete_temp_files', True))
    total_aovs = len(selected_aovs)
    frame_numbers = range(
        sequence_info['start_frame'],
        sequence_info['start_frame'] + sequence_info['count']
    )

    for aov_index, aov_name in enumerate(selected_aovs, start=1):
        if current_process['should_stop']:
            return False, "Conversion stopped by user"

        conversion_progress['current_stage'] = f"Preprocessing EXR to PNG ({aov_name}, {aov_index}/{total_aovs})"
        conversion_progress['progress'] = 0
        add_log_message(f"Preparing EXR AOV '{aov_name}' for {sequence_info['base_name']}")
        safe_base = sanitize_name(base_name or sequence_info['base_name'])
        safe_aov = sanitize_name(aov_name)
        temp_dir_name = f".tmp_{safe_base}_{safe_aov}_{uuid.uuid4().hex[:8]}"
        temp_dir = os.path.join(sequence_info['folder'], temp_dir_name)
        os.makedirs(temp_dir, exist_ok=True)
        temp_pattern = f"frame_%0{pad_len}d.png"

        try:
            tasks = []
            for frame_number in frame_numbers:
                source_frame = os.path.join(
                    sequence_info['folder'],
                    sequence_info['pattern'] % frame_number
                )
                png_frame = os.path.join(temp_dir, temp_pattern % frame_number)
                tasks.append((source_frame, png_frame, aov_map[aov_name], aov_name, frame_number))

            total_tasks = len(tasks)
            if total_tasks == 0:
                raise ValueError("No frames found for EXR preprocessing")

            worker_count = min(EXR_PREPROCESS_WORKERS, total_tasks)
            add_log_message(
                f"Preprocessing {total_tasks} EXR frames with {worker_count} parallel worker(s)"
            )

            completed_tasks = 0
            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                future_to_frame = {
                    executor.submit(preprocess_exr_frame_task, task): task[4]
                    for task in tasks
                }
                for future in as_completed(future_to_frame):
                    if current_process['should_stop']:
                        for pending_future in future_to_frame:
                            pending_future.cancel()
                        return False, "Conversion stopped by user"

                    frame_number = future_to_frame[future]
                    try:
                        future.result()
                    except Exception as e:
                        for pending_future in future_to_frame:
                            pending_future.cancel()
                        return False, f"Failed EXR preprocessing at frame {frame_number}: {e}"

                    completed_tasks += 1
                    preprocess_ratio = completed_tasks / total_tasks
                    conversion_progress['progress'] = min(99, preprocess_ratio * 100.0)

            temp_sequence = {
                'base_name': f"{sequence_info['base_name']}_{safe_aov}",
                'folder': temp_dir,
                'count': sequence_info['count'],
                'start_frame': sequence_info['start_frame'],
                'pattern': temp_pattern,
                'loop_count': sequence_info.get('loop_count', 1),
                'encode_quality': sequence_info.get('encode_quality', 'balanced'),
                'output_folder': sequence_info['folder'],
            }
            output_name = f"{safe_base}_{safe_aov}.mp4"
            conversion_progress['current_stage'] = f"Creating MP4 ({aov_name}, {aov_index}/{total_aovs})"
            conversion_progress['progress'] = 0
            success, result = convert_to_video(
                temp_sequence,
                output_name=output_name,
                framerate=framerate
            )
            if not success:
                return False, result
        finally:
            if delete_temp_files:
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    add_log_message(f"Deleted temp folder: {temp_dir}")
                except Exception as e:
                    add_log_message(f"Failed to clean temp folder {temp_dir}: {e}")
            else:
                add_log_message(f"Keeping temp folder: {temp_dir}")

    return True, "EXR conversion completed"

def find_image_sequences(folder_path):
    """Find all image sequences in the given folder and subfolders"""
    sequences = {}
    
    for root, _, files in os.walk(folder_path):
        # Group files by their base name (without number)
        file_groups = {}
        for file in files:
            if file.lower().endswith(SUPPORTED_SEQUENCE_EXTENSIONS):
                # Extract base name, digits, and extension (keep original case in base name)
                match = re.match(r'(.+?)(\d+)\.(png|jpg|jpeg|exr)$', file, flags=re.IGNORECASE)
                if match:
                    base_name = match.group(1)
                    frame_digits = match.group(2)
                    frame_number = int(frame_digits)
                    pad_len = len(frame_digits)
                    extension = match.group(3).lower()
                    group_key = (base_name, extension)
                    if group_key not in file_groups:
                        file_groups[group_key] = []
                    file_groups[group_key].append((frame_number, file, pad_len, extension))
        
        # Only keep groups with more than 1 image
        for (base_name, extension), files in file_groups.items():
            if len(files) > 1:
                # Sort files by frame number
                sorted_files = sorted(files, key=lambda x: x[0])
                start_frame = sorted_files[0][0]
                pad_len = max(f[2] for f in sorted_files)
                
                rel_path = os.path.relpath(root, folder_path)
                sequence_key = os.path.join(rel_path, f"{base_name}[{extension}]")
                sequences[sequence_key] = {
                    'base_name': base_name,
                    'folder': root,
                    'count': len(files),
                    'start_frame': start_frame,
                    'pattern': f"{base_name}%0{pad_len}d.{extension}",
                    'extension': extension,
                    'source_type': 'exr' if extension == 'exr' else 'image',
                    'selected_aovs': [],
                }
                add_log_message(
                    f"Found {extension.upper()} sequence: {base_name} with {len(files)} "
                    f"frames, starting at frame {start_frame}"
                )
    
    return sequences

def convert_to_video(sequence_info, output_name=None, framerate=24):
    """Convert image sequence to MP4 using ffmpeg"""
    global conversion_progress, current_process
    
    if current_process['should_stop']:
        return False, "Conversion stopped by user"
    
    if output_name is None:
        output_name = sequence_info['base_name'].strip('_') + '.mp4'
    
    output_folder = sequence_info.get('output_folder', sequence_info['folder'])
    output_path = os.path.join(output_folder, output_name)
    input_pattern = os.path.join(sequence_info['folder'], sequence_info['pattern'])
    
    # Store total frames for progress calculation, accounting for loop count
    loop_count = sequence_info.get('loop_count', 1)
    conversion_progress['total_frames'] = sequence_info['count'] * loop_count
    
    add_log_message(f"Starting conversion of {sequence_info['base_name']} with {loop_count} repetition(s)")
    add_log_message(f"Input pattern: {input_pattern}")
    add_log_message(f"Output path: {output_path}")
    add_log_message(f"Start frame: {sequence_info['start_frame']}, Total frames: {sequence_info['count'] * loop_count}")

    quality_key = sequence_info.get('encode_quality') or 'balanced'
    crf, x264_preset = ENCODE_QUALITY_PRESETS.get(
        quality_key, ENCODE_QUALITY_PRESETS['balanced']
    )
    add_log_message(f"Encode quality: {quality_key} (CRF {crf}, preset {x264_preset})")
    
    # First, get the resolution of the first image
    filter_complex = None
    first_frame_path = os.path.join(
        sequence_info['folder'],
        sequence_info['pattern'] % sequence_info['start_frame']
    )
    probe_cmd = [
        'ffprobe',
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height',
        '-of', 'json',
        first_frame_path,
    ]
    
    try:
        probe_process = subprocess.Popen(
            probe_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        probe_stdout, probe_stderr = probe_process.communicate()

        if probe_process.returncode != 0:
            add_log_message(f"ffprobe failed ({probe_process.returncode}): {probe_stderr.strip()}")
        else:
            try:
                probe_json = json.loads(probe_stdout or "{}")
                streams = probe_json.get("streams") or []
                if streams and isinstance(streams[0], dict):
                    width = int(streams[0].get("width") or 0)
                    height = int(streams[0].get("height") or 0)
                else:
                    width = height = 0

                if width > 0 and height > 0:
                    add_log_message(f"Detected resolution: {width}x{height}")

                    # Many encoders require even dimensions (e.g., yuv420p / H.264).
                    pad_width = width + (width % 2)
                    pad_height = height + (height % 2)

                    if pad_width != width or pad_height != height:
                        add_log_message(f"Adding padding to make dimensions even: {pad_width}x{pad_height}")
                        filter_complex = f"pad={pad_width}:{pad_height}:(ow-iw)/2:(oh-ih)/2:color=black"
                else:
                    add_log_message("Could not detect resolution; skipping padding filter.")
            except Exception as e:
                add_log_message(f"Error parsing ffprobe output; skipping padding filter: {e}")
    except Exception as e:
        add_log_message(f"Error detecting resolution: {e}")
        return False, str(e)
    
    cmd = [
        'ffmpeg', '-framerate', str(framerate),
        '-start_number', str(sequence_info['start_frame']),
    ]

    # Add stream loop if specified
    loop_count = sequence_info.get('loop_count', 1)
    if loop_count > 1:
        cmd.extend(['-stream_loop', str(loop_count - 1)])

    cmd.extend(['-i', input_pattern])
    
    # Calculate total duration in seconds
    total_frames = sequence_info['count'] * loop_count
    duration_seconds = total_frames / framerate
    
    # Generate silent audio track with aevalsrc instead of anullsrc
    cmd.extend([
        '-f', 'lavfi', 
        '-i', f'aevalsrc=0:d={duration_seconds}:s=48000',
    ])
    
    if filter_complex:
        cmd.extend(['-vf', filter_complex])
    
    cmd.extend([
        '-c:v', 'libx264',
        '-crf', crf,
        '-preset', x264_preset,
        '-c:a', 'aac',
        '-pix_fmt', 'yuv420p',
        '-progress', 'pipe:1',
        '-stats',
        '-y',
        output_path
    ])
    
    add_log_message(f"Running command: {' '.join(cmd)}")
    
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1  # Line buffered
        )
        
        current_process['process'] = process
        
        # Create a thread to read stderr
        def read_stderr():
            for line in process.stderr:
                add_log_message(f"FFmpeg: {line.strip()}")
        
        stderr_thread = threading.Thread(target=read_stderr)
        stderr_thread.daemon = True
        stderr_thread.start()
        
        while True:
            if current_process['should_stop']:
                process.terminate()
                add_log_message("Conversion stopped by user")
                return False, "Conversion stopped by user"
            
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            
            if line:
                add_log_message(f"Progress: {line.strip()}")
                progress = parse_ffmpeg_progress(line)
                if progress is not None:
                    conversion_progress['progress'] = progress
        
        process.wait()
        return_code = process.poll()
        
        if return_code == 0:
            conversion_progress['progress'] = 100
            add_log_message(f"Conversion completed successfully: {output_path}")
            return True, output_path
        else:
            error_output = process.stderr.read()
            add_log_message(f"FFmpeg error: {error_output}")
            return False, f"FFmpeg error: {error_output}"
            
    except Exception as e:
        add_log_message(f"Exception during conversion: {str(e)}")
        return False, str(e)
    finally:
        current_process['process'] = None

def convert_sequences(sequences_to_convert):
    """Convert multiple sequences and track progress"""
    global conversion_progress, current_process
    
    try:
        current_process['should_stop'] = False
        conversion_progress['is_converting'] = True
        conversion_progress['total_files'] = len(sequences_to_convert)
        conversion_progress['current_file_index'] = 0
        conversion_progress['log_messages'] = []
        conversion_progress['progress'] = 0
        
        add_log_message(f"Starting conversion of {len(sequences_to_convert)} sequences")
        
        for i, sequence in enumerate(sequences_to_convert, 1):
            if current_process['should_stop']:
                add_log_message("Conversion process stopped by user")
                break
                
            conversion_progress['current_file'] = sequence['base_name']
            conversion_progress['current_file_index'] = i
            conversion_progress['progress'] = 0
            
            framerate = sequence.get('framerate', 24)
            add_log_message(f"Processing sequence {i} of {conversion_progress['total_files']} at {framerate} fps")
            if is_exr_sequence(sequence):
                success, result = convert_exr_sequence_to_videos(sequence, framerate=framerate)
            else:
                conversion_progress['current_stage'] = "Creating MP4"
                success, result = convert_to_video(sequence, framerate=framerate)
            
            if not success:
                add_log_message(f"Error converting {sequence['base_name']}: {result}")
                if current_process['should_stop']:
                    break
        
    finally:
        conversion_progress['is_converting'] = False
        conversion_progress['progress'] = 100
        conversion_progress['current_stage'] = ''
        current_process['should_stop'] = False
        if current_process['process']:
            try:
                current_process['process'].terminate()
            except:
                pass
            current_process['process'] = None
        add_log_message("All conversions completed")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/scan', methods=['POST'])
def scan_folder():
    folder_path = request.form.get('folder_path')
    if not folder_path or not os.path.exists(folder_path):
        return jsonify({'error': 'Invalid folder path'}), 400
    
    sequences = find_image_sequences(folder_path)
    return jsonify({'sequences': sequences})

@app.route('/convert', methods=['POST'])
def convert_sequence():
    data = request.get_json()
    sequences_info = data.get('sequences_info', [])
    
    if not sequences_info:
        return jsonify({'error': 'No sequences provided'}), 400
        
    # Ensure each sequence has a framerate
    for sequence in sequences_info:
        if 'framerate' not in sequence:
            sequence['framerate'] = 24  # Default to 24 if not specified
        q = sequence.get('encode_quality')
        if q not in ENCODE_QUALITY_PRESETS:
            sequence['encode_quality'] = 'balanced'
        if is_exr_sequence(sequence):
            selected_aovs = sequence.get('selected_aovs') or []
            if not isinstance(selected_aovs, list):
                sequence['selected_aovs'] = []
            sequence['delete_temp_files'] = bool(sequence.get('delete_temp_files', True))

    # Start conversion in a separate thread
    thread = threading.Thread(target=convert_sequences, args=(sequences_info,))
    thread.start()

    return jsonify({'success': True, 'message': 'Conversion started'})

@app.route('/exr_aovs', methods=['POST'])
def exr_aovs():
    data = request.get_json() or {}
    sequence_info = data.get('sequence_info') or {}

    if not sequence_info or not is_exr_sequence(sequence_info):
        return jsonify({'error': 'Invalid EXR sequence info'}), 400

    try:
        aov_names, _ = list_exr_aovs(sequence_info)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    return jsonify({
        'success': True,
        'aovs': aov_names,
        'selected_aovs': sequence_info.get('selected_aovs') or aov_names
    })

@app.route('/stop', methods=['POST'])
def stop_conversion():
    """Stop the current conversion process"""
    try:
        current_process['should_stop'] = True
        conversion_progress['current_stage'] = 'Stopping...'
        if current_process['process']:
            current_process['process'].terminate()
            current_process['process'] = None
        add_log_message("Stopping conversion process...")
        return jsonify({'success': True, 'message': 'Stopping conversion...'})
    except Exception as e:
        add_log_message(f"Error stopping process: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/progress')
def get_progress():
    """Get the current conversion progress"""
    return jsonify(conversion_progress)

if __name__ == '__main__':
    app.run(debug=True) 