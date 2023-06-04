from django.urls import path
from videos import views

urls = [
    path("videos/", views.CodecUrlsListCreateAPIView.as_view()),
    path("videos/<uuid:pk>", views.CodecUrlsListCreateAPIView.as_view()),
    path("categorias/", views.CategoriasListCreateAPIView.as_view()),
    path("videos-uploaded/", views.VideosUploadedListCreateAPIView.as_view()),
    path("videos-uploaded/<uuid:pk>", views.VideosUploadedDetailAPIView.as_view()),
]
