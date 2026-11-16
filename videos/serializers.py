import os

from rest_framework import serializers
from videos.models import CodecUrls, Categorias, VideosUploaded
from videos.storage import DownloadStorage


class CategoryModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Categorias
        fields = "__all__"


class CodecUrlsSerializer(serializers.ModelSerializer):
    class Meta:
        model = CodecUrls
        fields = "__all__"
        read_only_fields = ["error_reason"]


class VideosUpladedSerializer(serializers.ModelSerializer):
    video = serializers.CharField(source="file_path", read_only=True)
    size_bytes = serializers.SerializerMethodField()
    has_thumbnail = serializers.SerializerMethodField()

    def get_has_thumbnail(self, obj):
        return bool(obj.codecurl_id and DownloadStorage().thumbnail_path(obj.codecurl_id).is_file())

    def get_size_bytes(self, obj):
        try:
            return os.stat(obj.file_path).st_size
        except (OSError, TypeError):
            return None

    class Meta:
        model = VideosUploaded
        fields = ["id", "video", "mime_type", "title", "category", "codecurl", "created_at", "updated_at", "visible", "size_bytes", "has_thumbnail"]
