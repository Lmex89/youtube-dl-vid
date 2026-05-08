import logging
import json
import time
from typing import Any, Optional

from django_ratelimit.exceptions import Ratelimited
from rest_framework.views import exception_handler
from rest_framework.exceptions import APIException
from rest_framework.response import Response

logger = logging.getLogger('videos')


def custom_exception_handler(exc: Exception, context: dict) -> Optional[Any]:
    """
    Custom exception handler with structured JSON logging.
    
    Features:
    - Logs full exception traceback with context
    - Includes request details (path, method, user, IP)
    - Adds request_id for correlation if available
    - Returns consistent error response format
    - Handles Ratelimited exceptions with 429 status
    
    Configure in settings.py:
        REST_FRAMEWORK = {
            'EXCEPTION_HANDLER': 'Youtube_dl_vid.exceptions.custom_exception_handler',
        }
    """
    request = context.get('request')
    view = context.get('view')
    
    request_id = getattr(request, 'request_id', 'unknown') if request else 'unknown'
    
    log_data = {
        'event': 'exception',
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'level': 'ERROR',
        'logger': 'videos',
        'request_id': request_id,
        'context': {
            'exception_type': exc.__class__.__name__,
            'exception_message': str(exc),
            'view': view.__class__.__name__ if view else 'unknown',
            'path': request.path if request else 'unknown',
            'method': request.method if request else 'unknown',
            'user': _get_user(request),
            'ip': _get_client_ip(request) if request else 'unknown',
        }
    }
    
    if isinstance(exc, Ratelimited):
        log_data['level'] = 'WARNING'
        log_data['event'] = 'rate_limit_exceeded'
        logger.warning(json.dumps(log_data))
        return Response(
            {"error": "rate limit exceeded", "detail": str(exc)},
            status=429,
        )
    
    logger.exception(json.dumps(log_data))
    
    response = exception_handler(exc, context)
    
    if response is not None:
        response.data['status_code'] = response.status_code
        response.data['path'] = request.path if request else None
        response.data['request_id'] = request_id
    
    return response


def _get_user(request: Any) -> str:
    """Get username or 'anonymous'."""
    if request and hasattr(request, 'user') and request.user.is_authenticated:
        return request.user.username
    return 'anonymous'


def _get_client_ip(request: Any) -> str:
    """Extract client IP from request."""
    if not request:
        return 'unknown'
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')
