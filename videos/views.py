from django.shortcuts import render

# Create your views here.
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from .models import ImgPortada, Categorias
from .serializers import CategoryModelSerializer, ImgPortadaSerializer
from django.db.models import Q


class ImgPortadaView(generics.RetrieveAPIView):
    queryset = ImgPortada.objects.all()
    serializer_class = ImgPortadaSerializer
    permission_classes = (IsAuthenticated,)

    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class ImgPortadataListCreateAPIView(generics.ListCreateAPIView):
    queryset = ImgPortada.objects.all()
    serializer_class = ImgPortadaSerializer
    permission_classes = (IsAuthenticated,)

    def filters(self):
        title = self.request.query_params.get("title")
        id_category = self.request.query_params.get("id_category")
        return title, id_category

    def get_queryset(self):
        title, id_category = self.filters()
        queryset = super().get_queryset().select_related("category")
        filters = Q()
        if title:
            filters &= Q(title=title)
        if id_category:
            filters &= Q(category_id=id_category)

        return queryset.filter(filters)

    def get(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)


class CategoriasListCreateAPIView(generics.ListCreateAPIView):
    queryset = Categorias.objects.all()
    serializer_class = CategoryModelSerializer
    permission_classes = (IsAuthenticated,)

    def get(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)
