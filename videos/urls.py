from django.urls import  path
from .views import ImgPortadaView, ImgPortadataListCreateAPIView

url_portada = [
    path('portada/',ImgPortadataListCreateAPIView.as_view()),
    path('portada/<uuid:pk>',
         ImgPortadaView.as_view()),
]