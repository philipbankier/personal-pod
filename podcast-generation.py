import os
import json
import datetime
import shutil
import logging
import boto3
from flask import Flask, request, jsonify
from podcastfy.client import generate_podcast
from pydub import AudioSegment
import re

# Set API keys as environment variables

os.environ["TTS_PROVIDER"] = "elevenlabs"  # Tell Podcastfy to use ElevenLabs for TTS

# Additional configuration for TTS
os.environ["TTS_MODEL"] = "elevenlabs"  # Explicitly set the TTS model
os.environ["USE_OPENAI_TTS"] = "false"  # Disable OpenAI TTS

app = Flask(__name__)

# Set up logging for debugging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Explicitly set the FFmpeg path for pydub
# Use an absolute path to your local ffmpeg binary (adjust if needed)
ffmpeg_path = os.path.abspath("bin/ffmpeg")
logger.info(f"Setting pydub's ffmpeg converter to: {ffmpeg_path}")
AudioSegment.converter = ffmpeg_path

# Also update the PATH environment variable in case generate_podcast uses it
current_path = os.environ.get("PATH", "")
os.environ["PATH"] = current_path + ":" + os.path.abspath("bin")
logger.info(f"Updated PATH: {os.environ['PATH']}")

def clean_report_text(text):
    """Clean report text by removing markdown and special characters."""
    # Remove markdown formatting
    text = text.replace('**', '')
    text = text.replace('#', '')
    # Replace multiple newlines with single newline
    text = re.sub(r'\n\s*\n', '\n', text)
    # Remove any other special characters that might cause issues
    text = re.sub(r'[^\w\s.,;:()\-\'"\n]', '', text)
    return text

@app.route('/generate-podcast', methods=['POST'])
def generate_podcast_endpoint():
    try:
        # Get the raw data
        raw_data = request.get_data().decode('utf-8', errors='ignore')
        logger.info("Received request with Content-Type: %s", request.headers.get('Content-Type', 'None'))
        
        try:
            # Parse JSON with more permissive settings
            data = json.loads(raw_data, strict=False)
            
            # Clean the report text immediately after parsing
            if 'report_text' in data:
                data['report_text'] = clean_report_text(data['report_text'])
                logger.info("Successfully cleaned report text")
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON parsing failed: {str(e)}")
            return jsonify({
                "error": "Invalid JSON format",
                "details": str(e),
                "raw_data_sample": raw_data[:200] + "..." if len(raw_data) > 200 else raw_data
            }), 400

        # Log successful parsing
        logger.info("Successfully parsed JSON data with keys: %s", list(data.keys()))
        
        # Handle both camelCase and snake_case variations of podcast_id
        podcast_id = data.get("podcast_id") or data.get("podcastId", "default")
        
        # Extract other fields with validation
        topic = data.get("prompt", "").strip()
        report_text = data.get("report_text", "").strip()
        podcast_name = data.get("podcast_name", f"Weekly Podcast on {topic}").strip()
        
        # Validate required fields
        missing_fields = []
        if not topic:
            missing_fields.append("prompt")
        if not report_text:
            missing_fields.append("report_text")
            
        if missing_fields:
            error_msg = f"Missing required fields: {', '.join(missing_fields)}"
            logger.error(error_msg)
            return jsonify({"error": error_msg}), 400
        
        logger.info(f"Processing request with:")
        logger.info(f"- podcast_id: {podcast_id}")
        logger.info(f"- podcast_name: {podcast_name}")
        logger.info(f"- prompt: {topic}")
        logger.info(f"- report_text length: {len(report_text)} characters")

        # Podcastfy Configuration
        intro = f"Welcome to {podcast_name}, by PersonalPod."
        PODCAST_CONFIG = {
            "podcast_name": podcast_name,
            "intro": intro,
            "conversation_style": ["professional", "analytical", "concise"],
            "roles_person1": "market analyst",
            "roles_person2": "tech strategist"
        }
        
        # File Generation in /tmp
        OUTPUT_FOLDER = "/tmp/output"
        podcasts_dir = os.path.join(OUTPUT_FOLDER, "podcasts")
        os.makedirs(podcasts_dir, exist_ok=True)
        
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        structured_content = (
            f"Weekly {topic} Podcast (ID: {podcast_id}) - {datetime.datetime.now().strftime('%Y-%m-%d')}\n\n"
            + report_text +
            "\n\nNote: This report is generated using AI analysis of market data and trends. Always conduct your own research."
        )
        
        # Log the structured content for debugging (you can remove this in production)
        logger.info("Structured content for podcast:")
        logger.info(structured_content)
        
        content_filename = f"podcast_content_{podcast_id}_{timestamp}.txt"
        content_path = os.path.join(podcasts_dir, content_filename)
        with open(content_path, "w") as f:
            f.write(structured_content)
        
        # Generate the podcast audio file using Podcastfy.
        logger.info("Starting podcast generation with Podcastfy using ElevenLabs TTS...")
        audio_path = generate_podcast(
            text=structured_content,
            conversation_config=PODCAST_CONFIG,
            tts_model="elevenlabs"  # Explicitly set ElevenLabs as TTS provider
        )
        logger.info(f"generate_podcast returned: {audio_path}")
        
        # If the audio file is not in our target folder, move it there.
        if audio_path and not audio_path.startswith(podcasts_dir):
            new_audio_path = os.path.join(podcasts_dir, os.path.basename(audio_path))
            shutil.move(audio_path, new_audio_path)
            audio_path = new_audio_path
        
        if not audio_path or not os.path.exists(audio_path):
            logger.error("Failed to generate podcast audio file")
            return jsonify({"error": "Podcast generation failed"}), 500
        
        logger.info(f"Audio file generated at: {audio_path}")
        
        # Upload to S3
        s3_bucket = "personal-pod"
        s3_prefix = "generated-podcasts/"
        filename = f"{podcast_id}_{os.path.basename(audio_path)}"
        s3_key = f"{s3_prefix}{filename}"
        
        with open(audio_path, "rb") as file_data:
            audio_bytes = file_data.read()
        
        s3 = boto3.client('s3')
        s3.put_object(
            Bucket=s3_bucket,
            Key=s3_key,
            Body=audio_bytes,
            ACL="public-read",  # Ensure the file is publicly accessible
            ContentType="audio/mpeg"  # Adjust if your file is in a different format
        )
        # Explicitly enforce public-read ACL.
        s3.put_object_acl(
            Bucket=s3_bucket,
            Key=s3_key,
            ACL="public-read"
        )
        
        public_url = f"https://{s3_bucket}.s3.amazonaws.com/{s3_key}"
        logger.info(f"Podcast uploaded to S3 at: {public_url}")
        
        return jsonify({
            "podcast_id": podcast_id,
            "podcastUrl": public_url,
            "audio_filename": filename,
            "text_file": content_path
        })
        
    except Exception as e:
        error_detail = {
            "error": str(e),
            "error_type": type(e).__name__,
            "timestamp": datetime.datetime.now().isoformat(),
            "request_data": {
                "podcast_id": data.get("podcast_id") or data.get("podcastId", "N/A"),
                "podcast_name": data.get("podcast_name", "N/A"),
                "prompt": data.get("prompt", "N/A"),
                "report_text_length": len(data.get("report_text", "")) if data.get("report_text") else 0
            }
        }
        logger.exception("Error in generate_podcast_endpoint")
        logger.error(f"Error details: {json.dumps(error_detail, indent=2)}")
        return jsonify(error_detail), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

