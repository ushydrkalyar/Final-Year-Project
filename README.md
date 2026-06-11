# 🎬 Automated Video Editing System

An intelligent AI-powered video editing pipeline that automatically removes unnecessary pauses, compresses videos intelligently, and generates subtitles while preserving natural speech rhythm.

## ✨ Features

- ✅ **Automatic Pause Detection** - Uses OpenAI Whisper to identify and analyze all pauses with word-level timestamps
- ✅ **AI Decision Making** - Intelligently classifies pauses as CUT, COMPRESS, or KEEP based on context
- ✅ **Automatic Subtitles** - Generates and burns SRT subtitles into the video
- ✅ **Style Learning** - Learn custom editing styles from YouTube reference videos
- ✅ **Multiple Presets** - Balanced, Aggressive, or Custom editing modes
- ✅ **REST API** - FastAPI backend for easy web/mobile integration
- ✅ **Real-time Logging** - Detailed step-by-step console output

## 📊 Results

- **Compression Rate**: 90-95% smaller file sizes
- **Processing Speed**: ~1-2 seconds per minute of video
- **Accuracy**: Word-level pause detection via Whisper

## 🛠️ Tech Stack

- **Backend**: FastAPI, Python 3.8+
- **Speech Recognition**: OpenAI Whisper
- **Audio Processing**: Librosa, FFmpeg
- **Video Editing**: FFmpeg with subtitle burning
- **Data Processing**: Pandas, NumPy
- **YouTube Downloading**: yt-dlp

## 📋 Prerequisites

- **Python 3.8** or higher
- **FFmpeg** - Download from https://ffmpeg.org/download.html
- **Git** - Download from https://git-scm.com/

## 🚀 Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/final_year_project.git
cd final_year_project
```

### 2. Install Dependencies

**Option A: Using Startup Script (Recommended)**
```powershell
.\start.ps1
```

**Option B: Manual Setup**
```bash
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
cd src
uvicorn app:app --reload
```

### 3. Test the System

In a **new terminal**:
```powershell
cd final_year_project\src
python test_api.py test_video.mp4
```

## 📁 Project Structure

```
final_year_project/
├── src/
│   ├── app.py                      # FastAPI server & orchestrator
│   ├── decision_engine.py          # AI pause classification logic
│   ├── transcription_service.py    # Whisper transcription
│   ├── feature_extractor.py        # Pause detection & analysis
│   ├── subtitle_service.py         # SRT subtitle generation
│   ├── style_extractor.py          # YouTube reference style learning
│   ├── render_engine.py            # Video editing & stitching
│   ├── test_api.py                 # Testing script
│   ├── test_video.mp4              # Sample test video 1
│   └── test_video2.mp4             # Sample test video 2
├── requirements.txt                # Python dependencies
├── start.ps1                       # PowerShell startup script
├── start.bat                       # Windows CMD startup script
├── .gitignore                      # Git ignore rules
└── README.md                       # This file
```

## 🔌 API Endpoints

### POST `/edit-video/`

Upload and process a video.

**Parameters:**
- `video_file` (required): MP4 video file
- `style_preset` (optional): `balanced` or `aggressive` (default: `balanced`)
- `reference_link` (optional): YouTube URL to learn custom editing style

**Example using test_api.py:**

```powershell
# Balanced mode (default)
python test_api.py test_video.mp4

# Aggressive mode (more cuts)
python test_api.py test_video.mp4 aggressive

# With YouTube reference (learns custom style)
python test_api.py test_video.mp4 balanced "https://www.youtube.com/watch?v=VIDEO_ID"
```

**Response:**
- `200 OK`: Edited MP4 video file
- `500 Error`: Processing failed (check server logs)

## 🧠 How It Works

### Pipeline Overview

```
Video Upload
    ↓
1. Audio Extraction (FFmpeg)
    ↓
2. Speech Transcription (Whisper)
    ↓
3. Pause Detection & Analysis
    ↓
4. Style Profile Selection
    ↓
5. AI Decision Making (CUT/COMPRESS/KEEP)
    ↓
6. Video Rendering & Stitching
    ↓
7. Cleanup Temp Files
    ↓
Edited Video Output
```

### Decision Logic

The AI makes decisions based on pause characteristics:

**Rule 1: Sentence-End Pauses**
- If pause > `max_duration` → COMPRESS to 0.5s
- Otherwise → KEEP as-is

**Rule 2: High-Energy Pauses** (laughs, sighs, emphasis)
- RMS energy > 0.08 → KEEP (preserves natural expression)

**Rule 3: Mid-Sentence Silence**
- If pause > `compress_duration` → CUT (remove entirely)
- Otherwise → KEEP as-is

## 🎯 Editing Modes

### Balanced (Default)
- **max_duration**: 1.5s (pauses longer than this are compressed)
- **compress_duration**: 0.8s (pauses longer than this are cut)
- **Use Case**: General purpose, maintains natural pacing

### Aggressive
- **max_duration**: 0.5s
- **compress_duration**: 0.3s
- **Use Case**: Fast-paced content, tight editing

### Custom (From YouTube Reference)
- Learns from a reference YouTube video
- Calculates thresholds based on reference's pause patterns
- **Use Case**: Match specific editing style

## 📊 Console Output Example

```
======================================================================
🚀 NEW JOB STARTED
======================================================================
📁 File: test_video.mp4
🎨 Preset: balanced

──────────────────────────────────────────────────────────────────────
Step 1: Audio Extraction & Transcription
──────────────────────────────────────────────────────────────────────
   ✅ Transcription complete
      • Segments: 42
      • Total words: 316

──────────────────────────────────────────────────────────────────────
Step 5: AI Decision Making
──────────────────────────────────────────────────────────────────────
   ✅ Decisions made:
      • CUT:      12 pauses
      • COMPRESS: 8 pauses
      • KEEP:     5 pauses

======================================================================
✅ JOB COMPLETED SUCCESSFULLY
   Total time: 22.81s (0.38m)
======================================================================
```

## 🐛 Troubleshooting

### FFmpeg Not Found
```powershell
choco install ffmpeg
# or manually from https://ffmpeg.org/download.html
```

### Connection Refused
Make sure the FastAPI server is running:
```powershell
.\start.ps1
```

### Module Not Found
Reinstall dependencies:
```powershell
pip install -r requirements.txt
```

### Slow First Run
First execution downloads Whisper model (~1.5GB). Subsequent runs use cached model.

## 🔄 Future Enhancements

- [ ] GPU acceleration for faster transcription
- [ ] Web UI for easy video uploads
- [ ] Batch processing for multiple videos
- [ ] Advanced editing presets (humor, content-specific)
- [ ] Video preview before download
- [ ] Export decision logs for analysis

## 📞 Support

If you encounter issues:
1. Check the server console output for error messages
2. Verify FFmpeg is installed: `ffmpeg -version`
3. Ensure Python 3.8+: `python --version`
4. Check requirements are installed: `pip list`

## 📜 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 👨‍💻 Author

Created as a Final Year Project - Automated Video Editing System

## 🤝 Contributing

Contributions are welcome! Please feel free to submit pull requests.

---

**Ready to edit videos automatically!** 🎬✨
