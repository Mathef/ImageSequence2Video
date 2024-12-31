import os
import re
import subprocess
import threading
import signal
import logging
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename
import json

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max-limit
app.config['UPLOAD_FOLDER'] = 'uploads'

# Global variables to store conversion state
conversion_progress = {
    'current_file': '',
    'progress': 0,
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
                # Calculate progress based on frame count and total frames
                return min(99, (frame / conversion_progress.get('total_frames', 240)) * 100)
        except Exception as e:
            logger.error(f"Error parsing progress: {e}")
    return None

def find_image_sequences(folder_path):
    """Find all image sequences in the given folder and subfolders"""
    sequences = {}
    
    for root, _, files in os.walk(folder_path):
        # Group files by their base name (without number)
        file_groups = {}
        for file in files:
            if file.lower().endswith(('.png', '.jpg', '.jpeg')):
                # Extract the base name and number
                match = re.match(r'(.+?)(\d+)\.(png|jpg|jpeg)$', file.lower())
                if match:
                    base_name = match.group(1)
                    if base_name not in file_groups:
                        file_groups[base_name] = []
                    file_groups[base_name].append(file)
        
        # Only keep groups with more than 1 image
        for base_name, files in file_groups.items():
            if len(files) > 1:
                rel_path = os.path.relpath(root, folder_path)
                sequence_key = os.path.join(rel_path, base_name)
                sequences[sequence_key] = {
                    'base_name': base_name,
                    'folder': root,
                    'count': len(files),
                    'pattern': f"{base_name}%05d.{files[0].split('.')[-1]}"
                }
    
    return sequences

def convert_to_video(sequence_info, output_name=None):
    """Convert image sequence to MP4 using ffmpeg"""
    global conversion_progress, current_process
    
    if current_process['should_stop']:
        return False, "Conversion stopped by user"
    
    if output_name is None:
        output_name = sequence_info['base_name'].strip('_') + '.mp4'
    
    output_path = os.path.join(sequence_info['folder'], output_name)
    input_pattern = os.path.join(sequence_info['folder'], sequence_info['pattern'])
    
    # Store total frames for progress calculation
    conversion_progress['total_frames'] = sequence_info['count']
    
    add_log_message(f"Starting conversion of {sequence_info['base_name']}")
    add_log_message(f"Input pattern: {input_pattern}")
    add_log_message(f"Output path: {output_path}")
    
    cmd = [
        'ffmpeg', '-framerate', '24',
        '-i', input_pattern,
        '-c:v', 'libx264',
        '-pix_fmt', 'yuv420p',
        '-progress', 'pipe:1',
        '-stats',
        '-y',  # Overwrite output file if exists
        output_path
    ]
    
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
    
    current_process['should_stop'] = False
    conversion_progress['is_converting'] = True
    conversion_progress['total_files'] = len(sequences_to_convert)
    conversion_progress['current_file_index'] = 0
    conversion_progress['log_messages'] = []
    
    add_log_message(f"Starting conversion of {len(sequences_to_convert)} sequences")
    
    for sequence in sequences_to_convert:
        if current_process['should_stop']:
            add_log_message("Conversion process stopped")
            break
            
        conversion_progress['current_file'] = sequence['base_name']
        conversion_progress['current_file_index'] += 1
        conversion_progress['progress'] = 0
        
        add_log_message(f"Processing sequence {conversion_progress['current_file_index']} of {conversion_progress['total_files']}")
        success, result = convert_to_video(sequence)
        
        if not success:
            add_log_message(f"Error converting {sequence['base_name']}: {result}")
            if current_process['should_stop']:
                break
    
    conversion_progress['is_converting'] = False
    conversion_progress['progress'] = 100
    current_process['should_stop'] = False
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
    
    # Start conversion in a separate thread
    thread = threading.Thread(target=convert_sequences, args=(sequences_info,))
    thread.start()
    
    return jsonify({'success': True, 'message': 'Conversion started'})

@app.route('/stop', methods=['POST'])
def stop_conversion():
    """Stop the current conversion process"""
    current_process['should_stop'] = True
    if current_process['process']:
        try:
            current_process['process'].terminate()
        except Exception as e:
            add_log_message(f"Error stopping process: {e}")
    return jsonify({'success': True, 'message': 'Stopping conversion...'})

@app.route('/progress')
def get_progress():
    """Get the current conversion progress"""
    return jsonify(conversion_progress)

if __name__ == '__main__':
    app.run(debug=True) 