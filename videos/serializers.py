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
    class Meta:
        model = VideosUploaded
        fields = "__all__"
