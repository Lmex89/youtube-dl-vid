from rest_framework import serializers
from videos.models import CodecUrls, Categorias, VideosUploaded


class CategoryModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Categorias
        fields = "__all__"


class CodecUrlsSerializer(serializers.ModelSerializer):
    class Meta:
        model = CodecUrls
        fields = "__all__"


class VideosUpladedSerializer(serializers.ModelSerializer):
    video = serializers.CharField(source="file_path", read_only=True)

    class Meta:
        model = VideosUploaded
        fields = ["id", "video", "mime_type", "title", "category", "codecurl", "created_at", "updated_at", "visible"]
