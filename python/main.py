#!/usr/bin/env python3
"""
Rock-Paper-Scissors Game — Arduino UNO Q

Uses the video_object_detection brick for Edge Impulse inference.
The brick manages the camera and runs detection in its Docker container.
Flask on port 5001 serves the custom web UI.
"""

import os
import time
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

# ─── LLM Brick (trash-talking commentator) ───────────────────────────────
_llm = None
LLM_PERSONA = (
    "You are the Arduino UNO Q, a cheeky and competitive robot playing "
    "Rock Paper Scissors against a human. Reply with a single short, funny, "
    "playful, PG-rated sentence. Do not use emojis. Do not explain the rules."
)
try:
    from arduino.app_bricks.llm import LargeLanguageModel
    _llm = LargeLanguageModel(system_prompt=LLM_PERSONA, max_tokens=80, temperature=0.9)
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
        self.llm_comment = None
        self.llm_state = 'idle'
        self.history = []

    def update_detection(self, label, confidence):
        with self._lock:
            if self._detection_locked:
                return
            prev = self.detection
            self.detection = label
            self.confidence = confidence
        if label != prev:
            print(f"[DETECT] {label} ({confidence:.0%})")

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
            self.llm_comment = None
            self.llm_state = 'idle'

        print(f"[GAME] Locked detection: {detected} ({conf:.0%})" if detected else
              "[GAME] Locked detection: none")

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

        self._generate_comment(winner, human_move, arduino_move)

        time.sleep(RESULT_HOLD_SECS)

        with self._lock:
            self.state = 'idle'
            self._detection_locked = False

        return round_record

    def _generate_comment(self, winner, human_move, arduino_move):
        """Ask the LLM for a witty line about the round. No-op if brick absent."""
        if not _llm:
            return

        if winner == 'human':
            prompt = (f"You just LOST: the human played {human_move} and beat your "
                      f"{arduino_move}. Make a funny, dramatic excuse for why you lost.")
        elif winner == 'arduino':
            prompt = (f"You just WON: your {arduino_move} beat the human's {human_move}. "
                      f"Tease the human in a funny, playful way.")
        elif winner == 'draw':
            prompt = (f"It's a draw — you both played {arduino_move}. "
                      f"Make a funny remark about the tie.")
        else:
            prompt = ("The human failed to show a clear hand gesture in time. "
                      "Playfully tell them to show their hand properly next round.")

        with self._lock:
            self.llm_state = 'thinking'
            self.llm_comment = None

        try:
            text = _llm.chat(prompt).strip()
        except Exception as e:
            print(f"[LLM] generation failed: {e}")
            with self._lock:
                self.llm_state = 'idle'
            return

        print(f"[LLM] {text}")
        with self._lock:
            self.llm_comment = text
            self.llm_state = 'ready'

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
            self.llm_comment = None
            self.llm_state = 'idle'
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
                'llmComment': self.llm_comment,
                'llmState': self.llm_state,
                'history': list(self.history),
            }


game = GameState()


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
