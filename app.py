import os
import re
import subprocess
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename
import json

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max-limit
app.config['UPLOAD_FOLDER'] = 'uploads'

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
    if output_name is None:
        output_name = sequence_info['base_name'].strip('_') + '.mp4'
    
    output_path = os.path.join(sequence_info['folder'], output_name)
    input_pattern = os.path.join(sequence_info['folder'], sequence_info['pattern'])
    
    cmd = [
        'ffmpeg', '-framerate', '24',
        '-i', input_pattern,
        '-c:v', 'libx264',
        '-pix_fmt', 'yuv420p',
        output_path
    ]
    
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return True, output_path
    except subprocess.CalledProcessError as e:
        return False, str(e)

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
    sequence_info = data.get('sequence_info')
    
    if not sequence_info:
        return jsonify({'error': 'No sequence information provided'}), 400
    
    success, result = convert_to_video(sequence_info)
    
    if success:
        return jsonify({'success': True, 'output_path': result})
    else:
        return jsonify({'success': False, 'error': result})

if __name__ == '__main__':
    app.run(debug=True) 