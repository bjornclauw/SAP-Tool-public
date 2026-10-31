"""Worker thread for SAP COM calls.

Handles the STA (Single Threaded Apartment) marshalling required by
SAP GUI Scripting. All COM calls run in this thread; results are
queued for the main thread to pick up via poll().

Usage:
    worker = SapWorker(backend_factory)
    job_id = worker.submit("set_property", path="wnd[0]", member="text", value="hello")
    # ... later, in main thread ...
    for result in worker.poll():
        if result.id == job_id:
            print(f"Completed: {result.value}")
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("sap_worker")


@dataclass
class JobResult:
    """Result of a completed job."""

    id: int
    ok: bool
    value: Any = None
    error: str | None = None


class SapWorker:
    """Manages a worker thread for SAP COM calls.

    The worker thread owns the SAP session object and all COM calls.
    Results are queued and polled by the main thread.
    """

    def __init__(self, backend_factory: Callable[[], Any]):
        self._backend_factory = backend_factory
        self._backend = None
        self._thread: threading.Thread | None = None
        self._queue: queue.Queue = queue.Queue()
        self._results: list[JobResult] = []
        self._lock = threading.Lock()
        self._next_id = 0
        self._running = False
        self._connect_pending = False
        self._connected = False
        self._connect_events: list[bool] = []
        self._connect_events_lock = threading.Lock()

    def start(self) -> None:
        """Start the worker thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()
        logger.info("SapWorker started")

    def stop(self) -> None:
        """Stop the worker thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        logger.info("SapWorker stopped")

    def submit(self, method: str, **kwargs) -> int:
        """Submit a job to the worker thread. Returns job ID."""
        job_id = self._next_id
        self._next_id += 1
        self._queue.put((job_id, method, kwargs))
        return job_id

    def disconnect(self) -> None:
        """Release the current backend connection, in the worker thread so
        COM stays on its STA thread. The next submitted job reconnects."""
        self.submit("disconnect")

    def poll(self) -> list[JobResult]:
        """Check for completed jobs. Returns list of JobResult."""
        results = []
        with self._lock:
            results = self._results[:]
            self._results.clear()
        return results

    def _emit_connect_event(self, connected: bool) -> None:
        """Record a connection-state change for the main thread to consume.
        Never calls Tk callbacks from the worker thread -- the toolbar drains
        these via drain_connect_events() inside its after() poll loop."""
        with self._connect_events_lock:
            self._connect_events.append(connected)

    def drain_connect_events(self) -> list[bool]:
        """Main-thread drain of connection-state changes (see
        _emit_connect_event). Returns list of connect results in order."""
        events = []
        with self._connect_events_lock:
            events = self._connect_events[:]
            self._connect_events.clear()
        return events

    def _worker_loop(self) -> None:
        """Main loop for the worker thread."""
        # Initialize COM in STA mode for SAP GUI Scripting
        com_initialized = False
        try:
            import pythoncom

            hr = pythoncom.CoInitialize()
            if hr is None or hr == 0:
                com_initialized = True
                logger.info("COM initialized in worker thread")
            else:
                # S_OK = 0, S_FALSE = 1, RPC_E_CHANGED_STATE = -2147417859
                logger.warning("COM initialization returned: %s", hr)
                com_initialized = True  # Still try to use it
        except ImportError:
            logger.info("pywin32 not available, COM calls will fail")
        except Exception as exc:
            logger.error("Failed to initialize COM: %s", exc)

        # Wait for first job or connect request
        try:
            while self._running:
                if self._connect_pending:
                    self._connect_pending = False
                    try:
                        if self._backend is None:
                            self._backend = self._backend_factory()
                        self._backend.connect()
                        self._connected = self._backend.is_connected()
                        logger.info("Backend created and connected in worker thread")
                        self._emit_connect_event(self._connected)
                    except Exception as exc:
                        logger.error("Backend connect failed: %s", exc)
                        self._connected = False
                        self._emit_connect_event(False)
                    continue

                try:
                    job_id, method, kwargs = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                try:
                    if method == "disconnect":
                        if self._backend is not None:
                            self._backend.disconnect()
                        self._connected = False
                        job_result = JobResult(id=job_id, ok=True, value=None)
                    else:
                        if self._backend is None:
                            self._backend = self._backend_factory()
                            self._backend.connect()
                            self._connected = self._backend.is_connected()
                            logger.info("Backend lazily created in worker thread")
                            self._emit_connect_event(self._connected)
                        elif not self._backend.is_connected():
                            # The run-boundary release dropped the attachment;
                            # re-attach before executing this job.
                            try:
                                self._backend.connect()
                                self._connected = self._backend.is_connected()
                                logger.info("Backend reconnected in worker thread")
                                self._emit_connect_event(self._connected)
                            except Exception as exc:
                                logger.error("Backend reconnect failed: %s", exc)
                                self._connected = False
                                self._emit_connect_event(False)

                        if method == "set_property":
                            path = kwargs["path"]
                            member = kwargs["member"]
                            value = kwargs["value"]
                            result = self._backend.set_property(path, member, value)
                            job_result = JobResult(id=job_id, ok=True, value=result)
                        elif method == "call_method":
                            path = kwargs["path"]
                            member = kwargs["member"]
                            args = kwargs.get("args", [])
                            result = self._backend.call_method(path, member, args)
                            job_result = JobResult(id=job_id, ok=True, value=result)
                        elif method == "get_property":
                            path = kwargs["path"]
                            member = kwargs["member"]
                            result = self._backend.get_property(path, member)
                            job_result = JobResult(id=job_id, ok=True, value=result)
                        elif method == "get_session_property":
                            member = kwargs["member"]
                            result = self._backend.get_session_property(member)
                            job_result = JobResult(id=job_id, ok=True, value=result)
                        else:
                            job_result = JobResult(
                                id=job_id, ok=False, error=f"Unknown method: {method}"
                            )

                except Exception as exc:
                    logger.error("Worker error: %s", exc)
                    job_result = JobResult(id=job_id, ok=False, error=str(exc))

                with self._lock:
                    self._results.append(job_result)
        finally:
            # Always cleanup COM if we initialized it
            if com_initialized:
                try:
                    import pythoncom

                    pythoncom.CoUninitialize()
                    logger.info("COM uninitialized in worker thread")
                except Exception:
                    pass

    def schedule_connect(self) -> None:
        """Schedule connect to happen in the worker thread."""
        self._connect_pending = True

    def connect(self) -> bool:
        """Connect to SAP GUI (runs in worker thread)."""
        if self._backend is None:
            self._backend = self._backend_factory()
        return self._backend.connect()

    def is_connected(self) -> bool:
        """Check connection status (main thread safe)."""
        return self._connected
