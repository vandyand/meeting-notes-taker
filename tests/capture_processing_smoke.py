"""Capture the native processing UI using temporary meeting data, no AI calls."""
import json
from pathlib import Path
from PIL import ImageGrab
from test_processing_feedback import FeedbackTests


def main():
    fixture = FeedbackTests()
    fixture.setUp()
    try:
        app, root, session = fixture.app, fixture.root, fixture.session
        transcripts = session / 'transcripts'
        (transcripts / 'diagnostics.json').write_text(json.dumps({'tracks': [
            {'path': 'system-audio.wav', 'kind': 'system', 'selected': True},
            {'path': 'microphone.wav', 'kind': 'microphone', 'selected': True},
        ]}))
        (transcripts / 'system-audio.json').write_text('{}')
        (session / 'visual').mkdir()
        (session / 'screenshots').mkdir()
        images = [str(session / 'screenshots' / f'image-{i:02d}.png') for i in range(16)]
        for path in images:
            Path(path).touch()
        (session / 'screenshots' / 'manifest.json').write_text(json.dumps({'frames': [{'path': path, 'selected': True} for path in images]}))
        (session / 'visual' / 'ocr.json').write_text(json.dumps({'selected_image_count': 16, 'results': [{'path': path, 'ok': True, 'status': 'complete', 'attempts': 1} for path in images]}))
        app.set_processing_controls(True)
        event = {'stage': 'transcript', 'item': 'microphone.wav', 'current': 2, 'total': 2, 'percent': 67,
                 'item_percent': 50, 'audio_seconds': 600, 'audio_total_seconds': 1200,
                 'progress': 'Transcribing audio 2 of 2 · 50% · Audio 10:00 / 20:00 · CUDA'}
        app.process_events.put(('progress', event))
        app.poll_process_events()
        root.deiconify()
        root.lift()
        root.attributes('-topmost', True)
        out = Path(__file__).resolve().parents[1] / '.artifacts'
        out.mkdir(exist_ok=True)
        for size in ('1120x820', '1000x760'):
            root.geometry(size + '+40+40')
            root.update()
            ImageGrab.grab(window=root.winfo_id()).save(out / f'processing-{size}.png')
        app.set_processing_controls(False)
        (transcripts / 'microphone.json').write_text('{}')
        (transcripts / 'cleaned.md').write_text('Fixture transcript')
        fixture.write_state('failed', 'notes', 'OpenRouter cannot fund this notes request (HTTP 402). Check your credit balance. Your OCR and transcripts are saved; Resume processing after adding credits.')
        app.update_processing_context()
        app.refresh_processing_details()
        app.process_stage_var.set('Notes need attention')
        app.process_var.set('OpenRouter cannot fund this notes request (HTTP 402). Add credits, then Resume processing. Completed OCR and audio will be reused.')
        root.update()
        ImageGrab.grab(window=root.winfo_id()).save(out / 'processing-credit-recovery.png')
        print(f'Native UI screenshots: {out}')
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
