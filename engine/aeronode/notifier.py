"""Pushes events to the n8n webhook.

Delivery runs on its own thread behind a bounded queue: if n8n is down or slow,
alerts are retried with exponential backoff and, in the worst case, dropped -
but the decision loop is never blocked waiting on a notification.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import httpx

from .models import Event, Severity

log = logging.getLogger("aeronode.notifier")


class Notifier:
    def __init__(self, webhook_url: str, min_severity: str = "warning", max_queue: int = 1000,
                 max_retries: int = 3) -> None:
        self.url = webhook_url
        self.min_rank = Severity(min_severity).rank
        self.max_retries = max_retries
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self._q: queue.Queue[Event | None] = queue.Queue(maxsize=max_queue)
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def start(self) -> None:
        if not self.enabled:
            log.warning("n8n webhook not configured - alerts disabled")
            return
        self._thread = threading.Thread(target=self._run, name="notifier", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread:
            self._q.put(None)
            self._thread.join(timeout=5)

    def notify(self, event: Event) -> None:
        if not self.enabled or event.severity.rank < self.min_rank:
            return
        try:
            self._q.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        with httpx.Client(timeout=5.0) as client:
            while True:
                event = self._q.get()
                if event is None:
                    return
                self._deliver(client, event)

    def _deliver(self, client: httpx.Client, event: Event) -> None:
        delay = 0.5
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = client.post(self.url, json=event.to_dict())
                if resp.status_code < 500:
                    if resp.status_code >= 400:
                        log.warning("n8n rejected event (%s): %s", resp.status_code, resp.text[:200])
                        self.failed += 1
                    else:
                        self.sent += 1
                    return
            except httpx.HTTPError as exc:
                log.debug("n8n delivery attempt %d failed: %s", attempt, exc)
            time.sleep(delay)
            delay *= 2
        self.failed += 1
        log.warning("gave up delivering %s for %s", event.type, event.drone_id)
