# users/middleware.py
import time


class PageTimingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.time()

        response = self.get_response(request)

        duration = round(time.time() - start, 2)
        path = request.path
        method = request.method
        status = getattr(response, "status_code", "unknown")

        print(f"[PAGE TIME] {method} {path} -> {duration}s | status={status}")

        return response