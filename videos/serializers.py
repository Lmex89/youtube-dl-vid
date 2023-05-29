from rest_framework import serializers
from .models import ImgPortada, Categorias


class CategoryModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Categorias
        fields = "__all__"


class ImgPortadaSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImgPortada
        fields = "__all__"
