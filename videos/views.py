import json
import logging
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, JsonResponse
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit
from loguru import logger
from rest_framework import generics, status
from rest_framework.response import Response

from videos.download import build_command, cleanup_old_downloads, run_download
from videos.models import Categorias, CodecUrls, StatusCodec, VideosUploaded
from videos.serializers import (
    CategoryModelSerializer,
    CodecUrlsSerializer,
    VideosUpladedSerializer,
)

logger = logging.getLogger('videos')


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
        logger.debug("Listing all codec URLs")
        return super().get(request, *args, **kwargs)

    @method_decorator(ratelimit(key='user_or_ip', rate='5/m', method='POST', block=True))
    def post(self, request, *args, **kwargs):
        url = request.data.get('url', 'unknown')
        logger.info(f"Creating codec URL entry: {url[:50]}...")
        response = super().post(request, *args, **kwargs)
        if response.status_code == 201:
            logger.info(f"Codec URL created successfully: {response.data.get('id')}")
        return response


class CategoriasListCreateAPIView(generics.ListCreateAPIView):
    queryset = Categorias.objects.all()
    serializer_class = CategoryModelSerializer
    permission_classes = ()

    @method_decorator(ratelimit(key='user_or_ip', rate='30/m', method='GET', block=True))
    def get(self, request, *args, **kwargs):
        logger.debug("Listing all categories")
        return super().get(request, *args, **kwargs)

    @method_decorator(ratelimit(key='user_or_ip', rate='10/m', method='POST', block=True))
    def post(self, request, *args, **kwargs):
        category_name = request.data.get('name', 'unknown')
        logger.info(f"Creating category: {category_name}")
        response = super().post(request, *args, **kwargs)
        if response.status_code == 201:
            logger.info(f"Category created successfully: {response.data.get('id')}")
        return response


class VideosUploadedListCreateAPIView(generics.ListCreateAPIView):
    queryset = VideosUploaded.objects.all()
    serializer_class = VideosUpladedSerializer
    permission_classes = ()

    @method_decorator(ratelimit(key='user_or_ip', rate='30/m', method='GET', block=True))
    def get(self, request, *args, **kwargs):
        logger.debug("Listing all uploaded videos")
        return super().get(request, *args, **kwargs)

    @method_decorator(ratelimit(key='user_or_ip', rate='5/m', method='POST', block=True))
    def post(self, request, *args, **kwargs):
        url = request.data.get("url")
        
        if not url:
            logger.warning(
                json.dumps({
                    "event": "video_download_failed",
                    "reason": "url_not_provided",
                    "user": getattr(request.user, 'username', 'anonymous'),
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
                "user": getattr(request.user, 'username', 'anonymous'),
            })
        )

        codecurl = CodecUrls.objects.create(url=url)
        logger.debug(f"Created CodecUrls instance: {codecurl.id}")
        
        cleanup_old_downloads(codecurl)
        logger.info(f"Cleaned up old downloads for URL: {url[:50]}...")

        downloads_dir = Path(settings.DOWNLOADS_DIR)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        output_path = downloads_dir / f"{codecurl.id}.mp4"
        log_path = Path("logs") / f"{codecurl.id}.txt"

        try:
            command = build_command(url, output_path)
            logger.debug(f"Built yt-dlp command: {' '.join(command[:4])}...")
            run_download(command, log_path)
        except Exception as exc:
            logger.exception(
                json.dumps({
                    "event": "video_download_failed",
                    "error": str(exc),
                    "error_type": exc.__class__.__name__,
                    "codecurl_id": str(codecurl.id),
                    "url_preview": url[:50] + "..." if len(url) > 50 else url,
                })
            )
            codecurl.status = StatusCodec.ERROR
            codecurl.save()
            return Response(
                data={"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        uploaded = VideosUploaded.objects.create(
            video_path=str(output_path),
            title="Test",
            codecurl=codecurl,
        )
        codecurl.status = StatusCodec.SUCCESS
        codecurl.save()

        logger.info(
            json.dumps({
                "event": "video_download_completed",
                "uploaded_id": str(uploaded.id),
                "codecurl_id": str(codecurl.id),
                "video_path": str(output_path),
            })
        )

        serializer = self.get_serializer(uploaded)
        return Response(data=serializer.data, status=status.HTTP_201_CREATED)


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
                "user": getattr(request.user, 'username', 'anonymous'),
            })
        )
        
        if not file_path.exists():
            logger.warning(
                json.dumps({
                    "event": "video_file_not_found",
                    "upload_id": str(upload.id),
                    "video_path": str(file_path),
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
            })
        )
        
        response = FileResponse(
            open(file_path, "rb"),
            as_attachment=True,
            filename=f"{upload.id}.mp4",
        )
        return response
