import json
import logging
import time
from django.db import connection
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger('videos')

SLOW_QUERY_THRESHOLD_MS = 500


class SlowQueryLoggingMiddleware(MiddlewareMixin):
    """
    Middleware to log database queries slower than threshold.
    
    Logs to separate file: /var/log/gunicorn/django_slow_queries.log
    Threshold: 500ms (configurable via SLOW_QUERY_THRESHOLD_MS)
    """
    
    def process_request(self, request):
        request._query_count_before = len(connection.queries)
        request._start_time = time.time()
    
    def process_response(self, request, response):
        if not hasattr(request, '_start_time'):
            return response
        
        queries = connection.queries[getattr(request, '_query_count_before', 0):]
        
        for query in queries:
            duration_ms = float(query.get('time', 0)) * 1000
            
            if duration_ms > SLOW_QUERY_THRESHOLD_MS:
                log_data = {
                    'event': 'slow_query',
                    'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    'level': 'WARNING',
                    'logger': 'videos',
                    'context': {
                        'duration_ms': round(duration_ms, 2),
                        'threshold_ms': SLOW_QUERY_THRESHOLD_MS,
                        'sql': query.get('sql', '')[:500],
                        'path': request.path,
                        'method': request.method,
                        'user': getattr(request.user, 'username', 'anonymous') if hasattr(request, 'user') else 'unknown',
                    }
                }
                
                logger.warning(json.dumps(log_data))
        
        return response
