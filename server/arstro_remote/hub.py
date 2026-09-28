"""State hub: the latest state per topic, pushed to every connected controller.

This is what keeps the three controllers in sync (ARC-03): whichever controller (or
background process) changes something, the owner of that state publishes the new
value here and every session - Bluetooth app, web page, CLI - receives

    {"ev": "state", "topic": "<topic>", "data": {...}}

Topics: recorder, recorder.settings, jobs, gallery, wifi, terminals, pairing, controllers.
A controller that connects gets the current value of every topic in its hello reply
(and can ask again with `state.get`), then only changes.
"""

import json
import logging
import threading

log = logging.getLogger("arstro.hub")

TOPICS = ("recorder", "recorder.settings", "jobs", "gallery", "wifi", "terminals",
          "pairing", "controllers")


class Hub:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}
        self._fingerprint = {}
        self._sessions = set()

    # ---------------------------------------------------------------- sessions
    def attach(self, session):
        with self._lock:
            self._sessions.add(session)

    def detach(self, session):
        with self._lock:
            self._sessions.discard(session)

    # ------------------------------------------------------------------- state
    def publish(self, topic, data, force=False):
        """Store `data` as the state of `topic` and push it to every controller.
        Unchanged values are not re-sent unless force=True."""
        try:
            fp = json.dumps(data, sort_keys=True, default=str)
        except (TypeError, ValueError):
            fp = None
        with self._lock:
            if not force and fp is not None and self._fingerprint.get(topic) == fp:
                return False
            self._state[topic] = data
            self._fingerprint[topic] = fp
            sessions = list(self._sessions)
        for s in sessions:
            try:
                s.post_state(topic, data)
            except Exception:
                log.debug("post_state to session failed", exc_info=True)
        return True

    def get(self, topic, default=None):
        with self._lock:
            return self._state.get(topic, default)

    def snapshot(self, topics=None):
        with self._lock:
            if topics:
                return {t: self._state.get(t) for t in topics}
            return dict(self._state)
