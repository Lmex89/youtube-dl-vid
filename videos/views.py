import json
import logging

from django.http import FileResponse, JsonResponse
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit
from rest_framework import generics, status
from rest_framework.response import Response

from videos.download import get_job_state, start_download_async
from videos.models import Categorias, CodecUrls, OutputProfile, VideosUploaded
from videos.profiles import PROFILES, get_profile
from videos.serializers import (
    CategoryModelSerializer,
    CodecUrlsSerializer,
    VideosUpladedSerializer,
)
from videos.storage import DownloadStorage

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
        response = super().get(request, *args, **kwargs)
        state = get_job_state(kwargs["pk"])
        if state:
            response.data.update(status=state.status, progress=state.progress)
        return response


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
        request_id = getattr(request, "request_id", "unknown")

        if not url:
            logger.warning(
                json.dumps({
                    "event": "video_download_failed",
                    "reason": "url_not_provided",
                    "user": getattr(request.user, "username", "anonymous"),
                    "request_id": request_id,
                })
            )
            return Response(
                data={"error": "url is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        requested_profile = (
            request.data.get("output_profile") or OutputProfile.STANDARD_VIDEO
        )
        try:
            profile = get_profile(requested_profile)
        except ValueError:
            logger.warning(
                json.dumps({
                    "event": "video_download_failed",
                    "reason": "invalid_output_profile",
                    "output_profile": str(requested_profile)[:50],
                    "request_id": request_id,
                })
            )
            return Response(
                data={
                    "error": "unsupported output_profile",
                    "allowed": sorted(PROFILES.keys()),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        logger.info(
            json.dumps({
                "event": "video_download_requested",
                "url_preview": url[:50] + "..." if len(url) > 50 else url,
                "output_profile": profile.name,
                "user": getattr(request.user, "username", "anonymous"),
                "request_id": request_id,
            })
        )

        codecurl = CodecUrls.objects.create(
            url=url,
            output_profile=profile.name,
        )
        logger.debug(
            json.dumps({
                "event": "codecurl_created",
                "codecurl_id": str(codecurl.id),
                "output_profile": profile.name,
                "request_id": request_id,
            })
        )

        start_download_async(codecurl, request_id=request_id)
        logger.info(
            json.dumps({
                "event": "video_download_started",
                "codecurl_id": str(codecurl.id),
                "url_preview": url[:50] + "..." if len(url) > 50 else url,
                "output_profile": profile.name,
                "request_id": request_id,
            })
        )

        return Response(
            data={
                "id": str(codecurl.id),
                "status": "pending",
                "url": url,
                "output_profile": profile.name,
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
        request_id = getattr(request, "request_id", "unknown")
        served = DownloadStorage().resolve(upload)

        if served.not_found_reason:
            details = {
                "event": "video_file_not_found",
                "upload_id": str(upload.id),
                "reason": served.not_found_reason,
                "request_id": request_id,
            }
            if served.path is not None:
                details["file_path"] = served.path.name
            logger.warning(json.dumps(details))
            return Response(
                data={"error": "file not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        logger.debug(
            json.dumps({
                "event": "video_file_download_requested",
                "upload_id": str(upload.id),
                "file_path": str(served.path),
                "user": getattr(request.user, "username", "anonymous"),
                "request_id": request_id,
            })
        )

        logger.info(
            json.dumps({
                "event": "video_file_streaming",
                "upload_id": str(upload.id),
                "filename": served.path.name,
                "mime_type": served.mime_type,
                "size_bytes": served.path.stat().st_size,
                "request_id": request_id,
            })
        )

        return FileResponse(
            open(served.path, "rb"),
            as_attachment=True,
            filename=served.filename,
            content_type=served.mime_type,
        )
