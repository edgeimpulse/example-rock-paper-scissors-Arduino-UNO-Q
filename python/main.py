#!/usr/bin/env python3
"""
Rock-Paper-Scissors Game — Arduino UNO Q

Uses the video_object_detection brick for Edge Impulse inference.
The brick manages the camera and runs detection in its Docker container.
Flask on port 5001 serves the custom web UI.
"""

import os
import time
import queue
import random
import logging
import threading
from flask import Flask, render_template, jsonify

# ─── Silence Flask HTTP logs ─────────────────────────────────────────────
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# ─── Video Object Detection Brick ────────────────────────────────────────
_detector = None
try:
    from arduino.app_bricks.video_objectdetection import VideoObjectDetection
    _detector = VideoObjectDetection(confidence=0.6, debounce_sec=0.0)
    print("[BRICK] VideoObjectDetection initialized")
except ImportError:
    print("[WARN] VideoObjectDetection brick not available — detection disabled")

# ─── LLM Brick (live play-by-play commentator) ───────────────────────────
_llm = None
LLM_PERSONA = (
    "You are an energetic live sports commentator narrating a Rock Paper "
    "Scissors duel between a Human and an Arduino robot. Reply with ONE short, "
    "punchy, exciting play-by-play line — like calling a football match. "
    "No emojis. Do not explain the rules. Do not use quotation marks."
)
try:
    from arduino.app_bricks.llm import LargeLanguageModel
    _llm = LargeLanguageModel(system_prompt=LLM_PERSONA, max_tokens=60, temperature=0.9)
    print("[BRICK] LargeLanguageModel initialized")
except ImportError:
    print("[WARN] LLM brick not available — commentary disabled")

# ─── App Runner ──────────────────────────────────────────────────────────
_App = None
try:
    from arduino.app_utils import App as _App
except ImportError:
    try:
        from arduino.app import App as _App
    except ImportError:
        try:
            from arduino import App as _App
        except ImportError:
            pass

# ─── Configuration ───────────────────────────────────────────────────────
CONFIDENCE_THRESHOLD = 0.6
VALID_LABELS = {'rock', 'paper', 'scissors'}
PORT = int(os.environ.get('FLASK_PORT', '5001'))
COUNTDOWN_SECS = 3
RESULT_HOLD_SECS = 3

WINS = {'Rock': 'Scissors', 'Scissors': 'Paper', 'Paper': 'Rock'}


# ─── Game State ──────────────────────────────────────────────────────────
class GameState:
    """Thread-safe game state with scoring and round history."""

    def __init__(self):
        self._lock = threading.Lock()
        self.human_wins = 0
        self.arduino_wins = 0
        self.draws = 0
        self.round_number = 0
        self.state = 'idle'
        self.countdown = None
        self.arduino_move = None
        self.human_move = None
        self.winner = None
        self.detection = None
        self.confidence = 0.0
        self._detection_locked = False
        self.commentary = []
        self.commentating = False
        self.history = []

    def update_detection(self, label, confidence):
        changed = False
        with self._lock:
            if self._detection_locked:
                return
            prev = self.detection
            self.detection = label
            self.confidence = confidence
            changed = label != prev
        if changed:
            print(f"[DETECT] {label} ({confidence:.0%})")
            enqueue_detection(label, confidence)

    def play_round(self):
        arduino_move = random.choice(['Rock', 'Paper', 'Scissors'])

        # Lock detection: snapshot the current gesture immediately
        with self._lock:
            self._detection_locked = True
            detected = self.detection
            conf = self.confidence
            self.state = 'countdown'
            self.arduino_move = arduino_move
            self.human_move = None
            self.winner = None

        locked_move = detected.capitalize() if detected and detected in VALID_LABELS else None

        print(f"[GAME] Locked detection: {detected} ({conf:.0%})" if detected else
              "[GAME] Locked detection: none")

        enqueue_milestone('round_start', human_move=locked_move)
        enqueue_milestone('arduino_choice', arduino_move=arduino_move, human_move=locked_move)

        for tick in [3, 2, 1]:
            with self._lock:
                self.countdown = tick
            time.sleep(1)

        with self._lock:
            self.state = 'evaluating'
            self.countdown = None

        human_move = detected.capitalize() if detected and detected in VALID_LABELS else None

        if human_move and WINS.get(human_move):
            if human_move == arduino_move:
                winner = 'draw'
            elif WINS[human_move] == arduino_move:
                winner = 'human'
            else:
                winner = 'arduino'
        else:
            winner = 'no_detection'

        with self._lock:
            self.human_move = human_move
            self.winner = winner
            self.round_number += 1

            if winner == 'human':
                self.human_wins += 1
            elif winner == 'arduino':
                self.arduino_wins += 1
            elif winner == 'draw':
                self.draws += 1

            round_record = {
                'round': self.round_number,
                'humanMove': human_move,
                'arduinoMove': arduino_move,
                'winner': winner,
                'confidence': conf,
            }
            self.history.insert(0, round_record)
            self.state = 'result'

        print(f"[GAME] Round {round_record['round']}: "
              f"Human={human_move or '?'} vs Arduino={arduino_move} -> {winner}")

        enqueue_milestone('result', winner=winner, human_move=human_move,
                          arduino_move=arduino_move)

        time.sleep(RESULT_HOLD_SECS)

        with self._lock:
            self.state = 'idle'
            self._detection_locked = False

        return round_record

    def add_commentary(self, text):
        with self._lock:
            self.commentary.insert(0, text)
            del self.commentary[10:]

    def set_commentating(self, value):
        with self._lock:
            self.commentating = value

    def reset(self):
        with self._lock:
            self.human_wins = 0
            self.arduino_wins = 0
            self.draws = 0
            self.round_number = 0
            self.state = 'idle'
            self.countdown = None
            self.arduino_move = None
            self.human_move = None
            self.winner = None
            self._detection_locked = False
            self.commentary.clear()
            self.commentating = False
            self.history.clear()
        print("[GAME] Scores reset")

    def to_dict(self):
        with self._lock:
            return {
                'humanWins': self.human_wins,
                'arduinoWins': self.arduino_wins,
                'draws': self.draws,
                'round': self.round_number,
                'state': self.state,
                'countdown': self.countdown,
                'arduinoMove': self.arduino_move,
                'humanMove': self.human_move,
                'winner': self.winner,
                'detection': self.detection,
                'confidence': self.confidence,
                'commentary': list(self.commentary),
                'commentating': self.commentating,
                'history': list(self.history),
            }


game = GameState()


# ─── Live Commentator ────────────────────────────────────────────────────
# A single worker thread turns game events into play-by-play lines. The local
# LLM is slow, so milestone events (round start, pick, result) are queued and
# never dropped, while rapid detection changes are coalesced to the latest one.
_milestones = queue.Queue()
_pending_detection = None
_pending_lock = threading.Lock()
_wake = threading.Event()


def enqueue_milestone(kind, **data):
    if not _llm:
        return
    _milestones.put({'kind': kind, **data})
    _wake.set()


def enqueue_detection(label, confidence):
    global _pending_detection
    if not _llm:
        return
    with _pending_lock:
        _pending_detection = {'kind': 'detection', 'label': label, 'confidence': confidence}
    _wake.set()


def _next_event():
    """Block until an event is available. Milestones take priority; detection
    changes are coalesced so only the most recent is narrated."""
    global _pending_detection
    while True:
        try:
            return _milestones.get_nowait()
        except queue.Empty:
            pass
        with _pending_lock:
            if _pending_detection is not None:
                event = _pending_detection
                _pending_detection = None
                return event
        _wake.wait()
        _wake.clear()


def _prompt_for(event):
    kind = event['kind']
    if kind == 'detection':
        if event['label']:
            return (f"Live: the human is now showing {event['label']} to the camera "
                    f"({event['confidence']:.0%} confident). Call it like a sportscaster.")
        return "Live: the human's hand went out of view — nothing detected. React quickly."
    if kind == 'round_start':
        hm = event.get('human_move') or 'no clear gesture yet'
        return f"The round is ON! The human has locked in {hm}. Hype up the showdown."
    if kind == 'arduino_choice':
        return (f"Arduino has locked in {event['arduino_move']}! The human is showing "
                f"{event.get('human_move') or 'nothing clear'}. Call who has the edge and the odds!")
    if kind == 'result':
        verdict = {
            'human': 'the HUMAN wins the round',
            'arduino': 'the ARDUINO wins the round',
            'draw': "it's a DRAW",
            'no_detection': 'no valid move from the human — no contest',
        }.get(event['winner'], event['winner'])
        return (f"FINAL WHISTLE: {verdict}! Human played "
                f"{event.get('human_move') or '—'}, Arduino played {event['arduino_move']}. "
                f"Give the dramatic verdict.")
    return None


def commentator_worker():
    while True:
        event = _next_event()
        prompt = _prompt_for(event)
        if not prompt:
            continue
        game.set_commentating(True)
        try:
            text = _llm.chat(prompt).strip()
        except Exception as e:
            print(f"[LLM] commentary failed: {e}")
            game.set_commentating(False)
            continue
        game.set_commentating(False)
        if text:
            print(f"[LLM] {text}")
            game.add_commentary(text)


if _llm:
    threading.Thread(target=commentator_worker, daemon=True).start()


# ─── Brick Detection Callback ────────────────────────────────────────────
def handle_detections(detections):
    """Called by the video_object_detection brick with detection results.

    The brick may pass either:
      - {label: {"confidence": float}} (dict values)
      - {label: float}                 (plain float values)
    """
    if not detections:
        return
    print(f"[BRICK-RAW] {detections}")
    valid = {}
    for k, v in detections.items():
        label = k.lower()
        if label not in VALID_LABELS:
            continue
        if isinstance(v, dict):
            conf = v.get("confidence")
        elif isinstance(v, list):
            conf = v[0].get("confidence") if v and isinstance(v[0], dict) else None
        else:
            conf = v
        if conf is not None and conf >= CONFIDENCE_THRESHOLD:
            valid[label] = conf
    if valid:
        best = max(valid, key=valid.get)
        game.update_detection(best, valid[best])


if _detector:
    _detector.on_detect_all(handle_detections)


# ─── Flask Application ───────────────────────────────────────────────────
app = Flask(__name__)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/state')
def api_state():
    return jsonify(game.to_dict())


@app.route('/api/play', methods=['POST'])
def api_play():
    state = game.to_dict()
    if state['state'] != 'idle':
        return jsonify({'status': 'busy', 'message': 'Round in progress'}), 409
    threading.Thread(target=game.play_round, daemon=True).start()
    return jsonify({'status': 'ok', 'message': 'Round started'})


@app.route('/api/reset', methods=['POST'])
def api_reset():
    game.reset()
    return jsonify({'status': 'ok'})


# ─── Entry Point ─────────────────────────────────────────────────────────
if __name__ == '__main__':
    print('=' * 50)
    print('  Rock Paper Scissors — Arduino UNO Q')
    print('=' * 50)
    print(f'[MODE] Brick: {"yes" if _detector else "no"}')
    print(f'[MODE] LLM: {"yes" if _llm else "no"}')
    print(f'[MODE] App runner: {"yes" if _App else "no"}')

    threading.Thread(
        target=lambda: app.run(
            host='0.0.0.0', port=PORT, threaded=True, use_reloader=False
        ),
        daemon=True
    ).start()
    print(f'[WEB] http://0.0.0.0:{PORT}')

    if _App:
        _App.run()
    else:
        print('[INFO] Running standalone (no App runner)')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print('\n[EXIT] Shutting down')
