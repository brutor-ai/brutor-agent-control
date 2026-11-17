# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Brutor logging system with standardized formatting,
structured context, and correlation tracking.
"""

import inspect
import json
import logging
import sys
import traceback
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from functools import wraps
from typing import Dict, Any, Optional, Union

# Context variables for request correlation
correlation_id: ContextVar[Optional[str]] = ContextVar('correlation_id', default=None)
user_context: ContextVar[Optional[str]] = ContextVar('user_context', default=None)
request_path: ContextVar[Optional[str]] = ContextVar('request_path', default=None)


@dataclass
class LogContext:
    """Structured logging context"""
    correlation_id: Optional[str] = None
    user_id: Optional[str] = None
    component: Optional[str] = None
    operation: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[Union[str, int]] = None
    duration_ms: Optional[float] = None
    request_path: Optional[str] = None
    request_method: Optional[str] = None
    request_url: Optional[str] = None
    status_code: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    result_count: Optional[int] = None
    filters: Optional[Dict[str, Any]] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        base = {k: v for k, v in asdict(self).items() if v not in (None, {}, [])}
        # flatten extra fields into top-level
        if "extra" in base:
            base.update(base.pop("extra"))
        return base


class StructuredFormatter(logging.Formatter):
    """Custom formatter for structured JSON logging with proper exception handling"""

    def format(self, record: logging.LogRecord) -> str:
        # Base log entry
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno
        }

        # Add correlation context from context vars
        if correlation_id.get():
            log_entry["correlation_id"] = correlation_id.get()
        if user_context.get():
            log_entry["user_id"] = user_context.get()
        if request_path.get():
            log_entry["request_path"] = request_path.get()

        # Add structured context from record extra data
        context_data = getattr(record, '_log_context', None)
        if context_data and isinstance(context_data, dict):
            log_entry.update(context_data)

        # Add exception information - properly handle exc_info
        if record.exc_info and record.exc_info != (None, None, None):
            # record.exc_info is a tuple (type, value, traceback) or None
            exc_type, exc_value, exc_tb = record.exc_info
            if exc_type is not None:
                log_entry["exception"] = {
                    "type": exc_type.__name__,
                    "message": str(exc_value),
                    "traceback": traceback.format_exception(exc_type, exc_value, exc_tb)
                }

        return json.dumps(log_entry, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """Human-readable formatter for development with proper exception handling"""

    def format(self, record: logging.LogRecord) -> str:
        # Base format
        base_msg = super().format(record)

        # Add context information
        context_parts = []
        if correlation_id.get():
            context_parts.append(f"corr_id={correlation_id.get()[:8]}")
        if user_context.get():
            context_parts.append(f"user={user_context.get()}")

        # Add context from record
        context_data = getattr(record, '_log_context', None)
        if context_data and isinstance(context_data, dict):
            for key, value in context_data.items():
                if key not in ['correlation_id', 'user_id']:  # Avoid duplicates
                    if isinstance(value, (str, int, float, bool)):
                        context_parts.append(f"{key}={value}")
                    elif isinstance(value, (list, tuple)) and len(value) <= 3:
                        context_parts.append(f"{key}={value}")

        if context_parts:
            context_str = " | " + " | ".join(context_parts)
            return f"{base_msg}{context_str}"

        return base_msg


class BrutorLogger:
    """Enhanced logger with structured context support and proper exception handling"""

    def __init__(self, name: str):
        self.logger = logging.getLogger(name)
        self._component = name.split('.')[-1]  # Extract component name

    def _normalize_context(self, context: Optional[Union[LogContext, Dict[str, Any]]]) -> Dict[str, Any]:
        """Convert context to a plain dictionary for logging"""
        if context is None:
            return {}
        elif isinstance(context, LogContext):
            return context.to_dict()
        elif isinstance(context, dict):
            return context.copy()
        else:
            # Fallback for unexpected types
            return {"context": str(context)}

    def _should_capture_exception(self, exc_info: Optional[Union[bool, tuple]]) -> Union[bool, tuple]:
        """
        Determine if we should capture exception info and return the proper value.

        Args:
            exc_info: Can be None, True, False, or an exception tuple

        Returns:
            Proper exc_info value for logging
        """
        if exc_info is None:
            # Auto-detect: capture if there's an active exception
            current_exc_info = sys.exc_info()
            return current_exc_info if current_exc_info[0] is not None else False
        elif exc_info is True:
            # Explicitly requested: get current exception info
            current_exc_info = sys.exc_info()
            return current_exc_info if current_exc_info[0] is not None else False
        elif exc_info is False:
            # Explicitly disabled
            return False
        elif isinstance(exc_info, tuple):
            # Explicit exception info provided
            return exc_info
        else:
            # Fallback
            return False

    def _log(self, level: int, message: str, context: Optional[Union[LogContext, Dict[str, Any]]] = None,
             exc_info: Optional[Union[bool, tuple]] = None, **kwargs):
        """Internal logging method with context support and proper caller info"""

        # Normalize context to plain dict
        context_dict = self._normalize_context(context)

        # Determine proper exc_info value
        resolved_exc_info = self._should_capture_exception(exc_info)

        # Get the caller's frame (skip this method and the public method)
        frame = inspect.currentframe()
        try:
            # Skip _log and the calling method (debug, info, etc.) to get the actual caller
            caller_frame = frame.f_back.f_back
            if caller_frame:
                # Create a new LogRecord with the correct caller information
                record = self.logger.makeRecord(
                    self.logger.name,
                    level,
                    caller_frame.f_code.co_filename,
                    caller_frame.f_lineno,
                    message,
                    (),
                    resolved_exc_info if resolved_exc_info else None,
                    caller_frame.f_code.co_name
                )
                # Store context in a safe attribute that won't conflict
                record._log_context = context_dict
                self.logger.handle(record)
            else:
                # Fallback to normal logging if frame inspection fails
                extra = {'_log_context': context_dict}
                self.logger.log(level, message, exc_info=resolved_exc_info, extra=extra, **kwargs)
        except Exception as logging_error:
            # If anything goes wrong with frame inspection or logging,
            # fall back to basic logging to avoid losing the original message
            try:
                print(f"Logging error occurred: {logging_error}", file=sys.stderr)
                extra = {'_log_context': context_dict}
                self.logger.log(level, message, exc_info=resolved_exc_info, extra=extra, **kwargs)
            except Exception as fallback_error:
                # Last resort - just print to stderr
                print(f"Critical logging failure: {fallback_error}", file=sys.stderr)
                print(f"Original message: {message}", file=sys.stderr)
        finally:
            del frame  # Prevent reference cycles

    def debug(self, message: str, context: Optional[Union[LogContext, Dict[str, Any]]] = None,
              exc_info: Optional[Union[bool, tuple]] = None, **kwargs):
        """Log debug message with optional exception info"""
        self._log(logging.DEBUG, message, context, exc_info, **kwargs)

    def info(self, message: str, context: Optional[Union[LogContext, Dict[str, Any]]] = None,
             exc_info: Optional[Union[bool, tuple]] = None, **kwargs):
        """Log info message with optional exception info"""
        self._log(logging.INFO, message, context, exc_info, **kwargs)

    def warning(self, message: str, context: Optional[Union[LogContext, Dict[str, Any]]] = None,
                exc_info: Optional[Union[bool, tuple]] = None, **kwargs):
        """Log warning message with optional exception info"""
        self._log(logging.WARNING, message, context, exc_info, **kwargs)

    def error(self, message: str, context: Optional[Union[LogContext, Dict[str, Any]]] = None,
              exc_info: Optional[Union[bool, tuple]] = None, **kwargs):
        """
        Log error message with intelligent exception handling.

        Args:
            message: The error message
            context: Structured context information
            exc_info: Exception info handling:
                - None (default): Auto-detect if there's an active exception
                - True: Force capture current exception (if any)
                - False: Never capture exception info
                - tuple: Use provided exception info tuple
        """
        # Default to auto-detection for error level
        if exc_info is None:
            exc_info = True  # Auto-detect active exception
        self._log(logging.ERROR, message, context, exc_info, **kwargs)

    def critical(self, message: str, context: Optional[Union[LogContext, Dict[str, Any]]] = None,
                 exc_info: Optional[Union[bool, tuple]] = None, **kwargs):
        """
        Log critical message with intelligent exception handling.

        Similar to error() but defaults to capturing exceptions for critical issues.
        """
        if exc_info is None:
            exc_info = True  # Auto-detect active exception
        self._log(logging.CRITICAL, message, context, exc_info, **kwargs)

    def exception(self, message: str, context: Optional[Union[LogContext, Dict[str, Any]]] = None, **kwargs):
        """
        Log an exception message with full traceback.

        This method should be called from an exception handler and will
        automatically capture the current exception info.
        """
        self.error(message, context, exc_info=True, **kwargs)

    # Convenience methods for common patterns
    def operation_start(self, operation: str, **context_kwargs):
        """Log the start of an operation"""
        context_kwargs = dict(context_kwargs)  # copy

        ctx = LogContext(
            component=self._component,
            operation=operation,
            correlation_id=context_kwargs.pop("correlation_id", correlation_id.get()),
            user_id=context_kwargs.pop("user_id", user_context.get()),
            extra=context_kwargs  # everything else goes here
        )
        self.info(f"Starting {operation}", context=ctx)

    def operation_success(self, operation: str, duration_ms: Optional[float] = None, **context_kwargs):
        """Log successful completion of an operation"""
        context_kwargs = dict(context_kwargs)  # copy

        ctx = LogContext(
            component=self._component,
            operation=operation,
            duration_ms=duration_ms,
            correlation_id=context_kwargs.pop("correlation_id", correlation_id.get()),
            user_id=context_kwargs.pop("user_id", user_context.get()),
            extra=context_kwargs  # everything else goes here
        )
        self.info(f"Completed {operation}", context=ctx)

    def operation_error(self, operation: str, error: str, duration_ms: Optional[float] = None,
                        capture_exception: bool = False, **context_kwargs):
        """
        Log operation failure with optional exception capture

        Args:
            operation: Name of the operation that failed
            error: Error message
            duration_ms: Operation duration
            capture_exception: Whether to capture current exception traceback
            **context_kwargs: Additional context
        """
        context_kwargs = dict(context_kwargs)  # copy

        ctx = LogContext(
            component=self._component,
            operation=operation,
            duration_ms=duration_ms,
            correlation_id=context_kwargs.pop("correlation_id", correlation_id.get()),
            user_id=context_kwargs.pop("user_id", user_context.get()),
            extra=context_kwargs  # everything else goes here
        )

        # Use exc_info based on capture_exception parameter
        self.error(f"Failed {operation}: {error}", context=ctx,
                   exc_info=capture_exception)

    def permission_denied(self, operation: str, user_id: str, required_permission: str,
                          resource_type: Optional[str] = None, resource_id: Optional[Union[str, int]] = None):
        """Log permission denied events (no exception capture needed)"""
        ctx = LogContext(
            component=self._component,
            operation=operation,
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            correlation_id=correlation_id.get(),
            extra={"required_permission": required_permission}
        )
        self.warning(f"Permission denied for {operation}: user needs {required_permission}",
                     context=ctx, exc_info=False)

    def authentication_failed(self, reason: str, client_id: Optional[str] = None):
        """Log authentication failures (no exception capture needed)"""
        ctx = LogContext(
            component=self._component,
            operation="authenticate",
            correlation_id=correlation_id.get(),
            extra={"auth_failure_reason": reason}
        )
        if client_id:
            ctx.user_id = client_id

        # Use WARNING level without stack traces for expected auth failures
        self.warning(f"Authentication failed: {reason}", context=ctx, exc_info=False)


def setup_logging(log_level: str = "INFO", use_json: bool = False,
                  log_file: Optional[str] = None) -> BrutorLogger:
    """
    Set up standardized logging for the MCP server
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        use_json: Use JSON formatter instead of human-readable
        log_file: Optional file to write logs to
    
    Returns:
        BrutorLogger instance for the main application
    """
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL
    }

    numeric_level = level_map.get(log_level.upper(), logging.INFO)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Choose formatter
    if use_json:
        formatter = StructuredFormatter()
    else:
        formatter = HumanFormatter(
            '%(asctime)s | %(levelname)-8s | %(name)-20s | %(funcName)-15s | %(lineno)-4d | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler if specified
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(numeric_level)
        # Always use JSON for file logging
        file_handler.setFormatter(StructuredFormatter())
        root_logger.addHandler(file_handler)

    # Configure third-party loggers
    third_party_loggers = {
        "httpx": max(numeric_level, logging.WARNING),
        "uvicorn": max(numeric_level, logging.INFO),
        "uvicorn.error": max(numeric_level, logging.INFO),
        "uvicorn.access": logging.WARNING if numeric_level > logging.INFO else numeric_level,
        "fastapi": max(numeric_level, logging.INFO),
        "mcp": numeric_level,
    }

    # Rename "uvicorn.error" → "uvicorn" in log output (uvicorn uses .error for all lifecycle messages)
    class _UvicornNameFilter(logging.Filter):
        def filter(self, record):
            if record.name == "uvicorn.error":
                record.name = "uvicorn"
            return True

    for logger_name, level in third_party_loggers.items():
        uv_logger = logging.getLogger(logger_name)
        uv_logger.setLevel(level)
        # Remove default handlers so uvicorn uses the root logger's formatter
        uv_logger.handlers.clear()

    logging.getLogger("uvicorn.error").addFilter(_UvicornNameFilter())

    # Return main application logger
    app_logger = BrutorLogger("brutor-ai-gateway")

    return app_logger


def with_correlation_id(func):
    """Decorator to generate correlation ID for request processing"""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        # Generate new correlation ID if not already set
        if not correlation_id.get():
            correlation_id.set(str(uuid.uuid4()))

        try:
            return await func(*args, **kwargs)
        finally:
            # Don't clear correlation_id here as it might be needed for logging after response
            pass

    return wrapper


def with_user_context(user_id: str):
    """Context manager to set user context for logging"""

    class UserContextManager:
        def __enter__(self):
            user_context.set(user_id)
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            user_context.set(None)

    return UserContextManager()


def with_request_context(path: str):
    """Context manager to set request path for logging"""

    class RequestContextManager:
        def __enter__(self):
            request_path.set(path)
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            request_path.set(None)

    return RequestContextManager()
