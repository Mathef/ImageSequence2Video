# Image Sequence to Video Converter

A Flask web application that converts image sequences to MP4 videos using FFmpeg.

## Prerequisites

- Python 3.7 or higher
- FFmpeg installed and available in system PATH
- pip (Python package installer)

## Installation

1. Clone this repository or download the files
2. Install the required Python packages:
```bash
pip install -r requirements.txt
```

## Usage

1. Run the Flask application:
```bash
python app.py
```

2. Open your web browser and navigate to `http://localhost:5000`

3. Enter the folder path containing your image sequences

4. Click "Scan Folder" to find all image sequences

5. Select the sequences you want to convert

6. Click "Convert Selected Sequences" to start the conversion process

## Features

- Scans folders and subfolders for image sequences
- Supports PNG, JPG, and JPEG formats
- Automatically detects image sequences based on naming patterns
- Converts sequences to MP4 using H.264 codec
- Shows conversion progress
- Names output videos based on sequence names

## Notes

- Image sequences should be numbered consecutively
- The application uses FFmpeg with the following settings:
  - Framerate: 24 fps
  - Codec: H.264
  - Pixel format: yuv420p
- Output videos will be saved in the same folder as the image sequences 