from pathlib import Path

from django.conf import settings
from django.http import FileResponse
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


class CodecUrlsDetailAPIView(generics.RetrieveAPIView):
    queryset = CodecUrls.objects.all()
    serializer_class = CodecUrlsSerializer
    permission_classes = ()


class CodecUrlsListCreateAPIView(generics.ListCreateAPIView):
    queryset = CodecUrls.objects.all()
    serializer_class = CodecUrlsSerializer
    permission_classes = ()


class CategoriasListCreateAPIView(generics.ListCreateAPIView):
    queryset = Categorias.objects.all()
    serializer_class = CategoryModelSerializer
    permission_classes = ()


class VideosUploadedListCreateAPIView(generics.ListCreateAPIView):
    queryset = VideosUploaded.objects.all()
    serializer_class = VideosUpladedSerializer
    permission_classes = ()

    def post(self, request, *args, **kwargs):
        url = request.data.get("url")
        if not url:
            return Response(
                data={"error": "url is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        codecurl = CodecUrls.objects.create(url=url)
        cleanup_old_downloads(codecurl)

        downloads_dir = Path(settings.DOWNLOADS_DIR)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        output_path = downloads_dir / f"{codecurl.id}.mp4"
        log_path = Path("logs") / f"{codecurl.id}.txt"

        try:
            command = build_command(url, output_path)
            logger.info(command)
            run_download(command, log_path)
        except Exception as exc:
            codecurl.status = StatusCodec.ERROR
            codecurl.save()
            logger.exception(str(exc))
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

        serializer = self.get_serializer(uploaded)
        return Response(data=serializer.data, status=status.HTTP_201_CREATED)


class VideosUploadedDetailAPIView(generics.RetrieveAPIView):
    queryset = VideosUploaded.objects.all()
    serializer_class = VideosUpladedSerializer
    permission_classes = ()

    def get(self, request, *args, **kwargs):
        upload = self.get_object()
        file_path = Path(upload.video_path)
        if not file_path.exists():
            return Response(
                data={"error": "file not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        response = FileResponse(
            open(file_path, "rb"),
            as_attachment=True,
            filename=f"{upload.id}.mp4",
        )
        return response
