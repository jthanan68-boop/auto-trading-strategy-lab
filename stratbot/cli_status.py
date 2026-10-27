"""
Terminal title/status indicator for CLI apps.
Shows "thinking/working" state in the terminal window title bar.
Works in most terminals (VS Code, Windows Terminal, iTerm2, gnome-terminal, etc.)
"""

import sys
import threading
import time
from contextlib import contextmanager
from typing import Optional


class TerminalStatus:
    """
    Manages terminal title to show thinking/working status.
    
    Usage:
        status = TerminalStatus()
        status.thinking("Analyzing market data...")
        # ... do work ...
        status.done("Complete")
    
    Or as context manager:
        with TerminalStatus.thinking("Fetching quotes"):
            quotes = fetch_quotes()
    """
    
    _instance: Optional["TerminalStatus"] = None
    _lock = threading.Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            return cls._instance
    
    def _init(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._spinner_idx = 0
        self._base_title = ""
        self._current_msg = ""
        self._is_tty = sys.stdout.isatty()
        self._original_title = ""
        
        # Try to capture original title on init
        if self._is_tty:
            try:
                # Request current window title (OSC 21)
                sys.stdout.write("\x1b[21t")
                sys.stdout.flush()
            except Exception:
                pass
    
    def _set_title(self, title: str):
        """Set terminal window title via OSC 2 (Set Window Title)."""
        if not self._is_tty:
            return
        try:
            # OSC 2 ; title ST
            sys.stdout.write(f"\x1b]2;{title}\x07")
            sys.stdout.flush()
        except Exception:
            pass
    
    def _spin(self):
        """Background spinner animation in terminal title."""
        while self._running:
            spinner = self._spinner_chars[self._spinner_idx % len(self._spinner_chars)]
            self._spinner_idx += 1
            title = f"{spinner} {self._current_msg}" if self._current_msg else f"{spinner} Working..."
            self._set_title(title)
            time.sleep(0.1)
        
        # Restore base title when done
        if self._base_title:
            self._set_title(self._base_title)
    
    def thinking(self, message: str = ""):
        """Start showing 'thinking' status with optional message."""
        self._current_msg = message
        self._base_title = f"StratBot • {message}" if message else "StratBot"
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
    
    def working(self, message: str = ""):
        """Alias for thinking()."""
        self.thinking(message)
    
    def done(self, message: str = "Ready"):
        """Stop spinner and show completion message briefly."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)
        self._set_title(f"✓ {message}")
        # Restore base title after 2 seconds
        def restore():
            time.sleep(2)
            if not self._running:  # Only restore if not restarted
                self._set_title(self._base_title or "StratBot")
        threading.Thread(target=restore, daemon=True).start()
    
    def idle(self, message: str = "Idle"):
        """Set idle status (no spinner)."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)
        self._base_title = message
        self._set_title(message)
    
    def error(self, message: str):
        """Show error status briefly."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)
        self._set_title(f"✗ {message}")
        def restore():
            time.sleep(3)
            if not self._running:
                self._set_title(self._base_title or "StratBot")
        threading.Thread(target=restore, daemon=True).start()
    
    @classmethod
    @contextmanager
    def thinking_context(cls, message: str):
        """Context manager for thinking status."""
        status = cls()
        status.thinking(message)
        try:
            yield status
        finally:
            status.done()
    
    @classmethod
    @contextmanager
    def working_context(cls, message: str):
        """Alias for thinking_context."""
        with cls.thinking_context(message) as status:
            yield status


# Convenience functions
_status_instance = TerminalStatus()

def thinking(message: str = ""):
    """Start thinking spinner with message."""
    _status_instance.thinking(message)

def working(message: str = ""):
    """Start working spinner with message."""
    _status_instance.working(message)

def done(message: str = "Ready"):
    """Mark task as done."""
    _status_instance.done(message)

def idle(message: str = "Idle"):
    """Set idle status."""
    _status_instance.idle(message)

def error(message: str):
    """Show error status."""
    _status_instance.error(message)

def set_base_title(title: str):
    """Set the base title shown when idle."""
    _status_instance._base_title = title
    if not _status_instance._running:
        _status_instance._set_title(title)


if __name__ == "__main__":
    # Demo
    print("Testing TerminalStatus...")
    
    with TerminalStatus.thinking_context("Analyzing market data..."):
        time.sleep(1.5)
    
    with TerminalStatus.working_context("Fetching live quotes"):
        time.sleep(1.0)
    
    done("All systems ready")
    time.sleep(1)
    
    idle("Waiting for next cycle")
    print("Demo complete - check terminal title")