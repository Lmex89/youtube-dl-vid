import os
import time

from django.core.files import File
from django.http import HttpResponse
from rest_framework import generics, status
from rest_framework.response import Response

from videos.functions_utils import services
from videos.models import Categorias, CodecUrls, StatusCodec, VideosUploaded
from videos.serializers import (CategoryModelSerializer, CodecUrlsSerializer,
                                VideosUpladedSerializer)


class CodeUrlsAPIView(generics.RetrieveAPIView):
    queryset = CodecUrls.objects.all()
    serializer_class = CodecUrlsSerializer
    permission_classes = ()

    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class CodecUrlsListCreateAPIView(generics.ListCreateAPIView):
    queryset = CodecUrls.objects.all()
    serializer_class = CodecUrlsSerializer
    permission_classes = ()

    def get(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)


class CategoriasListCreateAPIView(generics.ListCreateAPIView):
    queryset = Categorias.objects.all()
    serializer_class = CategoryModelSerializer
    permission_classes = ()

    def get(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)


class VideosUploadedListCreateAPIView(generics.ListCreateAPIView):
    queryset = VideosUploaded.objects.all()
    serializer_class = VideosUpladedSerializer
    permission_classes = ()

    def get(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def add_commands(self, *args, **kwargs):
        services.COMMAND_YT_DLP.append(kwargs.get("url"))
        services.COMMAND_YT_DLP.append(kwargs.get("output_flag"))
        services.COMMAND_YT_DLP.append(kwargs.get("path"))
        return services.COMMAND_YT_DLP

    def insert_commandos_from_str(self, url_):
        static_directory = os.path.abspath("static/")
        return self.add_commands(
            url=url_.url,
            output_flag="-o",
            path=f"{static_directory}/{url_.id}.mp4",
        )

    def create_code_url(self):
        url_ = CodecUrls.objects.create(url=self.request.data.get("url"))
        url_.save()
        return url_

    def post(self, request, *args, **kwargs):
        url_ = self.create_code_url()
        comandos = self.insert_commandos_from_str(url_)

        try:
            services.SpCommand(command_list=comandos, tmp_file="test.txt")
            time.sleep(3)
        except Exception as error:
            url_.status = StatusCodec.error
            url_.save()
            return Response(
                data=dict(error=str(error)), status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        try:
            with open(f"static/{url_.id}.mp4", "rb") as videop:
                return self.get_video_codec(videop, url_)
        except Exception as error:
            return Response(
                data=dict(error=str(error)), status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def get_video_codec(self, videop, url_):
        f_video = File(videop)
        data = VideosUploaded.objects.create(video=f_video, title="Test", codecurl=url_)

        serializer = self.get_serializer(data)

        url_.status = StatusCodec.success
        url_.save()

        return Response(data=serializer.data, status=status.HTTP_201_CREATED)


class VideosUploadedDetailAPIView(generics.RetrieveAPIView):
    queryset = VideosUploaded.objects.all()
    serializer_class = VideosUpladedSerializer
    permission_classes = ()

    def get(self, request, *args, **kwargs):
        video_codec = self.get_queryset().filter(pk=kwargs.get("pk")).first()
        file = video_codec.video
        with file.open() as f:
            file_content = f.read()
        response = HttpResponse(file_content, content_type="application/octet-stream")
        response["Content-Disposition"] = f'attachment; filename="{video_codec.id}.mp4"'
        return response
