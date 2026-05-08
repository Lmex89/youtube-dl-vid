import json
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger('videos')


class APILoggingMiddleware:
    """
    Middleware for logging API requests/responses in JSON format.
    
    Features:
    - Logs request method, path, user, IP
    - Logs response status code and duration
    - Masks sensitive data (passwords, tokens, etc.)
    - Truncates large request bodies (>1KB)
    - Generates unique request_id for correlation
    - Warns on slow requests (>2s)
    """
    
    SENSITIVE_FIELDS = [
        'password', 'secret', 'token', 'api_key', 'apikey', 
        'credentials', 'auth', 'authorization', 'access_token',
        'refresh_token', 'api_secret', 'private_key'
    ]
    MAX_BODY_SIZE = 1024  # 1KB
    SLOW_REQUEST_THRESHOLD = 2.0  # seconds
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        request_id = str(uuid.uuid4())[:8]
        setattr(request, 'request_id', request_id)
        
        start_time = time.time()
        
        self._log_request(request, request_id)
        
        response = self.get_response(request)
        
        duration = time.time() - start_time
        
        self._log_response(request, response, duration, request_id)
        
        return response
    
    def _log_request(self, request: Any, request_id: str) -> None:
        """Log incoming request with sanitized data."""
        log_data = {
            'event': 'request',
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'level': 'INFO',
            'logger': 'videos',
            'request_id': request_id,
            'context': {
                'method': request.method,
                'path': request.path,
                'user': self._get_user(request),
                'ip': self._get_client_ip(request),
                'user_agent': request.META.get('HTTP_USER_AGENT', '')[:200],
            }
        }
        
        if request.method in ['POST', 'PUT', 'PATCH']:
            body = self._sanitize_body(request.body)
            log_data['context']['body'] = body
        
        logger.info(json.dumps(log_data))
    
    def _log_response(self, request: Any, response: Any, duration: float, request_id: str) -> None:
        """Log response with status and duration."""
        status_code = response.status_code
        log_level = self._get_log_level(status_code)
        logging_level = getattr(logging, log_level, logging.INFO)
        
        log_data = {
            'event': 'response',
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'level': log_level,
            'logger': 'videos',
            'request_id': request_id,
            'context': {
                'method': request.method,
                'path': request.path,
                'status_code': status_code,
                'duration_ms': round(duration * 1000, 2),
                'duration_s': round(duration, 3),
            }
        }
        
        if duration > self.SLOW_REQUEST_THRESHOLD:
            log_data['context']['slow_request'] = True
            logger.warning(
                f"Slow response: {duration:.2f}s for {request.method} {request.path}",
                extra=log_data
            )
        else:
            logger.log(logging_level, json.dumps(log_data))
    
    def _sanitize_body(self, body: bytes) -> str:
        """Sanitize and truncate request body."""
        try:
            body_str = body.decode('utf-8')
            
            if len(body) > self.MAX_BODY_SIZE:
                return f"...[truncated {len(body)} bytes]..."
            
            try:
                data = json.loads(body_str)
                sanitized = self._sanitize_dict(data)
                return json.dumps(sanitized)
            except json.JSONDecodeError:
                return body_str[:self.MAX_BODY_SIZE]
                
        except Exception:
            return "...[unable to decode body]..."
    
    def _sanitize_dict(self, data: dict) -> dict:
        """Recursively sanitize sensitive fields in dictionary."""
        if not isinstance(data, dict):
            return data
        
        sanitized = {}
        for key, value in data.items():
            if any(sensitive in key.lower() for sensitive in self.SENSITIVE_FIELDS):
                sanitized[key] = '***REDACTED***'
            elif isinstance(value, dict):
                sanitized[key] = self._sanitize_dict(value)
            else:
                sanitized[key] = value
        return sanitized
    
    def _get_log_level(self, status_code: int) -> str:
        """Determine log level based on status code."""
        if status_code >= 500:
            return 'ERROR'
        elif status_code >= 400:
            return 'WARNING'
        return 'INFO'
    
    def _get_user(self, request: Any) -> str:
        """Get username or 'anonymous'."""
        if hasattr(request, 'user') and request.user.is_authenticated:
            return request.user.username
        return 'anonymous'
    
    def _get_client_ip(self, request: Any) -> str:
        """Extract client IP from request."""
        x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded:
            return x_forwarded.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR', 'unknown')
