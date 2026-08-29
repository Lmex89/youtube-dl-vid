import json
import logging
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, JsonResponse
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit
from rest_framework import generics, status
from rest_framework.response import Response

from videos.download import cleanup_old_downloads, start_download_async
from videos.models import Categorias, CodecUrls, VideosUploaded
from videos.serializers import (
    CategoryModelSerializer,
    CodecUrlsSerializer,
    VideosUpladedSerializer,
)

logger = logging.getLogger("videos")


def ratelimited_error(request, exception):
    return JsonResponse(
        {"error": "rate limit exceeded", "detail": str(exception)},
        status=429,
    )


class CodecUrlsDetailAPIView(generics.RetrieveAPIView):
    queryset = CodecUrls.objects.all()
    serializer_class = CodecUrlsSerializer
    permission_classes = ()

    @method_decorator(ratelimit(key='user_or_ip', rate='30/m', method='GET', block=True))
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class CodecUrlsListCreateAPIView(generics.ListCreateAPIView):
    queryset = CodecUrls.objects.all()
    serializer_class = CodecUrlsSerializer
    permission_classes = ()

    @method_decorator(ratelimit(key='user_or_ip', rate='30/m', method='GET', block=True))
    def get(self, request, *args, **kwargs):
        logger.debug(
            json.dumps({
                "event": "list_codecurls_request",
                "request_id": getattr(request, "request_id", "unknown"),
            })
        )
        return super().get(request, *args, **kwargs)

    @method_decorator(ratelimit(key='user_or_ip', rate='5/m', method='POST', block=True))
    def post(self, request, *args, **kwargs):
        url = request.data.get("url", "unknown")
        logger.info(
            json.dumps({
                "event": "create_codecurl_request",
                "url_preview": url[:50] + "..." if len(url) > 50 else url,
                "request_id": getattr(request, "request_id", "unknown"),
            })
        )
        response = super().post(request, *args, **kwargs)
        if response.status_code == 201:
            logger.info(
                json.dumps({
                    "event": "create_codecurl_success",
                    "codecurl_id": str(response.data.get("id")),
                    "request_id": getattr(request, "request_id", "unknown"),
                })
            )
        return response


class CategoriasListCreateAPIView(generics.ListCreateAPIView):
    queryset = Categorias.objects.all()
    serializer_class = CategoryModelSerializer
    permission_classes = ()

    @method_decorator(ratelimit(key='user_or_ip', rate='30/m', method='GET', block=True))
    def get(self, request, *args, **kwargs):
        logger.debug(
            json.dumps({
                "event": "list_categories_request",
                "request_id": getattr(request, "request_id", "unknown"),
            })
        )
        return super().get(request, *args, **kwargs)

    @method_decorator(ratelimit(key='user_or_ip', rate='10/m', method='POST', block=True))
    def post(self, request, *args, **kwargs):
        category_name = request.data.get("name", "unknown")
        logger.info(
            json.dumps({
                "event": "create_category_request",
                "category_name": category_name,
                "request_id": getattr(request, "request_id", "unknown"),
            })
        )
        response = super().post(request, *args, **kwargs)
        if response.status_code == 201:
            logger.info(
                json.dumps({
                    "event": "create_category_success",
                    "category_id": str(response.data.get("id")),
                    "request_id": getattr(request, "request_id", "unknown"),
                })
            )
        return response


class VideosUploadedListCreateAPIView(generics.ListCreateAPIView):
    queryset = VideosUploaded.objects.all()
    serializer_class = VideosUpladedSerializer
    permission_classes = ()

    @method_decorator(ratelimit(key='user_or_ip', rate='30/m', method='GET', block=True))
    def get(self, request, *args, **kwargs):
        logger.debug(
            json.dumps({
                "event": "list_uploaded_videos_request",
                "request_id": getattr(request, "request_id", "unknown"),
            })
        )
        return super().get(request, *args, **kwargs)

    @method_decorator(ratelimit(key='user_or_ip', rate='5/m', method='POST', block=True))
    def post(self, request, *args, **kwargs):
        url = request.data.get("url")
        
        if not url:
            logger.warning(
                json.dumps({
                    "event": "video_download_failed",
                    "reason": "url_not_provided",
                    "user": getattr(request.user, "username", "anonymous"),
                    "request_id": getattr(request, "request_id", "unknown"),
                })
            )
            return Response(
                data={"error": "url is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        logger.info(
            json.dumps({
                "event": "video_download_requested",
                "url_preview": url[:50] + "..." if len(url) > 50 else url,
                "user": getattr(request.user, "username", "anonymous"),
                "request_id": getattr(request, "request_id", "unknown"),
            })
        )

        codecurl = CodecUrls.objects.create(url=url)
        logger.debug(
            json.dumps({
                "event": "codecurl_created",
                "codecurl_id": str(codecurl.id),
                "request_id": getattr(request, "request_id", "unknown"),
            })
        )

        cleanup_old_downloads(codecurl)
        logger.info(
            json.dumps({
                "event": "old_downloads_cleaned",
                "codecurl_id": str(codecurl.id),
                "url_preview": url[:50] + "..." if len(url) > 50 else url,
                "request_id": getattr(request, "request_id", "unknown"),
            })
        )

        downloads_dir = Path(settings.DOWNLOADS_DIR)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        output_path = downloads_dir / f"{codecurl.id}.mp4"
        log_path = Path("logs") / f"{codecurl.id}.txt"
        request_id = getattr(request, "request_id", "unknown")

        start_download_async(
            codecurl,
            url,
            output_path,
            log_path,
            request_id=request_id,
        )
        logger.info(
            json.dumps({
                "event": "video_download_started",
                "codecurl_id": str(codecurl.id),
                "url_preview": url[:50] + "..." if len(url) > 50 else url,
                "request_id": request_id,
            })
        )

        return Response(
            data={
                "id": str(codecurl.id),
                "status": "pending",
                "url": url,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class VideosUploadedDetailAPIView(generics.RetrieveAPIView):
    queryset = VideosUploaded.objects.all()
    serializer_class = VideosUpladedSerializer
    permission_classes = ()

    @method_decorator(ratelimit(key='user_or_ip', rate='10/m', method='GET', block=True))
    def get(self, request, *args, **kwargs):
        upload = self.get_object()
        file_path = Path(upload.video_path)
        
        logger.debug(
            json.dumps({
                "event": "video_file_download_requested",
                "upload_id": str(upload.id),
                "video_path": str(file_path),
                "user": getattr(request.user, "username", "anonymous"),
                "request_id": getattr(request, "request_id", "unknown"),
            })
        )

        if not file_path.exists():
            logger.warning(
                json.dumps({
                    "event": "video_file_not_found",
                    "upload_id": str(upload.id),
                    "video_path": str(file_path),
                    "request_id": getattr(request, "request_id", "unknown"),
                })
            )
            return Response(
                data={"error": "file not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        logger.info(
            json.dumps({
                "event": "video_file_streaming",
                "upload_id": str(upload.id),
                "filename": file_path.name,
                "size_bytes": file_path.stat().st_size,
                "request_id": getattr(request, "request_id", "unknown"),
            })
        )
        
        response = FileResponse(
            open(file_path, "rb"),
            as_attachment=True,
            filename=f"{upload.id}.mp4",
        )
        return response
