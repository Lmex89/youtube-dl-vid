from django.urls import path
from videos import views

url_portada = [
    path("videos/", views.CodecUrlsListCreateAPIView.as_view()),
    path("videos/<uuid:pk>", views.CodecUrlsListCreateAPIView.as_view()),
]
